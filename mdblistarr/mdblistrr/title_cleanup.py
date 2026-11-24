"""Blacklist title candidates in the existing reconciliation cleanup lifecycle.

File DELETEs are synchronous native operations. Catalogue DELETE deliberately
uses deleteFiles=false: recursive folder deletion is unsafe for directory links.
Every uncertain request has durable original intent and is only reconciled by
reads; it is never automatically retried.
"""
from dataclasses import dataclass
from collections import deque
from datetime import timedelta
import posixpath
from urllib.parse import urlsplit

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .blacklist import BlacklistError, digest, positive_id, refresh_blacklists
from .models import (Preferences, SonarrInstance, RadarrInstance, TitleCleanupCandidate,
    TitleCleanupEvent, SonarrEpisodeSearchCommand, RadarrMovieSearchCommand,
    SonarrCleanupCandidate, RadarrCleanupCandidate)
from .media_display import safe_title
from .cleanup_lock import configuration_lock, RequestNotSent
from .jellyfin_cleanup import prepare as prepare_jellyfin, continue_cleanup as continue_jellyfin, JellyfinDeferred

TERMINAL = {'deleted', 'cancelled', 'already_absent'}


class CleanupDeferred(ValueError):
    pass


@dataclass
class TitleCleanupResult:
    attempts: int = 0
    stop: bool = False
    failures: int = 0


def target_fingerprint(target):
    url = urlsplit(target.url.rstrip('/'))
    return digest((url.scheme.lower(), url.hostname, url.port, url.path.rstrip('/')))


def validate_target(product, target):
    model = SonarrInstance if product == 'sonarr' else RadarrInstance
    current = model.objects.get(pk=target.pk)
    if (not current.is_ondemand_target or current.is_library_source
            or target_fingerprint(current) != target_fingerprint(target)
            or str(current.pk) != Preferences.get_value(f'{product}_reconciliation_target_id')):
        raise CleanupDeferred('Target roles or configuration changed.')
    # Equal credentials or equal endpoints expose accidental aliases even when
    # separate database records were configured. Never send target writes there.
    for source in model.objects.filter(is_library_source=True):
        if target_fingerprint(source) == target_fingerprint(current) or source.apikey == current.apikey:
            raise CleanupDeferred('Target aliases a permanent library instance.')


def live_settings():
    values = dict(Preferences.objects.filter(name__in=(
        'blacklist_cleanup_enabled', 'blacklist_cleanup_dry_run', 'blacklist_cleanup_grace_hours')).values_list('name', 'value'))
    try:
        grace = int(values.get('blacklist_cleanup_grace_hours', '24'))
    except (ValueError, TypeError):
        raise CleanupDeferred('Blacklist grace setting is invalid.') from None
    if not 0 <= grace <= 168:
        raise CleanupDeferred('Blacklist grace setting is invalid.')
    return (values.get('blacklist_cleanup_enabled') == '1'
            and values.get('blacklist_cleanup_dry_run', '1') == '0'), grace


class GuardedReconciliationAPI:
    """Recheck blacklist configuration at actual native mutation boundaries."""
    def __init__(self, api, product, target, membership):
        self.api, self.product, self.target, self.membership = api, product, target, membership

    def __getattr__(self, name):
        value = getattr(self.api, name)
        if not name.startswith(('put_', 'post_', 'trigger_', 'delete_')):
            return value

        def guarded(*args, **kwargs):
            with configuration_lock():
                try:
                    if not self.membership.current():
                        raise CleanupDeferred('Blacklist configuration changed before an Arr write.')
                    if self.membership.sources:
                        validate_target(self.product, self.target)
                except CleanupDeferred:
                    raise RequestNotSent('Blacklist configuration guard rejected an unsent request.') from None
                return value(*args, **kwargs)
        return guarded


def read(api, resource, params=None, allow_absent=False):
    response = api.cleanup_request('GET', resource, params=params)
    if allow_absent and response.get('status_code') == 404 and not response.get('error'):
        return None
    if response.get('status_code') != 200 or response.get('error'):
        raise CleanupDeferred('Required Arr evidence is unavailable.')
    data = response.get('data')
    if isinstance(data, dict) and any(k in data for k in ('error', 'errorMessage', 'result')):
        raise CleanupDeferred('Required Arr evidence is malformed.')
    return data


def media_identity(product, row):
    external_key = 'tvdbId' if product == 'sonarr' else 'tmdbId'
    if (not isinstance(row, dict) or not positive_id(row.get('id'))
            or not positive_id(row.get(external_key)) or type(row.get('monitored')) is not bool):
        raise CleanupDeferred('Target title identity is malformed.')
    path, added = row.get('path'), row.get('added')
    if (not isinstance(path, str) or not path.startswith('/') or path == '/'
            or posixpath.normpath(path) != path.rstrip('/')
            or not isinstance(added, str) or parse_datetime(added) is None):
        raise CleanupDeferred('Target title path or creation identity is unavailable.')
    return {'id': row['id'], 'external_id': row[external_key], 'path': path, 'added': added}


def file_inventory(product, api, media):
    sonarr = product == 'sonarr'
    resource = 'episodefile' if sonarr else 'moviefile'
    relation = 'seriesId' if sonarr else 'movieId'
    rows = read(api, resource, {relation: media['id']})
    if not isinstance(rows, list) or len(rows) > 20000:
        raise CleanupDeferred('Complete file inventory is unavailable.')
    files = {}
    for row in rows:
        if (not isinstance(row, dict) or not positive_id(row.get('id'))
                or row.get(relation) != media['id'] or row['id'] in files
                or type(row.get('size')) is not int or row['size'] < 0
                or not isinstance(row.get('path'), str)
                or posixpath.dirname(row['path']) == row['path']
                or posixpath.normpath(row['path']) != row['path']
                or not row['path'].startswith(media['path'].rstrip('/') + '/')
                or not isinstance(row.get('dateAdded'), str) or parse_datetime(row['dateAdded']) is None):
            raise CleanupDeferred('Associated file identity is incomplete or conflicting.')
        files[row['id']] = {key: row[key] for key in ('id', 'path', 'size', 'dateAdded')}
    if sonarr:
        episodes = read(api, 'episode', {'seriesId': media['id']})
        if not isinstance(episodes, list):
            raise CleanupDeferred('Episode inventory is unavailable.')
        referenced, seen = set(), set()
        for episode in episodes:
            if (not isinstance(episode, dict) or not positive_id(episode.get('id'))
                    or episode['id'] in seen or episode.get('seriesId') != media['id']
                    or type(episode.get('hasFile')) is not bool
                    or type(episode.get('monitored')) is not bool or episode['monitored']):
                raise CleanupDeferred('Episode identity or monitoring is not confirmed safe.')
            seen.add(episode['id'])
            file_id = episode.get('episodeFileId')
            if episode['hasFile']:
                if file_id not in files:
                    raise CleanupDeferred('Episode and file inventories disagree.')
                referenced.add(file_id)
            elif file_id not in (0, None):
                raise CleanupDeferred('Episode and file inventories disagree.')
        if referenced != set(files):
            raise CleanupDeferred('Episode inventory does not cover every associated file.')
    return [files[key] for key in sorted(files)]


def activity_safe(product, api, target, media_id):
    commands = (SonarrEpisodeSearchCommand.objects.filter(target_instance=target, target_series_id=media_id)
                if product == 'sonarr' else RadarrMovieSearchCommand.objects.filter(
                    target_instance=target, candidates__target_movie_id=media_id))
    if commands.filter(Q(status__in=('submitting', 'queued', 'started', 'ambiguous', 'unavailable'))
            | Q(status__in=('failed', 'aborted', 'cancelled', 'orphaned'), outcome_reconciled_at__isnull=True)).exists():
        raise CleanupDeferred('An application search has an unresolved outcome.')
    native = read(api, 'command')
    if not isinstance(native, list):
        raise CleanupDeferred('Native search/import activity is unavailable.')
    for command in native:
        if not isinstance(command, dict) or not positive_id(command.get('id')):
            raise CleanupDeferred('Native search/import activity is malformed.')
        if command.get('status') not in ('completed', 'failed', 'aborted', 'cancelled'):
            raise CleanupDeferred('Native activity is running or uncertain; removal deferred.')
    total, seen = None, set()
    relation = 'seriesId' if product == 'sonarr' else 'movieId'
    for page in range(1, 101):
        queue = read(api, 'queue', {'page': page, 'pageSize': 100, 'includeUnknownSeriesItems': 'true', 'includeUnknownMovieItems': 'true'})
        if (not isinstance(queue, dict) or type(queue.get('totalRecords')) is not int
                or queue['totalRecords'] < 0 or not isinstance(queue.get('records'), list)
                or queue.get('page') != page or queue.get('pageSize') != 100):
            raise CleanupDeferred('Download/import queue evidence is incomplete.')
        if total is not None and total != queue['totalRecords']:
            raise CleanupDeferred('Download/import queue changed during validation.')
        total = queue['totalRecords']
        for row in queue['records']:
            if not isinstance(row, dict) or not positive_id(row.get('id')) or row['id'] in seen:
                raise CleanupDeferred('Download/import queue identities are malformed.')
            seen.add(row['id'])
            if not positive_id(row.get(relation)) or row[relation] == media_id:
                raise CleanupDeferred('A download or import may still affect this title.')
        if len(seen) == total:
            return
        if not queue['records'] or len(seen) > total:
            break
    raise CleanupDeferred('Download/import queue pagination is incomplete.')


def transition(candidate, status, outcome, evidence=None):
    with transaction.atomic():
        candidate.status = status
        candidate.last_error = ''
        if status == 'deleted':
            candidate.deleted_at = timezone.now()
        if status == 'cancelled':
            candidate.cancelled_at = timezone.now()
        candidate.save()
        TitleCleanupEvent.objects.create(candidate=candidate, phase=status, outcome=outcome, evidence=evidence or {})


@transaction.atomic
def supersede_duplicate_candidates(candidate):
    """Cancel future file work without erasing a previous uncertain request."""
    sonarr = candidate.product == 'sonarr'
    model = SonarrCleanupCandidate if sonarr else RadarrCleanupCandidate
    identity = ({'target_series_id': candidate.target_media_id, 'tvdb_id': candidate.external_id}
                if sonarr else {'target_movie_id': candidate.target_media_id, 'tmdb_id': candidate.external_id})
    rows = list(model.objects.filter(target_instance_id=candidate.target_instance_id,
        reason='permanent_duplicate', status__in=('pending', 'ready'), **identity))
    if not rows:
        return
    uncertain = []
    for row in rows:
        if row.last_error.startswith(('cleanup post-delete verification uncertain',
                'cleanup delete uncertain', 'post-delete verification could not prove exact file absent')):
            uncertain.append(row.episode_file_id if sonarr else row.movie_file_id)
    model.objects.filter(pk__in=[row.pk for row in rows]).update(
        status='cancelled', cancelled_at=timezone.now(), ready_at=None, updated_at=timezone.now())
    TitleCleanupEvent.objects.create(candidate=candidate, phase='duplicate_superseded',
        outcome='permanent_duplicate_candidates_superseded', evidence={
            'candidate_ids': [row.pk for row in rows], 'uncertain_file_ids': sorted(set(uncertain))})


def verify_superseded_requests(candidate, api, files):
    """Changing removal reason must never retry an ambiguous file DELETE."""
    uncertain = set()
    for event in candidate.events.filter(outcome='permanent_duplicate_candidates_superseded'):
        uncertain.update(event.evidence['uncertain_file_ids'])
    resource = 'episodefile' if candidate.product == 'sonarr' else 'moviefile'
    for file_id in sorted(uncertain):
        if (any(row['id'] == file_id for row in files)
                or read(api, f'{resource}/{file_id}', allow_absent=True) is not None):
            raise CleanupDeferred('A previous duplicate-file deletion is unverified; it will not be repeated.')


def maintain_candidates(product, target, titles, membership, grace_hours):
    """No network or writes to Arr; preserve historical candidates and original intent."""
    if not membership.current():
        raise CleanupDeferred('Blacklist configuration changed during reconciliation.')
    key = 'tvdbId' if product == 'sonarr' else 'tmdbId'
    now = timezone.now()
    present = {row['id']: row for row in titles}
    for candidate in TitleCleanupCandidate.objects.filter(product=product, target_instance_id=target.pk).exclude(status__in=TERMINAL):
        if not membership.contains(product, candidate.external_id):
            if candidate.status in ('pending', 'ready', 'files_removed'):
                transition(candidate, 'cancelled', 'blacklist_membership_removed')
            else:
                candidate.events.get_or_create(phase='membership_removed',
                    outcome='blacklist_membership_removed_during_uncertain_request')
                candidate.last_error = 'Membership removed; original uncertain request requires reconciliation.'
                candidate.save(update_fields=['last_error', 'updated_at'])
        elif candidate.target_media_id not in present and candidate.status in ('pending', 'ready'):
            transition(candidate, 'already_absent', 'catalogue_absent_without_delete')
    for row in titles:
        external_id = row[key]
        if not membership.contains(product, external_id):
            continue
        candidate, created = TitleCleanupCandidate.objects.get_or_create(
            product=product, target_instance_id=target.pk, target_media_id=row['id'], defaults={
                'external_id': external_id, 'target_title': safe_title(row.get('title')),
                'target_fingerprint': target_fingerprint(target),
                'first_eligible_at': now, 'last_confirmed_at': now,
                'source_ids': membership.members[(product, external_id)]})
        if candidate.external_id != external_id or candidate.target_fingerprint != target_fingerprint(target):
            candidate.last_error = 'Target identity changed; original candidate retained.'
            candidate.save(update_fields=['last_error', 'updated_at'])
            continue
        if candidate.status == 'cancelled' and not candidate.events.filter(phase__in=('files_submitting', 'title_submitting')).exists():
            candidate.first_eligible_at = now
            candidate.evidence = {}
            transition(candidate, 'pending', 'membership_restored_new_grace')
        if candidate.status in TERMINAL:
            continue
        supersede_duplicate_candidates(candidate)
        candidate.last_confirmed_at = now
        candidate.source_ids = membership.members[(product, external_id)]
        candidate.target_title = safe_title(row.get('title')) or candidate.target_title
        if not candidate.evidence:
            try:
                candidate.evidence = {'media': media_identity(product, row)}
            except CleanupDeferred as exc:
                candidate.last_error = str(exc)
        if candidate.status in ('pending', 'ready'):
            candidate.ready_at = candidate.first_eligible_at + timedelta(hours=grace_hours)
            candidate.status = 'ready' if now >= candidate.ready_at else 'pending'
        candidate.save()
        if created:
            TitleCleanupEvent.objects.create(candidate=candidate, phase='pending', outcome='blacklist_match')


def verify_original_request(candidate, api):
    event = candidate.events.filter(phase__in=('files_submitting', 'title_submitting')).order_by('-id').first()
    if event is None:
        raise CleanupDeferred('Original deletion intent is unavailable.')
    product = candidate.product
    title_resource = 'series' if product == 'sonarr' else 'movie'
    file_resource = 'episodefile' if product == 'sonarr' else 'moviefile'
    if event.phase == 'files_submitting':
        file_id = event.evidence['file']['id']
        if read(api, f'{file_resource}/{file_id}', allow_absent=True) is not None:
            raise CleanupDeferred('Original file deletion is unverified; it will not be repeated.')
        media = read(api, f'{title_resource}/{candidate.target_media_id}', allow_absent=True)
        if media is None or media_identity(product, media) != candidate.evidence.get('media'):
            raise CleanupDeferred('Title changed while verifying original file removal.')
        remaining = file_inventory(product, api, candidate.evidence['media'])
        expected = [row for row in candidate.evidence['files'] if row['id'] != file_id]
        if remaining != expected:
            raise CleanupDeferred('File inventory changed during original removal verification.')
        candidate.evidence['files'] = remaining
        transition(candidate, 'ready' if remaining else 'files_removed', 'original_file_removal_verified', {'file_id': file_id})
    else:
        if read(api, f'{title_resource}/{candidate.target_media_id}', allow_absent=True) is not None:
            raise CleanupDeferred('Original title deletion is unverified; it will not be repeated.')
        catalogue = read(api, title_resource)
        external_key = 'tvdbId' if product == 'sonarr' else 'tmdbId'
        if not isinstance(catalogue, list) or any(not isinstance(row, dict) or not positive_id(row.get('id'))
                or not positive_id(row.get(external_key)) or row['id'] == candidate.target_media_id
                or row[external_key] == candidate.external_id for row in catalogue):
            raise CleanupDeferred('Complete catalogue removal could not be verified.')
        transition(candidate, 'deleted', 'native_files_and_title_removal_verified')


def process_title_cleanup(*, product, target, api, membership, remaining_budget, stop=False):
    result = TitleCleanupResult(stop=stop)
    candidates = deque(TitleCleanupCandidate.objects.filter(product=product, target_instance_id=target.pk).exclude(status__in=TERMINAL).order_by('first_eligible_at', 'id'))
    while candidates:
        candidate = candidates.popleft()
        try:
            validate_target(product, target)
            if candidate.target_fingerprint != target_fingerprint(target):
                raise CleanupDeferred('Original target configuration changed.')
            if candidate.status not in ('files_submitting', 'title_submitting', 'uncertain') and candidate.events.filter(
                    outcome='blacklist_membership_removed_during_uncertain_request').exists():
                transition(candidate, 'cancelled', 'blacklist_membership_removed_after_recovery')
                continue
            if candidate.status in ('files_submitting', 'title_submitting', 'uncertain'):
                verify_original_request(candidate, api)
                if candidate.status != 'deleted' and (
                        (membership.current() and not membership.contains(product, candidate.external_id))
                        or candidate.events.filter(outcome='blacklist_membership_removed_during_uncertain_request').exists()):
                    transition(candidate, 'cancelled', 'blacklist_membership_removed_after_recovery')
                # Reconciliation only; a later run can consider any next stage.
                continue
            if not membership.current() or not membership.contains(product, candidate.external_id):
                raise CleanupDeferred('Current blacklist membership is not verified.')
            resource = 'series' if product == 'sonarr' else 'movie'
            current = read(api, f'{resource}/{candidate.target_media_id}')
            identity = media_identity(product, current)
            if identity['external_id'] != candidate.external_id or current['monitored']:
                raise CleanupDeferred('Exact title identity or unmonitored state changed.')
            files = file_inventory(product, api, identity)
            verify_superseded_requests(candidate, api, files)
            changed = (identity != candidate.evidence.get('media')
                       or ('files' in candidate.evidence and files != candidate.evidence['files']))
            if changed:
                if candidate.events.filter(phase__in=('files_submitting', 'title_submitting')).exists():
                    raise CleanupDeferred('Associated title or files changed; original cleanup evidence retained.')
                _, grace = live_settings()
                candidate.first_eligible_at = timezone.now()
                candidate.ready_at = candidate.first_eligible_at + timedelta(hours=grace)
                candidate.evidence = {'media': identity, 'files': files}
                transition(candidate, 'pending', 'identity_changed_grace_reset')
                continue
            candidate.evidence['files'] = files
            candidate.save()
            if candidate.status == 'pending':
                continue
            activity_safe(product, api, target, candidate.target_media_id)
            if files:
                config = read(api, 'config/mediamanagement')
                if not isinstance(config, dict) or config.get('deleteEmptyFolders') is not False or config.get('recycleBin') != '':
                    raise CleanupDeferred('File removal requires empty-folder deletion off and no recycle bin in Arr.')
            candidate.last_error = ''
            candidate.save()
            if Preferences.get_value('blacklist_cleanup_dry_run', '1') != '0':
                continue
            if Preferences.get_value('blacklist_cleanup_enabled', '0') != '1':
                continue
            if result.stop or result.attempts >= remaining_budget:
                raise CleanupDeferred('Shared cleanup deletion budget exhausted or further deletion stopped.')
            # Only destructive work performs this second fresh list read. All
            # ordinary matching reuses one verified snapshot per reconciliation.
            fresh = refresh_blacklists()
            if not fresh.contains(product, candidate.external_id):
                transition(candidate, 'cancelled', 'membership_removed_before_delete')
                continue
            if not fresh.fresh():
                raise CleanupDeferred('Blacklist configuration changed before deletion.')
            validate_target(product, target)
            if Preferences.get_value('blacklist_cleanup_enabled', '0') != '1' or Preferences.get_value('blacklist_cleanup_dry_run', '1') != '0':
                continue
            with configuration_lock():
                if not fresh.fresh():
                    raise CleanupDeferred('Blacklist configuration changed immediately before submission.')
                validate_target(product, target)
                enabled, grace = live_settings()
                if not enabled:
                    continue
                if timezone.now() < candidate.first_eligible_at + timedelta(hours=grace):
                    raise CleanupDeferred('The current blacklist grace period has not elapsed.')
                # Bind optional work before final native checks: preparation
                # can perform slow network and filesystem reads.
                prepare_jellyfin(candidate)
                # Refresh native evidence after acquiring the lock: another
                # configuration request may have held it during a slow refresh.
                final = read(api, f'{resource}/{candidate.target_media_id}')
                if media_identity(product, final) != identity or final['monitored']:
                    raise CleanupDeferred('Target changed immediately before deletion.')
                if file_inventory(product, api, identity) != files:
                    raise CleanupDeferred('Files changed immediately before deletion.')
                verify_superseded_requests(candidate, api, files)
                activity_safe(product, api, target, candidate.target_media_id)
                if files:
                    config = read(api, 'config/mediamanagement')
                    if not isinstance(config, dict) or config.get('deleteEmptyFolders') is not False or config.get('recycleBin') != '':
                        raise CleanupDeferred('Arr file-deletion settings changed.')
                if not fresh.fresh():
                    raise CleanupDeferred('Blacklist verification expired during native validation.')
                if files:
                    file = files[0]
                    transition(candidate, 'files_submitting', 'intent_persisted', {'file': file})
                    endpoint = f"{'episodefile' if product == 'sonarr' else 'moviefile'}/{file['id']}"
                    params = None
                else:
                    transition(candidate, 'title_submitting', 'intent_persisted', {'media': identity})
                    endpoint = f'{resource}/{candidate.target_media_id}'
                    params = {'deleteFiles': 'false', 'addImportExclusion': 'false'}
                result.attempts += 1
                api.cleanup_request('DELETE', endpoint, params=params)
            # Success, timeout, and HTTP failure all require the same proof.
            verify_original_request(candidate, api)
            if candidate.status not in TERMINAL and result.attempts < remaining_budget:
                candidates.append(candidate)
        except (CleanupDeferred, BlacklistError, JellyfinDeferred) as exc:
            candidate.last_error = str(exc)
            if candidate.status in ('files_submitting', 'title_submitting', 'uncertain'):
                candidate.status = 'uncertain'
                result.stop = True
                result.failures += 1
            candidate.save()
    continuation = continue_jellyfin(product=product, target=target, api=api,
        membership=membership, remaining_budget=max(0, remaining_budget - result.attempts), stop=result.stop)
    result.attempts += continuation.attempts
    result.failures += continuation.failures
    result.stop = result.stop or continuation.stop
    return result

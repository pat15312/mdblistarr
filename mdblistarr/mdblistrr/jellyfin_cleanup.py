"""Optional continuation of blacklist cleanup in the existing product scheduler.

Native ``deleted`` records remain native successes. Only candidates bound before
their first native request acquire this continuation; upgrades never enrol old
deleted titles. Jellyfin item DELETE is deliberately unavailable here.
"""
import json
import os
import posixpath
import re
import time
from datetime import timedelta
from urllib.parse import urlsplit

import requests
from django.db import transaction
from django.utils import timezone

from . import empty_directories as directories
from .models import JellyfinCleanup, TitleCleanupEvent


class JellyfinDeferred(ValueError):
    pass


def configuration(product):
    if os.environ.get('MDBLISTARR_JELLYFIN_ENABLED', '0') != '1':
        return None
    try:
        url = os.environ['MDBLISTARR_JELLYFIN_URL'].rstrip('/')
        parsed = urlsplit(url)
        if (parsed.scheme not in ('http', 'https') or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or '..' in parsed.path.split('/')):
            raise ValueError
        roots = json.loads(os.environ['MDBLISTARR_JELLYFIN_ROOTS'])
        selected = roots[product]
        root, library_id = selected['root'], selected['library_id']
        if not directories.canonical(root) or not re.fullmatch(r'[a-fA-F0-9]{32}', library_id):
            raise ValueError
        key_path = os.environ['MDBLISTARR_JELLYFIN_API_KEY_FILE']
        if not os.path.isabs(key_path):
            raise ValueError
        with open(key_path, encoding='ascii') as stream:
            key = stream.read(1025).strip()
        if not re.fullmatch(r'[a-zA-Z0-9_-]{16,256}', key):
            raise ValueError
        return {'url': url, 'root': root, 'library_id': library_id.lower(), 'key': key}
    except (KeyError, TypeError, ValueError, OSError, UnicodeError):
        raise JellyfinDeferred('Jellyfin integration configuration is invalid or unavailable.') from None


class Client:
    def __init__(self, config):
        self.config = config
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.headers['Authorization'] = f'MediaBrowser Token="{config["key"]}"'
        self.deadline = time.monotonic() + 120

    def close(self):
        self.session.close()

    def request(self, method, resource, params=None):
        if time.monotonic() >= self.deadline:
            raise JellyfinDeferred('Jellyfin verification time budget expired.')
        try:
            with self.session.request(method, self.config['url'] + resource, params=params,
                    timeout=(5, 15), allow_redirects=False, stream=True) as response:
                if response.status_code != (204 if method == 'POST' else 200):
                    raise JellyfinDeferred('Jellyfin request failed; catalogue outcome is unverified.')
                if method == 'POST':
                    return None
                chunks, size = [], 0
                for chunk in response.iter_content(65536):
                    size += len(chunk)
                    if size > 8 * 1024 * 1024 or time.monotonic() >= self.deadline:
                        raise JellyfinDeferred('Jellyfin response exceeds verification limits.')
                    chunks.append(chunk)
                return json.loads(b''.join(chunks))
        except JellyfinDeferred:
            raise
        except (requests.RequestException, ValueError):
            raise JellyfinDeferred('Jellyfin request failed; catalogue outcome is unverified.') from None

    def library(self):
        libraries = self.request('GET', '/Library/VirtualFolders')
        if not isinstance(libraries, list) or any(not isinstance(row, dict)
                or not isinstance(row.get('ItemId'), str) for row in libraries):
            raise JellyfinDeferred('Jellyfin library evidence is malformed.')
        matches = [row for row in libraries if isinstance(row, dict)
                   and row.get('ItemId', '').replace('-', '').lower() == self.config['library_id']]
        if (len(matches) != 1 or matches[0].get('Locations') != [self.config['root']]
                or matches[0].get('RefreshStatus') not in ('Idle', 'Active')):
            raise JellyfinDeferred('Jellyfin library identity or root changed.')
        return matches[0]

    def binding(self, product):
        library = self.library()
        expected = 'tvshows' if product == 'sonarr' else 'movies'
        if library.get('CollectionType') != expected:
            raise JellyfinDeferred('Jellyfin library media type does not match the target.')
        server = self.request('GET', '/System/Info')
        if not isinstance(server, dict) or not isinstance(server.get('Id'), str) or not server['Id']:
            raise JellyfinDeferred('Jellyfin server identity is unavailable.')
        return {key: self.config[key] for key in ('url', 'root', 'library_id')} | {'server_id': server['Id']}

    def catalogue(self, product):
        rows, seen, total = [], set(), None
        types = ('Series', 'Season', 'Episode') if product == 'sonarr' else ('Movie',)
        for offset in range(0, 200000, 5000):
            response = self.request('GET', '/Items', {
                'ParentId': self.config['library_id'], 'Recursive': 'true',
                'IncludeItemTypes': ','.join(types), 'Fields': 'Path,ProviderIds',
                'StartIndex': offset, 'Limit': 5000, 'EnableUserData': 'false', 'EnableImages': 'false',
                'SortBy': 'SortName', 'SortOrder': 'Ascending'})
            if (not isinstance(response, dict) or type(response.get('TotalRecordCount')) is not int
                    or not 0 <= response['TotalRecordCount'] <= 200000
                    or not isinstance(response.get('Items'), list)):
                raise JellyfinDeferred('Jellyfin catalogue pagination is malformed.')
            if total is not None and total != response['TotalRecordCount']:
                raise JellyfinDeferred('Jellyfin catalogue changed during pagination.')
            total = response['TotalRecordCount']
            for row in response['Items']:
                virtual_child = (isinstance(row, dict) and row.get('Type') in ('Season', 'Episode')
                    and row.get('LocationType') == 'Virtual'
                    and isinstance(row.get('SeriesId'), str) and bool(row['SeriesId']))
                if (not isinstance(row, dict) or not isinstance(row.get('Id'), str)
                        or not row['Id'] or row['Id'] in seen or row.get('Type') not in types
                        or (not virtual_child and (not isinstance(row.get('Path'), str) or not row['Path']))
                        or (virtual_child and row.get('Path') not in (None, '')
                            and not isinstance(row['Path'], str))
                        or ('SeriesId' in row and not isinstance(row['SeriesId'], str))
                        or not isinstance(row.get('ProviderIds', {}), dict)):
                    raise JellyfinDeferred('Jellyfin catalogue identity or path is unavailable.')
                seen.add(row['Id'])
                rows.append({'Id': row['Id'], 'Type': row['Type'], 'Path': row.get('Path') or '',
                    'ProviderIds': row.get('ProviderIds', {}), 'SeriesId': row.get('SeriesId', ''),
                    'LocationType': row.get('LocationType', '')})
            if len(rows) == total:
                return sorted(rows, key=lambda row: row['Id'])
            if not response['Items'] or len(response['Items']) != 5000 or len(rows) > total:
                break
        raise JellyfinDeferred('Jellyfin catalogue pagination is incomplete.')

    def refresh(self):
        self.request('POST', '/Items/' + self.config['library_id'] + '/Refresh', {
            'Recursive': 'true', 'MetadataRefreshMode': 'None', 'ImageRefreshMode': 'None',
            'ReplaceAllMetadata': 'false', 'ReplaceAllImages': 'false'})


def matching(rows, candidate, path, original_ids=(), original_series_ids=()):
    provider = 'Tvdb' if candidate.product == 'sonarr' else 'Tmdb'
    known_series = {row['Id'] for row in rows if row['Type'] == 'Series'} | set(original_series_ids)
    if any(not row['Path'] and row.get('SeriesId') not in known_series for row in rows):
        raise JellyfinDeferred('A virtual Jellyfin item has unresolved series identity.')
    matches = [row for row in rows if row['Id'] in original_ids or row['Path'] == path
            or row['Path'].startswith(path + '/')
            or (row['Type'] == ('Series' if candidate.product == 'sonarr' else 'Movie')
                and str(row['ProviderIds'].get(provider, '')) == str(candidate.external_id))]
    series_ids = set(original_series_ids) | {row['Id'] for row in matches if row['Type'] == 'Series'}
    matched_ids = {row['Id'] for row in matches}
    return [row for row in rows if row['Id'] in matched_ids or (candidate.product == 'sonarr'
            and row.get('SeriesId') in series_ids)]


def save(stage, status, outcome, **evidence):
    with transaction.atomic():
        stage.status = status
        stage.last_error = ''
        stage.save()
        TitleCleanupEvent.objects.create(candidate=stage.candidate,
            phase='jellyfin', outcome=outcome, evidence=evidence)


def prepare(candidate):
    """Bind optional work before the first native mutation, never retroactively."""
    config = configuration(candidate.product)
    existing = JellyfinCleanup.objects.filter(candidate=candidate).first()
    if existing:
        if config is None:
            raise JellyfinDeferred('Jellyfin integration was disabled during this removal.')
        binding = {key: config[key] for key in ('url', 'root', 'library_id')}
        if any(existing.binding.get(key) != value for key, value in binding.items()):
            raise JellyfinDeferred('Original Jellyfin configuration changed.')
        if existing.directories['title'] != candidate.evidence['media']['path']:
            raise JellyfinDeferred('Original title directory binding changed.')
        try:
            for row in existing.directories['directories']:
                if directories.inspect(existing.directories, row['name']) == 'absent':
                    raise JellyfinDeferred('A captured directory disappeared before native completion.')
        except (OSError, directories.DirectoryDeferred):
            raise JellyfinDeferred('A captured directory identity changed before native completion.') from None
        return
    if config is None:
        return
    if candidate.events.filter(phase__in=('files_submitting', 'title_submitting')).exists():
        raise JellyfinDeferred('Original removal has no pre-deletion directory binding.')
    client = Client(config)
    try:
        binding = client.binding(candidate.product)
        title = candidate.evidence['media']['path']
        rows = matching(client.catalogue(candidate.product), candidate, title)
        provider = 'Tvdb' if candidate.product == 'sonarr' else 'Tmdb'
        primary_type = 'Series' if candidate.product == 'sonarr' else 'Movie'
        series_ids = {row['Id'] for row in rows if row['Type'] == 'Series'}
        for row in rows:
            within = row['Path'] == title or row['Path'].startswith(title + '/')
            if (row['Type'] in ('Season', 'Episode') and not row['Path']
                    and row.get('LocationType') == 'Virtual' and row.get('SeriesId') in series_ids):
                within = True
            if not within or (row['Type'] == primary_type
                    and str(row['ProviderIds'].get(provider, '')) != str(candidate.external_id)):
                raise JellyfinDeferred('Jellyfin title path and provider identities conflict.')
        season_paths = [posixpath.dirname(row['path']) for row in candidate.evidence.get('files', [])]
        if any(row['Type'] == 'Series' and row['Path'] == title for row in rows):
            season_paths.extend(row['Path'] for row in rows if row['Type'] == 'Season')
        plan = directories.snapshot(config['root'], title, candidate.product, season_paths)
        with transaction.atomic():
            stage = JellyfinCleanup.objects.create(candidate=candidate, binding=binding,
                directories=plan, original_item_ids=[row['Id'] for row in rows],
                original_series_ids=sorted(series_ids))
            save(stage, 'native_pending', 'jellyfin_binding_captured_before_native_removal')
    except (OSError, directories.DirectoryDeferred):
        raise JellyfinDeferred('Local directory binding is unavailable or unsafe.') from None
    finally:
        client.close()


def continue_cleanup(*, product, target, api, membership, remaining_budget, stop=False):
    # Imports stay local to keep native cleanup independent of optional services.
    from .title_cleanup import (validate_target, target_fingerprint, read, live_settings,
                                CleanupDeferred, TitleCleanupResult)
    from .blacklist import refresh_blacklists, BlacklistError
    from .cleanup_lock import configuration_lock
    result = TitleCleanupResult(stop=stop)
    stages = JellyfinCleanup.objects.filter(candidate__product=product,
        candidate__target_instance_id=target.pk, candidate__status='deleted').exclude(
        status='complete').select_related('candidate').order_by('updated_at', 'id')[:100]
    for stage in stages:
        client = None
        try:
            config = configuration(product)
            if config is None:
                raise JellyfinDeferred('Jellyfin integration is disabled; continuation is pending.')
            candidate, plan = stage.candidate, stage.directories
            validate_target(product, target)
            if candidate.target_fingerprint != target_fingerprint(target):
                raise JellyfinDeferred('Original target configuration changed.')
            client = Client(config)
            if client.binding(product) != stage.binding:
                raise JellyfinDeferred('Original Jellyfin server, library or root changed.')
            # A re-added native title must block all continuation, even with a
            # new native ID. Failed/malformed catalogues are never absence.
            resource, provider = ('series', 'tvdbId') if product == 'sonarr' else ('movie', 'tmdbId')
            native = read(api, resource)
            if (not isinstance(native, list) or any(not isinstance(row, dict)
                    or type(row.get('id')) is not int or row['id'] <= 0
                    or type(row.get(provider)) is not int or row[provider] <= 0
                    or not directories.canonical(row.get('path'))
                    or row['id'] == candidate.target_media_id or row[provider] == candidate.external_id
                    or row.get('path') == plan['title'] for row in native)):
                raise JellyfinDeferred('Native title absence is no longer established.')
            title_state = directories.inspect(plan, '')
            if title_state == 'absent':
                plan['removed'] = [row['name'] for row in plan['directories']]
                plan['intent'] = None
                if stage.status in ('native_pending', 'directory_pending', 'directory_submitting', 'uncertain'):
                    save(stage, 'directories_removed', 'captured_directories_verified_absent')
            elif plan['intent'] is not None:
                name = plan['intent']
                if directories.inspect(plan, name) != 'absent':
                    raise JellyfinDeferred('Original directory removal is unverified; it will not be repeated.')
                plan['removed'].append(name)
                plan['intent'] = None
                save(stage, 'directory_pending', 'original_directory_removal_verified', directory=name)
                continue  # Recovery is read-only for this candidate in this run.
            if title_state != 'absent':
                for row in list(reversed(plan['directories'])):
                    name = row['name']
                    if name in plan['removed']:
                        continue
                    state = directories.inspect(plan, name)
                    if state == 'absent':
                        plan['removed'].append(name)
                        save(stage, 'directory_pending', 'captured_directory_already_absent', directory=name)
                        continue
                    if state != 'empty':
                        raise JellyfinDeferred('Captured directory has remaining contents; cleanup is incomplete.')
                    enabled, _ = live_settings()
                    if not enabled:
                        raise JellyfinDeferred('Native removal complete; directory cleanup is disabled or dry run.')
                    if result.stop or result.attempts >= remaining_budget:
                        raise JellyfinDeferred('Shared cleanup deletion budget exhausted or stopped.')
                    fresh = refresh_blacklists()
                    with configuration_lock():
                        validate_target(product, target)
                        enabled, grace = live_settings()
                        if (not enabled or not fresh.fresh() or not fresh.contains(product, candidate.external_id)
                                or configuration(product) != config):
                            raise JellyfinDeferred('Current configuration or blacklist membership does not permit removal.')
                        if timezone.now() < candidate.first_eligible_at + timedelta(hours=grace):
                            raise JellyfinDeferred('The current blacklist grace period has not elapsed.')
                        # Re-read native identity immediately before rmdir.
                        latest = read(api, resource)
                        if latest != native:
                            raise JellyfinDeferred('Native catalogue changed before directory removal.')
                        if not fresh.fresh():
                            raise JellyfinDeferred('Blacklist evidence expired before directory removal.')
                        def intent():
                            plan['intent'] = name
                            save(stage, 'directory_submitting', 'directory_removal_intent', directory=name)
                            result.attempts += 1
                        directories.remove(plan, name, intent)
                        plan['removed'].append(name)
                        plan['intent'] = None
                        save(stage, 'directory_pending', 'directory_removal_verified', directory=name)
                if directories.inspect(plan, '') != 'absent':
                    raise JellyfinDeferred('Title directory disappearance remains unverified.')
                save(stage, 'directories_removed', 'captured_directories_verified_absent')
            rows = client.catalogue(product)
            if not matching(rows, candidate, plan['title'], stage.original_item_ids, stage.original_series_ids):
                # Detect same-count catalogue churn as well as pagination changes.
                if client.catalogue(product) != rows:
                    raise JellyfinDeferred('Jellyfin catalogue changed during absence verification.')
                if directories.inspect(plan, '') != 'absent':
                    raise JellyfinDeferred('Title directory reappeared during verification.')
                stage.refresh_status = client.library()['RefreshStatus']
                if read(api, resource) != native:
                    raise JellyfinDeferred('Native catalogue changed during Jellyfin verification.')
                stage.completed_at = timezone.now()
                save(stage, 'complete', 'jellyfin_identity_and_path_absence_verified')
                continue
            stage.refresh_status = client.library()['RefreshStatus']
            if stage.refresh_requested_at is None:
                enabled, _ = live_settings()
                if not enabled:
                    raise JellyfinDeferred('Jellyfin still retains the title; refresh is disabled or dry run.')
                fresh = refresh_blacklists()
                with configuration_lock():
                    validate_target(product, target)
                    if (not live_settings()[0] or configuration(product) != config or result.stop
                            or not fresh.fresh() or not fresh.contains(product, candidate.external_id)):
                        raise JellyfinDeferred('Configuration changed before Jellyfin refresh.')
                    stage.refresh_requested_at = timezone.now()
                    save(stage, 'refresh_submitting', 'jellyfin_refresh_intent')
                    client.refresh()
                    save(stage, 'refresh_pending', 'jellyfin_library_refresh_submitted')
            else:
                if stage.refresh_status == 'Idle':
                    stage.status = 'refresh_incomplete'
                    stage.last_error = 'Refresh outcome unverified; Jellyfin retains the title. No automatic retry.'
                stage.save()  # Never automatically repeat an uncertain refresh.
        except (JellyfinDeferred, CleanupDeferred, BlacklistError, directories.DirectoryDeferred, OSError) as exc:
            stage.last_error = (str(exc)[:255] if isinstance(exc, (JellyfinDeferred, CleanupDeferred,
                                BlacklistError, directories.DirectoryDeferred))
                                else 'Directory access or removal failed; original intent retained.')
            if stage.directories.get('intent') is not None:
                stage.status = 'uncertain'
                result.stop = True
                result.failures += 1
            elif stage.status == 'refresh_submitting':
                stage.status = 'refresh_uncertain'
                result.failures += 1
            stage.save()
        finally:
            if client is not None:
                client.close()
    return result

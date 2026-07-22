from dataclasses import dataclass, field
from datetime import timedelta
from django.utils import timezone
from .connect import sanitize_text
from .models import SonarrCleanupCandidate
from .sonarr_reconcile import calculate_episode_monitoring, episode_key, REASON_PERMANENT_DUPLICATE


@dataclass
class CleanupCounters:
    cleanup_candidates_new: int = 0
    cleanup_candidates_pending: int = 0
    cleanup_candidates_ready: int = 0
    cleanup_candidates_cancelled: int = 0
    cleanup_would_delete: int = 0
    cleanup_files_deleted: int = 0
    cleanup_files_already_absent: int = 0
    cleanup_deferred_by_limit: int = 0
    cleanup_failures: int = 0
    delete_attempts_consumed: int = 0
    stop_deletes_for_run: bool = False
    events: list = field(default_factory=list)


def _positive_int(value):
    try:
        if isinstance(value, bool):
            return None
        value = int(value)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def valid_file_id(value):
    return _positive_int(value) is not None


def _source_has_files(source_episodes):
    if not isinstance(source_episodes, list):
        return None
    source_has = {}
    for ep in source_episodes:
        if not isinstance(ep, dict):
            return None
        key = episode_key(ep)
        if key is None or key in source_has:
            return None
        source_has[key] = ep.get('hasFile') is True
    return source_has


def _group_target_episode_files(target_episodes):
    if not isinstance(target_episodes, list):
        return None
    groups = {}
    for ep in target_episodes:
        if not isinstance(ep, dict):
            return None
        file_id = _positive_int(ep.get('episodeFileId'))
        if file_id is not None:
            groups.setdefault(file_id, []).append(ep)
    return groups


def _keys_for_group(episodes):
    keys = []
    seen = set()
    for ep in episodes:
        if not isinstance(ep, dict):
            return None
        key = episode_key(ep)
        if key is None or key in seen:
            return None
        seen.add(key)
        keys.append([key[0], key[1]])
    return sorted(keys)


def eligible_episode_file_ids(source_episodes, target_episodes, stats):
    source_has = _source_has_files(source_episodes)
    groups = _group_target_episode_files(target_episodes)
    if source_has is None or groups is None:
        return {}
    key_to_file = {}
    duplicate_keys = set()
    for file_id, episodes in groups.items():
        for ep in episodes:
            key = episode_key(ep)
            if key is None:
                continue
            previous = key_to_file.get(key)
            if previous is not None:
                duplicate_keys.add(key)
            key_to_file[key] = file_id
    eligible = {}
    for file_id, episodes in groups.items():
        keys = []
        seen = set()
        ok = True
        for ep in episodes:
            key = episode_key(ep)
            if key is None or key in seen or key in duplicate_keys:
                ok = False; break
            seen.add(key)
            if _positive_int(ep.get('episodeFileId')) != file_id:
                ok = False; break
            if ep.get('hasFile') is not True or ep.get('monitored') is not False:
                ok = False; break
            if source_has.get(key) is not True:
                ok = False; break
            if stats.desired_by_key.get(key) is not False or stats.reason_by_key.get(key) != REASON_PERMANENT_DUPLICATE:
                ok = False; break
            keys.append([key[0], key[1]])
        if ok and keys:
            eligible[file_id] = sorted(keys)
    return eligible


def file_absent(target_episodes, file_id):
    if not isinstance(target_episodes, list):
        return False
    return not any(isinstance(ep, dict) and _positive_int(ep.get('episodeFileId')) == int(file_id) and ep.get('hasFile') is True for ep in target_episodes)


def validate_episode_response_for_cleanup(episodes):
    if not isinstance(episodes, list):
        return False
    seen = set()
    for ep in episodes:
        if not isinstance(ep, dict):
            return False
        if ep.get('result') or ep.get('error') or ep.get('errorMessage'):
            return False
        status_code = ep.get('status_code')
        if status_code is not None:
            try:
                if int(status_code) < 200 or int(status_code) >= 300:
                    return False
            except (TypeError, ValueError):
                return False
        if _positive_int(ep.get('id')) is None:
            return False
        key = episode_key(ep)
        if key is None or key in seen:
            return False
        seen.add(key)
        if not isinstance(ep.get('hasFile'), bool) or not isinstance(ep.get('monitored'), bool):
            return False
        if ep.get('hasFile') is True and _positive_int(ep.get('episodeFileId')) is None:
            return False
    return True


def _reset_candidate(cand, *, now, status=SonarrCleanupCandidate.STATUS_PENDING, keys=None, clear_error=True):
    cand.status = status
    cand.first_eligible_at = now
    cand.ready_at = None
    cand.deleted_at = None
    cand.cancelled_at = None
    if keys is not None:
        cand.linked_episode_keys = keys
    if clear_error:
        cand.last_error = ''


def _mark_cancelled(cand, now, reason):
    cand.status = SonarrCleanupCandidate.STATUS_CANCELLED
    cand.cancelled_at = now
    cand.ready_at = None
    cand.deleted_at = None
    cand.last_error = ''
    cand.save()
    return f'cleanup cancelled tvdb={cand.tvdb_id} series={cand.target_series_id} episodeFileId={cand.episode_file_id} reason={sanitize_text(reason)}'


def revalidate_candidate_for_delete(*, cand, source_api, target_api, source_series_id, include_specials, grace_hours, dry_run, remaining_delete_budget):
    now = timezone.now()
    if dry_run or remaining_delete_budget <= 0:
        return 'defer', 'dry_run_or_limit'
    if now < cand.first_eligible_at + timedelta(hours=int(grace_hours)):
        return 'defer', 'grace_not_elapsed'
    source_eps = source_api.get_episodes(source_series_id) if source_api and source_series_id else None
    target_eps = target_api.get_episodes(cand.target_series_id)
    if not validate_episode_response_for_cleanup(source_eps) or not validate_episode_response_for_cleanup(target_eps):
        cand.last_error = sanitize_text('cleanup revalidation uncertain: source or target episode response invalid')
        cand.save()
        return 'uncertain', cand.last_error
    stats = calculate_episode_monitoring(source_eps, target_eps, include_specials=include_specials)
    if stats.failures:
        cand.last_error = sanitize_text('cleanup revalidation uncertain: malformed episode data')
        cand.save()
        return 'uncertain', cand.last_error
    current_eligible = eligible_episode_file_ids(source_eps, target_eps, stats)
    current_keys = current_eligible.get(cand.episode_file_id)
    if current_keys == cand.linked_episode_keys:
        return 'eligible', ''
    if current_keys:
        _reset_candidate(cand, now=now, keys=current_keys)
        cand.last_confirmed_at = now
        cand.save()
        return 'reset', 'linked_set_changed'
    if file_absent(target_eps, cand.episode_file_id):
        cand.status = SonarrCleanupCandidate.STATUS_ALREADY_ABSENT
        cand.ready_at = None
        cand.deleted_at = None
        cand.last_confirmed_at = now
        cand.save()
        return 'already_absent', ''
    return 'cancel', 'revalidation_ineligible'


def process_cleanup_for_series(*, target_instance, tvdb_id, target_series_id, source_episodes, target_episodes, stats, target_api, source_api, source_series_id, include_specials=False, cleanup_enabled=False, dry_run=True, grace_hours=24, remaining_delete_budget=25, stop_real_deletes=False):
    counters = CleanupCounters()
    now = timezone.now()
    eligible = eligible_episode_file_ids(source_episodes, target_episodes, stats)
    active = SonarrCleanupCandidate.objects.filter(target_instance=target_instance, target_series_id=target_series_id)
    for cand in active:
        current_keys = eligible.get(cand.episode_file_id)
        if current_keys is None:
            if file_absent(target_episodes, cand.episode_file_id):
                if cand.status != SonarrCleanupCandidate.STATUS_ALREADY_ABSENT:
                    cand.status = SonarrCleanupCandidate.STATUS_ALREADY_ABSENT
                    cand.last_confirmed_at = now
                    cand.deleted_at = None
                    cand.ready_at = None
                    cand.save()
                    counters.cleanup_files_already_absent += 1
                    counters.events.append(f'cleanup already_absent tvdb={tvdb_id} series={target_series_id} episodeFileId={cand.episode_file_id}')
            elif cand.status not in (SonarrCleanupCandidate.STATUS_CANCELLED, SonarrCleanupCandidate.STATUS_DELETED, SonarrCleanupCandidate.STATUS_ALREADY_ABSENT):
                cand.tvdb_id = tvdb_id
                cand.target_series_id = target_series_id
                counters.cleanup_candidates_cancelled += 1
                counters.events.append(_mark_cancelled(cand, now, 'eligibility_disappeared'))
    for file_id, keys in eligible.items():
        cand, created = SonarrCleanupCandidate.objects.get_or_create(
            target_instance=target_instance,
            episode_file_id=file_id,
            defaults={'tvdb_id': tvdb_id, 'target_series_id': target_series_id, 'linked_episode_keys': keys, 'reason': REASON_PERMANENT_DUPLICATE, 'status': SonarrCleanupCandidate.STATUS_PENDING, 'first_eligible_at': now, 'last_confirmed_at': now},
        )
        terminal_reappeared = cand.status in (SonarrCleanupCandidate.STATUS_DELETED, SonarrCleanupCandidate.STATUS_ALREADY_ABSENT)
        if created:
            counters.cleanup_candidates_new += 1
            counters.cleanup_candidates_pending += 1
            counters.events.append(f'cleanup candidate created tvdb={tvdb_id} series={target_series_id} episodeFileId={file_id} linked={keys} reason=permanent_duplicate')
            continue
        if terminal_reappeared or cand.status == SonarrCleanupCandidate.STATUS_CANCELLED or cand.linked_episode_keys != keys:
            _reset_candidate(cand, now=now, keys=keys)
        cand.tvdb_id = tvdb_id
        cand.target_series_id = target_series_id
        cand.reason = REASON_PERMANENT_DUPLICATE
        cand.linked_episode_keys = keys
        cand.last_confirmed_at = now
        cand.last_error = ''
        if now >= cand.first_eligible_at + timedelta(hours=int(grace_hours)):
            if cand.status != SonarrCleanupCandidate.STATUS_READY:
                counters.events.append(f'cleanup candidate ready tvdb={tvdb_id} series={target_series_id} episodeFileId={file_id} linked={keys} reason=permanent_duplicate dry_run={dry_run}')
            cand.status = SonarrCleanupCandidate.STATUS_READY
            cand.ready_at = cand.ready_at or now
            counters.cleanup_candidates_ready += 1
        else:
            counters.cleanup_candidates_pending += 1
        cand.save()
    ready = list(SonarrCleanupCandidate.objects.filter(target_instance=target_instance, target_series_id=target_series_id, status=SonarrCleanupCandidate.STATUS_READY).order_by('ready_at','id'))
    for idx, cand in enumerate(ready):
        if not cleanup_enabled:
            continue
        if dry_run:
            counters.cleanup_would_delete += 1
            continue
        if stop_real_deletes or remaining_delete_budget <= counters.delete_attempts_consumed:
            counters.cleanup_deferred_by_limit += 1
            continue
        state, detail = revalidate_candidate_for_delete(
            cand=cand,
            source_api=source_api,
            target_api=target_api,
            source_series_id=source_series_id,
            include_specials=include_specials,
            grace_hours=grace_hours,
            dry_run=dry_run,
            remaining_delete_budget=remaining_delete_budget - counters.delete_attempts_consumed,
        )
        if state == 'eligible':
            counters.delete_attempts_consumed += 1
            res = target_api.delete_episode_files([cand.episode_file_id])
            fresh = target_api.get_episodes(target_series_id)
            if not validate_episode_response_for_cleanup(fresh):
                cand.last_error = sanitize_text('cleanup post-delete verification uncertain: invalid target episode response')
                cand.save()
                counters.cleanup_failures += 1
                counters.stop_deletes_for_run = True
                counters.cleanup_deferred_by_limit += len(ready) - idx - 1
                break
            absent = file_absent(fresh, cand.episode_file_id)
            api_failed = isinstance(res, dict) and (res.get('error') or res.get('errorMessage'))
            if absent and not api_failed:
                cand.status = SonarrCleanupCandidate.STATUS_DELETED
                cand.deleted_at = timezone.now()
                cand.last_error = ''
                cand.save()
                counters.cleanup_files_deleted += 1
                counters.events.append(f'cleanup deleted tvdb={tvdb_id} series={target_series_id} episodeFileId={cand.episode_file_id} linked={cand.linked_episode_keys}')
            elif absent and api_failed:
                cand.status = SonarrCleanupCandidate.STATUS_ALREADY_ABSENT
                cand.deleted_at = None
                cand.last_error = ''
                cand.save()
                counters.cleanup_files_already_absent += 1
                counters.events.append(f'cleanup already_absent tvdb={tvdb_id} series={target_series_id} episodeFileId={cand.episode_file_id}')
            else:
                cand.last_error = sanitize_text(f'cleanup delete uncertain: api={res}')
                cand.save()
                counters.cleanup_failures += 1
                counters.stop_deletes_for_run = True
                counters.cleanup_deferred_by_limit += len(ready) - idx - 1
                break
        elif state == 'cancel':
            counters.cleanup_candidates_cancelled += 1
            counters.events.append(_mark_cancelled(cand, timezone.now(), detail))
        elif state == 'already_absent':
            counters.cleanup_files_already_absent += 1
            counters.events.append(f'cleanup already_absent tvdb={tvdb_id} series={target_series_id} episodeFileId={cand.episode_file_id}')
        elif state == 'reset':
            counters.cleanup_candidates_pending += 1
            counters.events.append(f'cleanup candidate reset tvdb={tvdb_id} series={target_series_id} episodeFileId={cand.episode_file_id} reason={detail}')
        elif state == 'uncertain':
            counters.cleanup_failures += 1
            counters.stop_deletes_for_run = True
            counters.cleanup_deferred_by_limit += len(ready) - idx - 1
            counters.events.append(f'cleanup failure tvdb={tvdb_id} series={target_series_id} episodeFileId={cand.episode_file_id} reason={sanitize_text(detail)}')
            break
        else:
            counters.cleanup_deferred_by_limit += 1
    return counters

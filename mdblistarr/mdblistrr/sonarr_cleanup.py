from dataclasses import dataclass, field
from datetime import timedelta
from django.utils import timezone
from .connect import sanitize_text
from .models import SonarrCleanupCandidate
from .sonarr_reconcile import episode_key, REASON_PERMANENT_DUPLICATE


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
    events: list = field(default_factory=list)


def valid_file_id(value):
    try:
        return int(value) > 0
    except (TypeError, ValueError):
        return False


def linked_keys_for_file(target_episodes, episode_file_id):
    keys = []
    for ep in target_episodes:
        if not isinstance(ep, dict):
            return None
        if ep.get('episodeFileId') == episode_file_id:
            key = episode_key(ep)
            if key is None:
                return None
            keys.append([key[0], key[1]])
    return sorted(keys)


def eligible_episode_file_ids(source_episodes, target_episodes, stats):
    if not isinstance(source_episodes, list) or not isinstance(target_episodes, list):
        return {}
    source_has = {episode_key(ep): ep.get('hasFile') is True for ep in source_episodes if isinstance(ep, dict) and episode_key(ep) is not None}
    grouped = {}
    for ep in target_episodes:
        key = episode_key(ep) if isinstance(ep, dict) else None
        if key is None or ep.get('hasFile') is not True or not valid_file_id(ep.get('episodeFileId')):
            continue
        grouped.setdefault(int(ep.get('episodeFileId')), []).append(ep)
    eligible = {}
    for file_id, episodes in grouped.items():
        keys = []
        ok = True
        for ep in episodes:
            key = episode_key(ep)
            if ep.get('episodeFileId') != file_id or ep.get('monitored') is True:
                ok = False; break
            if source_has.get(key) is not True:
                ok = False; break
            if stats.desired_by_key.get(key) is not False or stats.reason_by_key.get(key) != REASON_PERMANENT_DUPLICATE:
                ok = False; break
            keys.append([key[0], key[1]])
        if ok:
            eligible[file_id] = sorted(keys)
    return eligible


def file_absent(target_episodes, file_id):
    return not any(isinstance(ep, dict) and ep.get('episodeFileId') == file_id and ep.get('hasFile') is True for ep in target_episodes)


def process_cleanup_for_series(*, target_instance, tvdb_id, target_series_id, source_episodes, target_episodes, stats, target_api, source_api=None, source_series_id=None, cleanup_enabled=False, dry_run=True, grace_hours=24, max_deletions=25, revalidate_func=None):
    counters = CleanupCounters()
    now = timezone.now()
    eligible = eligible_episode_file_ids(source_episodes, target_episodes, stats)
    active = SonarrCleanupCandidate.objects.filter(target_instance=target_instance, target_series_id=target_series_id).exclude(status__in=[SonarrCleanupCandidate.STATUS_DELETED, SonarrCleanupCandidate.STATUS_ALREADY_ABSENT])
    for cand in active:
        current_keys = eligible.get(cand.episode_file_id)
        if current_keys is None:
            if file_absent(target_episodes, cand.episode_file_id):
                cand.status = SonarrCleanupCandidate.STATUS_ALREADY_ABSENT; cand.last_confirmed_at = now; cand.save(); counters.cleanup_files_already_absent += 1; counters.events.append(f'cleanup already_absent tvdb={tvdb_id} series={target_series_id} episodeFileId={cand.episode_file_id}')
            else:
                cand.status = SonarrCleanupCandidate.STATUS_CANCELLED; cand.cancelled_at = now; cand.last_error = ''; cand.save(); counters.cleanup_candidates_cancelled += 1; counters.events.append(f'cleanup cancelled tvdb={tvdb_id} series={target_series_id} episodeFileId={cand.episode_file_id}')
    for file_id, keys in eligible.items():
        cand, created = SonarrCleanupCandidate.objects.get_or_create(target_instance=target_instance, episode_file_id=file_id, defaults={'tvdb_id': tvdb_id, 'target_series_id': target_series_id, 'linked_episode_keys': keys, 'reason': REASON_PERMANENT_DUPLICATE, 'status': SonarrCleanupCandidate.STATUS_PENDING, 'first_eligible_at': now, 'last_confirmed_at': now})
        if created:
            counters.cleanup_candidates_new += 1; counters.cleanup_candidates_pending += 1; counters.events.append(f'cleanup candidate created tvdb={tvdb_id} series={target_series_id} episodeFileId={file_id} linked={keys} reason=permanent_duplicate')
            continue
        if cand.linked_episode_keys != keys or cand.status == SonarrCleanupCandidate.STATUS_CANCELLED:
            cand.first_eligible_at = now; cand.ready_at = None; cand.status = SonarrCleanupCandidate.STATUS_PENDING
        cand.tvdb_id = tvdb_id; cand.target_series_id = target_series_id; cand.linked_episode_keys = keys; cand.last_confirmed_at = now; cand.last_error = ''
        if now >= cand.first_eligible_at + timedelta(hours=int(grace_hours)):
            if cand.status != SonarrCleanupCandidate.STATUS_READY:
                counters.events.append(f'cleanup candidate ready tvdb={tvdb_id} series={target_series_id} episodeFileId={file_id} linked={keys} reason=permanent_duplicate dry_run={dry_run}')
            cand.status = SonarrCleanupCandidate.STATUS_READY; cand.ready_at = cand.ready_at or now; counters.cleanup_candidates_ready += 1
        else:
            counters.cleanup_candidates_pending += 1
        cand.save()
    ready = list(SonarrCleanupCandidate.objects.filter(target_instance=target_instance, target_series_id=target_series_id, status=SonarrCleanupCandidate.STATUS_READY).order_by('ready_at','id'))
    deletions = 0
    for cand in ready:
        if deletions >= max_deletions:
            counters.cleanup_deferred_by_limit += 1; continue
        if not cleanup_enabled:
            continue
        if dry_run:
            counters.cleanup_would_delete += 1; continue
        try:
            if revalidate_func and not revalidate_func(cand):
                cand.status = SonarrCleanupCandidate.STATUS_CANCELLED; cand.cancelled_at = timezone.now(); cand.save(); counters.cleanup_candidates_cancelled += 1; continue
            res = target_api.delete_episode_files([cand.episode_file_id])
            fresh = target_api.get_episodes(target_series_id)
            absent = isinstance(fresh, list) and file_absent(fresh, cand.episode_file_id)
            if absent:
                cand.status = SonarrCleanupCandidate.STATUS_DELETED if not (isinstance(res, dict) and (res.get('error') or res.get('errorMessage'))) else SonarrCleanupCandidate.STATUS_ALREADY_ABSENT
                cand.deleted_at = timezone.now() if cand.status == SonarrCleanupCandidate.STATUS_DELETED else None
                cand.last_error = ''; cand.save(); deletions += 1
                if cand.status == SonarrCleanupCandidate.STATUS_DELETED: counters.cleanup_files_deleted += 1; counters.events.append(f'cleanup deleted tvdb={tvdb_id} series={target_series_id} episodeFileId={cand.episode_file_id} linked={cand.linked_episode_keys}')
                else: counters.cleanup_files_already_absent += 1
            else:
                cand.last_error = sanitize_text(res); cand.save(); counters.cleanup_failures += 1
        except Exception as e:
            cand.last_error = sanitize_text(e); cand.save(); counters.cleanup_failures += 1
    return counters

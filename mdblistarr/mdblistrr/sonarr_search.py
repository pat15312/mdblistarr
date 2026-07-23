from datetime import datetime
from django.utils import timezone
from .connect import sanitize_text
from .models import SonarrEpisodeSearchCandidate
from .sonarr_reconcile import episode_key, REASON_WANTED

SEARCH_STATUS_PENDING = SonarrEpisodeSearchCandidate.STATUS_PENDING
SEARCH_STATUS_SUBMITTED = SonarrEpisodeSearchCandidate.STATUS_SUBMITTED
SEARCH_STATUS_CANCELLED = SonarrEpisodeSearchCandidate.STATUS_CANCELLED


def _parse_sonarr_datetime(value):
    if value is None:
        return None, None
    if isinstance(value, str) and value.strip() == '':
        return None, None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).strip().replace('Z', '+00:00'))
        except (TypeError, ValueError):
            return None, 'invalid_lastSearchTime'
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt, timezone.get_current_timezone())
    return dt.astimezone(timezone.UTC), None


def _valid_positive_int(value):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def update_search_candidates_for_series(*, target_instance, tvdb_id, target_series_id, target_episodes, stats, applied_monitor_true_ids=None, series_monitored_confirmed=False, now=None):
    now = now or timezone.now()
    applied_monitor_true_ids = set(applied_monitor_true_ids or [])
    counters = {
        'search_candidates_new': 0,
        'search_candidates_pending': 0,
        'search_candidates_submitted': 0,
        'search_candidates_cancelled': 0,
        'search_candidates_deferred': 0,
        'search_failures': 0,
    }
    events = []
    eligible = {}
    by_id = {}
    for ep in target_episodes:
        episode_id = _valid_positive_int(ep.get('id')) if isinstance(ep, dict) else None
        if episode_id is not None:
            by_id[episode_id] = ep

    for episode_id in stats.wanted_missing_episode_ids if series_monitored_confirmed else []:
        ep = by_id.get(episode_id)
        key = episode_key(ep) if isinstance(ep, dict) else None
        if ep is None or key is None or stats.desired_by_key.get(key) is not True or stats.reason_by_key.get(key) != REASON_WANTED:
            continue
        last_search, error = _parse_sonarr_datetime(ep.get('lastSearchTime'))
        if error:
            counters['search_failures'] = 1
            events.append(f'search candidate failure tvdb={tvdb_id} series={target_series_id} episode={episode_id} reason={sanitize_text(error)}')
            return counters, events, True
        eligible[episode_id] = (ep, key, last_search)

    existing = {c.target_episode_id: c for c in SonarrEpisodeSearchCandidate.objects.filter(target_instance=target_instance, target_series_id=target_series_id)}
    seen = set()
    for episode_id, (ep, key, last_search) in eligible.items():
        seen.add(episode_id)
        cand = existing.get(episode_id)
        newly_monitored = episode_id in applied_monitor_true_ids
        if last_search is not None and not newly_monitored:
            if cand and cand.status == SEARCH_STATUS_PENDING:
                cand.status = SEARCH_STATUS_SUBMITTED
                cand.submitted_at = last_search
                cand.cancelled_at = None
                cand.last_error = ''
                cand.last_confirmed_at = now
                cand.save(update_fields=['status', 'submitted_at', 'cancelled_at', 'last_error', 'last_confirmed_at', 'updated_at'])
                counters['search_candidates_submitted'] += 1
                events.append(f'search candidate submitted tvdb={tvdb_id} series={target_series_id} episode={episode_id} source=lastSearchTime')
            continue
        defaults = {
            'target_series_id': target_series_id,
            'tvdb_id': tvdb_id,
            'season_number': key[0],
            'episode_number': key[1],
            'status': SEARCH_STATUS_PENDING,
            'first_eligible_at': now,
            'last_confirmed_at': now,
            'submitted_at': None,
            'cancelled_at': None,
            'last_error': '',
        }
        if cand is None:
            SonarrEpisodeSearchCandidate.objects.create(target_instance=target_instance, target_episode_id=episode_id, **defaults)
            counters['search_candidates_new'] += 1
            events.append(f'search candidate created tvdb={tvdb_id} series={target_series_id} episode={episode_id}')
        elif cand.status == SEARCH_STATUS_CANCELLED or newly_monitored:
            cand.target_series_id = target_series_id
            cand.tvdb_id = tvdb_id
            cand.season_number = key[0]
            cand.episode_number = key[1]
            cand.status = SEARCH_STATUS_PENDING
            cand.first_eligible_at = now
            cand.last_confirmed_at = now
            cand.submitted_at = None
            cand.cancelled_at = None
            cand.last_error = ''
            cand.save(update_fields=['target_series_id', 'tvdb_id', 'season_number', 'episode_number', 'status', 'first_eligible_at', 'last_confirmed_at', 'submitted_at', 'cancelled_at', 'last_error', 'updated_at'])
            counters['search_candidates_pending'] += 1
            events.append(f'search candidate reset tvdb={tvdb_id} series={target_series_id} episode={episode_id}')
        elif cand.status == SEARCH_STATUS_PENDING:
            cand.last_confirmed_at = now
            cand.last_error = '' if not cand.last_error else cand.last_error
            cand.save(update_fields=['last_confirmed_at', 'last_error', 'updated_at'])
            counters['search_candidates_pending'] += 1
        elif cand.status == SEARCH_STATUS_SUBMITTED:
            cand.last_confirmed_at = now
            cand.save(update_fields=['last_confirmed_at', 'updated_at'])
            counters['search_candidates_deferred'] += 1

    for episode_id, cand in existing.items():
        if episode_id in seen or cand.status != SEARCH_STATUS_PENDING:
            continue
        cand.status = SEARCH_STATUS_CANCELLED
        cand.cancelled_at = now
        cand.last_confirmed_at = now
        cand.last_error = ''
        cand.save(update_fields=['status', 'cancelled_at', 'last_confirmed_at', 'last_error', 'updated_at'])
        counters['search_candidates_cancelled'] += 1
        events.append(f'search candidate cancelled tvdb={cand.tvdb_id} series={cand.target_series_id} episode={episode_id}')

    return counters, events, False


def submit_pending_search_candidates(*, target_api, target_instance, target_series_id, batch_size=100, now=None):
    now = now or timezone.now()
    counters = {'submitted': 0, 'initial_submitted': 0, 'failures': 0}
    events = []
    pending = list(SonarrEpisodeSearchCandidate.objects.filter(
        target_instance=target_instance,
        target_series_id=target_series_id,
        status=SEARCH_STATUS_PENDING,
    ).order_by('target_episode_id'))
    for i in range(0, len(pending), batch_size):
        batch = pending[i:i + batch_size]
        if not batch:
            continue
        ids = [c.target_episode_id for c in batch]
        res = target_api.trigger_episode_search(ids)
        failed = False
        if res is None:
            failed = True
        elif isinstance(res, dict):
            status_code = res.get('status_code')
            if status_code is not None:
                try:
                    failed = int(status_code) < 200 or int(status_code) >= 300
                except (TypeError, ValueError):
                    failed = True
            if res.get('error') or res.get('errorMessage'):
                failed = True
        if failed:
            msg = sanitize_text(res if isinstance(res, str) else (res or 'EpisodeSearch failed'))
            for cand in batch:
                cand.last_error = msg
                cand.last_confirmed_at = now
                cand.save(update_fields=['last_error', 'last_confirmed_at', 'updated_at'])
            counters['failures'] += 1
            events.append(f'search candidate submission failure series={target_series_id} episodes={ids} reason={msg}')
            return counters, events, True
        for cand in batch:
            cand.status = SEARCH_STATUS_SUBMITTED
            cand.submitted_at = now
            cand.cancelled_at = None
            cand.last_error = ''
            cand.last_confirmed_at = now
            cand.save(update_fields=['status', 'submitted_at', 'cancelled_at', 'last_error', 'last_confirmed_at', 'updated_at'])
            events.append(f'search candidate submitted tvdb={cand.tvdb_id} series={cand.target_series_id} episode={cand.target_episode_id}')
        counters['submitted'] += len(batch)
        counters['initial_submitted'] += len(batch)
    return counters, events, False

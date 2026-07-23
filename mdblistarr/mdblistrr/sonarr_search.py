import ast
import json
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


def _aware(value):
    if value is None:
        return None
    if timezone.is_naive(value):
        return timezone.make_aware(value, timezone.get_current_timezone())
    return value.astimezone(timezone.UTC)


def _identity_changed(cand, key, tvdb_id, target_series_id):
    return (
        cand.tvdb_id != tvdb_id
        or cand.target_series_id != target_series_id
        or cand.season_number != key[0]
        or cand.episode_number != key[1]
    )


def _reset_pending(cand, *, tvdb_id, target_series_id, key, now):
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


def _mark_submitted(cand, *, submitted_at, now):
    cand.status = SEARCH_STATUS_SUBMITTED
    cand.submitted_at = submitted_at
    cand.cancelled_at = None
    cand.last_error = ''
    cand.last_confirmed_at = now
    cand.save(update_fields=['status', 'submitted_at', 'cancelled_at', 'last_error', 'last_confirmed_at', 'updated_at'])


def command_response_succeeded(response):
    if not isinstance(response, dict) or not response:
        return False
    if response.get('error') or response.get('errorMessage'):
        return False

    status_code = response.get('status_code')
    if isinstance(status_code, bool) or not isinstance(status_code, int):
        return False
    if status_code < 200 or status_code >= 300:
        return False

    if _valid_positive_int(response.get('id')) is None:
        return False

    body = response.get('body')
    if body is not None and not isinstance(body, dict):
        return False
    if response.get('name') == 'EpisodeSearch':
        return True
    return isinstance(body, dict) and body.get('name') == 'EpisodeSearch'


def command_response_failed(response):
    return not command_response_succeeded(response)


def _command_id(response):
    return response.get('id') if command_response_succeeded(response) else None


def _episode_search_failure_reason(response):
    if isinstance(response, dict):
        status_code = response.get('status_code')
        if isinstance(status_code, int) and not isinstance(status_code, bool):
            return f'http_{status_code}'
        if response.get('error'):
            return 'api_error'
        if response.get('errorMessage'):
            return 'api_error_message'
        return 'invalid_command_response'
    if isinstance(response, str):
        return sanitize_text(response)[:120] or 'request_error'
    return 'invalid_command_response'


def _parse_legacy_response(value):
    if not value or not isinstance(value, str):
        return None
    for parser in (json.loads, ast.literal_eval):
        try:
            parsed = parser(value)
        except (TypeError, ValueError, SyntaxError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _parse_queued_datetime(value):
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace('Z', '+00:00'))
    except ValueError:
        return None
    if timezone.is_naive(dt):
        return None
    return dt.astimezone(timezone.UTC)


def _legacy_response_submitted_at(cand):
    response = _parse_legacy_response(cand.last_error)
    if not command_response_succeeded(response):
        return None, None
    body = response.get('body')
    if not isinstance(body, dict):
        return None, None
    episode_ids = body.get('episodeIds')
    if not isinstance(episode_ids, list):
        return None, None
    valid_ids = []
    for episode_id in episode_ids:
        if _valid_positive_int(episode_id) is None:
            return None, None
        valid_ids.append(episode_id)
    if len(set(valid_ids)) != len(valid_ids) or cand.target_episode_id not in valid_ids:
        return None, None
    queued_at = _parse_queued_datetime(response.get('queued'))
    first_eligible_at = _aware(cand.first_eligible_at)
    if queued_at is None or first_eligible_at is None or queued_at < first_eligible_at:
        return None, None
    return queued_at, response.get('id')


def update_search_candidates_for_series(*, target_instance, tvdb_id, target_series_id, target_episodes, stats, applied_monitor_true_ids=None, series_monitored_confirmed=False, now=None):
    now = now or timezone.now()
    applied_monitor_true_ids = set(applied_monitor_true_ids or [])
    counters = {
        'search_candidates_new': 0,
        'search_candidates_pending': 0,
        'search_candidates_submitted': 0,
        'search_candidates_cancelled': 0,
        'search_candidates_deferred': 0,
        'search_candidates_recovered': 0,
        'search_recovery_failures': 0,
        'search_failures': 0,
    }
    events = []
    recovered_by_command = {}
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
        if cand is None:
            if last_search is not None and not newly_monitored:
                continue
            SonarrEpisodeSearchCandidate.objects.create(
                target_instance=target_instance, target_episode_id=episode_id,
                target_series_id=target_series_id, tvdb_id=tvdb_id, season_number=key[0], episode_number=key[1],
                status=SEARCH_STATUS_PENDING, first_eligible_at=now, last_confirmed_at=now,
                submitted_at=None, cancelled_at=None, last_error='')
            counters['search_candidates_new'] += 1
            events.append(f'search candidate created tvdb={tvdb_id} series={target_series_id} episode={episode_id}')
            continue

        identity_changed = _identity_changed(cand, key, tvdb_id, target_series_id)
        if newly_monitored or (identity_changed and cand.status in (SEARCH_STATUS_PENDING, SEARCH_STATUS_CANCELLED)):
            _reset_pending(cand, tvdb_id=tvdb_id, target_series_id=target_series_id, key=key, now=now)
            counters['search_candidates_pending'] += 1
            events.append(f'search candidate reset tvdb={tvdb_id} series={target_series_id} episode={episode_id}')
            continue

        if cand.status == SEARCH_STATUS_PENDING:
            submitted_at, command_id = _legacy_response_submitted_at(cand)
            if submitted_at is not None:
                _mark_submitted(cand, submitted_at=submitted_at, now=now)
                counters['search_candidates_recovered'] += 1
                recovered_by_command[command_id] = recovered_by_command.get(command_id, 0) + 1
            else:
                first_eligible_at = _aware(cand.first_eligible_at)
                if last_search is not None and first_eligible_at is not None and last_search >= first_eligible_at:
                    _mark_submitted(cand, submitted_at=last_search, now=now)
                    counters['search_candidates_submitted'] += 1
                    events.append(f'search candidate submitted tvdb={tvdb_id} series={target_series_id} episode={episode_id} source=lastSearchTime')
                else:
                    cand.last_confirmed_at = now
                    cand.save(update_fields=['last_confirmed_at', 'updated_at'])
                    counters['search_candidates_pending'] += 1
            continue

        if cand.status == SEARCH_STATUS_CANCELLED:
            cancelled_at = _aware(cand.cancelled_at)
            if last_search is not None and cancelled_at is not None and last_search > cancelled_at:
                _mark_submitted(cand, submitted_at=last_search, now=now)
                counters['search_candidates_submitted'] += 1
                events.append(f'search candidate submitted tvdb={tvdb_id} series={target_series_id} episode={episode_id} source=lastSearchTime')
            else:
                _reset_pending(cand, tvdb_id=tvdb_id, target_series_id=target_series_id, key=key, now=now)
                counters['search_candidates_pending'] += 1
                events.append(f'search candidate reset tvdb={tvdb_id} series={target_series_id} episode={episode_id}')
            continue

        if cand.status == SEARCH_STATUS_SUBMITTED:
            cand.last_confirmed_at = now
            cand.save(update_fields=['last_confirmed_at', 'updated_at'])
            counters['search_candidates_deferred'] += 1

    for command_id, recovered_count in recovered_by_command.items():
        events.append(f'EpisodeSearch candidate recovery series={target_series_id} command_id={command_id} recovered={recovered_count}')

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
        if command_response_failed(res):
            msg = _episode_search_failure_reason(res)
            for cand in batch:
                cand.last_error = msg
                cand.last_confirmed_at = now
                cand.save(update_fields=['last_error', 'last_confirmed_at', 'updated_at'])
            counters['failures'] += 1
            events.append(f'EpisodeSearch submission failure series={target_series_id} episodes={len(ids)} reason={msg}')
            return counters, events, True
        command_id = _command_id(res)
        for cand in batch:
            cand.status = SEARCH_STATUS_SUBMITTED
            cand.submitted_at = now
            cand.cancelled_at = None
            cand.last_error = ''
            cand.last_confirmed_at = now
            cand.save(update_fields=['status', 'submitted_at', 'cancelled_at', 'last_error', 'last_confirmed_at', 'updated_at'])
        events.append(f'EpisodeSearch queued series={target_series_id} command_id={command_id} episodes={len(batch)}')
        counters['submitted'] += len(batch)
        counters['initial_submitted'] += len(batch)
    return counters, events, False

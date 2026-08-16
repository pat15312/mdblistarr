import ast
import json
from datetime import datetime, timedelta
from django.utils import timezone
from django.db import transaction, models
from .connect import sanitize_text
from .models import (SonarrEpisodeSearchCandidate, SonarrEpisodeSearchCommand,
    SonarrEpisodeSearchCommandCandidate)
from .sonarr_reconcile import episode_key, REASON_WANTED

SEARCH_STATUS_PENDING = SonarrEpisodeSearchCandidate.STATUS_PENDING
SEARCH_STATUS_SUBMITTED = SonarrEpisodeSearchCandidate.STATUS_SUBMITTED
SEARCH_STATUS_CANCELLED = SonarrEpisodeSearchCandidate.STATUS_CANCELLED
SEARCH_STATUS_FAILED = SonarrEpisodeSearchCandidate.STATUS_FAILED


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
    cand.current_command = None
    cand.attempt_count = 0
    cand.retry_not_before = None
    cand.last_error = ''
    cand.save(update_fields=['target_series_id', 'tvdb_id', 'season_number', 'episode_number', 'status', 'first_eligible_at', 'last_confirmed_at', 'submitted_at', 'cancelled_at', 'current_command', 'attempt_count', 'retry_not_before', 'last_error', 'updated_at'])


def _mark_submitted(cand, *, submitted_at, now):
    cand.status = SEARCH_STATUS_SUBMITTED
    cand.submitted_at = submitted_at
    cand.cancelled_at = None
    cand.retry_not_before = None
    cand.last_error = ''
    cand.last_confirmed_at = now
    cand.save(update_fields=['status', 'submitted_at', 'cancelled_at', 'retry_not_before', 'last_error', 'last_confirmed_at', 'updated_at'])


def _cancel_retry_exhausted(cand, *, now):
    """Retire a current failure without rewriting its search-command history."""
    cand.status = SEARCH_STATUS_CANCELLED
    cand.cancelled_at = now
    cand.last_confirmed_at = now
    cand.retry_not_before = None
    cand.last_error = ''
    cand.save(update_fields=['status', 'cancelled_at', 'last_confirmed_at',
        'retry_not_before', 'last_error', 'updated_at'])


def resolve_failed_candidates_for_removed_series(*, target_instance,
        target_series_ids, now=None):
    """Resolve failures whose series is absent from a validated target snapshot."""
    now = now or timezone.now()
    present = set(target_series_ids)
    candidates = SonarrEpisodeSearchCandidate.objects.filter(
        target_instance=target_instance, status=SEARCH_STATUS_FAILED)
    count = 0
    events = []
    for cand in candidates:
        if cand.target_series_id in present:
            continue
        _cancel_retry_exhausted(cand, now=now)
        count += 1
        events.append(
            f'EpisodeSearch retry-exhausted candidate resolved tvdb={cand.tvdb_id} '
            f'series={cand.target_series_id} episode={cand.target_episode_id} '
            'reason=no_longer_eligible')
    return count, events


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

    logically_eligible = set()
    for episode_id in stats.wanted_missing_episode_ids:
        ep = by_id.get(episode_id)
        key = episode_key(ep) if isinstance(ep, dict) else None
        if ep is None or key is None or stats.desired_by_key.get(key) is not True or stats.reason_by_key.get(key) != REASON_WANTED:
            continue
        logically_eligible.add(episode_id)
        if not series_monitored_confirmed:
            continue
        last_search, error = _parse_sonarr_datetime(ep.get('lastSearchTime'))
        if error:
            counters['search_failures'] = 1
            events.append(f'search candidate failure tvdb={tvdb_id} series={target_series_id} episode={episode_id} reason={sanitize_text(error)}')
            return counters, events, True
        eligible[episode_id] = (ep, key, last_search)

    existing = {c.target_episode_id: c for c in SonarrEpisodeSearchCandidate.objects.filter(target_instance=target_instance, target_series_id=target_series_id)}
    for episode_id, (ep, key, last_search) in eligible.items():
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
        if episode_id in logically_eligible or cand.status not in (SEARCH_STATUS_PENDING, SEARCH_STATUS_FAILED):
            continue
        current_episode = by_id.get(episode_id)
        has_search_lineage = cand.attempt_count > 0 or cand.current_command_id is not None
        if cand.status == SEARCH_STATUS_FAILED and has_search_lineage and current_episode is not None:
            current_key = episode_key(current_episode)
            if current_key is None or _identity_changed(
                    cand, current_key, tvdb_id, target_series_id):
                # A reused or malformed episode identity is not evidence that the
                # retry-exhausted acquisition need was resolved. Fail closed.
                continue
        was_failed = cand.status == SEARCH_STATUS_FAILED
        _cancel_retry_exhausted(cand, now=now)
        counters['search_candidates_cancelled'] += 1
        if was_failed:
            events.append(f'EpisodeSearch retry-exhausted candidate resolved tvdb={cand.tvdb_id} series={cand.target_series_id} episode={episode_id} reason=no_longer_eligible')
        else:
            events.append(f'search candidate cancelled tvdb={cand.tvdb_id} series={cand.target_series_id} episode={episode_id}')

    return counters, events, False

KNOWN_COMMAND_STATUSES = frozenset(('queued', 'started', 'completed', 'failed', 'aborted', 'cancelled', 'orphaned'))
KNOWN_COMMAND_RESULTS = frozenset(('', 'unknown', 'successful', 'unsuccessful', 'indeterminate'))
TERMINAL_FAILURE_STATUSES = frozenset(('failed', 'aborted', 'cancelled', 'orphaned'))
ADOPTION_WINDOW = timedelta(minutes=10)
COMMAND_FALLBACK_LIMIT = 10
FALLBACK_DEFERRED = ('fallback_budget_deferred', None)


def _strict_command_datetime(value, field, required=False):
    if value in (None, '') and not required:
        return None
    if not isinstance(value, str):
        raise ValueError(f'invalid_{field}')
    try:
        parsed = datetime.fromisoformat(value.strip().replace('Z', '+00:00'))
    except (TypeError, ValueError):
        raise ValueError(f'invalid_{field}')
    if timezone.is_naive(parsed):
        raise ValueError(f'naive_{field}')
    return parsed.astimezone(timezone.UTC)


def validate_episode_search_command(resource, expected_episode_ids=None, expected_id=None):
    """Validate the security-relevant subset of a Sonarr V3 CommandResource."""
    if not isinstance(resource, dict) or resource.get('error') or resource.get('errorMessage'):
        raise ValueError('malformed_command')
    command_id = _valid_positive_int(resource.get('id'))
    if command_id is None or (expected_id is not None and command_id != expected_id):
        raise ValueError('invalid_command_id')
    body = resource.get('body')
    if not isinstance(body, dict):
        raise ValueError('invalid_command_body')
    identity = resource.get('name') or body.get('name')
    if identity != 'EpisodeSearch' or (resource.get('name') not in (None, 'EpisodeSearch')) or body.get('name') not in (None, 'EpisodeSearch'):
        raise ValueError('wrong_command_type')
    episode_ids = body.get('episodeIds')
    if not isinstance(episode_ids, list) or not episode_ids:
        raise ValueError('invalid_episode_ids')
    if any(_valid_positive_int(value) is None for value in episode_ids) or len(set(episode_ids)) != len(episode_ids):
        raise ValueError('invalid_episode_ids')
    if expected_episode_ids is not None and set(episode_ids) != set(expected_episode_ids):
        raise ValueError('mismatched_episode_ids')
    status = resource.get('status')
    if not isinstance(status, str) or status.lower() not in KNOWN_COMMAND_STATUSES:
        raise ValueError('unknown_command_status')
    status = status.lower()
    result = resource.get('result', '')
    if result is None:
        result = ''
    if not isinstance(result, str) or result.lower() not in KNOWN_COMMAND_RESULTS:
        raise ValueError('unknown_command_result')
    parsed = {'id': command_id, 'status': status, 'result': result.lower(), 'episode_ids': tuple(episode_ids)}
    for source, target in (('queued', 'queued_at'), ('started', 'started_at'), ('ended', 'ended_at')):
        parsed[target] = _strict_command_datetime(resource.get(source), source)
    return parsed


def validate_command_list(payload):
    if not isinstance(payload, list):
        raise ValueError('command_list_not_list')
    mapped = {}
    for item in payload:
        if isinstance(item, dict):
            body = item.get('body')
            identity = item.get('name') or (body.get('name') if isinstance(body, dict) else None)
            if identity != 'EpisodeSearch':
                continue
        parsed = validate_episode_search_command(item)
        if parsed['id'] in mapped:
            raise ValueError('duplicate_command_id')
        mapped[parsed['id']] = (item, parsed)
    return mapped


def poll_episode_search_commands(target_api, target_instance, fallback_limit=COMMAND_FALLBACK_LIMIT):
    """Poll once, then use a deterministic bounded fallback for absent tracked IDs."""
    try:
        mapped = validate_command_list(target_api.get_commands())
    except (TypeError, ValueError):
        return {}, True, 0
    active = list(SonarrEpisodeSearchCommand.objects.filter(
        target_instance=target_instance,
    ).exclude(status__in=('completed', 'superseded')).exclude(
        status__in=TERMINAL_FAILURE_STATUSES, outcome_reconciled_at__isnull=False,
    ).filter(sonarr_command_id__isnull=False).prefetch_related('candidate_links').order_by('last_fallback_checked_at', 'sonarr_command_id'))
    fallbacks = 0
    absent = [command for command in active if command.sonarr_command_id not in mapped]
    never_checked = [command for command in absent if command.last_fallback_checked_at is None]
    selected = (never_checked if never_checked else absent)[:fallback_limit]
    selected_ids = {command.id for command in selected}
    checked_at = timezone.now()
    for command in selected:
        if command.sonarr_command_id in mapped:
            continue
        fallbacks += 1
        resource = target_api.get_command(command.sonarr_command_id)
        command.last_fallback_checked_at = checked_at
        command.save(update_fields=['last_fallback_checked_at', 'updated_at'])
        status_code = resource.get('status_code') if isinstance(resource, dict) else None
        if status_code == 404:
            continue
        try:
            parsed = validate_episode_search_command(resource, _command_snapshot(command), command.sonarr_command_id)
        except ValueError:
            return mapped, True, fallbacks
        mapped[command.sonarr_command_id] = (resource, parsed)
    for command in absent:
        if command.id in selected_ids:
            continue
        mapped[command.sonarr_command_id] = FALLBACK_DEFERRED
    return mapped, False, fallbacks


def _command_snapshot(command):
    return list(command.candidate_links.order_by('id').values_list('target_episode_id', flat=True))


def _set_candidates_submitted(command, now):
    candidates = list(command.candidates.all())
    for candidate in candidates:
        candidate.status = SEARCH_STATUS_SUBMITTED
        candidate.current_command = command
        candidate.submitted_at = command.queued_at or now
        candidate.cancelled_at = None
        candidate.retry_not_before = None
        candidate.last_error = ''
        candidate.last_confirmed_at = now
        candidate.attempt_count = max(candidate.attempt_count, command.attempt_number)
    SonarrEpisodeSearchCandidate.objects.bulk_update(candidates, ['status', 'current_command', 'submitted_at', 'cancelled_at', 'retry_not_before', 'last_error', 'last_confirmed_at', 'attempt_count', 'updated_at'])


def _apply_validated_command(command, parsed, now, retry_delay_minutes, max_retries, eligible_episode_ids, episode_by_id):
    old_status = command.status
    status = parsed['status']
    # Sonarr explicitly distinguishes a completed-but-unsuccessful command.
    if status == 'completed' and parsed['result'] == 'unsuccessful':
        status = 'failed'
    elif status == 'completed' and parsed['result'] == 'indeterminate':
        status = 'ambiguous'
    command.sonarr_status = parsed['status']
    command.sonarr_result = parsed['result']
    command.queued_at = parsed['queued_at'] or command.queued_at
    command.started_at = parsed['started_at'] or command.started_at
    command.ended_at = parsed['ended_at'] or command.ended_at
    command.last_checked_at = now
    command.unavailable_since = None
    command.failure_reason = (
        'completed_unsuccessful' if status == 'failed' and parsed['status'] == 'completed'
        else 'completed_indeterminate' if status == 'ambiguous' and parsed['status'] == 'completed'
        else ''
    )
    command.status = status
    if status == 'completed' or status in TERMINAL_FAILURE_STATUSES or (parsed['status'] == 'completed' and status == 'ambiguous'):
        command.terminal_at = command.terminal_at or command.ended_at or now
    command.save()
    candidates = list(command.candidates.all())
    counters = {key: 0 for key in ('search_candidates_requeued','search_candidates_retry_exhausted','search_candidates_satisfied_by_file','search_candidates_satisfied_by_last_search')}
    if status in ('queued', 'started', 'completed'):
        _set_candidates_submitted(command, now)
    elif status in TERMINAL_FAILURE_STATUSES and command.outcome_reconciled_at is None:
        threshold = command.queued_at or command.submission_attempted_at
        for candidate in candidates:
            # A newer association wins; never roll it back from an old failure.
            if candidate.current_command_id != command.id:
                continue
            episode = episode_by_id.get(candidate.target_episode_id, {})
            if episode.get('hasFile') is True:
                candidate.status = SEARCH_STATUS_SUBMITTED
                candidate.retry_not_before = None
                candidate.last_error = ''
                counters['search_candidates_satisfied_by_file'] += 1
            else:
                last_search, error = _parse_sonarr_datetime(episode.get('lastSearchTime'))
                if not error and last_search is not None and last_search >= threshold:
                    candidate.status = SEARCH_STATUS_SUBMITTED
                    candidate.retry_not_before = None
                    candidate.last_error = ''
                    counters['search_candidates_satisfied_by_last_search'] += 1
                elif candidate.target_episode_id not in eligible_episode_ids:
                    candidate.status = SEARCH_STATUS_CANCELLED
                    candidate.cancelled_at = now
                    candidate.retry_not_before = None
                    candidate.last_error = ''
                elif candidate.attempt_count - 1 >= max_retries:
                    candidate.status = SEARCH_STATUS_FAILED
                    candidate.retry_not_before = None
                    candidate.last_error = f'EpisodeSearch {status}; automatic retry limit exhausted'
                    counters['search_candidates_retry_exhausted'] += 1
                else:
                    candidate.status = SEARCH_STATUS_PENDING
                    if candidate.retry_not_before is None:
                        candidate.retry_not_before = now + timedelta(minutes=retry_delay_minutes)
                    candidate.last_error = f'EpisodeSearch {status}; retry scheduled'
                    counters['search_candidates_requeued'] += 1
            candidate.last_confirmed_at = now
        SonarrEpisodeSearchCandidate.objects.bulk_update(candidates, ['status','cancelled_at','retry_not_before','last_error','last_confirmed_at','updated_at'])
        command.outcome_reconciled_at = now
        command.save(update_fields=['outcome_reconciled_at', 'updated_at'])
    changed = old_status != status
    return counters, changed, status


def reconcile_search_commands_for_series(*, target_instance, target_series_id, command_map, poll_failed,
        target_episodes, eligible_episode_ids, max_retries=3, retry_delay_minutes=30,
        missing_grace_hours=24, now=None):
    now = now or timezone.now()
    counters = {key: 0 for key in ('search_commands_polled','search_commands_queued','search_commands_started','search_commands_completed','search_commands_failed','search_commands_aborted','search_commands_cancelled','search_commands_orphaned','search_commands_ambiguous','search_commands_unavailable','search_command_poll_failures','search_candidates_requeued','search_candidates_retry_exhausted','search_candidates_satisfied_by_file','search_candidates_satisfied_by_last_search')}
    events, unsafe = [], False
    episode_by_id = {ep.get('id'): ep for ep in target_episodes if isinstance(ep, dict) and _valid_positive_int(ep.get('id'))}
    commands = list(SonarrEpisodeSearchCommand.objects.filter(
        target_instance=target_instance, target_series_id=target_series_id,
    ).exclude(status__in=('completed','superseded')).exclude(
        status__in=TERMINAL_FAILURE_STATUSES, outcome_reconciled_at__isnull=False,
    ).prefetch_related('candidate_links','candidates'))
    if not commands:
        return counters, events, False
    if poll_failed:
        counters['search_command_poll_failures'] = 1
        return counters, ['EpisodeSearch command polling failure series=%s' % target_series_id], True
    tracked_ids = set(SonarrEpisodeSearchCommand.objects.filter(target_instance=target_instance, sonarr_command_id__isnull=False).values_list('sonarr_command_id', flat=True))
    for command in commands:
        snapshot = _command_snapshot(command)
        entry = command_map.get(command.sonarr_command_id) if command.sonarr_command_id else None
        # Adopt only an exact, unique, time-consistent unclaimed command.
        if command.sonarr_command_id is None and command.status in ('submitting','ambiguous'):
            matches = []
            for cid, (_raw, parsed) in command_map.items():
                if parsed is None:
                    continue
                queued = parsed['queued_at']
                if cid not in tracked_ids and set(parsed['episode_ids']) == set(snapshot) and queued and abs(queued - command.submission_attempted_at) <= ADOPTION_WINDOW:
                    matches.append((cid, parsed))
            if len(matches) == 1:
                command.sonarr_command_id = matches[0][0]
                command.save(update_fields=['sonarr_command_id','updated_at'])
                tracked_ids.add(matches[0][0])
                entry = command_map[matches[0][0]]
            else:
                if command.status != 'ambiguous':
                    command.status = 'ambiguous'; command.save(update_fields=['status','updated_at'])
                counters['search_commands_ambiguous'] += 1; unsafe = True
                continue
        if entry is not None:
            if entry == FALLBACK_DEFERRED:
                counters['search_commands_unavailable'] += 1
                unsafe = True
                continue
            try:
                parsed = validate_episode_search_command(entry[0], snapshot, command.sonarr_command_id)
            except ValueError:
                command.status = 'ambiguous'; command.failure_reason = 'command_validation_failed'; command.last_checked_at = now; command.save()
                counters['search_commands_ambiguous'] += 1; unsafe = True
                continue
            counters['search_commands_polled'] += 1
            changes, transitioned, final_status = _apply_validated_command(command, parsed, now, retry_delay_minutes, max_retries, set(eligible_episode_ids), episode_by_id)
            for key, value in changes.items(): counters[key] += value
            counters['search_commands_' + final_status] += 1
            if final_status in ('ambiguous', 'unavailable'):
                unsafe = True
            if transitioned:
                events.append(f'EpisodeSearch {final_status} series={target_series_id} command_id={command.sonarr_command_id} episodes={len(snapshot)}')
            continue
        # Absence is evidence of nothing. Resolve only if every candidate has execution evidence.
        threshold = command.queued_at or command.submission_attempted_at
        evidence = True
        evidence_files = evidence_searches = 0
        for candidate in command.candidates.all():
            episode = episode_by_id.get(candidate.target_episode_id, {})
            last_search, error = _parse_sonarr_datetime(episode.get('lastSearchTime'))
            if episode.get('hasFile') is True:
                evidence_files += 1
            elif not error and last_search is not None and last_search >= threshold:
                evidence_searches += 1
            else:
                evidence = False; break
        if evidence and snapshot:
            command.status = 'completed'; command.terminal_at = now; command.ended_at = now; command.last_checked_at = now; command.failure_reason = 'completed_by_episode_evidence'; command.save()
            _set_candidates_submitted(command, now); counters['search_commands_completed'] += 1
            counters['search_candidates_satisfied_by_file'] += evidence_files
            counters['search_candidates_satisfied_by_last_search'] += evidence_searches
        else:
            previous_status = command.status
            first_missing = command.unavailable_since or now
            command.status = 'unavailable'; command.unavailable_since = first_missing; command.last_checked_at = now
            age = now - first_missing
            command.failure_reason = 'missing_after_grace' if age >= timedelta(hours=missing_grace_hours) else 'missing_within_grace'
            command.save(); counters['search_commands_unavailable'] += 1; unsafe = True
            if previous_status != 'unavailable':
                events.append(f'EpisodeSearch command unavailable series={target_series_id} command_id={command.sonarr_command_id}')
    return counters, events, unsafe


def submit_pending_search_candidates(*, target_api, target_instance, target_series_id, batch_size=100, now=None):
    now = now or timezone.now()
    counters = {'submitted': 0, 'initial_submitted': 0, 'retry_submitted': 0, 'failures': 0}
    events = []
    had_failure = False
    due = SonarrEpisodeSearchCandidate.objects.filter(
        target_instance=target_instance, target_series_id=target_series_id,
        status=SEARCH_STATUS_PENDING,
    ).filter(models.Q(retry_not_before__isnull=True) | models.Q(retry_not_before__lte=now)).select_related('current_command').order_by('target_episode_id')
    groups = {}
    for candidate in due:
        previous = candidate.current_command
        is_retry = candidate.attempt_count > 0
        # A retry without an exact, processed terminal predecessor is unsafe.
        if is_retry and (previous is None or previous.status not in TERMINAL_FAILURE_STATUSES or previous.outcome_reconciled_at is None):
            counters['failures'] += 1
            had_failure = True
            events.append(f'EpisodeSearch retry lineage failure series={target_series_id} episodes=1 reason=invalid_predecessor')
            continue
        key = (is_retry, previous.pk if previous else None, candidate.attempt_count)
        groups.setdefault(key, []).append(candidate)
    size = min(100, batch_size)
    for (is_retry, _previous_id, accepted_attempts), compatible in groups.items():
        previous = compatible[0].current_command if is_retry else None
        attempt_number = accepted_attempts + 1
        for i in range(0, len(compatible), size):
            batch = compatible[i:i + size]
            ids = [c.target_episode_id for c in batch]
            with transaction.atomic():
                command = SonarrEpisodeSearchCommand.objects.create(target_instance=target_instance, target_series_id=target_series_id, status='submitting', submission_attempted_at=now, attempt_number=attempt_number, retry_of=previous)
                SonarrEpisodeSearchCommandCandidate.objects.bulk_create([SonarrEpisodeSearchCommandCandidate(command=command, candidate=c, target_episode_id=c.target_episode_id) for c in batch])
                for c in batch:
                    c.current_command = command; c.status = SEARCH_STATUS_SUBMITTED; c.last_confirmed_at = now
                SonarrEpisodeSearchCandidate.objects.bulk_update(batch, ['current_command','status','last_confirmed_at','updated_at'])
            response = target_api.trigger_episode_search(ids)  # deliberately exactly one POST
            try:
                # Older Sonarr/PR #8 responses may omit body/status; validate all supplied identity data.
                if not command_response_succeeded(response):
                    raise ValueError(_episode_search_failure_reason(response))
                if isinstance(response.get('body'), dict):
                    returned_ids = response['body'].get('episodeIds')
                    if returned_ids is not None and (not isinstance(returned_ids, list) or any(_valid_positive_int(value) is None for value in returned_ids) or len(returned_ids) != len(set(returned_ids)) or set(returned_ids) != set(ids)):
                        raise ValueError('mismatched_episode_ids')
                status = str(response.get('status') or 'queued').lower()
                if status not in KNOWN_COMMAND_STATUSES:
                    raise ValueError('unknown_command_status')
                result = str(response.get('result') or '').lower()
                if result not in KNOWN_COMMAND_RESULTS:
                    raise ValueError('unknown_command_result')
                queued = _strict_command_datetime(response.get('queued'), 'queued') if response.get('queued') else now
                command.sonarr_command_id = _command_id(response); command.status = status; command.sonarr_status = status
                command.sonarr_result = result; command.queued_at = queued; command.last_checked_at = now; command.save()
                _set_candidates_submitted(command, now)
                counters['submitted'] += len(batch)
                if attempt_number == 1: counters['initial_submitted'] += len(batch)
                else: counters['retry_submitted'] += len(batch)
                events.append(f"EpisodeSearch {'retry ' if attempt_number > 1 else ''}queued series={target_series_id} command_id={command.sonarr_command_id} episodes={len(batch)} attempt={attempt_number}")
            except (ValueError, TypeError) as exc:
                status_code = response.get('status_code') if isinstance(response, dict) else None
                definite_rejection = isinstance(status_code, int) and not isinstance(status_code, bool) and not 200 <= status_code < 300
                command.status = 'superseded' if definite_rejection else 'ambiguous'
                command.failure_reason = sanitize_text(exc)[:255]; command.save()
                if definite_rejection:
                    for candidate in batch:
                        candidate.status = SEARCH_STATUS_PENDING
                        candidate.current_command = previous
                        candidate.last_error = command.failure_reason
                        candidate.last_confirmed_at = now
                    SonarrEpisodeSearchCandidate.objects.bulk_update(batch, ['status','current_command','last_error','last_confirmed_at','updated_at'])
                counters['failures'] += 1
                outcome = 'failure' if definite_rejection else 'ambiguous'
                events.append(f'EpisodeSearch submission {outcome} series={target_series_id} episodes={len(ids)} reason={command.failure_reason}')
                return counters, events, True
    return counters, events, had_failure

import ast
import json
from datetime import datetime, timedelta
from django.utils import timezone
from django.db import transaction, models
from .connect import sanitize_text
from .models import (RadarrMovieSearchCandidate, RadarrMovieSearchCommand,
    RadarrMovieSearchCommandCandidate)
from .radarr_reconcile import _positive_int

SEARCH_STATUS_PENDING = RadarrMovieSearchCandidate.STATUS_PENDING
SEARCH_STATUS_SUBMITTED = RadarrMovieSearchCandidate.STATUS_SUBMITTED
SEARCH_STATUS_CANCELLED = RadarrMovieSearchCandidate.STATUS_CANCELLED
SEARCH_STATUS_FAILED = RadarrMovieSearchCandidate.STATUS_FAILED


def _parse_radarr_datetime(value):
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


def _submission_time(fixed_now):
    return fixed_now or timezone.now()


def _reset_pending(cand, *, tmdb_id, now):
    cand.tmdb_id = tmdb_id
    cand.status = SEARCH_STATUS_PENDING
    cand.first_eligible_at = now
    cand.last_confirmed_at = now
    cand.submitted_at = None
    cand.cancelled_at = None
    cand.current_command = None
    cand.attempt_count = 0
    cand.retry_not_before = None
    cand.last_error = ''
    cand.save(update_fields=['tmdb_id','status','first_eligible_at','last_confirmed_at','submitted_at','cancelled_at','current_command','attempt_count','retry_not_before','last_error','updated_at'])


def _mark_submitted(cand, *, submitted_at, now):
    cand.status = SEARCH_STATUS_SUBMITTED
    cand.submitted_at = submitted_at
    cand.cancelled_at = None
    cand.retry_not_before = None
    cand.last_error = ''
    cand.last_confirmed_at = now
    cand.save(update_fields=['status','submitted_at','cancelled_at','retry_not_before','last_error','last_confirmed_at','updated_at'])


def command_response_succeeded(response, expected_movie_ids=None):
    if not isinstance(response, dict) or not response or response.get('error') or response.get('errorMessage'):
        return False
    code = response.get('status_code')
    if isinstance(code, bool) or not isinstance(code, int) or not 200 <= code < 300 or _valid_positive_int(response.get('id')) is None:
        return False
    body = response.get('body')
    if body is not None and not isinstance(body, dict): return False
    names = [name for name in (response.get('name'), body.get('name') if isinstance(body, dict) else None) if name is not None]
    if not names or any(name != 'MoviesSearch' for name in names): return False
    supplied = body.get('movieIds') if isinstance(body, dict) else response.get('movieIds')
    if supplied is None or not isinstance(supplied, list) or not supplied or any(_valid_positive_int(v) is None for v in supplied) or len(set(supplied)) != len(supplied): return False
    return expected_movie_ids is None or set(supplied) == set(expected_movie_ids)


def command_response_failed(response, expected_movie_ids=None): return not command_response_succeeded(response, expected_movie_ids)
def _command_id(response): return response.get('id') if isinstance(response, dict) else None

def _movie_search_failure_reason(response):
    if isinstance(response, dict):
        code=response.get('status_code')
        if isinstance(code,int) and not isinstance(code,bool): return f'http_{code}'
        if response.get('error') or response.get('errorMessage'): return 'api_error'
        return 'invalid_command_response'
    return sanitize_text(response)[:120] or 'request_error'


def update_movie_search_candidates(*, target_instance, target_movies, eligible_movie_ids,
        confirmed_monitored_ids, newly_monitored_ids=None,
        submission_blocked_movie_ids=None, now=None):
    now=now or timezone.now(); newly=set(newly_monitored_ids or [])
    eligible=set(eligible_movie_ids); confirmed=set(confirmed_monitored_ids)
    submission_blocked_movie_ids = submission_blocked_movie_ids if submission_blocked_movie_ids is not None else set()
    counters={k:0 for k in ('search_candidates_new','search_candidates_pending','search_candidates_submitted','search_candidates_cancelled','search_candidates_deferred','search_candidates_recovered','search_recovery_failures','search_failures')}; events=[]
    by_id={m['id']:m for m in target_movies}; existing={c.target_movie_id:c for c in RadarrMovieSearchCandidate.objects.filter(target_instance=target_instance)}; seen=set()
    for movie_id in sorted(eligible):
        seen.add(movie_id)
        if movie_id not in confirmed:
            submission_blocked_movie_ids.add(movie_id)
            if movie_id in existing:
                counters['search_candidates_deferred'] += 1
            continue
        movie=by_id.get(movie_id); last_search,error=_parse_radarr_datetime(movie.get('lastSearchTime'))
        if error:
            submission_blocked_movie_ids.add(movie_id)
            cand = existing.get(movie_id)
            if cand is not None:
                cand.last_confirmed_at = now
                cand.save(update_fields=['last_confirmed_at', 'updated_at'])
            counters['search_candidates_deferred'] += 1
            counters['search_failures'] += 1
            events.append(f'MoviesSearch candidate deferred movie={movie_id} reason={sanitize_text(error)}')
            continue
        cand=existing.get(movie_id); tmdb_id=movie['tmdbId']
        if cand is None:
            if last_search is not None and movie_id not in newly: continue
            RadarrMovieSearchCandidate.objects.create(target_instance=target_instance,target_movie_id=movie_id,tmdb_id=tmdb_id,first_eligible_at=now,last_confirmed_at=now)
            counters['search_candidates_new']+=1; continue
        has_search_lineage = cand.attempt_count > 0 or cand.current_command_id is not None
        identity_can_reset = cand.status in (SEARCH_STATUS_PENDING, SEARCH_STATUS_CANCELLED) and not has_search_lineage
        new_cycle_can_reset = movie_id in newly and not has_search_lineage
        if cand.tmdb_id != tmdb_id and has_search_lineage:
            submission_blocked_movie_ids.add(movie_id)
            counters['search_candidates_deferred'] += 1
            counters['search_failures'] += 1
            events.append(f'MoviesSearch candidate deferred movie={movie_id} reason=tmdb_identity_changed')
            continue
        if (cand.tmdb_id != tmdb_id and identity_can_reset) or new_cycle_can_reset:
            _reset_pending(cand,tmdb_id=tmdb_id,now=now); counters['search_candidates_pending']+=1; continue
        if cand.status == SEARCH_STATUS_PENDING:
            if last_search is not None and last_search >= _aware(cand.first_eligible_at):
                _mark_submitted(cand,submitted_at=last_search,now=now); counters['search_candidates_submitted']+=1
            else:
                cand.last_confirmed_at=now; cand.save(update_fields=['last_confirmed_at','updated_at']); counters['search_candidates_pending']+=1
        elif cand.status == SEARCH_STATUS_CANCELLED:
            if last_search is not None and cand.cancelled_at and last_search > _aware(cand.cancelled_at):
                _mark_submitted(cand,submitted_at=last_search,now=now); counters['search_candidates_submitted']+=1
            else:
                _reset_pending(cand,tmdb_id=tmdb_id,now=now); counters['search_candidates_pending']+=1
        else:
            cand.last_confirmed_at=now; cand.save(update_fields=['last_confirmed_at','updated_at'])
    for movie_id,cand in existing.items():
        if movie_id in seen or cand.status != SEARCH_STATUS_PENDING: continue
        cand.status=SEARCH_STATUS_CANCELLED; cand.cancelled_at=now; cand.last_confirmed_at=now; cand.retry_not_before=None; cand.last_error=''; cand.save(update_fields=['status','cancelled_at','last_confirmed_at','retry_not_before','last_error','updated_at']); counters['search_candidates_cancelled']+=1
    return counters,events,bool(counters['search_failures'])


KNOWN_COMMAND_STATUSES = frozenset(('queued', 'started', 'completed', 'failed', 'aborted', 'cancelled', 'orphaned'))
KNOWN_COMMAND_RESULTS = frozenset(('', 'unknown', 'successful', 'unsuccessful', 'indeterminate'))
TERMINAL_FAILURE_STATUSES = frozenset(('failed', 'aborted', 'cancelled', 'orphaned'))
ADOPTION_WINDOW = timedelta(minutes=10)
COMMAND_FALLBACK_LIMIT = 10
FALLBACK_DEFERRED = ('fallback_budget_deferred', None)


def _lifecycle_status(status, result):
    if status == 'completed' and result == 'unsuccessful':
        return 'failed', 'completed_unsuccessful'
    if status == 'completed' and result == 'indeterminate':
        return 'ambiguous', 'completed_indeterminate'
    return status, ''


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


def validate_movie_search_command(resource, expected_movie_ids=None, expected_id=None):
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
    if identity != 'MoviesSearch' or (resource.get('name') not in (None, 'MoviesSearch')) or body.get('name') not in (None, 'MoviesSearch'):
        raise ValueError('wrong_command_type')
    movie_ids = body.get('movieIds')
    if not isinstance(movie_ids, list) or not movie_ids:
        raise ValueError('invalid_movie_ids')
    if any(_valid_positive_int(value) is None for value in movie_ids) or len(set(movie_ids)) != len(movie_ids):
        raise ValueError('invalid_movie_ids')
    if expected_movie_ids is not None and set(movie_ids) != set(expected_movie_ids):
        raise ValueError('mismatched_movie_ids')
    status = resource.get('status')
    if not isinstance(status, str) or status.lower() not in KNOWN_COMMAND_STATUSES:
        raise ValueError('unknown_command_status')
    status = status.lower()
    result = resource.get('result', '')
    if result is None:
        result = ''
    if not isinstance(result, str) or result.lower() not in KNOWN_COMMAND_RESULTS:
        raise ValueError('unknown_command_result')
    parsed = {'id': command_id, 'status': status, 'result': result.lower(), 'movie_ids': tuple(movie_ids)}
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
            if identity != 'MoviesSearch':
                continue
        parsed = validate_movie_search_command(item)
        if parsed['id'] in mapped:
            raise ValueError('duplicate_command_id')
        mapped[parsed['id']] = (item, parsed)
    return mapped


def poll_movie_search_commands(target_api, target_instance, fallback_limit=COMMAND_FALLBACK_LIMIT):
    """Poll once, then use a deterministic bounded fallback for absent tracked IDs."""
    try:
        mapped = validate_command_list(target_api.get_commands())
    except (TypeError, ValueError):
        return {}, True, 0
    active = list(RadarrMovieSearchCommand.objects.filter(
        target_instance=target_instance,
    ).exclude(status__in=('completed', 'superseded')).exclude(
        status__in=TERMINAL_FAILURE_STATUSES, outcome_reconciled_at__isnull=False,
    ).filter(radarr_command_id__isnull=False).prefetch_related('candidate_links').order_by('last_fallback_checked_at', 'radarr_command_id'))
    fallbacks = 0
    absent = [command for command in active if command.radarr_command_id not in mapped]
    never_checked = [command for command in absent if command.last_fallback_checked_at is None]
    selected = (never_checked if never_checked else absent)[:fallback_limit]
    selected_ids = {command.id for command in selected}
    checked_at = timezone.now()
    for command in selected:
        if command.radarr_command_id in mapped:
            continue
        fallbacks += 1
        resource = target_api.get_command(command.radarr_command_id)
        command.last_fallback_checked_at = checked_at
        command.save(update_fields=['last_fallback_checked_at', 'updated_at'])
        status_code = resource.get('status_code') if isinstance(resource, dict) else None
        if status_code == 404:
            continue
        try:
            parsed = validate_movie_search_command(resource, _command_snapshot(command), command.radarr_command_id)
        except ValueError:
            return mapped, True, fallbacks
        mapped[command.radarr_command_id] = (resource, parsed)
    for command in absent:
        if command.id in selected_ids:
            continue
        mapped[command.radarr_command_id] = FALLBACK_DEFERRED
    return mapped, False, fallbacks


def _command_snapshot(command):
    return list(command.candidate_links.order_by('id').values_list('target_movie_id', flat=True))


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
    RadarrMovieSearchCandidate.objects.bulk_update(candidates, ['status', 'current_command', 'submitted_at', 'cancelled_at', 'retry_not_before', 'last_error', 'last_confirmed_at', 'attempt_count', 'updated_at'])


def _apply_validated_command(command, parsed, now, retry_delay_minutes, max_retries, eligible_movie_ids, movie_by_id):
    old_status = command.status
    status, mapped_reason = _lifecycle_status(parsed['status'], parsed['result'])
    command.radarr_status = parsed['status']
    command.radarr_result = parsed['result']
    command.queued_at = parsed['queued_at'] or command.queued_at
    command.started_at = parsed['started_at'] or command.started_at
    command.ended_at = parsed['ended_at'] or command.ended_at
    command.last_checked_at = now
    command.unavailable_since = None
    command.failure_reason = mapped_reason
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
            episode = movie_by_id.get(candidate.target_movie_id, {})
            if episode.get('hasFile') is True:
                candidate.status = SEARCH_STATUS_SUBMITTED
                candidate.retry_not_before = None
                candidate.last_error = ''
                counters['search_candidates_satisfied_by_file'] += 1
            else:
                last_search, error = _parse_radarr_datetime(episode.get('lastSearchTime'))
                if not error and last_search is not None and last_search >= threshold:
                    candidate.status = SEARCH_STATUS_SUBMITTED
                    candidate.retry_not_before = None
                    candidate.last_error = ''
                    counters['search_candidates_satisfied_by_last_search'] += 1
                elif candidate.target_movie_id not in eligible_movie_ids:
                    candidate.status = SEARCH_STATUS_CANCELLED
                    candidate.cancelled_at = now
                    candidate.retry_not_before = None
                    candidate.last_error = ''
                elif candidate.attempt_count - 1 >= max_retries:
                    candidate.status = SEARCH_STATUS_FAILED
                    candidate.retry_not_before = None
                    candidate.last_error = f'MoviesSearch {status}; automatic retry limit exhausted'
                    counters['search_candidates_retry_exhausted'] += 1
                else:
                    candidate.status = SEARCH_STATUS_PENDING
                    if candidate.retry_not_before is None:
                        candidate.retry_not_before = now + timedelta(minutes=retry_delay_minutes)
                    candidate.last_error = f'MoviesSearch {status}; retry scheduled'
                    counters['search_candidates_requeued'] += 1
            candidate.last_confirmed_at = now
        RadarrMovieSearchCandidate.objects.bulk_update(candidates, ['status','cancelled_at','retry_not_before','last_error','last_confirmed_at','updated_at'])
        command.outcome_reconciled_at = now
        command.save(update_fields=['outcome_reconciled_at', 'updated_at'])
    changed = old_status != status
    return counters, changed, status


def reconcile_movie_search_commands(*, target_instance, command_map, poll_failed,
        target_movies, eligible_movie_ids, max_retries=3, retry_delay_minutes=30,
        missing_grace_hours=24, now=None):
    now = now or timezone.now()
    counters = {key: 0 for key in ('search_commands_polled','search_commands_queued','search_commands_started','search_commands_completed','search_commands_failed','search_commands_aborted','search_commands_cancelled','search_commands_orphaned','search_commands_ambiguous','search_commands_unavailable','search_command_poll_failures','search_candidates_requeued','search_candidates_retry_exhausted','search_candidates_satisfied_by_file','search_candidates_satisfied_by_last_search')}
    events, unsafe = [], False
    movie_by_id = {ep.get('id'): ep for ep in target_movies if isinstance(ep, dict) and _valid_positive_int(ep.get('id'))}
    commands = list(RadarrMovieSearchCommand.objects.filter(
        target_instance=target_instance, ).exclude(status__in=('completed','superseded')).exclude(
        status__in=TERMINAL_FAILURE_STATUSES, outcome_reconciled_at__isnull=False,
    ).prefetch_related('candidate_links','candidates'))
    if not commands:
        return counters, events, False
    if poll_failed:
        counters['search_command_poll_failures'] = 1
        return counters, ['MoviesSearch command polling failure'], True
    tracked_ids = set(RadarrMovieSearchCommand.objects.filter(target_instance=target_instance, radarr_command_id__isnull=False).values_list('radarr_command_id', flat=True))
    for command in commands:
        snapshot = _command_snapshot(command)
        entry = command_map.get(command.radarr_command_id) if command.radarr_command_id else None
        # Adopt only an exact, unique, time-consistent unclaimed command.
        if command.radarr_command_id is None and command.status in ('submitting','ambiguous'):
            matches = []
            for cid, (_raw, parsed) in command_map.items():
                if parsed is None:
                    continue
                queued = parsed['queued_at']
                if cid not in tracked_ids and set(parsed['movie_ids']) == set(snapshot) and queued and abs(queued - command.submission_attempted_at) <= ADOPTION_WINDOW:
                    matches.append((cid, parsed))
            if len(matches) == 1:
                command.radarr_command_id = matches[0][0]
                command.save(update_fields=['radarr_command_id','updated_at'])
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
                parsed = validate_movie_search_command(entry[0], snapshot, command.radarr_command_id)
            except ValueError:
                command.status = 'ambiguous'; command.failure_reason = 'command_validation_failed'; command.last_checked_at = now; command.save()
                counters['search_commands_ambiguous'] += 1; unsafe = True
                continue
            counters['search_commands_polled'] += 1
            changes, transitioned, final_status = _apply_validated_command(command, parsed, now, retry_delay_minutes, max_retries, set(eligible_movie_ids), movie_by_id)
            for key, value in changes.items(): counters[key] += value
            counters['search_commands_' + final_status] += 1
            if final_status in ('ambiguous', 'unavailable'):
                unsafe = True
            if transitioned:
                events.append(f'MoviesSearch {final_status} command_id={command.radarr_command_id} movies={len(snapshot)}')
            continue
        # Absence is evidence of nothing. Resolve only if every candidate has execution evidence.
        threshold = command.queued_at or command.submission_attempted_at
        evidence = True
        evidence_files = evidence_searches = 0
        for candidate in command.candidates.all():
            episode = movie_by_id.get(candidate.target_movie_id, {})
            last_search, error = _parse_radarr_datetime(episode.get('lastSearchTime'))
            if episode.get('hasFile') is True:
                evidence_files += 1
            elif not error and last_search is not None and last_search >= threshold:
                evidence_searches += 1
            else:
                evidence = False; break
        if evidence and snapshot:
            command.status = 'completed'; command.terminal_at = now; command.ended_at = now; command.last_checked_at = now; command.failure_reason = 'completed_by_movie_evidence'; command.save()
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
                events.append(f'MoviesSearch command unavailable command_id={command.radarr_command_id}')
    return counters, events, unsafe


def submit_pending_search_candidates(*, target_api, target_instance, batch_size=100,
        submission_blocked_movie_ids=None, now=None):
    fixed_now = now
    evaluation_now = _submission_time(fixed_now)
    counters = {'submitted': 0, 'initial_submitted': 0, 'retry_submitted': 0, 'failures': 0}
    events = []
    had_failure = False
    blocked = set(submission_blocked_movie_ids or [])
    due = RadarrMovieSearchCandidate.objects.filter(
        target_instance=target_instance, status=SEARCH_STATUS_PENDING,
    ).filter(models.Q(retry_not_before__isnull=True) | models.Q(retry_not_before__lte=evaluation_now))
    if blocked:
        due = due.exclude(target_movie_id__in=blocked)
    due = due.select_related('current_command').order_by('target_movie_id')
    groups = {}
    for candidate in due:
        previous = candidate.current_command
        is_retry = candidate.attempt_count > 0
        # A retry without an exact, processed terminal predecessor is unsafe.
        if is_retry and (previous is None or previous.status not in TERMINAL_FAILURE_STATUSES or previous.outcome_reconciled_at is None):
            counters['failures'] += 1
            had_failure = True
            events.append(f'MoviesSearch retry lineage failure movies=1 reason=invalid_predecessor')
            continue
        key = (is_retry, previous.pk if previous else None, candidate.attempt_count)
        groups.setdefault(key, []).append(candidate)
    size = min(100, batch_size)
    for (is_retry, _previous_id, accepted_attempts), compatible in groups.items():
        previous = compatible[0].current_command if is_retry else None
        attempt_number = accepted_attempts + 1
        for i in range(0, len(compatible), size):
            batch_now = _submission_time(fixed_now)
            batch = compatible[i:i + size]
            ids = [c.target_movie_id for c in batch]
            with transaction.atomic():
                command = RadarrMovieSearchCommand.objects.create(target_instance=target_instance, status='submitting', submission_attempted_at=batch_now, attempt_number=attempt_number, retry_of=previous)
                RadarrMovieSearchCommandCandidate.objects.bulk_create([RadarrMovieSearchCommandCandidate(command=command, candidate=c, target_movie_id=c.target_movie_id) for c in batch])
                for c in batch:
                    c.current_command = command; c.status = SEARCH_STATUS_SUBMITTED; c.last_confirmed_at = batch_now
                RadarrMovieSearchCandidate.objects.bulk_update(batch, ['current_command','status','last_confirmed_at','updated_at'])
            response = target_api.trigger_movies_search(ids)  # deliberately exactly one POST
            try:
                # Older Sonarr/PR #8 responses may omit body/status; validate all supplied identity data.
                if not command_response_succeeded(response, ids):
                    raise ValueError(_movie_search_failure_reason(response))
                if isinstance(response.get('body'), dict):
                    returned_ids = response['body'].get('movieIds')
                    if returned_ids is not None and (not isinstance(returned_ids, list) or any(_valid_positive_int(value) is None for value in returned_ids) or len(returned_ids) != len(set(returned_ids)) or set(returned_ids) != set(ids)):
                        raise ValueError('mismatched_movie_ids')
                resource = dict(response)
                resource.setdefault('status', 'queued')
                resource.setdefault('result', '')
                parsed = validate_movie_search_command(resource, ids, response['id'])
                status, mapped_reason = _lifecycle_status(parsed['status'], parsed['result'])
                command.radarr_command_id = parsed['id']; command.status = status; command.radarr_status = parsed['status']
                command.radarr_result = parsed['result']; command.queued_at = parsed['queued_at'] or batch_now
                command.started_at = parsed['started_at']; command.ended_at = parsed['ended_at']; command.last_checked_at = batch_now
                command.failure_reason = mapped_reason
                if status == 'completed' or status in TERMINAL_FAILURE_STATUSES or status == 'ambiguous':
                    command.terminal_at = parsed['ended_at'] or batch_now
                command.save()
                _set_candidates_submitted(command, batch_now)
                counters['submitted'] += len(batch)
                if attempt_number == 1: counters['initial_submitted'] += len(batch)
                else: counters['retry_submitted'] += len(batch)
                events.append(f"MoviesSearch {'retry ' if attempt_number > 1 else ''}{status} command_id={command.radarr_command_id} movies={len(batch)} attempt={attempt_number}")
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
                        candidate.last_confirmed_at = batch_now
                    RadarrMovieSearchCandidate.objects.bulk_update(batch, ['status','current_command','last_error','last_confirmed_at','updated_at'])
                counters['failures'] += 1
                outcome = 'failure' if definite_rejection else 'ambiguous'
                events.append(f'MoviesSearch submission {outcome} movies={len(ids)} reason={command.failure_reason}')
                return counters, events, True
    return counters, events, had_failure

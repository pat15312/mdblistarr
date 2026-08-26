"""Persisted, read-only operational health for Sonarr and Radarr."""
import json
from datetime import timedelta

from django.db.models import Min
from django.utils import timezone

from .connect import sanitize_text
from .models import (
    ArrReconciliationStatus, Preferences, RadarrCleanupCandidate, RadarrInstance,
    RadarrMovieSearchCandidate, RadarrMovieSearchCommand, SonarrCleanupCandidate,
    SonarrEpisodeSearchCandidate, SonarrEpisodeSearchCommand, SonarrInstance,
)

PRODUCTS = ('sonarr', 'radarr')
ACTIVE_CANDIDATE_STATUSES = ('pending', 'submitted')
IN_FLIGHT_COMMAND_STATUSES = ('submitting', 'queued', 'started')
UNCERTAIN_COMMAND_STATUSES = ('ambiguous', 'unavailable')
TERMINAL_FAILURE_STATUSES = ('failed', 'aborted', 'cancelled', 'orphaned')
SEVERITY = {'error': 4, 'attention': 3, 'running': 2, 'healthy': 1, 'disabled': 0}
LATEST_ACTIVITY_FIELDS = {
    'sonarr': (
        ('series_compared', 'Series compared'),
        ('series_target_only', 'Target-only series'),
        ('episodes_inspected', 'Episodes inspected'),
        ('episodes_newly_monitored', 'Newly monitored'),
        ('episodes_newly_unmonitored', 'Newly unmonitored'),
        ('malformed_episodes', 'Malformed episodes'),
        ('season_update_failures', 'Season update failures'),
        ('series_update_failures', 'Series update failures'),
    ),
    'radarr': (
        ('movies_compared', 'Movies compared'),
        ('movies_target_only', 'Target-only movies'),
        ('movies_inspected', 'Movies inspected'),
        ('permanent_files_present', 'Permanent files'),
        ('target_files_present', 'Target files'),
        ('eligible_missing', 'Eligible missing'),
        ('unavailable', 'Unavailable'),
        ('movies_newly_monitored', 'Newly monitored'),
        ('movies_newly_unmonitored', 'Newly unmonitored'),
        ('monitor_update_failures', 'Monitor update failures'),
    ),
}


def _safe_id(value):
    try:
        value = int(value)
        return value if value > 0 else None
    except (TypeError, ValueError):
        return None


def _safe_int(value, default, minimum=0, maximum=10080):
    try:
        return max(minimum, min(maximum, int(value)))
    except (TypeError, ValueError):
        return default


def _safe_counters(counters):
    safe = {}
    for key, value in (counters or {}).items():
        if isinstance(key, str) and isinstance(value, (bool, int, float)) and not isinstance(value, complex):
            safe[key[:100]] = value
    json.dumps(safe)
    return safe


def _safe_message(message):
    # Status messages are deliberately generic, single-line, and never raw payloads.
    value = ' '.join(sanitize_text(str(message or '')).split())
    allowed = {
        'ok', 'partial_failure', 'exception', 'invalid_source_or_target',
        'source_target_same', 'source_validation_failed', 'target_validation_failed',
    }
    return value if value in allowed else 'reconciliation_failed'


def begin_reconciliation_status(product):
    """Record an actual run start. Call only after scheduling and lock checks."""
    if product not in PRODUCTS:
        raise ValueError('Unsupported Arr product')
    status, _ = ArrReconciliationStatus.objects.get_or_create(product=product)
    status.last_started_at = timezone.now()
    status.save(update_fields=['last_started_at', 'updated_at'])
    return status


def finish_reconciliation_status(product, result_code, message='', counters=None,
                                 source_instance_id=None, target_instance_id=None,
                                 source_ok=None, target_ok=None):
    """Record a terminal run using only already-known structured evidence."""
    status, _ = ArrReconciliationStatus.objects.get_or_create(product=product)
    now = timezone.now()
    code = int(result_code)
    outcome = ('success' if code == 200 else
               'partial_failure' if code == 207 else 'failure')
    status.last_completed_at = now
    status.last_result_code = code
    status.last_outcome = outcome
    status.last_message = _safe_message(message)
    status.last_counters = _safe_counters(counters)
    status.source_instance_id = _safe_id(source_instance_id)
    status.target_instance_id = _safe_id(target_instance_id)
    status.source_ok = source_ok
    status.target_ok = target_ok
    if outcome == 'success':
        status.last_success_at = now
    status.save()
    return status


def _configured_instance(model, instance_id):
    return model.objects.filter(pk=instance_id).values(
        'id', 'name', 'is_library_source', 'is_ondemand_target').first() if instance_id else None


def _search_metrics(candidate_model, command_model, target_id):
    empty = {key: 0 for key in ('pending', 'submitted', 'retry_exhausted', 'active_errors',
        'submitting', 'queued', 'started', 'in_flight', 'ambiguous', 'unavailable',
        'uncertain', 'unreconciled_terminal_failures', 'needs_attention')}
    empty['oldest_pending_at'] = None
    if not target_id:
        return empty
    candidates = candidate_model.objects.filter(target_instance_id=target_id)
    commands = command_model.objects.filter(target_instance_id=target_id)
    metrics = dict(empty)
    metrics.update({
        'pending': candidates.filter(status='pending').count(),
        'submitted': candidates.filter(status='submitted').count(),
        'retry_exhausted': candidates.filter(status='failed').count(),
        'active_errors': candidates.filter(status__in=ACTIVE_CANDIDATE_STATUSES).exclude(last_error='').count(),
        'oldest_pending_at': candidates.filter(status='pending').aggregate(value=Min('first_eligible_at'))['value'],
    })
    for state in IN_FLIGHT_COMMAND_STATUSES + UNCERTAIN_COMMAND_STATUSES:
        metrics[state] = commands.filter(status=state).count()
    metrics['in_flight'] = sum(metrics[state] for state in IN_FLIGHT_COMMAND_STATUSES)
    metrics['uncertain'] = sum(metrics[state] for state in UNCERTAIN_COMMAND_STATUSES)
    metrics['unreconciled_terminal_failures'] = commands.filter(
        status__in=TERMINAL_FAILURE_STATUSES, outcome_reconciled_at__isnull=True).count()
    # This is a deterministic count of actionable conditions, not historical work.
    # The categories are intentionally additive; it is not an entity-level count
    # across candidate/command relationships, which would require extra joins.
    metrics['needs_attention'] = sum(metrics[key] for key in (
        'retry_exhausted', 'active_errors', 'uncertain',
        'unreconciled_terminal_failures',
    ))
    return metrics


def _cleanup_metrics(model, target_id):
    metrics = {key: 0 for key in ('pending', 'ready', 'active_errors', 'deleted', 'cancelled', 'already_absent')}
    metrics.update({'oldest_pending_at': None, 'oldest_ready_at': None,
                    'ready_candidates': [], 'pending_candidates': []})
    if not target_id:
        return metrics
    candidates = model.objects.filter(target_instance_id=target_id)
    for state in ('pending', 'ready', 'deleted', 'cancelled', 'already_absent'):
        metrics[state] = candidates.filter(status=state).count()
    metrics['active_errors'] = candidates.filter(status__in=('pending', 'ready')).exclude(last_error='').count()
    metrics['oldest_pending_at'] = candidates.filter(status='pending').aggregate(value=Min('first_eligible_at'))['value']
    metrics['oldest_ready_at'] = candidates.filter(status='ready').aggregate(value=Min('ready_at'))['value']
    fields = ['id', 'status', 'target_title', 'target_year', 'first_eligible_at',
              'ready_at', 'last_error']
    is_sonarr = model is SonarrCleanupCandidate
    fields += (['tvdb_id', 'target_series_id', 'episode_file_id', 'linked_episode_keys']
               if is_sonarr else ['tmdb_id', 'target_movie_id', 'movie_file_id'])
    details = []
    for row in candidates.filter(status__in=('pending', 'ready')).values(*fields):
        external_id = row['tvdb_id' if is_sonarr else 'tmdb_id']
        file_id = row['episode_file_id' if is_sonarr else 'movie_file_id']
        title = row['target_title'].strip() if isinstance(row['target_title'], str) else ''
        display_title = title or f"{'TVDb' if is_sonarr else 'TMDb'} {external_id}"
        if title and row['target_year']:
            display_title += f" ({row['target_year']})"
        has_error = bool(row.pop('last_error', ''))
        item = {**row, 'display_title': display_title, 'external_id': external_id,
                'file_id': file_id, 'has_error': has_error}
        if is_sonarr:
            item['episode_labels'], item['episodes_display'] = _episode_labels(row.pop('linked_episode_keys', None))
        details.append(item)
    def sort_key(item, timestamp):
        value = item[timestamp]
        return (value is None, value or timezone.now(), item['id'])
    metrics['ready_candidates'] = sorted((x for x in details if x['status'] == 'ready'),
                                         key=lambda x: sort_key(x, 'ready_at'))
    metrics['pending_candidates'] = sorted((x for x in details if x['status'] == 'pending'),
                                           key=lambda x: sort_key(x, 'first_eligible_at'))
    return metrics


def _episode_labels(value):
    if not isinstance(value, (list, tuple)):
        return [], 'Unknown'
    keys = set()
    for item in value:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            return [], 'Unknown'
        season, episode = item
        if (not isinstance(season, int) or isinstance(season, bool) or season < 0 or
                not isinstance(episode, int) or isinstance(episode, bool) or episode < 0):
            return [], 'Unknown'
        keys.add((season, episode))
    if not keys:
        return [], 'Unknown'
    labels = [f'S{season:02d}E{episode:02d}' for season, episode in sorted(keys)]
    return labels, ', '.join(labels)


def reduce_overall_status(statuses):
    enabled = [value for value in statuses if value != 'disabled']
    if not enabled:
        return 'disabled'
    return max(enabled, key=lambda value: SEVERITY[value])


def _latest_activity(product, counters):
    """Return the small operator-facing monitoring summary, not raw counters."""
    return [
        {'key': key, 'label': label, 'value': counters[key]}
        for key, label in LATEST_ACTIVITY_FIELDS[product]
        if key in counters and (counters[key] or key in {
            'series_compared', 'episodes_inspected', 'movies_compared', 'movies_inspected'
        })
    ]


def _product_health(product, now):
    is_sonarr = product == 'sonarr'
    instance_model = SonarrInstance if is_sonarr else RadarrInstance
    candidate_model = SonarrEpisodeSearchCandidate if is_sonarr else RadarrMovieSearchCandidate
    command_model = SonarrEpisodeSearchCommand if is_sonarr else RadarrMovieSearchCommand
    cleanup_model = SonarrCleanupCandidate if is_sonarr else RadarrCleanupCandidate
    enabled = Preferences.get_value(f'{product}_reconciliation_enabled', '0') == '1'
    interval = _safe_int(Preferences.get_value(f'{product}_reconciliation_interval_minutes', '15'), 15, 1, 1440)
    source_id = _safe_id(Preferences.get_value(f'{product}_reconciliation_source_id'))
    target_id = _safe_id(Preferences.get_value(f'{product}_reconciliation_target_id'))
    source = _configured_instance(instance_model, source_id)
    target = _configured_instance(instance_model, target_id)
    issues = []
    config_errors = []
    if not source_id or not source:
        config_errors.append('Configured permanent/source instance is missing.')
    elif not source['is_library_source']:
        config_errors.append('Configured source is not a permanent/library source.')
    if not target_id or not target:
        config_errors.append('Configured On-Demand target instance is missing.')
    elif not target['is_ondemand_target']:
        config_errors.append('Configured target is not an On-Demand target.')
    if source_id and source_id == target_id:
        config_errors.append('Source and target must be different instances.')
    valid_target_id = target_id if target and target['is_ondemand_target'] and source_id != target_id else None
    search = _search_metrics(candidate_model, command_model, valid_target_id)
    cleanup = _cleanup_metrics(cleanup_model, valid_target_id)
    status = ArrReconciliationStatus.objects.filter(product=product).values().first()
    snapshot_matches_configuration = bool(status and
        status['source_instance_id'] == source_id and status['target_instance_id'] == target_id)
    threshold_minutes = max(120, interval * 4)
    threshold = timedelta(minutes=threshold_minutes)
    running = bool(status and status['last_started_at'] and (
        not status['last_completed_at'] or status['last_started_at'] > status['last_completed_at']))
    stale = running and now - status['last_started_at'] > threshold
    overdue = bool(enabled and snapshot_matches_configuration and status and status['last_completed_at'] and not running and
                   now - status['last_completed_at'] > threshold)
    counters = (status or {}).get('last_counters') or {}
    if enabled:
        issues.extend(config_errors)
        if status and not snapshot_matches_configuration and not config_errors:
            issues.append('Configuration changed since the last reconciliation; the current source/target pair has not yet been validated.')
        if stale and snapshot_matches_configuration:
            issues.append('Reconciliation appears incomplete/stale.')
        elif not status or not status['last_completed_at']:
            issues.append('No completed reconciliation has been recorded yet.')
        elif snapshot_matches_configuration and status['last_outcome'] == 'failure':
            issues.append('Latest reconciliation failed.')
        elif snapshot_matches_configuration and status['last_outcome'] == 'partial_failure':
            issues.append('Latest reconciliation partially failed.')
        if overdue:
            issues.append('Reconciliation is overdue.')
        if search['retry_exhausted']:
            issues.append(f"{search['retry_exhausted']} search candidates have exhausted retries.")
        if search['uncertain']:
            issues.append(f"{search['uncertain']} search commands are ambiguous or unavailable.")
        if search['unreconciled_terminal_failures']:
            issues.append(f"{search['unreconciled_terminal_failures']} terminal search failures await reconciliation.")
        if search['active_errors']:
            issues.append(f"{search['active_errors']} active search candidates contain an error.")
        if cleanup['active_errors']:
            issues.append(f"{cleanup['active_errors']} active cleanup candidates contain an error.")
        if snapshot_matches_configuration and counters.get('cleanup_failures', 0):
            issues.append('Latest cleanup activity contained failures.')
        if snapshot_matches_configuration and product == 'radarr' and counters.get('stop_deletes_for_run') is True:
            issues.append('Live cleanup stopped further deletions during the latest reconciliation because destructive verification became uncertain.')
    if not enabled:
        classification = 'disabled'
    elif config_errors or (stale and snapshot_matches_configuration) or (
            status and snapshot_matches_configuration and status['last_outcome'] == 'failure'):
        classification = 'error'
    elif status and not snapshot_matches_configuration:
        classification = 'attention'
    elif running:
        classification = 'running'
    elif issues:
        classification = 'attention'
    else:
        classification = 'healthy'
    cleanup_enabled = Preferences.get_value(f'{product}_cleanup_enabled', '0') == '1'
    cleanup_dry_run = Preferences.get_value(f'{product}_cleanup_dry_run', '1') != '0'
    return {
        'product': product, 'name': product.title(), 'classification': classification,
        'issues': issues, 'configuration_errors': config_errors,
        'configuration': {
            'enabled': enabled, 'source_name': source['name'] if source else 'Not configured',
            'target_name': target['name'] if target else 'Not configured', 'interval_minutes': interval,
            'search_enabled': Preferences.get_value(f'{product}_search_newly_eligible', '0') == '1',
            'cleanup_enabled': cleanup_enabled, 'cleanup_dry_run': cleanup_dry_run,
            'cleanup_mode': 'Disabled' if not cleanup_enabled else ('Dry run' if cleanup_dry_run else 'Live'),
            'cleanup_grace_hours': _safe_int(Preferences.get_value(f'{product}_cleanup_grace_hours', '24'), 24, 0, 168),
            'cleanup_max_deletions': _safe_int(Preferences.get_value(f'{product}_cleanup_max_deletions_per_run', '25'), 25, 1, 500),
        },
        'last_run': status, 'snapshot_matches_configuration': snapshot_matches_configuration,
        'snapshot_context': ('Current configuration' if snapshot_matches_configuration else
            'Previous configuration' if status else 'No snapshot'),
        'source_validation': (status['source_ok'] if snapshot_matches_configuration else None),
        'target_validation': (status['target_ok'] if snapshot_matches_configuration else None),
        'latest_counters': counters if snapshot_matches_configuration else {},
        'latest_activity': _latest_activity(product, counters) if snapshot_matches_configuration else [],
        'search': search, 'cleanup': cleanup,
        'running': running, 'stale': stale, 'overdue': overdue, 'threshold_minutes': threshold_minutes,
    }


def build_arr_health(now=None):
    """Build simple, template-safe health data without contacting external services."""
    now = now or timezone.now()
    products = [_product_health(product, now) for product in PRODUCTS]
    return {'overall': reduce_overall_status([item['classification'] for item in products]),
            'products': products, 'generated_at': now}

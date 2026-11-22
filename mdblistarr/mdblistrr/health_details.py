"""Bounded drill-downs using the same local query predicates as health counts."""
import json
from datetime import timezone

from django.core.paginator import Paginator
from django.db import connection
from django.db.models import CharField, F, Func, IntegerField, Value
from django.db.models.functions import TruncSecond
from django.http import Http404

from .arr_health import (_cleanup_groups, _title_cleanup_groups, _search_groups, _configured_instance, _safe_id,
    _episode_labels)
from .media_display import safe_title
from .models import (Preferences, SonarrInstance, RadarrInstance, SonarrCleanupCandidate,
    RadarrCleanupCandidate, SonarrEpisodeSearchCandidate, RadarrMovieSearchCandidate,
    SonarrEpisodeSearchCommand, RadarrMovieSearchCommand, TitleCleanupCandidate)

SEARCH_LABELS = {
    'pending': 'Pending', 'in_flight': 'In flight', 'needs_attention': 'Needs attention',
    'submitting': 'Submitting', 'queued': 'Queued', 'started': 'Started',
    'retry_exhausted': 'Retry exhausted', 'uncertain': 'Ambiguous/unavailable',
    'unreconciled_terminal_failures': 'Unreconciled failures', 'active_errors': 'Active candidate errors',
}
CLEANUP_LABELS = {
    'pending': 'Pending', 'ready': 'Ready', 'active_errors': 'Active errors',
    'deleted': 'Deleted', 'cancelled': 'Cancelled', 'already_absent': 'Already absent', 'in_flight': 'Unverified title removals',
}
ATTENTION_GROUPS = ('retry_exhausted', 'active_errors', 'uncertain', 'unreconciled_terminal_failures')
COMMAND_MEDIA_LIMIT = 100


def _title_sort_key(value):
    return safe_title(value).casefold() or None


def _episode_sort_part(value, part):
    """Earliest numeric pair, or unknown for malformed stored display metadata."""
    try:
        keys = json.loads(value)
    except (TypeError, ValueError):
        return None
    if not isinstance(keys, list):
        return None
    earliest = None
    for key in keys:
        if (not isinstance(key, list) or len(key) != 2 or
                any(type(number) is not int or not 0 <= number < 2**63 for number in key)):
            return None
        pair = tuple(key)
        if earliest is None or pair < earliest:
            earliest = pair
    return earliest[part] if earliest else None


def _cleanup_sort_columns(rows, metric, sonarr, kind):
    # SQLite scalar functions keep display normalization and Unicode case folding
    # identical to rendering without fetching the full history into Python.
    def episode(part):
        if sonarr and kind == 'cleanup':
            return Func('linked_episode_keys', Value(part),
                        function='health_episode_sort_part', output_field=IntegerField())
        return Value(None, output_field=IntegerField())

    return rows.annotate(
        # The template displays seconds. UTC also preserves chronology at DST folds.
        sort_time=TruncSecond('first_eligible_at' if metric == 'pending' else 'deleted_at',
                             tzinfo=timezone.utc),
        sort_title=Func('target_title', function='health_title_sort_key', output_field=CharField()),
        sort_season=episode(0), sort_episode=episode(1),
    )


def _media(candidate, sonarr, cleanup=False):
    external_id = candidate.tvdb_id if sonarr else candidate.tmdb_id
    title = safe_title(candidate.target_title) or f"{'TVDb' if sonarr else 'TMDb'} {external_id} (title unavailable)"
    identity = f"{'TVDb' if sonarr else 'TMDb'} {external_id}"
    if sonarr:
        episodes = (_episode_labels(candidate.linked_episode_keys)[1] if cleanup else
                    f'S{candidate.season_number:02d}E{candidate.episode_number:02d}')
        identity += f' · {episodes}'
    if cleanup:
        identity += f" · File {candidate.episode_file_id if sonarr else candidate.movie_file_id}"
    return {'title': title, 'identity': identity, 'external_id': external_id,
            'title_missing': not safe_title(candidate.target_title)}


def _timestamp(row, kind, category):
    if kind == 'command':
        field, label = {
            'submitting': ('submission_attempted_at', 'Submission attempted'),
            'queued': ('queued_at', 'Queued at'), 'started': ('started_at', 'Started at'),
            'unavailable': ('unavailable_since', 'Unavailable since'),
            'ambiguous': ('last_checked_at', 'Last checked'),
        }.get(row.status, ('terminal_at', 'Terminal state recorded'))
    elif category == 'pending':
        field, label = 'first_eligible_at', 'Eligible since'
    elif kind in ('cleanup', 'title') and category in ('ready', 'deleted', 'cancelled'):
        field, label = {'ready': ('ready_at', 'Ready since'), 'deleted': ('deleted_at', 'Deleted at'),
                        'cancelled': ('cancelled_at', 'Cancelled at')}[category]
    else:
        # No dedicated error/retry-exhausted/already-absent event timestamp exists.
        field, label = 'updated_at', 'Record last updated (not event time)'
    return {'timestamp': getattr(row, field), 'timestamp_label': label}


def build_health_details(product, section, metric, page_number=None):
    if product not in ('sonarr', 'radarr') or section not in ('search', 'cleanup'):
        raise Http404
    labels = SEARCH_LABELS if section == 'search' else CLEANUP_LABELS
    if metric not in labels:
        raise Http404
    sonarr = product == 'sonarr'
    instance_model = SonarrInstance if sonarr else RadarrInstance
    candidate_model = SonarrEpisodeSearchCandidate if sonarr else RadarrMovieSearchCandidate
    command_model = SonarrEpisodeSearchCommand if sonarr else RadarrMovieSearchCommand
    cleanup_model = SonarrCleanupCandidate if sonarr else RadarrCleanupCandidate
    source_id = _safe_id(Preferences.get_value(f'{product}_reconciliation_source_id'))
    target_id = _safe_id(Preferences.get_value(f'{product}_reconciliation_target_id'))
    target = _configured_instance(instance_model, target_id)
    if not target or not target['is_ondemand_target'] or source_id == target_id:
        target_id = None
    groups = (_search_groups(candidate_model, command_model, target_id) if section == 'search'
              else _cleanup_groups(cleanup_model, target_id))
    keys = ATTENTION_GROUPS if metric == 'needs_attention' else (metric,)
    sort_cleanup = section == 'cleanup' and metric in ('pending', 'deleted')
    fields = ['id', 'kind', 'category']
    ordering = ['category', 'id']
    if sort_cleanup:
        connection.ensure_connection()
        connection.connection.create_function('health_title_sort_key', 1, _title_sort_key, deterministic=True)
        connection.connection.create_function('health_episode_sort_part', 2, _episode_sort_part, deterministic=True)
        fields += ['sort_time', 'sort_title', 'sort_season', 'sort_episode']
        ordering = [F('sort_time').desc(nulls_last=True), F('sort_title').asc(nulls_last=True),
                    F('sort_season').asc(nulls_last=True), F('sort_episode').asc(nulls_last=True),
                    'kind', 'id']
    queries = []
    models = {}
    for key in keys:
        if key not in groups:
            continue
        rows = groups[key]
        kind = 'cleanup' if section == 'cleanup' else 'command' if rows.model is command_model else 'candidate'
        models[kind] = rows.model
        if sort_cleanup:
            rows = _cleanup_sort_columns(rows, metric, sonarr, kind)
        queries.append(rows.order_by().annotate(
            kind=Value(kind, output_field=CharField()), category=Value(key, output_field=CharField())
        ).values(*fields))
    if section == 'cleanup':
        title_groups = _title_cleanup_groups(product, target_id)
        models['title'] = TitleCleanupCandidate
        rows = title_groups[metric]
        if sort_cleanup:
            rows = _cleanup_sort_columns(rows, metric, sonarr, 'title')
        queries.append(rows.order_by().annotate(
            kind=Value('title', output_field=CharField()), category=Value(metric, output_field=CharField())
        ).values(*fields))
    references = queries[0].union(*queries[1:], all=True) if len(queries) > 1 else queries[0]
    page = Paginator(references.order_by(*ordering), 25).get_page(page_number)
    refs = list(page.object_list)
    records = {kind: model.objects.in_bulk([ref['id'] for ref in refs if ref['kind'] == kind])
               for kind, model in models.items()}
    entries = []
    for ref in refs:
        kind, category = ref['kind'], ref['category']
        row = records[kind].get(ref['id'])
        if row is None:
            continue  # A concurrent reconciliation may retire a record.
        if kind == 'command':
            # Associations explain the command count; one batch may contain many media items.
            candidates = row.candidates.filter(target_instance_id=target_id).order_by('id')
            media_count = candidates.count()
            media = [_media(candidate, sonarr) for candidate in candidates[:COMMAND_MEDIA_LIMIT]]
        elif kind == 'title':
            media_count = 1
            media = [{'title': safe_title(row.target_title) or f'{row.external_id} (title unavailable)',
                      'identity': f"{'TVDb' if sonarr else 'TMDb'} {row.external_id} · Entire {'television series' if sonarr else 'movie'}",
                      'external_id': row.external_id, 'title_missing': False}]
        else:
            media_count, media = 1, [_media(row, sonarr, kind == 'cleanup')]
        action, outcome = '', ''
        if section == 'cleanup':
            if kind == 'title':
                subject = 'television series' if sonarr else 'movie'
                action = f'Entire {subject} removed' if row.status == 'deleted' else f'Remove entire {subject}'
                verified = row.events.filter(outcome='original_file_removal_verified').count()
                outcome = f'{verified} associated file removal(s) verified. ' + row.last_error
                if row.status == 'cancelled' and verified:
                    outcome += ' Cleanup cancelled after partial removal; files are not restored.'
            else:
                subject = 'Episode file' if sonarr else 'Movie file'
                action = f'{subject} removed' if row.status == 'deleted' else f'Remove {subject.lower()}'
        entries.append({
            'id': row.pk, 'kind': kind, 'category': labels.get(category, category),
            'status': row.get_status_display(), 'media': media,
            'media_count': media_count, 'media_truncated': media_count > len(media),
            'reason': 'Blacklisted' if kind == 'title' else 'Permanent duplicate' if kind == 'cleanup' else '',
            'action': action, 'outcome': outcome,
            **_timestamp(row, kind, category),
        })
    page.object_list = entries
    missing_title_ids = sorted({item['external_id'] for entry in entries for item in entry['media']
                               if item['title_missing']})
    return {'title': f'{product.title()} - {section.title()} - {labels[metric]}',
            'page_obj': page, 'product': product, 'section': section, 'metric': metric,
            'target_id': target_id, 'missing_title_ids': missing_title_ids}

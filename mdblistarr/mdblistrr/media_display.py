"""Best-effort display metadata; never used to make lifecycle decisions."""
import logging
import time

from django.db import transaction
from django.db.models import Case, CharField, Value, When

from .connect import sanitize_text
from .models import (SonarrEpisodeSearchCandidate, RadarrMovieSearchCandidate,
                     SonarrCleanupCandidate, RadarrCleanupCandidate)

logger = logging.getLogger(__name__)
TITLE_LOOKUP_LIMIT = 25
TITLE_LOOKUP_SECONDS = 20


def safe_title(value):
    return ' '.join(sanitize_text(value).split())[:255] if isinstance(value, str) else ''


def recover_missing_titles(product, target_id, external_ids):
    """Explicit metadata-only action; never called by health rendering.

    Use Arr's catalogue lookup so removed library records can still be named.
    IDs come from the server-selected detail page, not submitted form fields.
    """
    from .arr import SonarrAPI, RadarrAPI

    result = dict(recovered=0, updated=0, unresolved=0, failed=0, deferred=0, next_after=0)
    ids = sorted({value for value in external_ids if type(value) is int and value > 0})
    if not ids or not target_id:
        return result
    sonarr = product == 'sonarr'
    if product not in ('sonarr', 'radarr'):
        raise ValueError('Unsupported product')
    key, field = ('tvdbId', 'tvdb_id') if sonarr else ('tmdbId', 'tmdb_id')
    models = ((SonarrCleanupCandidate, SonarrEpisodeSearchCandidate) if sonarr else
              (RadarrCleanupCandidate, RadarrMovieSearchCandidate))
    try:
        api = (SonarrAPI if sonarr else RadarrAPI)(instance_id=target_id)
    except Exception:
        result['failed'] = len(ids)
        return result
    deadline = time.monotonic() + TITLE_LOOKUP_SECONDS
    for index, external_id in enumerate(ids):
        if index >= TITLE_LOOKUP_LIMIT or time.monotonic() >= deadline:
            result['deferred'] = len(ids) - index
            result['next_after'] = ids[index - 1] if index else 0
            break
        try:
            payload = api.lookup_display_metadata(external_id)
            if isinstance(payload, dict) and payload.get('error'):
                result['failed'] += 1
                result['deferred'] = len(ids) - index - 1
                result['next_after'] = external_id if result['deferred'] else 0
                break  # Avoid repeatedly contacting an unavailable service.
            # Catalogue items may have local id=0: only the exact TVDb/TMDb
            # identity is authoritative for this display-only operation.
            item = payload[0] if isinstance(payload, list) and len(payload) == 1 else None
            title = safe_title(item.get('title')) if isinstance(item, dict) else ''
            if (not title or type(item.get(key)) is not int or item[key] != external_id or
                    any(item.get(name) for name in ('error', 'errorMessage', 'result'))):
                result['unresolved'] += 1
                continue
            updated = 0
            with transaction.atomic():
                for model in models:
                    updated += model.objects.filter(target_instance_id=target_id, target_title='',
                        **{field: external_id}).update(target_title=title)
            result['recovered'] += 1
            result['updated'] += updated
        except Exception:
            result['failed'] += 1
            result['deferred'] = len(ids) - index - 1
            result['next_after'] = external_id if result['deferred'] else 0
            break
    return result


def title_recovery_message(result):
    message = (f"Recovered {result['recovered']} title(s); updated {result['updated']} local record(s). "
               f"Unresolved: {result['unresolved']}. Failed: {result['failed']}. "
               f"Deferred: {result['deferred']}.")
    if result['unresolved']:
        message += ' Arr did not return a unique matching title for some IDs.'
    if result['failed']:
        message += ' Could not retrieve or save metadata. Check the configured Arr connection and database.'
    if result['deferred']:
        message += ' Refresh again to process remaining titles.'
    return message


def _title_index(media, external_key):
    """Index titles only when snapshot identities are unambiguous."""
    if not isinstance(media, list):
        raise ValueError('Invalid media snapshot')
    titles, ids, external_ids = {}, set(), set()
    for item in media:
        if not isinstance(item, dict):
            raise ValueError('Invalid media item')
        item_id, external_id = item.get('id'), item.get(external_key)
        if any(not isinstance(value, int) or isinstance(value, bool) or value <= 0
               for value in (item_id, external_id)):
            raise ValueError('Invalid media identity')
        if item_id in ids or external_id in external_ids:
            raise ValueError('Conflicting media identity')
        ids.add(item_id)
        external_ids.add(external_id)
        title = safe_title(item.get('title'))
        if title:
            titles[external_id] = title
    return titles


def backfill_candidate_titles(product, target, source_media, target_media):
    """Fill historical title gaps from already-read, validated Arr snapshots.

    Matching uses TVDb/TMDb identity, not a file ID or an instance-local media
    ID that may have been reused. Prefer the target's title; the source can
    supply it even after the target media record has been removed.
    """
    try:
        if product not in ('sonarr', 'radarr'):
            raise ValueError('Unsupported product')
        sonarr = product == 'sonarr'
        external_key = 'tvdbId' if sonarr else 'tmdbId'
        external_field = 'tvdb_id' if sonarr else 'tmdb_id'
        titles = _title_index(source_media, external_key)
        titles.update(_title_index(target_media, external_key))
        if not titles:
            return
        models = ((SonarrCleanupCandidate, SonarrEpisodeSearchCandidate) if sonarr else
                  (RadarrCleanupCandidate, RadarrMovieSearchCandidate))
        # Roll back metadata failures independently, including inside a caller's
        # transaction. These writes must not poison subsequent lifecycle work.
        with transaction.atomic():
            for model in models:
                missing = model.objects.filter(target_instance=target, target_title='')
                matches = sorted(external_id for external_id in
                    missing.values_list(external_field, flat=True).distinct()
                    if external_id in titles)
                for start in range(0, len(matches), 100):
                    batch = matches[start:start + 100]
                    # UPDATE deliberately bypasses auto_now and touches no
                    # lifecycle fields. Recheck blank titles at write time.
                    missing.filter(**{f'{external_field}__in': batch}).update(
                        target_title=Case(*[
                            When(**{external_field: external_id}, then=Value(titles[external_id]))
                            for external_id in batch
                        ], output_field=CharField()))
    except Exception:
        logger.warning('Candidate title backfill could not be completed for %s.', product)


def refresh_search_titles(product, target, media):
    """Use already-read target resources, preserving all lifecycle timestamps."""
    try:
        sonarr = product == 'sonarr'
        model = SonarrEpisodeSearchCandidate if sonarr else RadarrMovieSearchCandidate
        media_field = 'target_series_id' if sonarr else 'target_movie_id'
        external_field = 'tvdb_id' if sonarr else 'tmdb_id'
        external_key = 'tvdbId' if sonarr else 'tmdbId'
        by_id = {item['id']: item for item in media}
        changed = []
        for candidate in model.objects.filter(target_instance=target,
                **{f'{media_field}__in': by_id}).iterator():
            item = by_id[getattr(candidate, media_field)]
            if item.get(external_key) != getattr(candidate, external_field):
                continue
            title = safe_title(item.get('title'))
            if title and title != candidate.target_title:
                candidate.target_title = title
                changed.append(candidate)
        if changed:
            model.objects.bulk_update(changed, ['target_title'], batch_size=100)
    except Exception:
        logger.warning('Search display metadata could not be refreshed for %s.', product)

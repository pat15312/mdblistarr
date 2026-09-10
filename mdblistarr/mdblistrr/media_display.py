"""Best-effort display metadata; never used to make lifecycle decisions."""
import logging

from .connect import sanitize_text
from .models import SonarrEpisodeSearchCandidate, RadarrMovieSearchCandidate

logger = logging.getLogger(__name__)


def safe_title(value):
    return ' '.join(sanitize_text(value).split())[:255] if isinstance(value, str) else ''


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

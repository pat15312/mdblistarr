"""Pure, fail-closed Radarr movie monitoring decisions."""
from dataclasses import dataclass, field


@dataclass
class RadarrReconciliationResult:
    monitor_true_ids: list = field(default_factory=list)
    monitor_false_ids: list = field(default_factory=list)
    movies_inspected: int = 0
    movies_compared: int = 0
    movies_target_only: int = 0
    movies_newly_monitored: int = 0
    movies_newly_unmonitored: int = 0
    movies_unchanged: int = 0
    permanent_files_present: int = 0
    target_files_present: int = 0
    eligible_missing: int = 0
    unavailable: int = 0
    malformed_movies: int = 0
    monitor_update_failures: int = 0
    failures: int = 0


def _positive_int(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def validate_movie_response(movies, target=False, label='source'):
    """Validate an entire Radarr /movie response before it can drive writes."""
    if not isinstance(movies, list):
        return False, f'{label}_movies_not_list'
    ids, tmdb_ids = set(), set()
    for index, movie in enumerate(movies):
        prefix = f'{label}_movie_{index}'
        if not isinstance(movie, dict):
            return False, f'{prefix}_not_dict'
        if any(movie.get(key) for key in ('result', 'error', 'errorMessage')):
            return False, f'{prefix}_api_error'
        if 'status_code' in movie:
            status = movie['status_code']
            if isinstance(status, bool) or not isinstance(status, int) or not 200 <= status < 300:
                return False, f'{prefix}_bad_status'
        movie_id, tmdb_id = _positive_int(movie.get('id')), _positive_int(movie.get('tmdbId'))
        if movie_id is None:
            return False, f'{prefix}_invalid_id'
        if tmdb_id is None:
            return False, f'{prefix}_invalid_tmdb_id'
        if not isinstance(movie.get('hasFile'), bool):
            return False, f'{prefix}_invalid_has_file'
        if target and not isinstance(movie.get('monitored'), bool):
            return False, f'{prefix}_invalid_monitored'
        if target and not isinstance(movie.get('isAvailable'), bool):
            return False, f'{prefix}_invalid_is_available'
        if movie_id in ids:
            return False, f'{label}_duplicate_movie_id'
        if tmdb_id in tmdb_ids:
            return False, f'{label}_duplicate_tmdb_id'
        ids.add(movie_id); tmdb_ids.add(tmdb_id)
    return True, ''


def calculate_movie_monitoring(source_movies, target_movies):
    """Return monitoring changes using TMDB identity and Radarr availability only."""
    result = RadarrReconciliationResult()
    source_by_tmdb = {movie['tmdbId']: movie for movie in source_movies}
    for target in target_movies:
        result.movies_inspected += 1
        source = source_by_tmdb.get(target['tmdbId'])
        if source is None:
            result.movies_target_only += 1
        else:
            result.movies_compared += 1
        permanent_file = source is not None and source['hasFile'] is True
        target_file = target['hasFile'] is True
        if permanent_file:
            result.permanent_files_present += 1
        if target_file:
            result.target_files_present += 1
        desired = False
        if not permanent_file and not target_file:
            if target['isAvailable'] is True:
                desired = True
                result.eligible_missing += 1
            else:
                result.unavailable += 1
        if target['monitored'] is desired:
            result.movies_unchanged += 1
        elif desired:
            result.monitor_true_ids.append(target['id'])
        else:
            result.monitor_false_ids.append(target['id'])
    result.monitor_true_ids.sort()
    result.monitor_false_ids.sort()
    return result

"""Shared SQL ordering for bounded cleanup displays; never changes lifecycle order."""
import json
from datetime import timezone

from django.db import connection
from django.db.models import CharField, F, Func, IntegerField, Value
from django.db.models.functions import TruncSecond

from .media_display import safe_title

CLEANUP_DISPLAY_ORDER = (
    F('sort_time').desc(nulls_last=True), F('sort_title').asc(nulls_last=True),
    F('sort_season').asc(nulls_last=True), F('sort_episode').asc(nulls_last=True),
)


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


def cleanup_sort_columns(rows, timestamp, sonarr, kind):
    # SQLite scalar functions keep display normalization and Unicode case folding
    # identical to rendering without fetching the full history into Python.
    connection.ensure_connection()
    connection.connection.create_function('health_title_sort_key', 1, _title_sort_key, deterministic=True)
    connection.connection.create_function('health_episode_sort_part', 2, _episode_sort_part, deterministic=True)

    def episode(part):
        if sonarr and kind == 'cleanup':
            return Func('linked_episode_keys', Value(part),
                        function='health_episode_sort_part', output_field=IntegerField())
        return Value(None, output_field=IntegerField())

    return rows.annotate(
        # The template displays seconds. UTC also preserves chronology at DST folds.
        sort_time=TruncSecond(timestamp, tzinfo=timezone.utc),
        sort_title=Func('target_title', function='health_title_sort_key', output_field=CharField()),
        sort_season=episode(0), sort_episode=episode(1),
    )

from dataclasses import dataclass, field
from datetime import datetime, time as dt_time
from django.utils import timezone

AIR_DATE_AIRED = 'aired'
AIR_DATE_FUTURE = 'future'
AIR_DATE_MALFORMED = 'malformed'
AIR_DATE_UNSCHEDULED = 'unscheduled'


def _blank_air_date(value):
    return value is None or (isinstance(value, str) and value.strip() == '')


def _parse_air_datetime(ep):
    values = [ep.get('airDateUtc'), ep.get('airDate')]
    non_blank = [value for value in values if not _blank_air_date(value)]
    if not non_blank:
        return None, AIR_DATE_UNSCHEDULED
    value = non_blank[0]
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip().replace('Z', '+00:00')
        try:
            dt = datetime.fromisoformat(text)
        except (TypeError, ValueError):
            try:
                dt = datetime.combine(datetime.fromisoformat(text[:10]).date(), dt_time.min)
            except Exception:
                return None, AIR_DATE_MALFORMED
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt, timezone.get_current_timezone())
    return dt.astimezone(timezone.UTC), None


def air_date_status(ep, now=None):
    now = now or timezone.now()
    if timezone.is_naive(now):
        now = timezone.make_aware(now, timezone.get_current_timezone())
    air_dt, error = _parse_air_datetime(ep)
    if error:
        return error
    return AIR_DATE_AIRED if air_dt <= now.astimezone(timezone.UTC) else AIR_DATE_FUTURE


def _normal_int(value):
    try:
        if isinstance(value, bool):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def episode_key(ep):
    season = _normal_int(ep.get('seasonNumber'))
    number = _normal_int(ep.get('episodeNumber'))
    if season is None or number is None:
        return None
    return (season, number)


def is_relevant_episode(ep, include_specials=False, now=None):
    season = _normal_int(ep.get('seasonNumber'))
    number = _normal_int(ep.get('episodeNumber'))
    if season is None or number is None or season < 0 or number < 0:
        return False, 'malformed'
    if season == 0 and not include_specials:
        return False, 'special'
    date_state = air_date_status(ep, now=now)
    if date_state == AIR_DATE_MALFORMED:
        return False, 'malformed'
    if date_state == AIR_DATE_FUTURE:
        return False, 'future'
    if date_state == AIR_DATE_UNSCHEDULED:
        return False, 'unscheduled'
    return True, None


def determine_series_completeness(episodes, include_specials=False, now=None):
    if not isinstance(episodes, list):
        return {'complete': False, 'relevant': 0, 'missing': 0, 'no_relevant': True, 'malformed': True, 'specials_ignored': 0, 'future_ignored': 0, 'unscheduled_ignored': 0}
    relevant = missing = specials = future = malformed = unscheduled = 0
    for ep in episodes:
        if not isinstance(ep, dict):
            malformed += 1
            continue
        ok, reason = is_relevant_episode(ep, include_specials, now)
        if not ok:
            if reason == 'special':
                specials += 1
            elif reason == 'future':
                future += 1
            elif reason == 'unscheduled':
                unscheduled += 1
            else:
                malformed += 1
            continue
        relevant += 1
        if ep.get('hasFile') is not True:
            missing += 1
    return {'complete': relevant > 0 and missing == 0 and malformed == 0, 'relevant': relevant, 'missing': missing, 'no_relevant': relevant == 0, 'malformed': malformed > 0, 'specials_ignored': specials, 'future_ignored': future, 'unscheduled_ignored': unscheduled}


@dataclass
class ReconcileStats:
    series_compared: int = 0
    series_target_only: int = 0
    episodes_inspected: int = 0
    episodes_newly_monitored: int = 0
    episodes_newly_unmonitored: int = 0
    episodes_unchanged: int = 0
    searches_triggered: int = 0
    specials_ignored: int = 0
    future_episodes_ignored: int = 0
    unscheduled_episodes_ignored: int = 0
    failures: int = 0
    malformed_episodes: int = 0
    seasons_newly_monitored: int = 0
    seasons_newly_unmonitored: int = 0
    seasons_unchanged: int = 0
    season_update_failures: int = 0
    monitor_true_ids: list = field(default_factory=list)
    monitor_false_ids: list = field(default_factory=list)
    search_ids: list = field(default_factory=list)
    desired_season_monitoring: dict = field(default_factory=dict)


def calculate_episode_monitoring(source_episodes, target_episodes, include_specials=False, search_newly_eligible=False, now=None):
    stats = ReconcileStats()
    if not isinstance(source_episodes, list) or not isinstance(target_episodes, list):
        stats.failures = 1
        return stats
    permanent = {}
    for ep in source_episodes:
        if not isinstance(ep, dict):
            stats.failures = 1
            stats.malformed_episodes += 1
            return stats
        key = episode_key(ep)
        if key is None:
            stats.failures = 1
            stats.malformed_episodes += 1
            return stats
        permanent[key] = ep.get('hasFile') is True
    for ep in target_episodes:
        if not isinstance(ep, dict) or ep.get('id') is None or episode_key(ep) is None:
            stats.failures = 1
            stats.malformed_episodes += 1
            return stats
        stats.episodes_inspected += 1
        ok, reason = is_relevant_episode(ep, include_specials, now)
        if not ok:
            desired = False
            if reason == 'special':
                stats.specials_ignored += 1
            elif reason == 'future':
                stats.future_episodes_ignored += 1
            elif reason == 'unscheduled':
                stats.unscheduled_episodes_ignored += 1
            else:
                stats.failures = 1
                stats.malformed_episodes += 1
                return stats
        else:
            desired = not permanent.get(episode_key(ep), False)
        season = episode_key(ep)[0]
        stats.desired_season_monitoring.setdefault(season, False)
        if desired:
            stats.desired_season_monitoring[season] = True
        current = ep.get('monitored') is True
        if current == desired:
            stats.episodes_unchanged += 1
            continue
        if desired:
            stats.monitor_true_ids.append(ep['id'])
            if search_newly_eligible and ep.get('hasFile') is not True:
                stats.search_ids.append(ep['id'])
        else:
            stats.monitor_false_ids.append(ep['id'])
    return stats

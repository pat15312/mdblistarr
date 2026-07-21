from dataclasses import dataclass, field
from datetime import datetime, time as dt_time
from django.utils import timezone


def _parse_air_datetime(ep):
    value = ep.get('airDateUtc') or ep.get('airDate')
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).replace('Z', '+00:00')
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            try:
                dt = datetime.combine(datetime.fromisoformat(text[:10]).date(), dt_time.min)
            except Exception:
                return None
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt, timezone.get_current_timezone())
    return dt.astimezone(timezone.UTC)


def episode_key(ep):
    return (ep.get('seasonNumber'), ep.get('episodeNumber'))


def is_aired(ep, now=None):
    now = now or timezone.now()
    if timezone.is_naive(now):
        now = timezone.make_aware(now, timezone.get_current_timezone())
    air_dt = _parse_air_datetime(ep)
    return air_dt is not None and air_dt <= now.astimezone(timezone.UTC)


def is_relevant_episode(ep, include_specials=False, now=None):
    season = ep.get('seasonNumber')
    if season is None:
        return False, 'malformed'
    if int(season) == 0 and not include_specials:
        return False, 'special'
    if int(season) < 0:
        return False, 'malformed'
    if not is_aired(ep, now=now):
        return False, 'future'
    return True, None


def determine_series_completeness(episodes, include_specials=False, now=None):
    if not isinstance(episodes, list):
        return {'complete': False, 'relevant': 0, 'missing': 0, 'no_relevant': True, 'malformed': True, 'specials_ignored': 0, 'future_ignored': 0}
    relevant = missing = specials = future = malformed = 0
    for ep in episodes:
        if not isinstance(ep, dict):
            malformed += 1
            continue
        ok, reason = is_relevant_episode(ep, include_specials, now)
        if not ok:
            if reason == 'special': specials += 1
            elif reason == 'future': future += 1
            else: malformed += 1
            continue
        relevant += 1
        if ep.get('hasFile') is not True:
            missing += 1
    return {'complete': relevant > 0 and missing == 0, 'relevant': relevant, 'missing': missing, 'no_relevant': relevant == 0, 'malformed': malformed > 0, 'specials_ignored': specials, 'future_ignored': future}


@dataclass
class ReconcileStats:
    series_compared: int = 0
    series_unmatched: int = 0
    episodes_inspected: int = 0
    episodes_newly_monitored: int = 0
    episodes_newly_unmonitored: int = 0
    episodes_unchanged: int = 0
    searches_triggered: int = 0
    specials_ignored: int = 0
    future_episodes_ignored: int = 0
    failures: int = 0
    monitor_true_ids: list = field(default_factory=list)
    monitor_false_ids: list = field(default_factory=list)
    search_ids: list = field(default_factory=list)


def calculate_episode_monitoring(source_episodes, target_episodes, include_specials=False, search_newly_eligible=False, now=None):
    stats = ReconcileStats()
    if not isinstance(source_episodes, list) or not isinstance(target_episodes, list):
        stats.failures = 1
        return stats
    permanent = {}
    for ep in source_episodes:
        if not isinstance(ep, dict) or ep.get('seasonNumber') is None or ep.get('episodeNumber') is None:
            stats.failures = 1
            return stats
        permanent[episode_key(ep)] = ep.get('hasFile') is True
    for ep in target_episodes:
        if not isinstance(ep, dict) or ep.get('id') is None or ep.get('seasonNumber') is None or ep.get('episodeNumber') is None:
            stats.failures = 1
            return stats
        stats.episodes_inspected += 1
        ok, reason = is_relevant_episode(ep, include_specials, now)
        if not ok:
            desired = False
            if reason == 'special': stats.specials_ignored += 1
            elif reason == 'future': stats.future_episodes_ignored += 1
        else:
            desired = not permanent.get(episode_key(ep), False)
        current = ep.get('monitored') is True
        if current == desired:
            stats.episodes_unchanged += 1
            continue
        if desired:
            stats.episodes_newly_monitored += 1
            stats.monitor_true_ids.append(ep['id'])
            if search_newly_eligible and ep.get('hasFile') is not True:
                stats.search_ids.append(ep['id'])
        else:
            stats.episodes_newly_unmonitored += 1
            stats.monitor_false_ids.append(ep['id'])
    return stats

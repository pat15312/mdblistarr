"""Shared due-slot gating for Sonarr and Radarr reconciliation."""
import json
from datetime import datetime, timedelta

from django.utils import timezone

from .models import Preferences


SUPPORTED_INTERVALS = (5, 15, 30)


def scheduler_due_time(task):
    """Recover the heartbeat being run by django-scheduled-tasks' immediate backend."""
    from django_scheduled_tasks.base import scheduler
    from django_scheduled_tasks.models import ScheduledTaskRunLog

    schedule = next((item for item in scheduler.schedules if item.task is task), None)
    if schedule is None:
        return None
    run_log = ScheduledTaskRunLog.objects.filter(task_hash=schedule.to_sha_hex()).first()
    return run_log.next_scheduled_run_time if run_log else None


def normalise_interval(value, default=15):
    try:
        interval = int(value)
    except (TypeError, ValueError):
        interval = default
    return interval if interval in SUPPORTED_INTERVALS else default


def interval_slot(at, interval):
    if timezone.is_naive(at):
        at = timezone.make_aware(at, timezone.get_current_timezone())
    return at.replace(second=0, microsecond=0) - timedelta(minutes=at.minute % interval)


def _parse_datetime(value):
    parsed = datetime.fromisoformat(value)
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, timezone.get_current_timezone())
    return parsed


class ReconciliationSchedule:
    """Persist product slot state independently of best-effort health data."""

    def __init__(self, product, interval):
        self.interval = normalise_interval(interval)
        self.preference_name = f'{product}_reconciliation_schedule_state'

    def _load(self):
        raw = Preferences.get_value(self.preference_name, '')
        try:
            state = json.loads(raw) if raw else {}
            if state.get('interval') != self.interval:
                return {}
            for key in ('serviced', 'pending'):
                if state.get(key):
                    state[key] = _parse_datetime(state[key])
            return state
        except (TypeError, ValueError):
            return {}

    def _save(self, serviced=None, pending=None):
        value = {'interval': self.interval}
        if serviced:
            value['serviced'] = serviced.isoformat()
        if pending:
            value['pending'] = pending.isoformat()
        Preferences.set_value(self.preference_name, json.dumps(value, separators=(',', ':')))

    def due(self, scheduled_for):
        state = self._load()
        candidate = state.get('pending') or interval_slot(scheduled_for, self.interval)
        serviced = state.get('serviced')
        return candidate if serviced is None or candidate > serviced else None

    def defer(self, slot):
        state = self._load()
        serviced, pending = state.get('serviced'), state.get('pending')
        if serviced is None or slot > serviced:
            self._save(serviced, min(pending, slot) if pending else slot)

    def claim(self, scheduled_for):
        """Consume a due slot once the product lock has been obtained."""
        slot = self.due(scheduled_for)
        if slot is not None:
            self._save(serviced=slot)
        return slot

    def service_manually(self, at):
        """Suppress only the next boundary after an equivalent manual run."""
        slot = interval_slot(at, self.interval)
        if at > slot:
            slot += timedelta(minutes=self.interval)
        serviced = self._load().get('serviced')
        self._save(serviced=max(serviced, slot) if serviced else slot)

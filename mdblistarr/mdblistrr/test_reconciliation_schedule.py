"""Regression tests for delayed Arr reconciliation scheduler heartbeats."""
import fcntl
import os
import tempfile
from datetime import UTC, datetime
from unittest.mock import Mock, patch

os.environ.setdefault('MDBLISTARR_ENCRYPTION_KEY', 'MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=')

from django.test import TestCase
from django.utils import timezone

from .cron import reconcile_radarr_ondemand, reconcile_sonarr_ondemand
from .models import Preferences, RadarrInstance, SonarrInstance
from .reconciliation_schedule import ReconciliationSchedule, scheduler_due_time


def at(hour, minute, second=0):
    return datetime(2026, 8, 18, hour, minute, second, tzinfo=UTC)


class DueSlotTests(TestCase):
    def test_delayed_slot_is_serviced_once_and_next_slot_runs(self):
        schedule = ReconciliationSchedule('radarr', 15)
        self.assertEqual(schedule.claim(at(19, 45)), at(19, 45))
        self.assertIsNone(schedule.claim(at(19, 50)))
        self.assertEqual(schedule.claim(at(20, 0)), at(20, 0))

    def test_supported_intervals_deduplicate(self):
        for product, interval, minute in (
                ('five', 5, 45), ('fifteen', 15, 45), ('thirty', 30, 30)):
            schedule = ReconciliationSchedule(product, interval)
            self.assertEqual(schedule.claim(at(19, minute)), at(19, minute))
            self.assertIsNone(schedule.claim(at(19, minute + 4)))

    def test_pending_lock_slot_survives_restart_and_long_delay(self):
        schedule = ReconciliationSchedule('sonarr', 15)
        schedule.defer(at(19, 45))
        restarted = ReconciliationSchedule('sonarr', 15)
        self.assertEqual(restarted.claim(at(20, 0)), at(19, 45))
        self.assertEqual(restarted.claim(at(20, 5)), at(20, 0))
        self.assertIsNone(restarted.claim(at(20, 10)))

    def test_interval_changes_discard_incompatible_slot_state(self):
        ReconciliationSchedule('radarr', 15).claim(at(19, 45))
        self.assertEqual(ReconciliationSchedule('radarr', 30).claim(at(20, 0)), at(20, 0))
        self.assertEqual(ReconciliationSchedule('radarr', 15).claim(at(20, 15)), at(20, 15))

    def test_started_manual_attempt_consumes_only_next_slot(self):
        schedule = ReconciliationSchedule('radarr', 15)
        schedule.service_manually(at(19, 59, 59))
        self.assertIsNone(schedule.claim(at(19, 59, 59)))
        self.assertIsNone(schedule.claim(at(20, 0)))
        self.assertEqual(schedule.claim(at(20, 15)), at(20, 15))


class SchedulerDueTimeCompatibilityTests(TestCase):
    def _assert_hash_lookup(self, schedule, expected_hash):
        task = object()
        schedule.task = task
        run_log = Mock(next_scheduled_run_time=at(19, 45))
        with patch('django_scheduled_tasks.base.scheduler.schedules', {schedule}), patch(
                'django_scheduled_tasks.models.ScheduledTaskRunLog.objects.filter') as query:
            query.return_value.first.return_value = run_log
            self.assertEqual(scheduler_due_time(task), at(19, 45))
        query.assert_called_once_with(task_hash=expected_hash)

    def test_02_binary_schedule_hash(self):
        class Schedule02:
            def to_sha_bytes(self):
                return b'\x12\x34'

        self._assert_hash_lookup(Schedule02(), b'\x12\x34')

    def test_03_hex_schedule_hash(self):
        class Schedule03:
            def to_sha_bytes(self):
                raise AssertionError('0.3 lookup must use the database hex representation')

            def to_sha_hex(self):
                return '1234'

        self._assert_hash_lookup(Schedule03(), '1234')


class DelayedReconciliationIntegrationTests(TestCase):
    def setUp(self):
        self.paths = []

    def _path(self):
        descriptor, path = tempfile.mkstemp()
        os.close(descriptor)
        self.paths.append(path)
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        return path

    def _configure(self, product, model):
        source = model.objects.create(name='Source', url='http://source', apikey='key',
            is_library_source=True)
        target = model.objects.create(name='Target', url='http://target', apikey='key',
            is_ondemand_target=True)
        for suffix, value in (('enabled', '1'), ('interval_minutes', '15'),
                ('source_id', source.id), ('target_id', target.id)):
            Preferences.set_value(f'{product}_reconciliation_{suffix}', str(value))

    def test_radarr_1945_heartbeat_runs_at_194639(self):
        # Production showed Sonarr 19:45:00-19:46:39, then a SUCCESSFUL
        # Radarr wrapper which had only returned "Not scheduled interval".
        self._configure('radarr', RadarrInstance)
        source_api, target_api = Mock(), Mock()
        source_api.get_movies.return_value = []
        target_api.get_movies.return_value = []
        with patch('mdblistrr.cron.RADARR_RECONCILE_LOCK_PATH', self._path()), patch(
                'mdblistrr.cron.RadarrAPI', side_effect=[source_api, target_api]), patch(
                'mdblistrr.cron.timezone.now', return_value=at(19, 46, 39)):
            result = reconcile_radarr_ondemand(scheduled_for=at(19, 45))
        self.assertEqual(result['result'], 200)
        source_api.get_movies.assert_called_once_with()

    def test_sonarr_heartbeat_runs_after_multi_minute_delay(self):
        self._configure('sonarr', SonarrInstance)
        source_api, target_api = Mock(), Mock()
        source_api.get_series.return_value = []
        target_api.get_series.return_value = []
        with patch('mdblistrr.cron.RECONCILE_LOCK_PATH', self._path()), patch(
                'mdblistrr.cron.SonarrAPI', side_effect=[source_api, target_api]), patch(
                'mdblistrr.cron.timezone.now', return_value=at(19, 48)):
            result = reconcile_sonarr_ondemand(scheduled_for=at(19, 45))
        self.assertEqual(result['result'], 200)
        source_api.get_series.assert_called_once_with()

    def test_lock_contention_defers_slot_but_failure_consumes_it(self):
        self._configure('radarr', RadarrInstance)
        path = self._path()
        with open(path, 'a+') as held:
            fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch('mdblistrr.cron.RADARR_RECONCILE_LOCK_PATH', path):
                result = reconcile_radarr_ondemand(scheduled_for=at(19, 45))
        self.assertIn('running', result['message'].lower())
        self.assertEqual(ReconciliationSchedule('radarr', 15).due(at(19, 50)), at(19, 45))
        with patch('mdblistrr.cron.RADARR_RECONCILE_LOCK_PATH', path), patch(
                'mdblistrr.cron.RadarrAPI', side_effect=RuntimeError('ordinary failure')):
            with self.assertRaises(RuntimeError):
                reconcile_radarr_ondemand(scheduled_for=at(19, 50))
        self.assertIsNone(ReconciliationSchedule('radarr', 15).due(at(19, 50)))

    def test_started_manual_failure_consumes_next_scheduled_slot(self):
        self._configure('radarr', RadarrInstance)
        with patch('mdblistrr.cron.RADARR_RECONCILE_LOCK_PATH', self._path()), patch(
                'mdblistrr.cron.RadarrAPI', side_effect=RuntimeError('ordinary failure')), patch(
                'mdblistrr.cron.timezone.now', return_value=at(19, 59, 59)):
            with self.assertRaises(RuntimeError):
                reconcile_radarr_ondemand(force=True)
        self.assertIsNone(ReconciliationSchedule('radarr', 15).due(at(20, 0)))
        self.assertEqual(ReconciliationSchedule('radarr', 15).due(at(20, 15)), at(20, 15))

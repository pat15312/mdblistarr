"""Integration coverage for health writes through real reconciliation entry points."""
import fcntl
import json
import os
import tempfile
from unittest.mock import Mock, patch

os.environ.setdefault('MDBLISTARR_ENCRYPTION_KEY', 'MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=')

from django.test import TestCase
from django.utils import timezone

from .cron import reconcile_radarr_ondemand, reconcile_sonarr_ondemand
from .models import ArrReconciliationStatus, Preferences, RadarrInstance, SonarrInstance


STATUS_FIELDS = (
    'last_started_at', 'last_completed_at', 'last_success_at', 'last_outcome',
    'last_result_code', 'last_counters', 'source_instance_id',
    'target_instance_id', 'source_ok', 'target_ok',
)


def _status_values(product):
    row = ArrReconciliationStatus.objects.get(product=product)
    return {field: getattr(row, field) for field in STATUS_FIELDS}


class ReconciliationHealthIntegrationMixin:
    product = None
    instance_model = None
    lock_setting = None
    api_setting = None
    reconcile = None

    def setUp(self):
        self.source = self.instance_model.objects.create(
            name='Source', url='http://source/private', apikey='source-secret',
            is_library_source=True)
        self.target = self.instance_model.objects.create(
            name='Target', url='http://target/private', apikey='target-secret',
            is_library_source=False, is_ondemand_target=True)
        for suffix, value in (
            ('enabled', '1'), ('source_id', self.source.id),
            ('target_id', self.target.id), ('interval_minutes', '15'),
        ):
            Preferences.set_value(f'{self.product}_reconciliation_{suffix}', str(value))
        fd, self.lock_path = tempfile.mkstemp()
        os.close(fd)
        self.addCleanup(lambda: os.path.exists(self.lock_path) and os.unlink(self.lock_path))

    def healthy_snapshot(self):
        from .arr_health import finish_reconciliation_status
        return finish_reconciliation_status(
            self.product, 200, 'ok', {'preserved': 7}, self.source.id,
            self.target.id, source_ok=True, target_ok=True)

    def invoke(self, source_api, target_api, force=True):
        with patch(self.lock_setting, self.lock_path), patch(
                self.api_setting, side_effect=[source_api, target_api]):
            return self.reconcile(force=force)

    def test_disabled_not_scheduled_and_lock_held_preserve_healthy_snapshot(self):
        self.healthy_snapshot()
        before = _status_values(self.product)
        with patch('mdblistrr.cron.begin_reconciliation_status') as begin, patch(
                'mdblistrr.cron.finish_reconciliation_status') as finish:
            Preferences.set_value(f'{self.product}_reconciliation_enabled', '0')
            with patch(self.api_setting) as factory:
                self.reconcile(force=True)
                factory.assert_not_called()
            self.assertEqual(_status_values(self.product), before)

            Preferences.set_value(f'{self.product}_reconciliation_enabled', '1')
            Preferences.set_value(f'{self.product}_reconciliation_interval_minutes', '30')
            outside_interval = timezone.now().replace(minute=1)
            with patch('mdblistrr.cron.timezone.now', return_value=outside_interval), patch(self.api_setting) as factory:
                self.reconcile(force=False)
                factory.assert_not_called()
            self.assertEqual(_status_values(self.product), before)

            with open(self.lock_path, 'a+') as held:
                fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                with patch(self.lock_setting, self.lock_path), patch(self.api_setting) as factory:
                    result = self.reconcile(force=True)
                    factory.assert_not_called()
                self.assertIn('running', result['message'].lower())
            begin.assert_not_called()
            finish.assert_not_called()
        self.assertEqual(_status_values(self.product), before)

    def assert_success_status(self, result):
        row = ArrReconciliationStatus.objects.get(product=self.product)
        self.assertEqual(row.last_result_code, 200)
        self.assertEqual(row.last_outcome, 'success')
        self.assertLessEqual(row.last_started_at, row.last_completed_at)
        self.assertEqual(row.last_success_at, row.last_completed_at)
        self.assertEqual(row.source_instance_id, self.source.id)
        self.assertEqual(row.target_instance_id, self.target.id)
        self.assertIs(row.source_ok, True)
        self.assertIs(row.target_ok, True)
        self.assertTrue(row.last_counters)
        self.assertEqual(json.loads(json.dumps(result)), result)

    def assert_partial_preserves_success(self, result, previous_success):
        row = ArrReconciliationStatus.objects.get(product=self.product)
        self.assertEqual(result['result'], 207)
        self.assertEqual(row.last_outcome, 'partial_failure')
        self.assertEqual(row.last_result_code, 207)
        self.assertEqual(row.last_success_at, previous_success)
        self.assertGreaterEqual(row.last_completed_at, row.last_started_at)
        self.assertTrue(row.last_counters)

    def assert_validation_failure(self, source_response, target_response, source_ok, target_ok):
        previous_success = self.healthy_snapshot().last_success_at
        source_api, target_api = self.apis(source_response, target_response)
        result = self.invoke(source_api, target_api)
        row = ArrReconciliationStatus.objects.get(product=self.product)
        self.assertEqual(result['result'], 502)
        self.assertEqual(row.last_outcome, 'failure')
        self.assertEqual(row.last_success_at, previous_success)
        self.assertIs(row.source_ok, source_ok)
        self.assertIs(row.target_ok, target_ok)


class SonarrReconciliationHealthIntegrationTests(ReconciliationHealthIntegrationMixin, TestCase):
    product = 'sonarr'
    instance_model = SonarrInstance
    lock_setting = 'mdblistrr.cron.RECONCILE_LOCK_PATH'
    api_setting = 'mdblistrr.cron.SonarrAPI'
    reconcile = staticmethod(reconcile_sonarr_ondemand)

    def apis(self, source_response=None, target_response=None):
        source_api, target_api = Mock(), Mock()
        source_api.get_series.return_value = [] if source_response is None else source_response
        target_api.get_series.return_value = [] if target_response is None else target_response
        return source_api, target_api

    def test_success_records_structured_status_without_extra_arr_calls(self):
        source_api, target_api = self.apis()
        result = self.invoke(source_api, target_api)
        self.assertEqual(set(('result', 'failures', 'message', 'counters')) - set(result), set())
        self.assertEqual((result['result'], result['failures'], result['message']), (200, 0, 'ok'))
        self.assert_success_status(result)
        source_api.get_series.assert_called_once_with()
        target_api.get_series.assert_called_once_with()

    def test_begin_failure_does_not_block_success(self):
        source_api, target_api = self.apis()
        with patch('mdblistrr.cron.begin_reconciliation_status',
                   side_effect=RuntimeError('health begin failed')):
            result = self.invoke(source_api, target_api)
        self.assertEqual(result['result'], 200)
        source_api.get_series.assert_called_once_with()
        target_api.get_series.assert_called_once_with()

    def test_finish_failure_preserves_success(self):
        source_api, target_api = self.apis()
        with patch('mdblistrr.cron.finish_reconciliation_status',
                   side_effect=RuntimeError('health finish failed')):
            result = self.invoke(source_api, target_api)
        self.assertEqual(result['result'], 200)

    def test_finish_failure_preserves_partial_result(self):
        malformed_target = [{'id': 1, 'tvdbId': 1, 'monitored': 'bad', 'seasons': []}]
        source_api, target_api = self.apis([], malformed_target)
        with patch('mdblistrr.cron.finish_reconciliation_status',
                   side_effect=RuntimeError('health finish failed')):
            result = self.invoke(source_api, target_api)
        self.assertEqual(result['result'], 207)

    def test_core_and_finish_failures_preserve_handled_result(self):
        source_api, target_api = self.apis()
        source_api.get_series.side_effect = RuntimeError('core failed')
        with patch('mdblistrr.cron.finish_reconciliation_status',
                   side_effect=RuntimeError('health finish failed')):
            result = self.invoke(source_api, target_api)
        self.assertEqual(result, {'result': 500, 'message': 'exception'})

    def test_partial_failure_preserves_success(self):
        previous = self.healthy_snapshot().last_success_at
        malformed_target = [{'id': 1, 'tvdbId': 1, 'monitored': 'not-a-boolean', 'seasons': []}]
        source_api, target_api = self.apis([], malformed_target)
        result = self.invoke(source_api, target_api)
        self.assert_partial_preserves_success(result, previous)
        self.assertEqual(result['counters']['failures'], 1)

    def test_source_and_target_validation_failures(self):
        self.assert_validation_failure({'error': 'source unavailable'}, [], False, None)
        self.assert_validation_failure([], {'error': 'target unavailable'}, True, False)

    def test_unexpected_exception_is_terminal_sanitised_and_handled(self):
        previous = self.healthy_snapshot().last_success_at
        source_api, target_api = self.apis()
        source_api.get_series.side_effect = RuntimeError(
            'source-secret Traceback /private/source raw-payload')
        result = self.invoke(source_api, target_api)
        row = ArrReconciliationStatus.objects.get(product='sonarr')
        self.assertEqual(result, {'result': 500, 'message': 'exception'})
        self.assertEqual(row.last_outcome, 'failure')
        self.assertEqual(row.last_success_at, previous)
        self.assertEqual(row.last_message, 'exception')
        self.assertEqual((row.source_instance_id, row.target_instance_id),
                         (self.source.id, self.target.id))
        self.assertNotIn('secret', row.last_message)
        self.assertNotIn('/private', row.last_message)


class RadarrReconciliationHealthIntegrationTests(ReconciliationHealthIntegrationMixin, TestCase):
    product = 'radarr'
    instance_model = RadarrInstance
    lock_setting = 'mdblistrr.cron.RADARR_RECONCILE_LOCK_PATH'
    api_setting = 'mdblistrr.cron.RadarrAPI'
    reconcile = staticmethod(reconcile_radarr_ondemand)

    def apis(self, source_response=None, target_response=None):
        source_api, target_api = Mock(), Mock()
        source_api.get_movies.return_value = [] if source_response is None else source_response
        target_api.get_movies.return_value = [] if target_response is None else target_response
        target_api.put_movie_monitor.return_value = {}
        return source_api, target_api

    @staticmethod
    def movie(movie_id=1):
        return {'id': movie_id, 'tmdbId': movie_id, 'hasFile': False,
                'monitored': False, 'isAvailable': True, 'title': 'Movie'}

    def test_success_records_cleanup_counters_without_extra_arr_calls_and_clears_mismatch(self):
        old_target = self.target
        new_target = RadarrInstance.objects.create(name='New target', url='http://new-target',
            apikey='new-secret', is_library_source=False, is_ondemand_target=True)
        self.healthy_snapshot()
        Preferences.set_value('radarr_reconciliation_target_id', str(new_target.id))
        self.target = new_target
        source_api, target_api = self.apis()
        result = self.invoke(source_api, target_api)
        self.assert_success_status(result)
        for key in (
            'cleanup_candidates_new', 'cleanup_candidates_pending', 'cleanup_candidates_ready',
            'cleanup_candidates_cancelled', 'cleanup_would_delete', 'cleanup_files_deleted',
            'cleanup_files_already_absent', 'cleanup_deferred_by_limit',
            'cleanup_edition_conflicts', 'cleanup_safety_deferred', 'cleanup_failures',
            'delete_attempts_consumed', 'stop_deletes_for_run',
        ):
            self.assertIn(key, result['counters'])
        source_api.get_movies.assert_called_once_with()
        target_api.get_movies.assert_called_once_with()
        self.assertNotEqual(old_target.id, new_target.id)

    def test_begin_failure_does_not_block_success(self):
        source_api, target_api = self.apis()
        with patch('mdblistrr.cron.begin_reconciliation_status',
                   side_effect=RuntimeError('health begin failed')):
            result = self.invoke(source_api, target_api)
        self.assertEqual(result['result'], 200)
        source_api.get_movies.assert_called_once_with()
        target_api.get_movies.assert_called_once_with()

    def test_finish_failure_preserves_success(self):
        source_api, target_api = self.apis()
        with patch('mdblistrr.cron.finish_reconciliation_status',
                   side_effect=RuntimeError('health finish failed')):
            result = self.invoke(source_api, target_api)
        self.assertEqual(result['result'], 200)

    def test_finish_failure_preserves_partial_result(self):
        source_api, target_api = self.apis([], [self.movie()])
        target_api.put_movie_monitor.return_value = {'error': 'monitor failed'}
        with patch('mdblistrr.cron.finish_reconciliation_status',
                   side_effect=RuntimeError('health finish failed')):
            result = self.invoke(source_api, target_api)
        self.assertEqual(result['result'], 207)

    def test_core_and_health_finish_failures_preserve_original_exception(self):
        core_error = RuntimeError('sentinel core failure')
        source_api, target_api = self.apis()
        source_api.get_movies.side_effect = core_error
        with patch('mdblistrr.cron.finish_reconciliation_status',
                   side_effect=RuntimeError('health finish failed')):
            with self.assertRaises(RuntimeError) as raised:
                self.invoke(source_api, target_api)
        self.assertIs(raised.exception, core_error)

    def test_invalid_configuration_reuses_core_preference_values(self):
        self.target.delete()
        original_get_value = Preferences.get_value
        preference_reads = []

        def count_get_value(name, default=None):
            preference_reads.append(name)
            return original_get_value(name, default)

        with patch.object(Preferences, 'get_value', side_effect=count_get_value):
            result = self.reconcile(force=True)
        self.assertEqual(result, {'result': 400, 'message': 'invalid_source_or_target'})
        self.assertEqual(preference_reads.count('radarr_reconciliation_source_id'), 1)
        self.assertEqual(preference_reads.count('radarr_reconciliation_target_id'), 1)

    def test_successful_begin_and_core_exception_records_terminal_failure(self):
        core_error = RuntimeError('sentinel core failure')
        source_api, target_api = self.apis()
        source_api.get_movies.side_effect = core_error
        with self.assertRaises(RuntimeError) as raised:
            self.invoke(source_api, target_api)
        self.assertIs(raised.exception, core_error)
        row = ArrReconciliationStatus.objects.get(product='radarr')
        self.assertEqual(row.last_outcome, 'failure')
        self.assertEqual(row.last_result_code, 500)
        self.assertEqual(row.last_message, 'exception')
        self.assertIsNotNone(row.last_completed_at)

    def test_begin_failure_and_core_exception_does_not_invent_terminal_row(self):
        core_error = RuntimeError('sentinel core failure')
        source_api, target_api = self.apis()
        source_api.get_movies.side_effect = core_error
        with patch('mdblistrr.cron.begin_reconciliation_status',
                   side_effect=RuntimeError('health begin failed')), patch(
                   'mdblistrr.cron.finish_reconciliation_status') as finish:
            with self.assertRaises(RuntimeError) as raised:
                self.invoke(source_api, target_api)
        self.assertIs(raised.exception, core_error)
        finish.assert_not_called()
        self.assertFalse(ArrReconciliationStatus.objects.filter(product='radarr').exists())

    def test_partial_failure_preserves_success(self):
        previous = self.healthy_snapshot().last_success_at
        source_api, target_api = self.apis([], [self.movie()])
        target_api.put_movie_monitor.return_value = {'error': 'monitor failed'}
        result = self.invoke(source_api, target_api)
        self.assert_partial_preserves_success(result, previous)
        self.assertEqual(result['counters']['monitor_update_failures'], 1)

    def test_source_and_target_validation_failures(self):
        self.assert_validation_failure({'error': 'source unavailable'}, [], False, None)
        self.assert_validation_failure([], {'error': 'target unavailable'}, True, False)

    def test_unexpected_exception_is_terminal_sanitised_and_propagates(self):
        previous = self.healthy_snapshot().last_success_at
        source_api, target_api = self.apis()
        source_api.get_movies.side_effect = RuntimeError(
            'source-secret Traceback /private/source raw-payload')
        with self.assertRaises(RuntimeError):
            self.invoke(source_api, target_api)
        row = ArrReconciliationStatus.objects.get(product='radarr')
        self.assertEqual(row.last_outcome, 'failure')
        self.assertEqual(row.last_success_at, previous)
        self.assertEqual(row.last_message, 'exception')
        self.assertEqual((row.source_instance_id, row.target_instance_id),
                         (self.source.id, self.target.id))
        self.assertNotIn('secret', row.last_message)
        self.assertNotIn('/private', row.last_message)

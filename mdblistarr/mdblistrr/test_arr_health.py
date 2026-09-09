import json
import os
from datetime import timedelta
from unittest.mock import patch

os.environ.setdefault('MDBLISTARR_ENCRYPTION_KEY', 'MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=')

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .arr_health import (begin_reconciliation_status, build_arr_health,
                         finish_reconciliation_status, reduce_overall_status)
from .models import (
    ArrReconciliationStatus, Preferences, RadarrCleanupCandidate, RadarrInstance,
    RadarrMovieSearchCandidate, RadarrMovieSearchCommand, SonarrCleanupCandidate,
    SonarrEpisodeSearchCandidate, SonarrEpisodeSearchCommand, SonarrInstance,
)


class ArrStatusRecorderTests(TestCase):
    def test_success_updates_single_product_row_and_json_safe_counters(self):
        begin_reconciliation_status('sonarr')
        first = finish_reconciliation_status('sonarr', 200, 'ok', {'count': 2, 'stop': False})
        previous_success = first.last_success_at
        begin_reconciliation_status('sonarr')
        second = finish_reconciliation_status('sonarr', 207, 'partial_failure', {'failures': 1})
        self.assertEqual(ArrReconciliationStatus.objects.filter(product='sonarr').count(), 1)
        self.assertEqual(second.last_success_at, previous_success)
        self.assertEqual(second.last_outcome, 'partial_failure')
        json.dumps(second.last_counters)

    def test_failure_preserves_success_and_sanitises_message_and_counters(self):
        finish_reconciliation_status('radarr', 200, 'ok')
        success = ArrReconciliationStatus.objects.get(product='radarr').last_success_at
        row = finish_reconciliation_status('radarr', 500, 'apikey=secret\nTraceback /private/file',
                                            {'safe': 1, 'payload': {'apikey': 'secret'}})
        self.assertEqual(row.last_success_at, success)
        self.assertEqual(row.last_message, 'reconciliation_failed')
        self.assertEqual(row.last_counters, {'safe': 1})
        self.assertNotIn('secret', str(row.__dict__))

    def test_endpoint_validation_evidence_is_nullable(self):
        row = finish_reconciliation_status('sonarr', 502, 'source_validation_failed',
                                            source_instance_id=3, target_instance_id=4,
                                            source_ok=False, target_ok=None)
        self.assertIs(row.source_ok, False)
        self.assertIsNone(row.target_ok)


class ClassificationTests(TestCase):
    def setUp(self):
        self.now = timezone.now()
        self.source = SonarrInstance.objects.create(name='Permanent', url='https://secret-source', apikey='secret', is_library_source=True)
        self.target = SonarrInstance.objects.create(name='On Demand', url='https://secret-target', apikey='secret', is_library_source=False, is_ondemand_target=True)
        for name, value in {
            'sonarr_reconciliation_enabled': '1', 'sonarr_reconciliation_source_id': self.source.id,
            'sonarr_reconciliation_target_id': self.target.id, 'sonarr_reconciliation_interval_minutes': '15',
        }.items():
            Preferences.set_value(name, str(value))

    def _status(self, outcome='success', completed=None, started=None, counters=None):
        return ArrReconciliationStatus.objects.create(
            product='sonarr', last_started_at=started or ((completed - timedelta(minutes=1)) if completed else self.now - timedelta(minutes=2)),
            last_completed_at=completed, last_success_at=completed if outcome == 'success' else None,
            last_result_code=200 if outcome == 'success' else 207,
            last_outcome=outcome, last_counters=counters or {}, source_ok=True, target_ok=True,
            source_instance_id=self.source.id, target_instance_id=self.target.id)

    def test_overall_reducer_precedence_and_disabled(self):
        self.assertEqual(reduce_overall_status(['disabled', 'disabled']), 'disabled')
        self.assertEqual(reduce_overall_status(['disabled', 'healthy']), 'healthy')
        self.assertEqual(reduce_overall_status(['running', 'attention']), 'attention')
        self.assertEqual(reduce_overall_status(['error', 'attention']), 'error')

    def test_recent_success_is_healthy_and_disabled_is_disabled(self):
        self._status(completed=self.now - timedelta(minutes=5))
        self.assertEqual(build_arr_health(self.now)['products'][0]['classification'], 'healthy')
        Preferences.set_value('sonarr_reconciliation_enabled', '0')
        self.assertEqual(build_arr_health(self.now)['products'][0]['classification'], 'disabled')

    def test_partial_overdue_and_stale_states(self):
        row = self._status('partial_failure', self.now - timedelta(minutes=5))
        self.assertEqual(build_arr_health(self.now)['products'][0]['classification'], 'attention')
        row.last_outcome = 'success'; row.last_result_code = 200
        row.last_completed_at = self.now - timedelta(minutes=121); row.last_started_at = row.last_completed_at
        row.save()
        self.assertTrue(build_arr_health(self.now)['products'][0]['overdue'])
        row.last_started_at = self.now - timedelta(minutes=121); row.last_completed_at = self.now - timedelta(minutes=130)
        row.save()
        product = build_arr_health(self.now)['products'][0]
        self.assertEqual(product['classification'], 'error')
        self.assertTrue(product['stale'])

    def test_recent_incomplete_is_running(self):
        self._status(completed=self.now - timedelta(minutes=10), started=self.now - timedelta(minutes=1))
        self.assertEqual(build_arr_health(self.now)['products'][0]['classification'], 'running')

    def test_edition_conflict_and_dry_run_ready_alone_are_not_unhealthy(self):
        self._status(completed=self.now - timedelta(minutes=5), counters={'cleanup_edition_conflicts': 3, 'cleanup_would_delete': 1})
        Preferences.set_value('sonarr_cleanup_enabled', '1'); Preferences.set_value('sonarr_cleanup_dry_run', '1')
        SonarrCleanupCandidate.objects.create(target_instance=self.target, tvdb_id=1, target_series_id=1,
            episode_file_id=1, first_eligible_at=self.now, last_confirmed_at=self.now, ready_at=self.now, status='ready')
        self.assertEqual(build_arr_health(self.now)['products'][0]['classification'], 'healthy')

    def test_source_target_and_both_id_changes_invalidate_snapshot(self):
        self._status(completed=self.now - timedelta(minutes=5))
        other_source = SonarrInstance.objects.create(name='Other source', url='http://source-2',
            apikey='secret', is_library_source=True)
        other_target = SonarrInstance.objects.create(name='Other target', url='http://target-2',
            apikey='secret', is_library_source=False, is_ondemand_target=True)
        for changes in (
            {'sonarr_reconciliation_source_id': other_source.id},
            {'sonarr_reconciliation_source_id': self.source.id,
             'sonarr_reconciliation_target_id': other_target.id},
            {'sonarr_reconciliation_source_id': other_source.id,
             'sonarr_reconciliation_target_id': other_target.id},
        ):
            for name, value in changes.items():
                Preferences.set_value(name, str(value))
            product = build_arr_health(self.now)['products'][0]
            self.assertEqual(product['classification'], 'attention')
            self.assertFalse(product['snapshot_matches_configuration'])
            self.assertIsNone(product['source_validation'])
            self.assertIsNone(product['target_validation'])
            self.assertEqual(product['latest_activity'], [])
            self.assertIn('Configuration changed since the last reconciliation', product['issues'][0])

    def test_name_only_change_does_not_invalidate_snapshot(self):
        self._status(completed=self.now - timedelta(minutes=5))
        self.target.name = 'Renamed On Demand'
        self.target.save(update_fields=['name'])
        product = build_arr_health(self.now)['products'][0]
        self.assertTrue(product['snapshot_matches_configuration'])
        self.assertEqual(product['classification'], 'healthy')

    def test_new_pair_terminal_run_clears_mismatch(self):
        self._status(completed=self.now - timedelta(minutes=5))
        other_target = SonarrInstance.objects.create(name='Other target', url='http://target-2',
            apikey='secret', is_library_source=False, is_ondemand_target=True)
        Preferences.set_value('sonarr_reconciliation_target_id', str(other_target.id))
        self.assertFalse(build_arr_health(self.now)['products'][0]['snapshot_matches_configuration'])
        finish_reconciliation_status('sonarr', 200, 'ok', {'episodes_inspected': 2},
            self.source.id, other_target.id, source_ok=True, target_ok=True)
        product = build_arr_health(self.now)['products'][0]
        self.assertTrue(product['snapshot_matches_configuration'])
        self.assertEqual(product['classification'], 'healthy')

    def test_latest_activity_is_curated_and_readable(self):
        self._status(completed=self.now - timedelta(minutes=5), counters={
            'series_compared': 2, 'episodes_inspected': 5,
            'search_candidates_pending': 99, 'cleanup_candidates_ready': 10,
        })
        activity = build_arr_health(self.now)['products'][0]['latest_activity']
        self.assertEqual(activity, [
            {'key': 'series_compared', 'label': 'Series compared', 'value': 2},
            {'key': 'episodes_inspected', 'label': 'Episodes inspected', 'value': 5},
        ])


class TargetScopedMetricsAndViewTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.staff = User.objects.create_superuser('staff', password='password')
        self.nonstaff = User.objects.create_user('user', password='password')
        self.s_source = SonarrInstance.objects.create(name='Sonarr Permanent', url='/source/path', apikey='SONARR_SECRET')
        self.s_target = SonarrInstance.objects.create(name='Sonarr Target', url='http://target', apikey='TARGET_SECRET', is_library_source=False, is_ondemand_target=True)
        self.s_old = SonarrInstance.objects.create(name='Old Target', url='http://old', apikey='OLD_SECRET', is_library_source=False, is_ondemand_target=True)
        self.r_source = RadarrInstance.objects.create(name='Radarr Permanent', url='http://r-source', apikey='RADARR_SECRET')
        self.r_target = RadarrInstance.objects.create(name='Radarr Target', url='http://r-target', apikey='R_TARGET_SECRET', is_library_source=False, is_ondemand_target=True)
        for product, source, target in (('sonarr', self.s_source, self.s_target), ('radarr', self.r_source, self.r_target)):
            for suffix, value in (('enabled', '1'), ('source_id', source.id), ('target_id', target.id), ('interval_minutes', '15')):
                Preferences.set_value(f'{product}_reconciliation_{suffix}', str(value))
            finish_reconciliation_status(product, 200, 'ok', source_instance_id=source.id,
                                         target_instance_id=target.id, source_ok=True, target_ok=True)

    def _candidate(self, product, item_id, status='pending', last_error=''):
        now = timezone.now()
        if product == 'sonarr':
            return SonarrEpisodeSearchCandidate(target_instance=self.s_target,
                target_series_id=1, target_episode_id=item_id, tvdb_id=item_id,
                season_number=1, episode_number=item_id, status=status,
                first_eligible_at=now, last_confirmed_at=now, last_error=last_error)
        return RadarrMovieSearchCandidate(target_instance=self.r_target,
            target_movie_id=item_id, tmdb_id=item_id, status=status,
            first_eligible_at=now, last_confirmed_at=now, last_error=last_error)

    def _product(self, product):
        return build_arr_health()['products'][0 if product == 'sonarr' else 1]

    def test_submitted_history_is_not_workload_or_unhealthy_for_either_product(self):
        for product, model in (
                ('sonarr', SonarrEpisodeSearchCandidate),
                ('radarr', RadarrMovieSearchCandidate)):
            model.objects.bulk_create([
                self._candidate(product, item_id, status='submitted')
                for item_id in range(1, 102)
            ])
            search = self._product(product)['search']
            self.assertEqual(search['submitted'], 101)
            self.assertEqual(search['in_flight'], 0)
            self.assertEqual(search['needs_attention'], 0)
            self.assertEqual(self._product(product)['classification'], 'healthy')

        self.client.force_login(self.staff)
        body = self.client.get(reverse('arr_health_view')).content.decode()
        self.assertNotIn('Submitted:', body)
        self.assertNotIn('Submitted: <strong>101</strong>', body)

    def test_started_and_pending_are_current_but_do_not_need_attention(self):
        now = timezone.now()
        for product, candidate_model, command_model, target, extra in (
                ('sonarr', SonarrEpisodeSearchCandidate, SonarrEpisodeSearchCommand,
                 self.s_target, {'target_series_id': 1}),
                ('radarr', RadarrMovieSearchCandidate, RadarrMovieSearchCommand,
                 self.r_target, {})):
            candidate_model.objects.bulk_create([
                self._candidate(product, 200, status='submitted'),
                self._candidate(product, 201, status='pending'),
            ])
            command_model.objects.create(target_instance=target, status='started',
                                         submission_attempted_at=now, **extra)
            search = self._product(product)['search']
            self.assertEqual(search['pending'], 1)
            self.assertEqual(search['in_flight'], 1)
            self.assertEqual(search['started'], 1)
            self.assertEqual(search['needs_attention'], 0)
            self.assertEqual(self._product(product)['classification'], 'healthy')

    def test_actionable_search_conditions_are_counted_for_either_product(self):
        now = timezone.now()
        for product, candidate_model, command_model, target, extra in (
                ('sonarr', SonarrEpisodeSearchCandidate, SonarrEpisodeSearchCommand,
                 self.s_target, {'target_series_id': 1}),
                ('radarr', RadarrMovieSearchCandidate, RadarrMovieSearchCommand,
                 self.r_target, {})):
            candidate_model.objects.bulk_create([
                self._candidate(product, 300, status='failed'),
                self._candidate(product, 301, status='pending', last_error='private raw error'),
            ])
            command_model.objects.create(target_instance=target, status='ambiguous',
                                         submission_attempted_at=now, **extra)
            command_model.objects.create(target_instance=target, status='unavailable',
                                         submission_attempted_at=now, **extra)
            command_model.objects.create(target_instance=target, status='failed',
                                         submission_attempted_at=now, **extra)
            search = self._product(product)['search']
            self.assertEqual(search['retry_exhausted'], 1)
            self.assertEqual(search['active_errors'], 1)
            self.assertEqual(search['uncertain'], 2)
            self.assertEqual(search['unreconciled_terminal_failures'], 1)
            self.assertEqual(search['needs_attention'], 5)
            self.assertEqual(self._product(product)['classification'], 'attention')

        self.client.force_login(self.staff)
        response = self.client.get(reverse('arr_health_view'))
        for label in ('Pending', 'In flight', 'Needs attention', 'Retry exhausted',
                      'Ambiguous/unavailable', 'Unreconciled failures'):
            self.assertContains(response, label)
        self.assertNotContains(response, 'private raw error')

    def test_sonarr_metrics_are_scoped_to_current_target(self):
        now = timezone.now()
        for target in (self.s_target, self.s_old):
            SonarrEpisodeSearchCandidate.objects.create(target_instance=target, target_series_id=1,
                target_episode_id=target.id, tvdb_id=1, season_number=1, episode_number=1,
                status='pending', first_eligible_at=now, last_confirmed_at=now)
            SonarrCleanupCandidate.objects.create(target_instance=target, tvdb_id=1, target_series_id=1,
                episode_file_id=target.id, status='ready', first_eligible_at=now,
                last_confirmed_at=now, ready_at=now)
        product = build_arr_health()['products'][0]
        self.assertEqual(product['search']['pending'], 1)
        self.assertEqual(product['cleanup']['ready'], 1)

    def test_radarr_metrics_and_attention(self):
        now = timezone.now()
        candidate = RadarrMovieSearchCandidate.objects.create(target_instance=self.r_target,
            target_movie_id=1, tmdb_id=1, status='failed', first_eligible_at=now, last_confirmed_at=now)
        RadarrMovieSearchCommand.objects.create(target_instance=self.r_target, status='ambiguous', submission_attempted_at=now)
        RadarrCleanupCandidate.objects.create(target_instance=self.r_target, tmdb_id=1, source_movie_id=1,
            source_movie_file_id=1, target_movie_id=1, movie_file_id=1, status='pending',
            first_eligible_at=now, last_confirmed_at=now, last_error='raw private error')
        product = build_arr_health()['products'][1]
        self.assertEqual(product['search']['retry_exhausted'], 1)
        self.assertEqual(product['search']['uncertain'], 1)
        self.assertEqual(product['cleanup']['active_errors'], 1)
        self.assertEqual(product['classification'], 'attention')

    def test_health_access_get_only_and_no_network_or_secret_leakage(self):
        url = reverse('arr_health_view')
        self.assertEqual(self.client.get(url).status_code, 302)
        self.client.force_login(self.nonstaff)
        self.assertEqual(self.client.get(url).status_code, 302)
        self.client.force_login(self.staff)
        with patch('mdblistrr.arr.SonarrAPI') as sonarr, patch('mdblistrr.arr.RadarrAPI') as radarr, patch('mdblistrr.arr.MdblistAPI') as mdblist:
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Arr Health')
        self.assertContains(response, 'Sonarr Permanent')
        self.assertContains(response, 'Radarr Target')
        body = response.content.decode()
        for secret in ('SONARR_SECRET', 'RADARR_SECRET', '/source/path', 'http://r-target', 'raw private error', 'retry now', 'force-delete'):
            self.assertNotIn(secret, body)
        sonarr.assert_not_called(); radarr.assert_not_called(); mdblist.assert_not_called()
        self.assertEqual(self.client.post(url).status_code, 405)

    def _cleanup_candidate(self, product, file_id, status='ready', **overrides):
        now = timezone.now()
        common = dict(status=status, first_eligible_at=now, last_confirmed_at=now,
                      ready_at=now if status == 'ready' else None)
        if product == 'sonarr':
            model = SonarrCleanupCandidate
            common.update(target_instance=self.s_target, tvdb_id=12345,
                          target_series_id=20, episode_file_id=file_id,
                          linked_episode_keys=[[1, 1]])
        else:
            model = RadarrCleanupCandidate
            common.update(target_instance=self.r_target, tmdb_id=1584,
                          source_movie_id=10, source_movie_file_id=11,
                          target_movie_id=30, movie_file_id=file_id)
        common.update(overrides)
        return model.objects.create(**common)

    def test_cleanup_details_defaults_identity_and_terminal_counts(self):
        for product, prefix, external_id in (('sonarr', 'TVDb', 12345), ('radarr', 'TMDb', 1584)):
            with self.subTest(product=product):
                ready = self._cleanup_candidate(product, 101, last_error='PRIVATE_ERROR')
                self.assertEqual(ready.target_title, '')
                self.assertIsNone(ready.target_year)
                pending = self._cleanup_candidate(product, 102, 'pending', target_title='Title', target_year=2024)
                for index, status in enumerate(('deleted', 'cancelled', 'already_absent'), 103):
                    self._cleanup_candidate(product, index, status)
                data = self._product(product)['cleanup']
                for key in ('ready', 'pending', 'deleted', 'cancelled', 'already_absent', 'active_errors'):
                    self.assertEqual(data[key], 1)
                self.assertEqual(data['oldest_ready_at'], ready.ready_at)
                self.assertEqual(data['oldest_pending_at'], pending.first_eligible_at)
                for state, row in (('ready', ready), ('pending', pending)):
                    self.assertFalse(data[f'{state}_candidates_truncated'])
                    self.assertEqual(len(data[f'{state}_candidates']), 1)
                    item = data[f'{state}_candidates'][0]
                    self.assertEqual(item['id'], row.pk)
                    self.assertEqual(item['status'], state)
                    self.assertEqual(item['external_id'], external_id)
                    self.assertEqual(item['file_id'], 101 if state == 'ready' else 102)
                    self.assertIs(item['has_error'], state == 'ready')
                    self.assertNotIn('last_error', item)
                    self.assertNotIn('linked_episode_keys', item)
                    self.assertEqual(item['display_title'], f'{prefix} {external_id}' if state == 'ready' else 'Title (2024)')
                    self.assertEqual(item['ready_at'], row.ready_at)
                    self.assertEqual(item['first_eligible_at'], row.first_eligible_at)
                    if product == 'sonarr':
                        self.assertEqual((item['tvdb_id'], item['target_series_id'], item['episode_file_id']), (12345, 20, item['file_id']))
                    else:
                        self.assertEqual((item['tmdb_id'], item['target_movie_id'], item['movie_file_id']), (1584, 30, item['file_id']))

    def test_cleanup_current_target_and_invalid_configuration(self):
        for product, instance_model, target in (('sonarr', SonarrInstance, self.s_target), ('radarr', RadarrInstance, self.r_target)):
            old = instance_model.objects.create(name='Previous', url='http://old', apikey='x', is_library_source=False, is_ondemand_target=True)
            for state, fid in (('ready', 1), ('pending', 2)):
                current = self._cleanup_candidate(product, fid, state)
                self._cleanup_candidate(product, fid, state, target_instance=old)
                self.assertEqual([x['id'] for x in self._product(product)['cleanup'][f'{state}_candidates']], [current.pk])
            for invalid_id in (999999, old.pk):
                if invalid_id == old.pk:
                    old.is_ondemand_target = False
                    old.save()
                Preferences.set_value(f'{product}_reconciliation_target_id', str(invalid_id))
                data = self._product(product)['cleanup']
                for state in ('ready', 'pending'):
                    self.assertEqual(data[state], 0)
                    self.assertEqual(data[f'{state}_candidates'], [])
                    self.assertFalse(data[f'{state}_candidates_truncated'])
            Preferences.set_value(f'{product}_reconciliation_target_id', str(target.pk))

    def test_cleanup_limits_ordering_and_sql_are_bounded_per_status(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        from .arr_health import CLEANUP_CANDIDATE_DISPLAY_LIMIT, _cleanup_metrics
        limit = CLEANUP_CANDIDATE_DISPLAY_LIMIT
        self.assertEqual(limit, 100)
        now = timezone.now()
        for product, model, target in (('sonarr', SonarrCleanupCandidate, self.s_target), ('radarr', RadarrCleanupCandidate, self.r_target)):
            expected = {}
            for state, timestamp in (('ready', 'ready_at'), ('pending', 'first_eligible_at')):
                rows = []
                # Reverse chronological insertion, with timestamp ties across the cutoff.
                for index in range(limit + 7):
                    row = self._cleanup_candidate(product, (1000 if state == 'ready' else 2000) + index, state,
                        **{timestamp: now - timedelta(hours=index // 3)})
                    rows.append(row)
                expected[state] = [r.pk for r in sorted(rows, key=lambda r: (getattr(r, timestamp), r.pk))][:limit]
            with CaptureQueriesContext(connection) as queries:
                data = _cleanup_metrics(model, target.pk)
            selects = [q['sql'] for q in queries if 'target_title' in q['sql']]
            self.assertEqual(len(selects), 2)
            for sql, (state, timestamp) in zip(selects, (('ready', 'ready_at'), ('pending', 'first_eligible_at'))):
                self.assertIn(f'LIMIT {limit}', sql)
                self.assertRegex(sql, r'ORDER BY .+ ASC, .+ ASC LIMIT')
                self.assertIn(f"= '{state}'", sql)
                self.assertEqual(data[state], limit + 7)
                self.assertEqual([x['id'] for x in data[f'{state}_candidates']], expected[state])
                self.assertTrue(data[f'{state}_candidates_truncated'])
            for state in ('ready', 'pending'):
                model.objects.filter(status=state).exclude(pk__in=expected[state]).delete()
            boundary = _cleanup_metrics(model, target.pk)
            for state in ('ready', 'pending'):
                self.assertEqual(len(boundary[f'{state}_candidates']), limit)
                self.assertFalse(boundary[f'{state}_candidates_truncated'])

    def test_sonarr_episode_display_validates_persisted_json(self):
        cases = [([[1, 1]], 'S01E01'), ([[1, 2], [1, 1], [1, 2]], 'S01E01, S01E02'),
                 ([[0, 3]], 'S00E03'), ([[100, 123]], 'S100E123')]
        cases += [(bad, 'Unknown') for bad in ('PRIVATE_JSON', {'private': 'PRIVATE_JSON'}, [], [[True, 1]], [[1, False]], [[-1, 1]], [[1, '2']], [[1]], [[1, 2, 3]], [[1, 1], 'bad'])]
        candidate = self._cleanup_candidate('sonarr', 10)
        for keys, expected in cases:
            with self.subTest(keys=keys):
                candidate.linked_episode_keys = keys
                candidate.save()
                item = self._product('sonarr')['cleanup']['ready_candidates'][0]
                self.assertNotIn('linked_episode_keys', item)
                self.assertEqual(item['episodes_display'], expected)
                self.assertEqual(item['episode_labels'], [] if expected == 'Unknown' else expected.split(', '))

    def test_cleanup_rendering_and_zero_network_contract(self):
        from contextlib import ExitStack
        from django.utils.formats import date_format
        self.client.force_login(self.staff)
        for product in ('sonarr', 'radarr'):
            self._cleanup_candidate(product, 678, target_title='<b>Title</b>', target_year=2024,
                                    last_error='PRIVATE_ERROR')
            self._cleanup_candidate(product, 679, 'pending')
        bad = self._cleanup_candidate('sonarr', 680, linked_episode_keys={'PRIVATE_JSON': [1]})
        with ExitStack() as stack:
            # Patch constructors on the classes themselves, catching already-imported aliases too.
            guards = [stack.enter_context(patch(path, side_effect=AssertionError('Health must not contact external services')))
                      for path in ('mdblistrr.arr.SonarrAPI.__init__', 'mdblistrr.arr.RadarrAPI.__init__',
                                   'mdblistrr.arr.MdblistAPI.__init__', 'mdblistrr.connect.Connect.__init__',
                                   'requests.sessions.Session.request', 'socket.create_connection')]
            health = build_arr_health()
            response = self.client.get(reverse('arr_health_view'))
            for guard in guards:
                guard.assert_not_called()
        self.assertEqual(len(health['products']), 2)
        self.assertContains(response, 'Ready for deletion', count=2)
        self.assertContains(response, '&lt;b&gt;Title&lt;/b&gt; (2024)', count=2)
        for text in ('TVDb 12345', 'TMDb 1584', 'EpisodeFile ID', 'MovieFile ID', 'S01E01', 'Unknown', '>678<', '>12345<', '>1584<', date_format(timezone.localtime(bad.ready_at), 'Y-m-d H:i:s T')):
            self.assertContains(response, text)
        self.assertContains(response, 'Pending cleanup candidates (1)', count=2)
        body = response.content.decode()
        self.assertNotIn('<b>Title</b>', body)
        self.assertNotIn('PRIVATE_ERROR', body)
        self.assertNotIn('PRIVATE_JSON', body)
        self.assertNotIn('Showing first', body)
        from lxml import html
        document = html.fromstring(body)
        for heading in document.xpath('//h4[text()="Ready for deletion"]'):
            self.assertEqual(heading.xpath('ancestor::details'), [])
        for details in document.xpath('//details'):
            self.assertNotIn('open', details.attrib)
            self.assertNotIn('Ready for deletion', details.text_content())
        for model in (SonarrCleanupCandidate, RadarrCleanupCandidate):
            model.objects.filter(status='pending').delete()
        self.assertNotContains(self.client.get(reverse('arr_health_view')), '<details')
        for model in (SonarrCleanupCandidate, RadarrCleanupCandidate):
            model.objects.all().delete()
        empty = self.client.get(reverse('arr_health_view'))
        self.assertNotContains(empty, 'Ready for deletion')
        self.assertNotContains(empty, '<details')
        self.assertNotContains(empty, '<table')
        self.assertContains(empty, 'Cleanup — current persistent state', count=2)

    def test_cleanup_rendered_truncation_and_specials(self):
        from .arr_health import CLEANUP_CANDIDATE_DISPLAY_LIMIT
        self.client.force_login(self.staff)
        for product in ('sonarr', 'radarr'):
            for state, offset in (('ready', 0), ('pending', 1000)):
                for index in range(CLEANUP_CANDIDATE_DISPLAY_LIMIT + 1):
                    self._cleanup_candidate(product, offset + index + 1, state)
        SonarrCleanupCandidate.objects.update(linked_episode_keys=[[0, 3]])
        response = self.client.get(reverse('arr_health_view'))
        for state in ('ready', 'pending'):
            self.assertContains(response, f'Showing first 100 of 101 {state} candidates.', count=2)
        self.assertContains(response, 'Pending cleanup candidates (101)', count=2)
        self.assertContains(response, 'S00E03')


    def test_sonarr_episode_labels_are_bounded_without_changing_persisted_keys(self):
        from .arr_health import CLEANUP_EPISODE_LABEL_DISPLAY_LIMIT
        limit = CLEANUP_EPISODE_LABEL_DISPLAY_LIMIT
        keys = [[1, number] for number in range(limit + 50, 0, -1)] * 2
        candidate = self._cleanup_candidate('sonarr', 10, linked_episode_keys=keys)
        item = self._product('sonarr')['cleanup']['ready_candidates'][0]
        expected = [f'S01E{number:02d}' for number in range(1, limit + 1)]
        self.assertEqual(item['episode_labels'], expected)
        self.assertEqual(item['episodes_display'], ', '.join(expected) + ' … (additional episodes omitted)')
        self.assertNotIn('linked_episode_keys', item)
        candidate.refresh_from_db()
        self.assertEqual(candidate.linked_episode_keys, keys)
        self.client.force_login(self.staff)
        response = self.client.get(reverse('arr_health_view'))
        self.assertContains(response, 'additional episodes omitted')
        self.assertNotContains(response, f'S01E{limit + 1:02d}')
        # Duplicates alone do not trigger truncation at the boundary.
        candidate.linked_episode_keys = [[1, number] for number in range(1, limit + 1)] * 2
        candidate.save()
        item = self._product('sonarr')['cleanup']['ready_candidates'][0]
        self.assertEqual(item['episodes_display'], ', '.join(expected))
        # Validation still examines the tail; malformed input never masquerades as complete.
        candidate.linked_episode_keys = keys + [['PRIVATE_TAIL', 1]]
        candidate.save()
        item = self._product('sonarr')['cleanup']['ready_candidates'][0]
        self.assertEqual(item['episode_labels'], [])
        self.assertEqual(item['episodes_display'], 'Unknown')

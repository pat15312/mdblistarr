"""Historical Arr titles are metadata, independent of lifecycle state."""
from contextlib import ExitStack
from datetime import timedelta
import os
import tempfile
from unittest.mock import Mock, patch

from django.db import DatabaseError, connection
from django.db.models.query import QuerySet
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from . import test_arr_health
from .cron import reconcile_sonarr_ondemand, reconcile_radarr_ondemand
from .media_display import backfill_candidate_titles
from .models import SonarrEpisodeSearchCandidate


class CandidateTitleBackfillTests(TestCase):
    setUp = test_arr_health.TargetScopedMetricsAndViewTests.setUp
    _candidate = test_arr_health.TargetScopedMetricsAndViewTests._candidate
    _cleanup_candidate = test_arr_health.TargetScopedMetricsAndViewTests._cleanup_candidate

    def _target(self, product):
        return self.s_target if product == 'sonarr' else self.r_target

    def _media(self, product, external_id=101, title='Example title', item_id=200):
        return {'id': item_id, 'tvdbId' if product == 'sonarr' else 'tmdbId': external_id,
                'title': title, 'hasFile': True}

    def _cleanup(self, product, file_id=1, status='deleted', **overrides):
        external_field = 'tvdb_id' if product == 'sonarr' else 'tmdb_id'
        return self._cleanup_candidate(product, file_id, status,
            **{external_field: 101, **overrides})

    def test_all_candidate_states_receive_titles_without_changing_other_fields(self):
        for product in ('sonarr', 'radarr'):
            rows = []
            for index, status in enumerate(('pending', 'ready', 'deleted', 'cancelled', 'already_absent'), 1):
                rows.append(self._cleanup(product, index, status,
                    deleted_at=timezone.now() - timedelta(days=30),
                    cancelled_at=timezone.now() - timedelta(days=20), last_error='synthetic error'))
            for index, status in enumerate(('pending', 'submitted', 'cancelled', 'failed'), 101):
                row = self._candidate(product, index, status)
                row.save(); rows.append(row)
            originals = [type(row).objects.values().get(pk=row.pk) for row in rows]
            media = [self._media(product, i, item_id=i) for i in range(101, 105)]
            backfill_candidate_titles(product, self._target(product), [], media)
            for row, original in zip(rows, originals):
                current = type(row).objects.values().get(pk=row.pk)
                self.assertEqual(current.pop('target_title'), 'Example title')
                original.pop('target_title')
                self.assertEqual(current, original)
            # Existing stored names are preserved on subsequent runs.
            backfill_candidate_titles(product, self._target(product), [],
                [self._media(product, i, title='Changed title', item_id=i) for i in range(101, 105)])
            for row in rows:
                row.refresh_from_db()
                self.assertEqual(row.target_title, 'Example title')

    def test_source_fallback_uses_external_identity_and_is_target_scoped(self):
        for product in ('sonarr', 'radarr'):
            row = self._cleanup(product)
            source = self.s_source if product == 'sonarr' else self.r_source
            other = self._cleanup(product, target_instance=source)
            # The target's old local ID now describes an unrelated media item.
            local_id = row.target_series_id if product == 'sonarr' else row.target_movie_id
            backfill_candidate_titles(product, self._target(product),
                [self._media(product, title='Permanent title', item_id=900)],
                [self._media(product, external_id=999, title='Unrelated title', item_id=local_id)])
            row.refresh_from_db(); other.refresh_from_db()
            self.assertEqual(row.target_title, 'Permanent title')
            self.assertEqual(other.target_title, '')

    def test_target_title_preferred_and_missing_titles_not_invented(self):
        for product in ('sonarr', 'radarr'):
            row = self._cleanup(product)
            backfill_candidate_titles(product, self._target(product), [], [])
            row.refresh_from_db(); self.assertEqual(row.target_title, '')
            backfill_candidate_titles(product, self._target(product),
                [self._media(product, title='Source title')],
                [self._media(product, title='Target title')])
            row.refresh_from_db(); self.assertEqual(row.target_title, 'Target title')
            type(row).objects.filter(pk=row.pk).update(target_title='')
            backfill_candidate_titles(product, self._target(product),
                [self._media(product, title='Source title')],
                [self._media(product, title=None)])
            row.refresh_from_db(); self.assertEqual(row.target_title, 'Source title')

    def test_malformed_or_conflicting_snapshot_identity_defers_backfill(self):
        for product in ('sonarr', 'radarr'):
            row = self._cleanup(product)
            invalid_snapshots = [None, {}, [None], [self._media(product, external_id=True)],
                [self._media(product, item_id='200')],
                [self._media(product), self._media(product, item_id=201)],
                [self._media(product), self._media(product, external_id=102)]]
            for bad in invalid_snapshots:
                with self.subTest(product=product, snapshot=bad), self.assertLogs('mdblistrr.media_display'):
                    backfill_candidate_titles(product, self._target(product), [], bad)
                row.refresh_from_db(); self.assertEqual(row.target_title, '')
            # No source fallback when the target snapshot has conflicting identity.
            with self.assertLogs('mdblistrr.media_display'):
                backfill_candidate_titles(product, self._target(product),
                    [self._media(product)], [self._media(product), self._media(product)])
            row.refresh_from_db(); self.assertEqual(row.target_title, '')

    def test_backfilled_modal_names_are_sanitized_and_render_without_network(self):
        self.client.force_login(self.staff)
        for product in ('sonarr', 'radarr'):
            self._cleanup(product)
            title = '<b>Example title</b> apikey=synthetic-secret'
            backfill_candidate_titles(product, self._target(product), [self._media(product, title=title)], [])
            with ExitStack() as stack:
                guards = [stack.enter_context(patch(path, side_effect=AssertionError('No network')))
                          for path in ('mdblistrr.arr.SonarrAPI.__init__', 'mdblistrr.arr.RadarrAPI.__init__',
                                       'mdblistrr.arr.MdblistAPI.__init__', 'requests.sessions.Session.request',
                                       'socket.create_connection')]
                response = self.client.get(reverse('arr_health_details_view', args=[product, 'cleanup', 'deleted']),
                    HTTP_X_REQUESTED_WITH='XMLHttpRequest')
                for guard in guards:
                    guard.assert_not_called()
            self.assertContains(response, '&lt;b&gt;Example title&lt;/b&gt;')
            self.assertNotContains(response, 'synthetic-secret')
            self.assertNotContains(response, '(title unavailable)')

    def test_title_updates_are_batched_and_bounded_in_sql(self):
        rows = [SonarrEpisodeSearchCandidate(target_instance=self.s_target, target_series_id=i,
                    target_episode_id=i, tvdb_id=i, season_number=1, episode_number=1,
                    first_eligible_at=timezone.now(), last_confirmed_at=timezone.now())
                for i in range(1, 206)]
        SonarrEpisodeSearchCandidate.objects.bulk_create(rows)
        media = [self._media('sonarr', i, 'Example title ' * 30, i) for i in range(1, 206)]
        with CaptureQueriesContext(connection) as queries:
            backfill_candidate_titles('sonarr', self.s_target, [], media)
        updates = [q['sql'] for q in queries if q['sql'].startswith('UPDATE')]
        self.assertEqual(len(updates), 3)
        for sql in updates:
            self.assertLessEqual(sql.count('WHEN '), 100)
            self.assertNotIn('updated_at', sql)
        self.assertEqual(SonarrEpisodeSearchCandidate.objects.filter(target_title=('Example title ' * 30)[:255]).count(), 205)

    def test_database_failure_rolls_back_metadata_and_leaves_connection_usable(self):
        row = self._cleanup('sonarr')
        search = self._candidate('sonarr', 101); search.save()
        update = QuerySet.update

        def fail_search_update(queryset, **kwargs):
            if queryset.model is SonarrEpisodeSearchCandidate:
                raise DatabaseError('synthetic failure')
            return update(queryset, **kwargs)

        with patch.object(QuerySet, 'update', fail_search_update), self.assertLogs('mdblistrr.media_display'):
            backfill_candidate_titles('sonarr', self.s_target, [], [self._media('sonarr')])
        row.refresh_from_db(); search.refresh_from_db()
        self.assertEqual(row.target_title, '')
        self.assertEqual(search.target_title, '')

    def test_reconciliation_backfills_deleted_history_even_without_target_media_or_cleanup_enabled(self):
        for product, reconcile, api_path, lock_setting in (
                ('sonarr', reconcile_sonarr_ondemand, 'mdblistrr.cron.SonarrAPI', 'mdblistrr.cron.RECONCILE_LOCK_PATH'),
                ('radarr', reconcile_radarr_ondemand, 'mdblistrr.cron.RadarrAPI', 'mdblistrr.cron.RADARR_RECONCILE_LOCK_PATH')):
            row = self._cleanup(product, deleted_at=timezone.now() - timedelta(days=30))
            original = type(row).objects.values().get(pk=row.pk)
            source_api, target_api = Mock(), Mock()
            method = 'get_series' if product == 'sonarr' else 'get_movies'
            getattr(source_api, method).return_value = [self._media(product, title='Recovered historical title')]
            getattr(target_api, method).return_value = []
            with tempfile.TemporaryDirectory() as directory, patch(lock_setting, os.path.join(directory, 'reconcile.lock')), patch(
                    api_path, side_effect=[source_api, target_api]):
                result = reconcile(force=True)
            self.assertEqual(result['result'], 200)
            current = type(row).objects.values().get(pk=row.pk)
            self.assertEqual(current.pop('target_title'), 'Recovered historical title')
            original.pop('target_title'); self.assertEqual(current, original)
            self.assertEqual([call[0] for call in source_api.mock_calls], [method])
            self.assertEqual([call[0] for call in target_api.mock_calls], [method])

            # A metadata failure must not alter the core result or add Arr calls.
            type(row).objects.filter(pk=row.pk).update(target_title='')
            source_api.reset_mock(); target_api.reset_mock()
            with tempfile.TemporaryDirectory() as directory, patch(lock_setting, os.path.join(directory, 'reconcile.lock')), patch(
                    api_path, side_effect=[source_api, target_api]), patch(
                    'mdblistrr.media_display._title_index', side_effect=RuntimeError('synthetic failure')), self.assertLogs('mdblistrr.media_display'):
                failed_metadata_result = reconcile(force=True)
            self.assertEqual(failed_metadata_result, result)
            row.refresh_from_db(); self.assertEqual(row.target_title, '')
            self.assertEqual([call[0] for call in source_api.mock_calls], [method])
            self.assertEqual([call[0] for call in target_api.mock_calls], [method])

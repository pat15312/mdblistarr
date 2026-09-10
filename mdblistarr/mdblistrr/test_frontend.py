from datetime import datetime, timedelta, timezone as dt_timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone
from lxml import html

from .health_details import build_health_details
from .media_display import refresh_search_titles
from .models import (Log, Preferences, SonarrEpisodeSearchCandidate, RadarrMovieSearchCandidate,
    SonarrEpisodeSearchCommand, RadarrMovieSearchCommand)
from . import test_arr_health


class LogPageTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = get_user_model().objects.create_superuser('log-admin', password='password')
        cls.now = datetime(2026, 7, 1, 12, tzinfo=dt_timezone.utc)
        Log.objects.bulk_create([
            Log(date=cls.now + timedelta(minutes=i // 3), provider=(i % 3) + 1,
                status=1, text=f'Event {i}') for i in range(213)
        ])

    def setUp(self):
        self.client.force_login(self.staff)
        self.url = reverse('log_view')

    def test_default_sizes_stable_order_and_all_history(self):
        for size in (10, 25, 50):
            query = {} if size == 25 else {'page_size': size}
            first = self.client.get(self.url, query).context['page_obj']
            second = self.client.get(self.url, {**query, 'page': 2}).context['page_obj']
            self.assertEqual(first.paginator.count, 213)
            self.assertEqual(len(first), size)
            expected = list(Log.objects.order_by('-date', '-id').values_list('id', flat=True)[:size * 2])
            self.assertEqual([x.pk for x in first] + [x.pk for x in second], expected)
        last = self.client.get(self.url, {'page': 999}).context['page_obj']
        self.assertEqual(last.end_index(), 213)
        self.assertEqual(self.client.get(self.url, {'page': 'bad'}).context['page_obj'].number, 1)

    def test_combined_filters_timezone_boundaries_and_pagination_links(self):
        with timezone.override(ZoneInfo('Europe/London')):
            query = {'provider': '2', 'page_size': 10, 'from_date': '2026-07-01T13:10:00',
                     'to_date': '2026-07-01T13:25:00'}
            response = self.client.get(self.url, query)
            page = response.context['page_obj']
            self.assertEqual(page.paginator.count, 16)
            self.assertTrue(all(row.provider == 2 for row in page))
            self.assertContains(response, 'Europe/London')
            doc = html.fromstring(response.content)
            next_url = doc.xpath('//nav[@aria-label="Pagination"]//a[text()="Next"]/@href')[0]
            next_page = self.client.get(next_url).context['page_obj']
            self.assertEqual(len(next_page), 6)
            self.assertEqual(next_page[-1].date, self.now + timedelta(minutes=10))
            # Filter submissions omit page; pagination alone preserves it.
            self.assertFalse(doc.xpath('//form[@id="log-filters"]//*[@name="page"]'))
            self.assertEqual(self.client.get(self.url, {**query, 'page_size': 50}).context['page_obj'].number, 1)

    def test_open_ranges_sources_and_empty_results(self):
        for query, count in (({'from_date': '2026-07-01T13:10:00Z'}, 3),
                             ({'to_date': '2026-07-01T12:00:00Z'}, 3),
                             ({'provider': '1'}, 71), ({'provider': '2'}, 71),
                             ({'from_date': '2030-01-01T00:00:00Z'}, 0)):
            self.assertEqual(self.client.get(self.url, query).context['page_obj'].paginator.count, count)

    def test_invalid_filters_fail_closed_including_dst_gaps_and_folds(self):
        for query in ({'page_size': '100000'}, {'provider': '3'}, {'from_date': 'invalid'},
                      {'from_date': '2026-07-02T00:00', 'to_date': '2026-07-01T00:00'}):
            response = self.client.get(self.url, query)
            self.assertTrue(response.context['filter_form'].errors)
            self.assertEqual(response.context['page_obj'].paginator.count, 0)
        with timezone.override(ZoneInfo('Europe/London')):
            for value in ('2026-03-29T01:30', '2026-10-25T01:30'):
                self.assertTrue(self.client.get(self.url, {'from_date': value}).context['filter_form'].errors)


class HealthDetailTests(TestCase):
    setUp = test_arr_health.TargetScopedMetricsAndViewTests.setUp
    _candidate = test_arr_health.TargetScopedMetricsAndViewTests._candidate
    _cleanup_candidate = test_arr_health.TargetScopedMetricsAndViewTests._cleanup_candidate
    _product = test_arr_health.TargetScopedMetricsAndViewTests._product

    def _url(self, product='sonarr', section='search', metric='pending'):
        return reverse('arr_health_details_view', args=[product, section, metric])

    def test_search_counts_match_details_for_both_products(self):
        for product, command_model, target in (
                ('sonarr', SonarrEpisodeSearchCommand, self.s_target),
                ('radarr', RadarrMovieSearchCommand, self.r_target)):
            pending = self._candidate(product, 1, last_error='PRIVATE_ERROR')
            pending.target_title = 'Example title'; pending.save()
            failed = self._candidate(product, 2, 'failed'); failed.save()
            for state in ('submitting', 'queued', 'started', 'ambiguous', 'unavailable', 'failed'):
                kwargs = {'target_series_id': 1} if product == 'sonarr' else {}
                command = command_model.objects.create(target_instance=target, status=state,
                    submission_attempted_at=timezone.now(), **kwargs)
                through = command.candidates.through
                field = 'target_episode_id' if product == 'sonarr' else 'target_movie_id'
                through.objects.create(command=command, candidate=pending, **{field: 1})
            counts = self._product(product)['search']
            for metric in ('pending', 'in_flight', 'needs_attention', 'retry_exhausted',
                           'uncertain', 'unreconciled_terminal_failures', 'active_errors',
                           'submitting', 'queued', 'started'):
                detail = build_health_details(product, 'search', metric)['page_obj']
                self.assertEqual(detail.paginator.count, counts[metric], (product, metric))
                self.assertEqual(len(detail), counts[metric])
                self.assertTrue(all(entry['media'] for entry in detail))
            self.assertEqual(counts['needs_attention'], 5)
            detail = build_health_details(product, 'search', 'pending')['page_obj'][0]
            self.assertEqual(detail['timestamp'], pending.first_eligible_at)
            self.assertEqual(detail['timestamp_label'], 'Eligible since')
            self.assertEqual(detail['media'][0]['title'], 'Example title')
            for metric in ('retry_exhausted', 'active_errors'):
                entry = build_health_details(product, 'search', metric)['page_obj'][0]
                self.assertIn('not event time', entry['timestamp_label'])

    def test_cleanup_categories_timestamps_and_target_scope(self):
        for product in ('sonarr', 'radarr'):
            for i, state in enumerate(('pending', 'ready', 'deleted', 'cancelled', 'already_absent'), 1):
                row = self._cleanup_candidate(product, i, state, target_title='Example movie/show',
                    deleted_at=timezone.now() if state == 'deleted' else None,
                    cancelled_at=timezone.now() if state == 'cancelled' else None,
                    last_error='PRIVATE_ERROR')
                entry = build_health_details(product, 'cleanup', state)['page_obj'][0]
                field = {'pending': 'first_eligible_at', 'ready': 'ready_at', 'deleted': 'deleted_at',
                         'cancelled': 'cancelled_at', 'already_absent': 'updated_at'}[state]
                self.assertEqual(entry['timestamp'], getattr(row, field))
                self.assertEqual(entry['id'], row.pk)
            self.assertEqual(build_health_details(product, 'cleanup', 'active_errors')['page_obj'].paginator.count, 2)
            Preferences.set_value(f'{product}_reconciliation_target_id', '999999')
            self.assertEqual(build_health_details(product, 'cleanup', 'pending')['page_obj'].paginator.count, 0)

    def test_bounded_details_and_command_membership(self):
        for number in range(1, 28):
            self._candidate('radarr', number).save()
        first = build_health_details('radarr', 'search', 'pending')['page_obj']
        last = build_health_details('radarr', 'search', 'pending', 2)['page_obj']
        self.assertEqual(first.paginator.count, 27)
        self.assertEqual((len(first), len(last)), (25, 2))
        self.assertFalse({x['id'] for x in first} & {x['id'] for x in last})
        from .health_details import COMMAND_MEDIA_LIMIT
        command = RadarrMovieSearchCommand.objects.create(target_instance=self.r_target,
            status='queued', submission_attempted_at=timezone.now())
        for number in range(100, 100 + COMMAND_MEDIA_LIMIT + 1):
            candidate = self._candidate('radarr', number); candidate.save()
            command.candidates.through.objects.create(command=command, candidate=candidate, target_movie_id=number)
        entry = build_health_details('radarr', 'search', 'queued')['page_obj'][0]
        self.assertEqual(len(entry['media']), COMMAND_MEDIA_LIMIT)
        self.assertEqual(entry['media_count'], COMMAND_MEDIA_LIMIT + 1)
        self.assertTrue(entry['media_truncated'])

    def test_access_validation_no_network_and_escaped_rendering(self):
        url = self._url()
        self.assertEqual(self.client.get(url).status_code, 302)
        self.client.force_login(self.nonstaff)
        self.assertEqual(self.client.get(url).status_code, 302)
        self.client.force_login(self.staff)
        row = self._candidate('sonarr', 1, last_error='PRIVATE_ERROR')
        row.target_title = '<b>Example</b>'; row.save()
        with patch('requests.sessions.Session.request', side_effect=AssertionError('network')):
            response = self.client.get(url, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
            self.assertContains(response, '&lt;b&gt;Example&lt;/b&gt;')
            self.assertNotContains(response, 'PRIVATE_ERROR')
            self.assertNotContains(response, '<b>Example</b>')
            self.assertNotContains(response, '<html')
            self.assertContains(self.client.get(url), '<html')
            page = self.client.get(reverse('arr_health_view'))
        self.assertNotContains(page, '—')
        doc = html.fromstring(page.content)
        self.assertTrue(doc.xpath('//a[@data-health-detail][text()="1"]'))
        self.assertFalse(doc.xpath('//a[@data-health-detail][text()="0"]'))
        self.assertEqual(self.client.post(url).status_code, 405)
        self.assertEqual(self.client.get(self._url(product='other')).status_code, 404)
        self.assertEqual(self.client.get(self._url(metric='other')).status_code, 404)

    def test_title_refresh_preserves_state_identity_and_timestamps(self):
        for product, target in (('sonarr', self.s_target), ('radarr', self.r_target)):
            candidate = self._candidate(product, 1, 'failed'); candidate.save()
            before = candidate.updated_at
            external_key = 'tvdbId' if product == 'sonarr' else 'tmdbId'
            refresh_search_titles(product, target, [{'id': 1, external_key: 1, 'title': 'Example title'}])
            candidate.refresh_from_db()
            self.assertEqual(candidate.target_title, 'Example title')
            self.assertEqual(candidate.status, 'failed')
            self.assertEqual(candidate.updated_at, before)
            refresh_search_titles(product, target, [{'id': 1, external_key: 999, 'title': 'Wrong identity'}])
            candidate.refresh_from_db()
            self.assertEqual(candidate.target_title, 'Example title')
            with patch.object(type(candidate).objects, 'bulk_update', side_effect=RuntimeError('private')):
                refresh_search_titles(product, target, [{'id': 1, external_key: 1, 'title': 'New title'}])
            candidate.refresh_from_db()
            self.assertEqual(candidate.target_title, 'Example title')

    def test_identity_reset_clears_stale_title_for_both_products(self):
        from .sonarr_search import _reset_pending as reset_sonarr
        from .radarr_search import _reset_pending as reset_radarr
        for product in ('sonarr', 'radarr'):
            candidate = self._candidate(product, 1)
            candidate.target_title = 'Previous identity title'
            candidate.save()
            if product == 'sonarr':
                reset_sonarr(candidate, tvdb_id=999, target_series_id=1, key=(1, 1), now=timezone.now())
            else:
                reset_radarr(candidate, tmdb_id=999, now=timezone.now())
            candidate.refresh_from_db()
            self.assertEqual(candidate.target_title, '')

    def test_command_timestamps_and_missing_metadata_are_explicit(self):
        for product, model, target in (
                ('sonarr', SonarrEpisodeSearchCommand, self.s_target),
                ('radarr', RadarrMovieSearchCommand, self.r_target)):
            now = timezone.now()
            for state, field in (('submitting', 'submission_attempted_at'), ('queued', 'queued_at'),
                                 ('started', 'started_at'), ('unavailable', 'unavailable_since'),
                                 ('ambiguous', 'last_checked_at'), ('failed', 'terminal_at')):
                kwargs = {'target_series_id': 1} if product == 'sonarr' else {}
                command = model.objects.create(target_instance=target, status=state,
                    **{**kwargs, 'submission_attempted_at': now, field: now})
                metric = ('uncertain' if state in ('ambiguous', 'unavailable') else
                          'unreconciled_terminal_failures' if state == 'failed' else state)
                entries = build_health_details(product, 'search', metric)['page_obj']
                entry = next(x for x in entries if x['id'] == command.pk)
                self.assertEqual(entry['timestamp'], now)
                self.assertEqual(entry['media'], [])
                self.assertFalse(entry['media_truncated'])
            command = model.objects.get(target_instance=target, status='queued')
            command.queued_at = None; command.save()
            self.assertIsNone(build_health_details(product, 'search', 'queued')['page_obj'][0]['timestamp'])

    def test_details_exclude_other_targets_and_invalid_roles(self):
        for product, target, source in (('sonarr', self.s_target, self.s_source),
                                        ('radarr', self.r_target, self.r_source)):
            row = self._candidate(product, 1); row.save()
            other = self._candidate(product, 2)
            other.target_instance = source; other.save()
            self.assertEqual(build_health_details(product, 'search', 'pending')['page_obj'].paginator.count, 1)
            target.is_ondemand_target = False; target.save()
            self.assertEqual(build_health_details(product, 'search', 'pending')['page_obj'].paginator.count, 0)
            target.is_ondemand_target = True; target.save()
            Preferences.set_value(f'{product}_reconciliation_source_id', str(target.pk))
            self.assertEqual(build_health_details(product, 'search', 'pending')['page_obj'].paginator.count, 0)


class SearchTitleMigrationTests(TransactionTestCase):
    def test_upgrade_preserves_existing_candidates_commands_and_timestamps(self):
        previous = ('mdblistrr', '0012_radarrcleanupcandidate_target_title_and_more')
        latest = ('mdblistrr', '0013_search_candidate_titles')
        executor = MigrationExecutor(connection)
        self.addCleanup(lambda: MigrationExecutor(connection).migrate([latest]))
        executor.migrate([previous])
        apps = executor.loader.project_state([previous]).apps
        now = timezone.now()
        originals = {}
        for product, instance_name, candidate_name, command_name in (
                ('sonarr', 'SonarrInstance', 'SonarrEpisodeSearchCandidate', 'SonarrEpisodeSearchCommand'),
                ('radarr', 'RadarrInstance', 'RadarrMovieSearchCandidate', 'RadarrMovieSearchCommand')):
            target = apps.get_model('mdblistrr', instance_name).objects.create(
                name='Example target', url='https://example.test', apikey='synthetic',
                is_library_source=False, is_ondemand_target=True)
            command = apps.get_model('mdblistrr', command_name).objects.create(
                target_instance=target, status='queued', submission_attempted_at=now,
                **({'target_series_id': 1} if product == 'sonarr' else {}))
            candidate_model = apps.get_model('mdblistrr', candidate_name)
            candidate_model.objects.create(target_instance=target, status='submitted',
                first_eligible_at=now, last_confirmed_at=now, current_command=command,
                attempt_count=2, submitted_at=now,
                **({'target_series_id': 1, 'target_episode_id': 2, 'tvdb_id': 3,
                    'season_number': 1, 'episode_number': 2} if product == 'sonarr' else
                   {'target_movie_id': 1, 'tmdb_id': 2}))
            originals[candidate_name] = candidate_model.objects.values().get()
        executor = MigrationExecutor(connection)
        executor.migrate([latest])
        apps = executor.loader.project_state([latest]).apps
        for model_name, original in originals.items():
            upgraded = apps.get_model('mdblistrr', model_name).objects.values().get()
            self.assertEqual(upgraded.pop('target_title'), '')
            self.assertEqual(upgraded, original)

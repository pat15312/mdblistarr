"""Explicit title recovery must be independent of Arr lifecycle operations."""
from contextlib import ExitStack
from unittest.mock import Mock, patch

from django.db import DatabaseError
from django.db.models.query import QuerySet
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from . import test_arr_health
from .arr import SonarrAPI, RadarrAPI
from .media_display import recover_missing_titles
from .models import Preferences, SonarrEpisodeSearchCandidate, RadarrMovieSearchCommand


class TitleRecoveryTests(TestCase):
    setUp = test_arr_health.TargetScopedMetricsAndViewTests.setUp
    _candidate = test_arr_health.TargetScopedMetricsAndViewTests._candidate
    _cleanup_candidate = test_arr_health.TargetScopedMetricsAndViewTests._cleanup_candidate

    def _row(self, product, number=1, **overrides):
        return self._cleanup_candidate(product, number, 'deleted',
            **{'tvdb_id' if product == 'sonarr' else 'tmdb_id': number, **overrides})

    def _url(self, product, refresh=True, section='cleanup', metric='deleted'):
        return reverse('arr_health_refresh_titles' if refresh else 'arr_health_details_view',
                       args=[product, section, metric])

    def _payload(self, product, number=1, title='Recovered title'):
        # Catalogue results need not be in the library and can have local id=0.
        return [{'id': 0, 'tvdbId' if product == 'sonarr' else 'tmdbId': number, 'title': title}]

    def test_deleted_history_recovered_without_reconciliation_or_library_records(self):
        self.client.force_login(self.staff)
        for product in ('sonarr', 'radarr'):
            Preferences.set_value(f'{product}_reconciliation_enabled', '0')
            rows = [self._row(product, 1), self._row(product, 2,
                **{'tvdb_id' if product == 'sonarr' else 'tmdb_id': 1})]
            search = self._candidate(product, 1, 'failed'); search.save(); rows.append(search)
            old = self._row(product, 3, target_instance=self.s_source if product == 'sonarr' else self.r_source)
            originals = [type(row).objects.values().get(pk=row.pk) for row in rows]
            with patch(f'mdblistrr.arr.{product.title()}API') as factory:
                api = factory.return_value
                api.lookup_display_metadata.return_value = self._payload(product)
                response = self.client.post(self._url(product), {'page': 1},
                    HTTP_X_REQUESTED_WITH='XMLHttpRequest')
                self.assertEqual([call[0] for call in api.mock_calls], ['lookup_display_metadata'])
                api.lookup_display_metadata.assert_called_once_with(1)
            self.assertContains(response, 'Recovered title')
            self.assertContains(response, 'updated 3 local record(s)')
            self.assertNotContains(response, '(title unavailable)')
            for row, original in zip(rows, originals):
                current = type(row).objects.values().get(pk=row.pk)
                self.assertEqual(current.pop('target_title'), 'Recovered title')
                original.pop('target_title'); self.assertEqual(current, original)
            old.refresh_from_db(); self.assertEqual(old.target_title, '')

    def test_invalid_ambiguous_or_missing_metadata_never_supplies_a_title(self):
        for product, target in (('sonarr', self.s_target), ('radarr', self.r_target)):
            row = self._row(product)
            bad = [[], None, {}, self._payload(product, 99), self._payload(product, True),
                   self._payload(product, title='  '), self._payload(product) * 2,
                   [{**self._payload(product)[0], 'errorMessage': 'synthetic error'}]]
            for payload in bad:
                with self.subTest(product=product, payload=payload), patch(
                        f'mdblistrr.arr.{product.title()}API') as factory:
                    factory.return_value.lookup_display_metadata.return_value = payload
                    result = recover_missing_titles(product, target.id, [1])
                    self.assertEqual(result['unresolved'], 1)
                row.refresh_from_db(); self.assertEqual(row.target_title, '')

    def test_failed_requests_stop_and_report_failure_without_raw_errors(self):
        self.client.force_login(self.staff)
        self._row('sonarr'); self._row('sonarr', 2)
        with patch('mdblistrr.arr.SonarrAPI') as factory:
            factory.return_value.lookup_display_metadata.side_effect = RuntimeError('PRIVATE_URL SECRET')
            response = self.client.post(self._url('sonarr'), HTTP_X_REQUESTED_WITH='XMLHttpRequest')
            self.assertEqual(factory.return_value.lookup_display_metadata.call_count, 1)
        self.assertContains(response, 'Failed: 1. Deferred: 1.')
        self.assertNotContains(response, 'PRIVATE_URL')
        self.assertNotContains(response, 'SECRET')

    def test_database_failure_rolls_back_both_models_preserving_lifecycle(self):
        row = self._row('sonarr')
        search = self._candidate('sonarr', 1); search.save()
        update = QuerySet.update

        def fail_search(queryset, **kwargs):
            if queryset.model is SonarrEpisodeSearchCandidate:
                raise DatabaseError('private failure')
            return update(queryset, **kwargs)

        with patch('mdblistrr.arr.SonarrAPI') as factory, patch.object(QuerySet, 'update', fail_search):
            factory.return_value.lookup_display_metadata.return_value = self._payload('sonarr')
            result = recover_missing_titles('sonarr', self.s_target.id, [1])
        self.assertEqual(result['failed'], 1)
        self.assertEqual(result['updated'], 0)
        row.refresh_from_db(); search.refresh_from_db()
        self.assertEqual((row.target_title, search.target_title), ('', ''))

    def test_page_scope_ignores_submitted_ids_and_refresh_keeps_pagination_working(self):
        self.client.force_login(self.staff)
        rows = [self._row('radarr', number) for number in range(1, 28)]
        with patch('mdblistrr.arr.RadarrAPI') as factory:
            factory.return_value.lookup_display_metadata.side_effect = lambda number: self._payload('radarr', number)
            response = self.client.post(self._url('radarr'), {'page': 2, 'external_ids': '1'},
                HTTP_X_REQUESTED_WITH='XMLHttpRequest')
            self.assertEqual([call.args[0] for call in factory.return_value.lookup_display_metadata.call_args_list], [26, 27])
        rows[0].refresh_from_db(); self.assertEqual(rows[0].target_title, '')
        self.assertContains(response, self._url('radarr', False) + '?page=1')
        self.assertNotContains(response, '/titles?page=')
        self.assertContains(response, 'Page 2 of 2')
        response = self.client.get(self._url('radarr', False), {'page': 2, 'title_after': 26})
        self.assertNotContains(response, 'title_after=26&amp;page=1')
        self.assertContains(response, self._url('radarr', False) + '?page=1')

    def test_limits_deduplication_and_existing_title_preservation(self):
        row = self._row('sonarr', target_title='Original name')
        with patch('mdblistrr.arr.SonarrAPI') as factory:
            factory.return_value.lookup_display_metadata.side_effect = lambda number: self._payload('sonarr', number)
            result = recover_missing_titles('sonarr', self.s_target.id, list(range(1, 31)) * 2)
            self.assertEqual(factory.return_value.lookup_display_metadata.call_count, 25)
            self.assertEqual(result['deferred'], 5)
        row.refresh_from_db(); self.assertEqual(row.target_title, 'Original name')
        with patch('mdblistrr.arr.SonarrAPI') as factory, patch(
                'mdblistrr.media_display.time.monotonic', side_effect=[0, 21]):
            result = recover_missing_titles('sonarr', self.s_target.id, [1, 2])
            factory.return_value.lookup_display_metadata.assert_not_called()
            self.assertEqual(result['deferred'], 2)

    def test_command_titles_continue_past_unresolved_ids_in_a_bounded_batch(self):
        self.client.force_login(self.staff)
        command = RadarrMovieSearchCommand.objects.create(target_instance=self.r_target,
            status='queued', submission_attempted_at=timezone.now())
        for number in range(1, 31):
            candidate = self._candidate('radarr', number); candidate.save()
            command.candidates.through.objects.create(command=command, candidate=candidate,
                target_movie_id=number)
        original = RadarrMovieSearchCommand.objects.values().get(pk=command.pk)
        url = self._url('radarr', section='search', metric='in_flight')
        with patch('mdblistrr.arr.RadarrAPI') as factory:
            factory.return_value.lookup_display_metadata.return_value = []
            response = self.client.post(url, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
            self.assertContains(response, 'Unresolved: 25. Failed: 0. Deferred: 5.')
            self.assertEqual(response.context['title_refresh_after'], 25)
            factory.return_value.lookup_display_metadata.reset_mock()
            factory.return_value.lookup_display_metadata.side_effect = lambda number: self._payload('radarr', number)
            response = self.client.post(url, {'after': 25}, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
            self.assertEqual([call.args[0] for call in factory.return_value.lookup_display_metadata.call_args_list], list(range(26, 31)))
            self.assertContains(response, 'Recovered 5 title(s)')
            self.assertEqual(response.context['title_refresh_after'], 0)
            self.assertEqual(response.context['page_obj'].paginator.count, 1)
        self.assertEqual(RadarrMovieSearchCommand.objects.values().get(pk=command.pk), original)

    def test_auth_csrf_method_and_invalid_configuration_prevent_requests(self):
        self._row('sonarr')
        url = self._url('sonarr')
        with patch('mdblistrr.arr.SonarrAPI') as factory:
            self.assertIn(self.client.post(url).status_code, (302, 401))
            self.client.force_login(self.nonstaff)
            self.assertIn(self.client.post(url).status_code, (302, 403))
            self.client.force_login(self.staff)
            self.assertEqual(self.client.get(url).status_code, 405)
            csrf = Client(enforce_csrf_checks=True); csrf.force_login(self.staff)
            self.assertEqual(csrf.post(url).status_code, 403)
            self.assertEqual(self.client.post(self._url('sonarr', metric='invalid')).status_code, 404)
            Preferences.set_value('sonarr_reconciliation_target_id', str(self.s_source.id))
            self.assertEqual(self.client.post(url).status_code, 302)
            factory.assert_not_called()

    def test_rendering_remains_network_free_and_no_js_refresh_reports_result(self):
        self.client.force_login(self.staff)
        self._row('sonarr')
        with ExitStack() as stack:
            guards = [stack.enter_context(patch(path, side_effect=AssertionError('No network')))
                      for path in ('mdblistrr.arr.SonarrAPI', 'mdblistrr.arr.RadarrAPI',
                                   'requests.sessions.Session.request', 'socket.create_connection')]
            self.assertContains(self.client.get(self._url('sonarr', False)), 'Refresh missing titles')
            self.assertEqual(self.client.get(reverse('arr_health_view')).status_code, 200)
            for guard in guards:
                guard.assert_not_called()
        with patch('mdblistrr.arr.SonarrAPI') as factory:
            factory.return_value.lookup_display_metadata.return_value = self._payload('sonarr', title='<b>Example</b> apikey=synthetic-secret')
            response = self.client.post(self._url('sonarr'), follow=True)
        self.assertContains(response, 'Recovered 1 title(s)')
        self.assertContains(response, '&lt;b&gt;Example&lt;/b&gt;')
        self.assertNotContains(response, 'synthetic-secret')
        self.assertNotContains(response, 'Refresh missing titles')

    def test_catalogue_transport_uses_only_bounded_get_and_checks_http_status(self):
        for api_class, path, params in (
                (SonarrAPI, '/series/lookup', {'term': 'tvdb:1'}),
                (RadarrAPI, '/movie/lookup/tmdb', {'tmdbId': 1})):
            api = api_class(url='https://arr.example/base', apikey='fake-key')
            with patch.object(api.connect.session, 'get') as get:
                get.return_value = Mock(status_code=200)
                data = {'title': 'Catalogue title', 'tvdbId': 1, 'tmdbId': 1, 'id': 0}
                get.return_value.json.return_value = [data] if api_class is SonarrAPI else data
                self.assertEqual(api.lookup_display_metadata(1), [data])
                self.assertEqual(get.call_args.args, ('https://arr.example/base/api/v3' + path,))
                self.assertEqual(get.call_args.kwargs['params'], params)
                self.assertEqual(get.call_args.kwargs['timeout'], (3, 5))
                self.assertFalse(get.call_args.kwargs['allow_redirects'])
                get.return_value.status_code = 401
                self.assertEqual(api.lookup_display_metadata(1), {'error': 'metadata_request_failed'})
                get.side_effect = RuntimeError('private error')
                self.assertEqual(api.lookup_display_metadata(1), {'error': 'metadata_request_failed'})
                get.reset_mock()
                api.lookup_display_metadata(True)
                get.assert_not_called()

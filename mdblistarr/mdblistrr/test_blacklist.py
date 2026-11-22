import copy
import os
from datetime import timedelta
from unittest.mock import Mock, patch, call

os.environ.setdefault('MDBLISTARR_ENCRYPTION_KEY', 'MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=')

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase, Client
from django.urls import reverse
from django.utils import timezone

from .blacklist import (BlacklistError, BlacklistEvidence, ListClient, list_path,
    add_source, refresh_blacklists, configuration_signature)
from .models import (BlacklistSource, Preferences, TitleCleanupCandidate, TitleCleanupEvent,
    SonarrInstance, RadarrInstance, SonarrCleanupCandidate, SonarrEpisodeSearchCommand)
from .title_cleanup import maintain_candidates, process_title_cleanup
from .sonarr_reconcile import calculate_episode_monitoring
from .radarr_reconcile import calculate_movie_monitoring
from .health_details import build_health_details
from .arr_health import build_arr_health


def item(media='movie', tmdb=100, tvdb=None):
    return {'id': tmdb, 'mediatype': media, 'tvdb_id': tvdb,
            'ids': {'tmdb': tmdb, 'tvdb': tvdb}}


def page(movies=(), shows=(), more=False, total=None):
    rows = {'movies': list(movies), 'shows': list(shows)}
    headers = {'X-Has-More': str(more).lower(),
               'X-Total-Items': str(total if total is not None else len(movies) + len(shows))}
    return rows, headers


class ListParsingTests(SimpleTestCase):
    def setUp(self):
        self.client = ListClient(api=Mock())

    def test_supported_references_and_no_arbitrary_network_destinations(self):
        self.assertEqual(list_path('123'), '/lists/123')
        self.assertEqual(list_path('https://mdblist.com/lists/example/exclusions/'), '/lists/example/exclusions')
        self.assertEqual(list_path('https://mdblist.com/lists/share/' + 'a'*32), '/lists/share/' + 'a'*32)
        for value in ('0', '-1', 'https://evil.test/lists/1', 'http://mdblist.com/lists/1',
                      'https://mdblist.com@evil.test/lists/1', 'https://mdblist.com/lists/1?apikey=x',
                      'https://mdblist.com:443/lists/1', 'https://mdblist.com/lists/../user'):
            with self.subTest(value=value), self.assertRaises(BlacklistError):
                list_path(value)

    def test_complete_mixed_membership_deduplicates_by_typed_identity(self):
        self.client.get = Mock(return_value=page([item(), item()], [item('show', 100, 200)], total=3))
        members = self.client.membership('/lists/1', 3)
        self.assertEqual(members, [{'media': 'movie', 'tmdb': 100, 'tvdb': None},
                                   {'media': 'show', 'tmdb': 100, 'tvdb': 200}])

    def test_cursor_and_legacy_pagination(self):
        first = page([item()], more=True, total=2)
        first[0]['pagination'] = {'next_cursor': 'next-page'}
        self.client.get = Mock(side_effect=[first, page([item(tmdb=101)], total=2)])
        self.assertEqual(len(self.client.membership('/lists/1', 2)), 2)
        self.assertEqual(self.client.get.call_args.args[1]['cursor'], 'next-page')
        self.client.get = Mock(side_effect=[page([item()], more=True, total=2), page([item(tmdb=101)], total=2)])
        self.assertEqual(len(self.client.membership('/lists/1', 2)), 2)
        self.assertEqual(self.client.get.call_args.args[1]['offset'], 1)

    def test_contradictory_body_pagination_and_expired_client_fail_closed(self):
        for pagination in ({'total': 2}, {'offset': 1}, {'has_more': True},
                           {'has_more': 'false'}, {'limit': True}, {'next_cursor': 'a'}):
            response = page([item()])
            response[0]['pagination'] = pagination
            with self.subTest(pagination=pagination), self.assertRaises(BlacklistError):
                self.client.get = Mock(return_value=response)
                self.client.membership('/lists/1', 1)
        api = Mock()
        client = ListClient(api)
        client.deadline = 0
        with self.assertRaisesRegex(BlacklistError, 'time budget'):
            client.get('/lists/1')
        api._auth.assert_not_called()

    def test_incomplete_malformed_and_conflicting_memberships_fail_closed(self):
        cases = [({}, {}), page([], total=1), page([{'id': 1}], total=1),
                 page([dict(item(), ids={'tmdb': 999})], total=1),
                 page([], [item('show', 100, True)], total=1)]
        for response in cases:
            with self.subTest(response=response), self.assertRaises(BlacklistError):
                self.client.get = Mock(return_value=response)
                self.client.membership('/lists/1', 1)

    def test_show_translation_requires_exact_provider_ids_and_type(self):
        self.client.get = Mock(return_value=({'type': 'show', 'ids': {'tmdb': 100, 'tvdb': 200}}, {}))
        self.assertEqual(self.client.identity(item('show'), 'show')['tvdb'], 200)
        self.client.translations.clear()
        self.client.get = Mock(return_value=({'type': 'show', 'ids': {'tmdb': 101, 'tvdb': 200}}, {}))
        with self.assertRaises(BlacklistError):
            self.client.identity(item('show'), 'show')

    def test_same_count_edit_between_reads_is_not_a_snapshot(self):
        self.client.metadata = Mock(return_value=[{'id': 1, 'items': 1, 'name': 'Example'}])
        self.client.membership = Mock(side_effect=[[{'tmdb': 1}], [{'tmdb': 2}]])
        with self.assertRaises(BlacklistError):
            self.client.snapshot('/lists/1', 1)

    def test_authentication_and_redirects_are_bounded_and_errors_do_not_expose_tokens(self):
        for oauth in (False, True):
            with self.subTest(oauth=oauth):
                api = Mock()
                api._auth.return_value = {'headers': {'Authorization': 'Bearer synthetic'} if oauth else None,
                                         'params': None if oauth else {'apikey': 'synthetic'}}
                response = Mock(status_code=200, headers={})
                response.json.return_value = {'ok': True}
                api.connect.session.get.return_value = response
                ListClient(api).get('/lists/share/' + 'a'*32)
                args = api.connect.session.get.call_args.kwargs
                self.assertFalse(args['allow_redirects'])
                self.assertEqual(args['timeout'], (5, 20))
                self.assertEqual(args['params'], {} if oauth else {'apikey': 'synthetic'})
                response.status_code = 401
                with self.assertRaisesRegex(BlacklistError, 'authentication'):
                    ListClient(api).get('/lists/1')
                response.status_code = 302
                with self.assertRaises(BlacklistError):
                    ListClient(api).get('/lists/1')


class SourcePersistenceTests(TestCase):
    def test_numeric_identity_and_cross_source_conflicts_never_replace_good_snapshot(self):
        first = BlacklistSource.objects.create(list_id=1, name='Original', api_path='/lists/1',
            snapshot=[{'media': 'show', 'tmdb': 100, 'tvdb': 200}])
        for conflicting in ({'media': 'show', 'tmdb': 100, 'tvdb': 201},
                            {'media': 'show', 'tmdb': 101, 'tvdb': 200}):
            with patch('mdblistrr.blacklist.ListClient') as cls:
                cls.return_value.metadata.return_value = [{'id': 2, 'items': 1, 'name': 'Other'}]
                cls.return_value.snapshot.return_value = ('Other', [conflicting])
                with self.assertRaisesRegex(BlacklistError, 'different list identity'):
                    add_source('3')
                with self.assertRaisesRegex(BlacklistError, 'disagree'):
                    add_source('2')
                self.assertEqual(BlacklistSource.objects.count(), 1)
                first.refresh_from_db()
                self.assertEqual(first.snapshot[0]['tvdb'], 200)

    def test_freshness_is_required_for_deletion_without_expiring_long_reconciliation(self):
        evidence = BlacklistEvidence(configuration_signature(), {}, timezone.now() - timedelta(hours=1))
        self.assertTrue(evidence.current())
        self.assertFalse(evidence.fresh())
        BlacklistSource.objects.create(list_id=1, name='New', api_path='/lists/1')
        self.assertFalse(evidence.current())

    def test_unlisted_reference_is_encrypted_and_failed_refresh_retains_snapshot(self):
        path = '/lists/share/' + 'a'*32
        with patch('mdblistrr.blacklist.ListClient') as cls:
            cls.return_value.metadata.return_value = [{'id': 1, 'items': 1, 'name': 'Synthetic list'}]
            cls.return_value.snapshot.return_value = ('Synthetic list', [{'media': 'movie', 'tmdb': 100, 'tvdb': None}])
            add_source('https://mdblist.com' + path)
            row = BlacklistSource.objects.get()
            self.assertEqual(row.api_path, path)
            from django.db import connection
            with connection.cursor() as cursor:
                cursor.execute('SELECT api_path FROM mdblistrr_blacklistsource')
                self.assertNotIn(path, cursor.fetchone()[0])
            previous = row.snapshot, row.last_success_at
            cls.return_value.snapshot.side_effect = BlacklistError('Unavailable evidence.')
            with self.assertRaises(BlacklistError):
                refresh_blacklists()
            row.refresh_from_db()
            self.assertEqual((row.snapshot, row.last_success_at), previous)
            self.assertEqual(row.last_error, 'Unavailable evidence.')

    def test_multiple_sources_deduplicate_and_removed_configuration_invalidates_evidence(self):
        for i in (1, 2):
            BlacklistSource.objects.create(list_id=i, name=str(i), api_path=f'/lists/{i}')
        with patch('mdblistrr.blacklist.ListClient') as cls:
            cls.return_value.snapshot.return_value = ('Synthetic', [{'media': 'movie', 'tmdb': 100, 'tvdb': None}])
            evidence = refresh_blacklists()
        self.assertEqual(evidence.members, {('radarr', 100): [1, 2]})
        BlacklistSource.objects.filter(list_id=1).delete()
        self.assertFalse(evidence.current())


class NativeArr:
    """Stateful synthetic Arr: proves durable transitions, not real filesystem behaviour."""
    def __init__(self, product, with_file=True):
        self.product = product
        self.resource = 'series' if product == 'sonarr' else 'movie'
        self.file_resource = 'episodefile' if product == 'sonarr' else 'moviefile'
        self.row = {'id': 10, 'tvdbId': 200, 'tmdbId': 100, 'monitored': False,
                    'path': '/media/synthetic', 'added': '2026-01-01T00:00:00Z', 'title': 'Synthetic title',
                    'hasFile': with_file, 'isAvailable': True, 'seasons': [{'seasonNumber': 1, 'monitored': False}]}
        self.files = ([{'id': 20, 'seriesId': 10, 'movieId': 10, 'path': '/media/synthetic/file.mkv',
                       'size': 100, 'dateAdded': '2026-01-02T00:00:00Z'}] if with_file else [])
        self.deleted = []
        self.commands = []
        self.queue = []
        self.config = {'deleteEmptyFolders': False, 'recycleBin': ''}
        self.uncertain = False
        self.fail_reads = False
        self.before_delete = None

    def cleanup_request(self, method, resource, params=None, payload=None):
        if method == 'DELETE':
            if self.before_delete:
                self.before_delete(resource)
            self.deleted.append((resource, params))
            if self.uncertain:
                return {'error': 'request_uncertain'}
            if resource.startswith(self.file_resource + '/'):
                self.files = [row for row in self.files if row['id'] != int(resource.split('/')[-1])]
            else:
                self.row = None
            return {'status_code': 200, 'data': None}
        if self.fail_reads:
            return {'status_code': 503}
        if resource == self.resource:
            data = [self.row] if self.row else []
        elif resource == f'{self.resource}/10':
            data = self.row
        elif resource == self.file_resource:
            data = self.files
        elif resource.startswith(self.file_resource + '/'):
            data = next((row for row in self.files if row['id'] == int(resource.split('/')[-1])), None)
        elif resource == 'episode':
            data = [{'id': 30, 'seriesId': 10, 'hasFile': bool(self.files), 'monitored': False,
                     'episodeFileId': self.files[0]['id'] if self.files else 0}]
        elif resource == 'command':
            data = self.commands
        elif resource == 'queue':
            data = {'page': params['page'], 'pageSize': 100, 'totalRecords': len(self.queue), 'records': self.queue}
        elif resource == 'config/mediamanagement':
            data = self.config
        else:
            raise AssertionError(resource)
        return {'status_code': 404 if data is None else 200, 'data': copy.deepcopy(data)}


class TitleLifecycleTests(TestCase):
    def setup_product(self, product='sonarr', with_file=True, grace=0):
        model = SonarrInstance if product == 'sonarr' else RadarrInstance
        self.target = model.objects.create(name='Target', url='http://target.invalid', apikey='target-key',
                                          is_library_source=False, is_ondemand_target=True)
        Preferences.set_value(product + '_reconciliation_target_id', str(self.target.pk))
        Preferences.set_value('blacklist_cleanup_enabled', '1')
        Preferences.set_value('blacklist_cleanup_dry_run', '0')
        Preferences.set_value('blacklist_cleanup_grace_hours', str(grace))
        self.api = NativeArr(product, with_file)
        self.product = product
        external_id = 200 if product == 'sonarr' else 100
        self.membership = BlacklistEvidence(configuration_signature(), {(product, external_id): [1]}, timezone.now())
        maintain_candidates(product, self.target, [self.api.row], self.membership, grace)
        return TitleCleanupCandidate.objects.get()

    def process(self, budget=25):
        with patch('mdblistrr.title_cleanup.refresh_blacklists', return_value=self.membership):
            return process_title_cleanup(product=self.product, target=self.target, api=self.api,
                membership=self.membership, remaining_budget=budget)

    def test_configuration_and_native_identity_rechecked_after_lock_wait(self):
        from contextlib import contextmanager
        for product in ('sonarr', 'radarr'):
            for mutation in ('disable', 'dry_run', 'grace', 'file', 'activity'):
                with self.subTest(product=product, mutation=mutation):
                    candidate = self.setup_product(product)
                    @contextmanager
                    def concurrent_change():
                        if mutation == 'disable':
                            Preferences.set_value('blacklist_cleanup_enabled', '0')
                        elif mutation == 'dry_run':
                            Preferences.set_value('blacklist_cleanup_dry_run', '1')
                        elif mutation == 'grace':
                            Preferences.set_value('blacklist_cleanup_grace_hours', '24')
                        elif mutation == 'file':
                            self.api.files[0]['path'] = '/media/synthetic/replacement.mkv'
                        else:
                            self.api.commands = [{'id': 7, 'status': 'started'}]
                        yield
                    with patch('mdblistrr.title_cleanup.configuration_lock', concurrent_change):
                        self.assertEqual(self.process().attempts, 0)
                    self.assertEqual(self.api.deleted, [])
                    self.assertFalse(candidate.events.filter(phase__in=('files_submitting', 'title_submitting')).exists())
                    TitleCleanupEvent.objects.all().delete()
                    TitleCleanupCandidate.objects.all().delete()
                    self.target.delete()

    def test_changed_files_restart_grace_before_any_submission(self):
        candidate = self.setup_product(grace=24)
        self.process()
        original_start = candidate.first_eligible_at
        self.api.files[0]['size'] += 1
        self.process()
        candidate.refresh_from_db()
        self.assertGreater(candidate.first_eligible_at, original_start)
        self.assertEqual(candidate.status, 'pending')
        self.assertEqual(candidate.evidence['files'][0]['size'], 101)
        self.assertEqual(self.api.deleted, [])

    def test_stale_revalidation_and_persisted_unresolved_search_block_removal(self):
        candidate = self.setup_product()
        self.membership.verified_at -= timedelta(hours=1)
        self.assertEqual(self.process().attempts, 0)
        candidate.refresh_from_db()
        self.assertIn('configuration changed', candidate.last_error.lower())
        self.membership.verified_at = timezone.now()
        SonarrEpisodeSearchCommand.objects.create(target_instance=self.target, target_series_id=10,
            status='ambiguous', submission_attempted_at=timezone.now())
        self.assertEqual(self.process().attempts, 0)
        candidate.refresh_from_db()
        self.assertIn('unresolved', candidate.last_error)

    def test_alias_of_permanent_source_is_never_written(self):
        self.setup_product()
        SonarrInstance.objects.create(name='Permanent', url='http://other.invalid',
            apikey=self.target.apikey, is_library_source=True)
        self.assertEqual(self.process().attempts, 0)
        self.assertEqual(self.api.deleted, [])

    def test_sonarr_whole_series_and_radarr_whole_movie_removal(self):
        for product in ('sonarr', 'radarr'):
            with self.subTest(product=product):
                candidate = self.setup_product(product)
                def durable(resource):
                    candidate.refresh_from_db()
                    self.assertIn(candidate.status, ('files_submitting', 'title_submitting'))
                    self.assertTrue(candidate.events.filter(phase=candidate.status).exists())
                self.api.before_delete = durable
                self.assertEqual(self.process().attempts, 2)
                candidate.refresh_from_db()
                self.assertEqual(candidate.status, 'deleted')
                self.assertIsNotNone(candidate.deleted_at)
                self.assertEqual(self.api.deleted[-1][1], {'deleteFiles': 'false', 'addImportExclusion': 'false'})
                self.assertIsNone(self.api.row)
                TitleCleanupEvent.objects.all().delete()
                TitleCleanupCandidate.objects.all().delete()

    def test_disabled_dry_run_grace_and_shared_budget(self):
        candidate = self.setup_product(grace=24)
        self.assertEqual(self.process().attempts, 0)
        candidate.first_eligible_at -= timedelta(hours=25)
        candidate.save()
        maintain_candidates(self.product, self.target, [self.api.row], self.membership, 24)
        Preferences.set_value('blacklist_cleanup_dry_run', '1')
        self.assertEqual(self.process().attempts, 0)
        Preferences.set_value('blacklist_cleanup_dry_run', '0')
        Preferences.set_value('blacklist_cleanup_enabled', '0')
        self.assertEqual(self.process().attempts, 0)
        Preferences.set_value('blacklist_cleanup_enabled', '1')
        self.assertEqual(self.process(budget=0).attempts, 0)
        self.assertEqual(self.process(budget=1).attempts, 1)
        candidate.refresh_from_db()
        self.assertEqual(candidate.status, 'files_removed')
        self.assertIsNotNone(self.api.row)
        self.assertEqual(self.process(budget=1).attempts, 1)

    def test_uncertain_file_request_survives_restart_and_is_never_repeated(self):
        candidate = self.setup_product()
        self.api.uncertain = True
        self.assertTrue(self.process().stop)
        candidate.refresh_from_db()
        self.assertEqual(candidate.status, 'uncertain')
        self.assertEqual(self.process().attempts, 0)
        self.assertEqual(len(self.api.deleted), 1)
        self.api.files = []  # independent later evidence resolves the original request
        self.api.uncertain = False
        self.assertEqual(self.process().attempts, 0)
        candidate.refresh_from_db()
        self.assertEqual(candidate.status, 'files_removed')
        self.assertEqual(self.process().attempts, 1)
        self.assertEqual(len(self.api.deleted), 2)

    def test_uncertain_title_request_is_not_repeated(self):
        candidate = self.setup_product('radarr', with_file=False)
        self.api.uncertain = True
        self.assertTrue(self.process().stop)
        self.assertEqual(self.process().attempts, 0)
        self.assertEqual(len(self.api.deleted), 1)
        self.api.row = None
        self.assertEqual(self.process().attempts, 0)
        candidate.refresh_from_db()
        self.assertEqual(candidate.status, 'deleted')

    def test_changed_membership_cancels_without_restoring_partial_files(self):
        candidate = self.setup_product()
        self.assertEqual(self.process(budget=1).attempts, 1)
        empty = BlacklistEvidence(configuration_signature(), {}, timezone.now())
        maintain_candidates(self.product, self.target, [self.api.row], empty, 0)
        candidate.refresh_from_db()
        self.assertEqual(candidate.status, 'cancelled')
        self.assertEqual(self.process().attempts, 0)
        self.assertIsNotNone(self.api.row)
        self.assertEqual(self.api.files, [])

    def test_network_and_activity_and_identity_changes_block_every_destructive_stage(self):
        for product in ('sonarr', 'radarr'):
            for condition in ('network', 'queue', 'command', 'path', 'file', 'monitor', 'empty_folders', 'recycle_bin', 'permanent'):
                with self.subTest(product=product, condition=condition):
                    candidate = self.setup_product(product)
                    if condition == 'network': self.api.fail_reads = True
                    if condition == 'queue': self.api.queue = [{'id': 1, 'seriesId': 10, 'movieId': 10}]
                    if condition == 'command': self.api.commands = [{'id': 1, 'status': 'started'}]
                    if condition == 'path': self.api.row['path'] = '/media/replaced'
                    if condition == 'file': self.api.files[0]['movieId'] = self.api.files[0]['seriesId'] = 999
                    if condition == 'monitor': self.api.row['monitored'] = True
                    if condition == 'empty_folders': self.api.config['deleteEmptyFolders'] = True
                    if condition == 'recycle_bin': self.api.config['recycleBin'] = '/recycle'
                    if condition == 'permanent':
                        self.target.is_library_source = True
                        self.target.save()
                    self.assertEqual(self.process().attempts, 0)
                    self.assertEqual(self.api.deleted, [])
                    candidate.refresh_from_db()
                    self.assertTrue(candidate.last_error)
                    TitleCleanupEvent.objects.all().delete()
                    TitleCleanupCandidate.objects.all().delete()
                    SonarrInstance.objects.all().delete()
                    RadarrInstance.objects.all().delete()

    def test_membership_revalidation_failure_and_removal_block_delete(self):
        candidate = self.setup_product()
        for response in (BlacklistError('Unavailable'), BlacklistEvidence(configuration_signature(), {}, timezone.now())):
            with patch('mdblistrr.title_cleanup.refresh_blacklists', side_effect=response if isinstance(response, Exception) else None,
                       return_value=response):
                result = process_title_cleanup(product=self.product, target=self.target, api=self.api,
                    membership=self.membership, remaining_budget=25)
                self.assertEqual(result.attempts, 0)
        candidate.refresh_from_db()
        self.assertEqual(candidate.status, 'cancelled')


class BlacklistMonitoringTests(SimpleTestCase):
    def test_blacklist_overrides_target_only_series_and_movies(self):
        episode = {'id': 1, 'seasonNumber': 1, 'episodeNumber': 1, 'airDateUtc': '2020-01-01T00:00:00Z',
                   'hasFile': False, 'monitored': True}
        result = calculate_episode_monitoring([], [episode], search_newly_eligible=True, blacklisted=True)
        self.assertEqual(result.monitor_true_ids, [])
        self.assertEqual(result.monitor_false_ids, [1])
        self.assertEqual(result.search_ids, [])
        self.assertEqual(result.wanted_missing_episode_ids, [])
        self.assertFalse(result.desired_series_monitoring)
        self.assertEqual(result.reason_by_key, {(1, 1): 'blacklisted'})
        movie = {'id': 2, 'tmdbId': 100, 'hasFile': False, 'isAvailable': True, 'monitored': True}
        self.assertEqual(calculate_movie_monitoring([], [movie], {100}).monitor_false_ids, [2])
        self.assertEqual(calculate_movie_monitoring([], [dict(movie, monitored=False)], set()).monitor_true_ids, [2])


class BlacklistUITests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_superuser('admin', 'test@example.invalid', 'Synthetic-test-password-123!')
        self.client.force_login(self.admin)

    def test_configuration_is_staff_only_post_and_csrf_protected(self):
        url = reverse('configure_blacklist')
        self.assertEqual(self.client.get(url).status_code, 405)
        csrf = Client(enforce_csrf_checks=True)
        csrf.force_login(self.admin)
        self.assertEqual(csrf.post(url, {'action': 'settings'}).status_code, 403)
        self.client.logout()
        self.assertIn(self.client.post(url, {'action': 'settings'}).status_code, (302, 401))

    def test_blacklist_tab_defaults_and_configuration_persist(self):
        response = self.client.get(reverse('home_view'))
        self.assertContains(response, 'id="blacklist-tab"')
        self.client.post(reverse('configure_blacklist'), {'action': 'settings', 'dry_run': 'on', 'grace_hours': '24'})
        self.assertEqual(Preferences.get_value('blacklist_cleanup_enabled'), '0')
        self.assertEqual(Preferences.get_value('blacklist_cleanup_dry_run'), '1')
        self.assertEqual(self.client.session['active_tab'], 'blacklist')

    def test_unified_history_preserves_file_totals_and_labels_title_actions_without_network(self):
        target = SonarrInstance.objects.create(name='Target', url='http://target.invalid', apikey='key', is_ondemand_target=True)
        Preferences.set_value('sonarr_reconciliation_target_id', str(target.pk))
        now = timezone.now()
        SonarrCleanupCandidate.objects.create(target_instance=target, tvdb_id=200, target_series_id=10,
            episode_file_id=20, first_eligible_at=now, last_confirmed_at=now, status='deleted', deleted_at=now)
        TitleCleanupCandidate.objects.create(product='sonarr', target_instance_id=target.pk, target_fingerprint='a'*64,
            target_media_id=11, external_id=201, target_title='Preserved deleted title', first_eligible_at=now,
            last_confirmed_at=now, status='deleted', deleted_at=now)
        with patch('requests.sessions.Session.request', side_effect=AssertionError('Health must not use network')):
            details = build_health_details('sonarr', 'cleanup', 'deleted')
            self.assertEqual(details['page_obj'].paginator.count, 2)
            self.assertEqual({e['reason'] for e in details['page_obj']}, {'Permanent duplicate', 'Blacklisted'})
            self.assertEqual({e['action'] for e in details['page_obj']}, {'Episode file removed', 'Entire television series removed'})
            response = self.client.get(reverse('arr_health_view'))
            self.assertContains(response, '1 deleted. Whole-title candidates:')
            self.assertContains(self.client.get(reverse('arr_health_details_view', args=['sonarr','cleanup','deleted'])), 'Preserved deleted title')


class ReconciliationIntegrationTests(TestCase):
    def test_target_only_blacklist_suppresses_monitoring_search_and_duplicate_deletion_for_both_products(self):
        import tempfile
        from .cron import reconcile_sonarr_ondemand, reconcile_radarr_ondemand
        from .models import SonarrEpisodeSearchCandidate, RadarrMovieSearchCandidate
        for product, model, reconcile in (
                ('sonarr', SonarrInstance, reconcile_sonarr_ondemand),
                ('radarr', RadarrInstance, reconcile_radarr_ondemand)):
            with self.subTest(product=product):
                source = model.objects.create(name='Permanent', url='http://source.invalid',
                    apikey='source-key', is_library_source=True)
                target = model.objects.create(name='On Demand', url='http://target.invalid',
                    apikey='target-key', is_ondemand_target=True, is_library_source=False)
                for suffix, value in {'enabled': '1', 'source_id': source.pk, 'target_id': target.pk}.items():
                    Preferences.set_value(f'{product}_reconciliation_{suffix}', str(value))
                Preferences.set_value(product + '_search_newly_eligible', '1')
                Preferences.set_value(product + '_cleanup_enabled', '1')
                Preferences.set_value(product + '_cleanup_dry_run', '0')
                Preferences.set_value('blacklist_cleanup_enabled', '0')
                Preferences.set_value('blacklist_cleanup_dry_run', '1')
                native = NativeArr(product, with_file=False)
                native.row['monitored'] = True
                native.row['seasons'][0]['monitored'] = True
                source_api, target_api = Mock(), Mock()
                source_api.get_series.return_value = source_api.get_movies.return_value = []
                target_api.get_series.side_effect = target_api.get_movies.side_effect = lambda: [copy.deepcopy(native.row)]
                episode = {'id': 30, 'seriesId': 10, 'seasonNumber': 1, 'episodeNumber': 1,
                    'hasFile': False, 'monitored': True, 'airDateUtc': '2020-01-01T00:00:00Z',
                    'episodeFileId': 0, 'lastSearchTime': None}
                target_api.get_episodes.side_effect = lambda *args: [copy.deepcopy(episode)]
                def episodes(ids, monitored):
                    self.assertFalse(monitored)
                    episode['monitored'] = monitored
                    return {'status_code': 202}
                def title(ids, monitored):
                    self.assertFalse(monitored)
                    native.row['monitored'] = monitored
                    return {'status_code': 202}
                target_api.put_episode_monitor.side_effect = episodes
                target_api.put_movie_monitor.side_effect = title
                target_api.put_series_monitor.side_effect = title
                target_api.post_seasonpass.return_value = {'status_code': 202}
                target_api.cleanup_request.side_effect = native.cleanup_request
                source_row = BlacklistSource.objects.create(list_id=1, name='Synthetic', api_path='/lists/1')
                membership = BlacklistEvidence(configuration_signature(),
                    {(product, 200 if product == 'sonarr' else 100): [1]}, timezone.now())
                with tempfile.NamedTemporaryFile() as lock, patch(
                        ('mdblistrr.cron.RECONCILE_LOCK_PATH' if product == 'sonarr' else 'mdblistrr.cron.RADARR_RECONCILE_LOCK_PATH'), lock.name), patch(
                        f'mdblistrr.cron.{product.title()}API', side_effect=[source_api, target_api]), patch(
                        'mdblistrr.cron.refresh_blacklists', return_value=membership):
                    result = reconcile(force=True)
                self.assertEqual(result['result'], 200, result)
                self.assertFalse(native.row['monitored'])
                self.assertEqual(native.deleted, [])
                target_api.trigger_episode_search.assert_not_called()
                target_api.trigger_movies_search.assert_not_called()
                target_api.delete_episode_file.assert_not_called()
                target_api.delete_movie_file.assert_not_called()
                self.assertEqual(source_api.method_calls, [call.get_series()]
                    if product == 'sonarr' else [call.get_movies()])
                self.assertEqual(TitleCleanupCandidate.objects.filter(product=product).get().status, 'pending')
                candidates = SonarrEpisodeSearchCandidate if product == 'sonarr' else RadarrMovieSearchCandidate
                self.assertFalse(candidates.objects.filter(status='pending').exists())
                source_row.delete()

    def test_known_unsent_search_keeps_pending_intent_without_ambiguous_command(self):
        from .title_cleanup import GuardedReconciliationAPI
        from .models import (SonarrEpisodeSearchCandidate, RadarrMovieSearchCandidate,
            SonarrEpisodeSearchCommand, RadarrMovieSearchCommand)
        from .sonarr_search import submit_pending_search_candidates as submit_sonarr
        from .radarr_search import submit_pending_search_candidates as submit_radarr
        for product, model, candidates, commands, submit in (
                ('sonarr', SonarrInstance, SonarrEpisodeSearchCandidate, SonarrEpisodeSearchCommand, submit_sonarr),
                ('radarr', RadarrInstance, RadarrMovieSearchCandidate, RadarrMovieSearchCommand, submit_radarr)):
            with self.subTest(product=product):
                target = model.objects.create(name='Target', url='http://target.invalid', apikey='key',
                    is_ondemand_target=True, is_library_source=False)
                Preferences.set_value(product + '_reconciliation_target_id', str(target.pk))
                evidence = BlacklistEvidence(configuration_signature(), {}, timezone.now())
                row = BlacklistSource.objects.create(list_id=1, name='Changed config', api_path='/lists/1')
                fields = {'target_series_id': 10, 'target_episode_id': 30, 'tvdb_id': 200,
                    'season_number': 1, 'episode_number': 1} if product == 'sonarr' else {'target_movie_id': 10, 'tmdb_id': 100}
                candidate = candidates.objects.create(target_instance=target, first_eligible_at=timezone.now(),
                    last_confirmed_at=timezone.now(), **fields)
                native = Mock()
                guarded = GuardedReconciliationAPI(native, product, target, evidence)
                result, _, failed = submit(target_api=guarded, target_instance=target,
                    **({'target_series_id': 10} if product == 'sonarr' else {}))
                self.assertTrue(failed)
                self.assertEqual(result['submitted'], 0)
                native.trigger_episode_search.assert_not_called()
                native.trigger_movies_search.assert_not_called()
                candidate.refresh_from_db()
                self.assertEqual(candidate.status, 'pending')
                self.assertEqual(candidate.attempt_count, 0)
                self.assertIsNone(candidate.current_command_id)
                self.assertEqual(commands.objects.get().status, 'superseded')
                self.assertEqual(commands.objects.get().failure_reason, 'request_not_sent_blacklist_guard')
                row.delete()


class SecretValidationTests(TestCase):
    def test_corrupt_encrypted_share_reference_fails_startup_without_replacing_it(self):
        from django.core.management import call_command
        from django.core.management.base import CommandError
        from django.db import connection
        BlacklistSource.objects.create(list_id=1, name='Share', api_path='/lists/share/' + 'a'*32)
        invalid = 'mdblistarr:v1:fernet:invalid-ciphertext'
        with connection.cursor() as cursor:
            cursor.execute('UPDATE mdblistrr_blacklistsource SET api_path=%s', [invalid])
        with self.assertRaisesRegex(CommandError, 'could not be authenticated'):
            call_command('encrypt_secrets')
        with connection.cursor() as cursor:
            cursor.execute('SELECT api_path FROM mdblistrr_blacklistsource')
            self.assertEqual(cursor.fetchone()[0], invalid)


from django.test import TransactionTestCase


class BlacklistMigrationTests(TransactionTestCase):
    def test_upgrade_preserves_credentials_preferences_and_historical_file_rows(self):
        from django.db import connection
        from django.db.migrations.executor import MigrationExecutor
        before = [('mdblistrr', '0013_search_candidate_titles')]
        after = [('mdblistrr', '0014_blacklist_title_cleanup')]
        executor = MigrationExecutor(connection)
        executor.migrate(before)
        try:
            old = executor.loader.project_state(before).apps
            target = old.get_model('mdblistrr', 'SonarrInstance').objects.create(
                name='Retained', url='http://target.invalid', apikey='synthetic-retained',
                is_ondemand_target=True, is_library_source=False)
            preference = old.get_model('mdblistrr', 'Preferences').objects.create(
                name='sonarr_cleanup_enabled', value='1')
            now = timezone.now()
            row = old.get_model('mdblistrr', 'SonarrCleanupCandidate').objects.create(
                target_instance_id=target.pk, target_series_id=10, tvdb_id=200, episode_file_id=20,
                first_eligible_at=now, last_confirmed_at=now, status='deleted', deleted_at=now)
            with connection.cursor() as cursor:
                cursor.execute('SELECT apikey FROM mdblistrr_sonarrinstance WHERE id=%s', [target.pk])
                encrypted_before = cursor.fetchone()[0]
            executor = MigrationExecutor(connection)
            executor.migrate(after)
            target.refresh_from_db()
            row.refresh_from_db()
            preference.refresh_from_db()
            self.assertEqual(target.apikey, 'synthetic-retained')
            self.assertEqual((row.status, row.deleted_at, row.episode_file_id), ('deleted', now, 20))
            self.assertEqual(preference.value, '1')
            with connection.cursor() as cursor:
                cursor.execute('SELECT apikey FROM mdblistrr_sonarrinstance WHERE id=%s', [target.pk])
                self.assertEqual(cursor.fetchone()[0], encrypted_before)
            self.assertFalse(BlacklistSource.objects.exists())
            self.assertFalse(TitleCleanupCandidate.objects.exists())
            self.assertEqual(Preferences.get_value('blacklist_cleanup_enabled', '0'), '0')
            self.assertEqual(Preferences.get_value('blacklist_cleanup_dry_run', '1'), '1')
        finally:
            MigrationExecutor(connection).migrate(after)

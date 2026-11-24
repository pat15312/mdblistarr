import copy
import json
import os
from pathlib import Path
import tempfile
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from . import empty_directories as folders
from . import jellyfin_cleanup as integration
from .blacklist import BlacklistEvidence, configuration_signature
from .models import (JellyfinCleanup, Preferences, RadarrInstance, SonarrInstance,
                     TitleCleanupCandidate, TitleCleanupEvent)
from .test_blacklist import NativeArr
from .title_cleanup import maintain_candidates, process_title_cleanup


class DirectoryTests(SimpleTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'library'
        self.root.mkdir()
        self.title = self.root / 'Synthetic show'
        self.title.mkdir()
        self.season = self.title / 'Season 01'
        self.season.mkdir()

    def plan(self):
        return folders.snapshot(str(self.root), str(self.title), 'sonarr',
                                [str(self.title / f'Season {index:02}') for index in (1, 2)])

    def test_multiple_seasons_nonrecursive_removal_preserves_backing_object(self):
        (self.title / 'Season 02').mkdir()
        backing = Path(self.temp.name) / 'remote-object'
        backing.write_bytes(b'retained remote object')
        link = self.season / 'episode.mkv'
        link.symlink_to(backing)
        plan = self.plan()
        self.assertEqual(folders.inspect(plan, 'Season 01'), 'nonempty')
        with self.assertRaises(folders.DirectoryDeferred):
            folders.remove(plan, 'Season 01', lambda: self.fail('Intent before emptiness'))
        link.unlink()  # Synthetic native Arr file removal.
        intents = []
        for name in ('Season 02', 'Season 01', ''):
            folders.remove(plan, name, lambda: intents.append(name))
        self.assertEqual(intents, ['Season 02', 'Season 01', ''])
        self.assertFalse(self.title.exists())
        self.assertEqual(backing.read_bytes(), b'retained remote object')

    def test_hidden_files_sidecars_and_dangling_links_block_removal(self):
        for name, symlink in (('.hidden', False), ('metadata.nfo', False), ('dangling', True)):
            path = self.season / name
            if symlink:
                path.symlink_to('/nonexistent-synthetic-target')
            else:
                path.write_text('preserve')
            with self.subTest(name=name), self.assertRaises(folders.DirectoryDeferred):
                folders.remove(self.plan(), 'Season 01', lambda: self.fail('Unexpected intent'))
            self.assertTrue(os.path.lexists(path))
            path.unlink()

    def test_symlink_ancestor_and_replaced_title_are_rejected(self):
        alias = Path(self.temp.name) / 'alias'
        alias.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(OSError):
            folders.snapshot(str(alias), str(alias / self.title.name), 'sonarr')
        plan = self.plan()
        original = self.root / 'original'
        self.title.rename(original)
        self.title.mkdir()
        with self.assertRaises(folders.DirectoryDeferred):
            folders.remove(plan, '', lambda: self.fail('Unexpected intent'))
        self.assertTrue(original.exists())
        self.assertTrue(self.title.exists())

    def test_season_replaced_by_symlink_and_bind_mount_identity_rejected(self):
        plan = self.plan()
        self.season.rmdir()
        self.season.symlink_to(Path(self.temp.name), target_is_directory=True)
        with self.assertRaises(OSError):
            folders.remove(plan, 'Season 01', lambda: self.fail('Unexpected intent'))
        with self.assertRaises(folders.DirectoryDeferred):
            folders.same_mount({'device': 1, 'mount': '10'}, {'device': 1, 'mount': '11'})

    def test_concurrent_import_after_intent_is_not_deleted(self):
        plan = self.plan()
        imported = self.season / 'arriving.mkv'
        with self.assertRaises(OSError):
            folders.remove(plan, 'Season 01', lambda: imported.write_bytes(b'new import'))
        self.assertEqual(imported.read_bytes(), b'new import')

    def test_name_replacement_after_intent_is_rejected(self):
        plan = self.plan()
        old = self.title / 'original-season'
        def replace():
            self.season.rename(old)
            self.season.mkdir()
        with self.assertRaises(folders.DirectoryDeferred):
            folders.remove(plan, 'Season 01', replace)
        self.assertTrue(old.exists())
        self.assertTrue(self.season.exists())

    def test_nested_directories_movie_subfolders_and_outside_root_refuse(self):
        (self.season / 'nested').mkdir()
        with self.assertRaises(folders.DirectoryDeferred):
            self.plan()
        with self.assertRaises(folders.DirectoryDeferred):
            folders.snapshot(str(self.root), str(self.title), 'radarr')
        with self.assertRaises(folders.DirectoryDeferred):
            folders.snapshot(str(self.root), str(Path(self.temp.name)), 'sonarr')

    def test_permission_failure_is_not_absence(self):
        with patch('mdblistrr.empty_directories.os.open', side_effect=PermissionError):
            with self.assertRaises(PermissionError):
                folders.inspect({'root': str(self.root), 'title': str(self.title),
                                 'directories': [{'name': ''}]}, '')

    def test_namespace_mount_ids_can_change_but_root_identity_cannot(self):
        plan = self.plan()
        actual = folders.mount_identity
        with patch.object(folders, 'mount_identity', side_effect=lambda fd: dict(actual(fd), mount='new-namespace')):
            self.assertEqual(folders.inspect(plan, 'Season 01'), 'empty')
        self.root.rename(self.root.with_name('old-root'))
        self.root.mkdir()
        with self.assertRaises(folders.DirectoryDeferred):
            folders.inspect(plan, '')

    def test_unsubstantiated_empty_subdirectory_is_preserved(self):
        (self.title / 'Extras').mkdir()
        with self.assertRaises(folders.DirectoryDeferred):
            self.plan()
        self.assertTrue((self.title / 'Extras').is_dir())


class FixtureArr(NativeArr):
    def cleanup_request(self, method, resource, params=None, payload=None):
        if method == 'GET' and resource == 'episode':
            # Two episodes can refer to one multi-episode file.
            rows = [{'id': 100 + index * 2 + linked, 'seriesId': 10, 'hasFile': True,
                     'monitored': False, 'episodeFileId': file['id']}
                    for index, file in enumerate(self.files) for linked in (0, 1)]
            return {'status_code': 200, 'data': rows}
        return super().cleanup_request(method, resource, params, payload)


class FixtureJellyfin:
    """Retains empty directories until a refresh, as observed on the live server."""
    def __init__(self, config, product, title):
        self.config, self.product, self.title = config, product, title
        self.refreshes = 0
        self.refreshed = False
        self.fail_refresh = False
        self.retain = False
        self.items = [{'Id': 'item-1', 'Type': 'Series' if product == 'sonarr' else 'Movie',
                       'Path': str(title) if product == 'sonarr' else str(title / 'movie.mkv'),
                       'ProviderIds': {'Tvdb': '200'} if product == 'sonarr' else {'Tmdb': '100'}}]

    def binding(self, product):
        return {key: self.config[key] for key in ('url', 'root', 'library_id')} | {'server_id': 'synthetic-server'}

    def catalogue(self, product):
        return [] if self.refreshed and not self.retain else copy.deepcopy(self.items)

    def library(self):
        return {'RefreshStatus': 'Idle'}

    def refresh(self):
        self.refreshes += 1
        if self.fail_refresh:
            raise integration.JellyfinDeferred('Synthetic refresh timeout.')
        self.refreshed = not self.title.exists()

    def close(self):
        pass


class ContinuationTests(TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def setup_product(self, product='sonarr', files=1):
        self.product = product
        root = Path(self.temp.name) / product
        root.mkdir()
        self.title = root / 'Synthetic title'
        self.title.mkdir()
        self.backing = Path(self.temp.name) / ('remote-' + product)
        self.backing.write_bytes(b'original backing object')
        model = SonarrInstance if product == 'sonarr' else RadarrInstance
        self.target = model.objects.create(name='Target', url='http://target.invalid', apikey='target',
            is_ondemand_target=True, is_library_source=False)
        Preferences.set_value(product + '_reconciliation_target_id', str(self.target.pk))
        Preferences.set_value('blacklist_cleanup_enabled', '1')
        Preferences.set_value('blacklist_cleanup_dry_run', '0')
        Preferences.set_value('blacklist_cleanup_grace_hours', '0')
        self.api = FixtureArr(product, with_file=bool(files))
        self.api.row['path'] = str(self.title)
        self.api.files = []
        for index in range(files):
            parent = self.title
            if product == 'sonarr':
                parent = self.title / f'Season {index + 1:02}'
                parent.mkdir()
            path = parent / f'media-{index}.mkv'
            path.symlink_to(self.backing)
            self.api.files.append({'id': 20 + index, 'seriesId': 10, 'movieId': 10,
                'path': str(path), 'size': 100, 'dateAdded': '2026-01-02T00:00:00Z'})
        def native_delete(resource):
            if resource.startswith(self.api.file_resource + '/'):
                file = next(row for row in self.api.files if row['id'] == int(resource.split('/')[-1]))
                Path(file['path']).unlink()  # Only a disposable fixture link.
        self.api.before_delete = native_delete
        self.membership = BlacklistEvidence(configuration_signature(),
            {(product, 200 if product == 'sonarr' else 100): [1]}, timezone.now())
        maintain_candidates(product, self.target, [self.api.row], self.membership, 0)
        self.candidate = TitleCleanupCandidate.objects.get(product=product)
        self.config = {'url': 'http://jellyfin.invalid', 'root': str(root), 'library_id': 'a' * 32, 'key': 'synthetic-key-123456'}
        self.jellyfin = FixtureJellyfin(self.config, product, self.title)

    def process(self, budget=25):
        with patch('mdblistrr.jellyfin_cleanup.configuration', return_value=self.config), \
                patch('mdblistrr.jellyfin_cleanup.Client', return_value=self.jellyfin), \
                patch('mdblistrr.title_cleanup.refresh_blacklists', return_value=self.membership), \
                patch('mdblistrr.blacklist.refresh_blacklists', return_value=self.membership):
            return process_title_cleanup(product=self.product, target=self.target, api=self.api,
                membership=self.membership, remaining_budget=budget)

    def test_both_products_complete_only_after_directory_and_catalogue_verification(self):
        for product, files in (('sonarr', 2), ('radarr', 2)):
            with self.subTest(product=product):
                self.setup_product(product, files)
                result = self.process()
                stage = JellyfinCleanup.objects.get(candidate=self.candidate)
                self.assertEqual(stage.status, 'refresh_pending')
                self.candidate.refresh_from_db()
                native_deleted_at = self.candidate.deleted_at
                self.assertEqual(self.candidate.status, 'deleted')
                self.assertFalse(self.title.exists())
                self.assertEqual(result.attempts, 6 if product == 'sonarr' else 4)
                self.assertEqual(self.jellyfin.refreshes, 1)
                self.assertEqual(self.process().attempts, 0)
                stage.refresh_from_db()
                self.assertEqual(stage.status, 'complete')
                self.assertIsNotNone(stage.completed_at)
                self.assertEqual(self.backing.read_bytes(), b'original backing object')
                self.candidate.refresh_from_db()
                self.assertEqual(self.candidate.deleted_at, native_deleted_at)
                phases = list(self.candidate.events.values_list('outcome', flat=True))
                self.assertLess(phases.index('jellyfin_binding_captured_before_native_removal'), phases.index('intent_persisted'))

    def test_shared_budget_defers_folders_without_repeating_native_requests(self):
        self.setup_product()
        self.assertEqual(self.process(budget=2).attempts, 2)
        self.assertTrue(self.title.exists())
        self.assertEqual(len(self.api.deleted), 2)
        self.assertEqual(self.process(budget=1).attempts, 1)
        self.assertTrue(self.title.exists())
        self.assertEqual(self.process(budget=1).attempts, 1)
        self.assertFalse(self.title.exists())
        self.assertEqual(len(self.api.deleted), 2)

    def test_hidden_sidecar_defers_after_native_removal(self):
        self.setup_product('radarr')
        sidecar = self.title / '.keep.nfo'
        sidecar.write_text('retain metadata')
        self.assertEqual(self.process().attempts, 2)
        stage = JellyfinCleanup.objects.get(candidate=self.candidate)
        self.assertIn('remaining contents', stage.last_error)
        self.assertEqual(sidecar.read_text(), 'retain metadata')
        self.assertEqual(self.jellyfin.refreshes, 0)

    def test_concurrent_import_persists_uncertainty_and_never_retries(self):
        self.setup_product('radarr')
        original_rmdir = os.rmdir
        imported = self.title / 'concurrent.mkv'
        def arriving(path, **kwargs):
            imported.write_text('new import')
            return original_rmdir(path, **kwargs)
        with patch('mdblistrr.empty_directories.os.rmdir', side_effect=arriving):
            self.assertTrue(self.process().stop)
        self.assertEqual(imported.read_text(), 'new import')
        with patch('mdblistrr.empty_directories.os.rmdir') as remove:
            self.assertEqual(self.process().attempts, 0)
            remove.assert_not_called()
        stage = JellyfinCleanup.objects.get(candidate=self.candidate)
        self.assertEqual(stage.status, 'uncertain')
        self.assertEqual(stage.directories['intent'], '')

    def test_crash_after_rmdir_recovers_original_intent_read_only(self):
        self.setup_product()
        original_rmdir = os.rmdir
        def crash(path, **kwargs):
            original_rmdir(path, **kwargs)
            raise SystemExit('synthetic process death')
        with patch('mdblistrr.empty_directories.os.rmdir', side_effect=crash), self.assertRaises(SystemExit):
            self.process()
        stage = JellyfinCleanup.objects.get(candidate=self.candidate)
        self.assertEqual(stage.directories['intent'], 'Season 01')
        self.assertEqual(self.process().attempts, 0)
        self.assertTrue(self.title.exists())
        stage.refresh_from_db()
        self.assertIsNone(stage.directories['intent'])
        self.assertEqual(self.process().attempts, 1)

    def test_refresh_timeout_is_not_repeated_and_later_absence_resolves(self):
        self.setup_product('radarr')
        self.jellyfin.fail_refresh = True
        self.process()
        stage = JellyfinCleanup.objects.get(candidate=self.candidate)
        self.assertEqual(stage.status, 'refresh_uncertain')
        self.process()
        self.assertEqual(self.jellyfin.refreshes, 1)
        self.jellyfin.refreshed = True
        Preferences.set_value('blacklist_cleanup_enabled', '0')
        Preferences.set_value('blacklist_cleanup_dry_run', '1')
        self.process()
        stage.refresh_from_db()
        self.assertEqual(stage.status, 'complete')

    def test_disabled_and_dry_run_do_not_start_native_or_directory_work(self):
        self.setup_product()
        for key in ('blacklist_cleanup_enabled', 'blacklist_cleanup_dry_run'):
            Preferences.set_value(key, '0' if key.endswith('enabled') else '1')
            self.assertEqual(self.process().attempts, 0)
            self.assertFalse(JellyfinCleanup.objects.exists())
            self.assertTrue(self.title.exists())
            Preferences.set_value(key, '1' if key.endswith('enabled') else '0')

    def test_disable_after_native_completion_blocks_folder_and_refresh(self):
        self.setup_product()
        self.process(budget=2)
        Preferences.set_value('blacklist_cleanup_enabled', '0')
        self.assertEqual(self.process().attempts, 0)
        self.assertTrue(self.title.exists())
        self.assertEqual(self.jellyfin.refreshes, 0)

    def test_revoked_membership_readded_title_and_replaced_directory_are_blocked(self):
        self.setup_product('radarr')
        original = copy.deepcopy(self.api.row)
        self.process(budget=2)
        allowed = self.membership
        self.membership = BlacklistEvidence(configuration_signature(), {}, timezone.now())
        self.assertEqual(self.process().attempts, 0)
        self.membership = allowed
        self.api.row = dict(original, id=99)
        self.assertEqual(self.process().attempts, 0)
        self.api.row = None
        self.title.rename(self.title.with_name('original'))
        self.title.mkdir()
        self.assertEqual(self.process().attempts, 0)
        self.assertTrue(self.title.exists())

    def test_historical_deleted_rows_are_never_enrolled(self):
        self.setup_product('radarr')
        self.config = None
        self.process()
        self.assertFalse(JellyfinCleanup.objects.exists())
        self.config = self.jellyfin.config
        self.process()
        self.assertFalse(JellyfinCleanup.objects.exists())
        self.assertTrue(self.title.exists())

    def test_conflicting_jellyfin_identity_blocks_before_native_deletion(self):
        self.setup_product()
        self.jellyfin.items[0]['ProviderIds'] = {'Tvdb': '999'}
        self.assertEqual(self.process().attempts, 0)
        self.assertEqual(self.api.deleted, [])
        self.assertTrue(self.title.exists())

    def test_native_monitoring_and_file_changes_during_preparation_block_delete(self):
        for product in ('sonarr', 'radarr'):
            self.setup_product(product)
            original = self.jellyfin.catalogue
            def changed(kind):
                self.api.row['monitored'] = True
                return original(kind)
            with patch.object(self.jellyfin, 'catalogue', side_effect=changed):
                self.assertEqual(self.process().attempts, 0)
            self.assertEqual(self.api.deleted, [])
            self.assertTrue(self.title.exists())

    def test_orphan_episode_prevents_false_completion(self):
        self.setup_product()
        self.process()
        self.jellyfin.refreshed = False
        self.jellyfin.items = [{'Id': 'orphan-episode', 'Type': 'Episode',
            'Path': str(self.title / 'Season 01' / 'episode.mkv'), 'ProviderIds': {'Tvdb': '123456'}}]
        self.process()
        stage = JellyfinCleanup.objects.get(candidate=self.candidate)
        self.assertEqual(stage.status, 'refresh_incomplete')
        self.assertIsNone(stage.completed_at)
        self.assertIn('unverified', stage.last_error)
        self.assertEqual(self.jellyfin.refreshes, 1)

    def test_native_readdition_during_final_catalogue_reads_defers_completion(self):
        self.setup_product('radarr')
        original = copy.deepcopy(self.api.row)
        self.process()
        catalogue = self.jellyfin.catalogue
        def readded(product):
            self.api.row = original
            return catalogue(product)
        with patch.object(self.jellyfin, 'catalogue', side_effect=readded):
            self.process()
        stage = JellyfinCleanup.objects.get(candidate=self.candidate)
        self.assertIsNone(stage.completed_at)
        self.assertIn('Native catalogue changed', stage.last_error)

    def test_empty_season_requires_matching_jellyfin_series_and_season_evidence(self):
        self.setup_product(files=0)
        season = self.title / 'Season 01'
        season.mkdir()
        self.assertEqual(self.process().attempts, 0)
        self.jellyfin.items.append({'Id': 'season', 'Type': 'Season', 'Path': str(season), 'ProviderIds': {}})
        self.assertEqual(self.process().attempts, 3)
        self.assertFalse(self.title.exists())

    def test_more_than_one_batch_of_blocked_stages_gets_fair_attention(self):
        self.setup_product('radarr')
        self.process(budget=2)
        original = JellyfinCleanup.objects.get(candidate=self.candidate)
        for index in range(100):
            candidate = TitleCleanupCandidate.objects.get(pk=self.candidate.pk)
            candidate.pk = None
            candidate.target_media_id = 1000 + index
            candidate.external_id = 1000 + index
            candidate.save()
            JellyfinCleanup.objects.create(candidate=candidate, binding=original.binding,
                directories=original.directories)
        last = JellyfinCleanup.objects.order_by('-id').first()
        original_time = last.updated_at
        self.config = None
        self.process()
        last.refresh_from_db()
        self.assertEqual(last.updated_at, original_time)
        self.process()
        last.refresh_from_db()
        self.assertGreater(last.updated_at, original_time)

    def test_read_only_health_keeps_native_history_and_separate_completion(self):
        from .health_details import build_health_details
        from .arr_health import build_arr_health
        self.setup_product('radarr')
        self.process(budget=2)
        with patch('requests.Session.request', side_effect=AssertionError('Health made a request')):
            details = build_health_details('radarr', 'cleanup', 'deleted')
            health = build_arr_health()
        entry = details['page_obj'].object_list[0]
        self.assertIn('removed from Arr', entry['action'])
        self.assertIn('Jellyfin:', entry['outcome'])
        self.assertIsNotNone(health)


class ClientTests(SimpleTestCase):
    def setUp(self):
        self.config = {'url': 'https://jellyfin.invalid', 'root': '/library/tv',
                       'library_id': 'a' * 32, 'key': 'synthetic-key-123456'}
        self.client = integration.Client(self.config)
        self.addCleanup(self.client.close)

    def test_incomplete_duplicate_and_malformed_catalogues_never_prove_absence(self):
        row = {'Id': 'one', 'Type': 'Series', 'Path': '/library/tv/One', 'ProviderIds': {}}
        for response in ({}, {'Items': [], 'TotalRecordCount': 1},
                         {'Items': [row, row], 'TotalRecordCount': 2},
                         {'Items': [dict(row, Path=None)], 'TotalRecordCount': 1}):
            with self.subTest(response=response), self.assertRaises(integration.JellyfinDeferred):
                self.client.request = Mock(return_value=response)
                self.client.catalogue('sonarr')

    def test_movie_versions_are_all_matched_by_provider(self):
        candidate = Mock(product='radarr', external_id=100)
        rows = [{'Id': str(i), 'Type': 'Movie', 'Path': f'/library/movies/Title/version{i}.mkv',
                 'ProviderIds': {'Tmdb': '100'}} for i in (1, 2)]
        self.assertEqual(integration.matching(rows, candidate, '/library/movies/Title'), rows)

    def test_full_pagination_includes_episodes_without_series_provider_collision(self):
        rows = [{'Id': str(i), 'Type': 'Episode', 'Path': f'/library/tv/Other/{i}.mkv',
                 'ProviderIds': {'Tvdb': '200'}} for i in range(5001)]
        self.client.request = Mock(side_effect=[{'Items': rows[:5000], 'TotalRecordCount': 5001},
                                               {'Items': rows[5000:], 'TotalRecordCount': 5001}])
        result = self.client.catalogue('sonarr')
        self.assertEqual(len(result), 5001)
        self.assertIn('Episode', self.client.request.call_args.args[2]['IncludeItemTypes'])
        self.assertEqual(integration.matching(result, Mock(product='sonarr', external_id=200), '/library/tv/Target'), [])

    def test_malformed_library_ids_fail_closed(self):
        for value in (None, 1, [], {}):
            with self.subTest(value=value), self.assertRaises(integration.JellyfinDeferred):
                self.client.request = Mock(return_value=[{'ItemId': value}])
                self.client.library()

    def test_virtual_children_are_validated_and_matched_by_original_series(self):
        rows = [{'Id': 'series', 'Type': 'Series', 'Path': '/library/tv/Title', 'ProviderIds': {'Tvdb': '200'}},
                {'Id': 'virtual-season', 'Type': 'Season', 'LocationType': 'Virtual', 'SeriesId': 'series'},
                {'Id': 'virtual-episode', 'Type': 'Episode', 'LocationType': 'Virtual', 'SeriesId': 'series'},
                {'Id': 'other-virtual', 'Type': 'Season', 'LocationType': 'Virtual', 'SeriesId': 'other'},
                {'Id': 'other', 'Type': 'Series', 'Path': '/library/tv/Other', 'ProviderIds': {'Tvdb': '300'}}]
        self.client.request = Mock(return_value={'Items': rows, 'TotalRecordCount': 5})
        catalogue = self.client.catalogue('sonarr')
        candidate = Mock(product='sonarr', external_id=200)
        self.assertEqual({r['Id'] for r in integration.matching(catalogue, candidate, '/library/tv/Title')},
                         {'series', 'virtual-season', 'virtual-episode'})
        # Original series may be gone while its virtual children remain.
        orphaned = [r for r in catalogue if r['Id'] != 'series']
        self.assertEqual({r['Id'] for r in integration.matching(orphaned, candidate, '/library/tv/Title', ['series'], ['series'])},
                         {'virtual-season', 'virtual-episode'})
        with self.assertRaises(integration.JellyfinDeferred):
            integration.matching(orphaned, candidate, '/library/tv/Title')
        wrong_parent = [dict(r, SeriesId='virtual-episode') if r['Id']=='virtual-season' else r for r in catalogue]
        with self.assertRaises(integration.JellyfinDeferred):
            integration.matching(wrong_parent, candidate, '/library/tv/Title')
        for invalid in ({'SeriesId': ''}, {'SeriesId': None}, {'LocationType': 'FileSystem'}, {'Path': 12}):
            with self.subTest(invalid=invalid), self.assertRaises(integration.JellyfinDeferred):
                self.client.request = Mock(return_value={'Items': [dict(rows[1], **invalid)], 'TotalRecordCount': 1})
                self.client.catalogue('sonarr')

    def test_authentication_redirect_and_network_failures_are_sanitised(self):
        import requests
        for status in (401, 403, 302, 500):
            response = Mock(status_code=status)
            context = Mock()
            context.__enter__ = Mock(return_value=response)
            context.__exit__ = Mock(return_value=False)
            with patch.object(self.client.session, 'request', return_value=context) as request:
                with self.assertRaises(integration.JellyfinDeferred) as error:
                    self.client.library()
                self.assertNotIn(self.config['key'], str(error.exception))
                self.assertFalse(request.call_args.kwargs['allow_redirects'])
        with patch.object(self.client.session, 'request', side_effect=requests.Timeout(self.config['key'])):
            with self.assertRaises(integration.JellyfinDeferred) as error:
                self.client.library()
            self.assertNotIn(self.config['key'], str(error.exception))

    def test_refresh_is_supported_parent_refresh_without_item_delete(self):
        self.client.request = Mock()
        self.client.refresh()
        args = self.client.request.call_args
        self.assertEqual(args.args[:2], ('POST', '/Items/' + 'a' * 32 + '/Refresh'))
        self.assertEqual(args.args[2]['ReplaceAllMetadata'], 'false')

    def test_disabled_config_and_invalid_secret_do_not_expose_credentials(self):
        with patch.dict(os.environ, {'MDBLISTARR_JELLYFIN_ENABLED': '0'}):
            self.assertIsNone(integration.configuration('sonarr'))
        with patch.dict(os.environ, {'MDBLISTARR_JELLYFIN_ENABLED': '1',
                'MDBLISTARR_JELLYFIN_URL': 'http://user:private@invalid',
                'MDBLISTARR_JELLYFIN_ROOTS': '{}'}):
            with self.assertRaisesRegex(integration.JellyfinDeferred, '^Jellyfin integration configuration'):
                integration.configuration('sonarr')

import os
from collections import Counter
from html.parser import HTMLParser
from unittest.mock import Mock, patch

from cryptography.fernet import Fernet
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase, override_settings

from .cron import get_mdblist_queue_to_arr, get_radarr_sync_instances, get_sonarr_sync_instances, post_radarr_payload
from .models import Preferences, RadarrInstance, SonarrInstance
from .views import RadarrInstanceForm, SonarrInstanceForm
from . import crypto

TEST_KEY = Fernet.generate_key().decode()


class ElementCollector(HTMLParser):
    def __init__(self):
        super().__init__()
        self.elements = []

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))


class EncryptionKeyMixin:
    def setUp(self):
        super().setUp()
        self._old_key = os.environ.get('MDBLISTARR_ENCRYPTION_KEY')
        os.environ['MDBLISTARR_ENCRYPTION_KEY'] = TEST_KEY
        crypto._fernet = None

    def tearDown(self):
        if self._old_key is None:
            os.environ.pop('MDBLISTARR_ENCRYPTION_KEY', None)
        else:
            os.environ['MDBLISTARR_ENCRYPTION_KEY'] = self._old_key
        crypto._fernet = None
        super().tearDown()


class RadarrRoleMigrationTests(EncryptionKeyMixin, TransactionTestCase):
    migrate_from = ('mdblistrr', '0007_sonarrepisodesearchcandidate_attempt_count_and_more')
    migrate_to = ('mdblistrr', '0008_radarrinstance_is_library_source_and_more')

    def test_existing_row_is_altered_in_place_with_safe_defaults(self):
        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_from])
        old_apps = executor.loader.project_state([self.migrate_from]).apps
        OldRadarr = old_apps.get_model('mdblistrr', 'RadarrInstance')
        row = OldRadarr.objects.create(
            id=73, name='Existing', url='http://existing', apikey='encrypted-value',
            quality_profile='12', root_folder='/movies', enable_queue_import=True,
        )
        created_at = row.created_at
        with connection.cursor() as cursor:
            cursor.execute('SELECT apikey FROM mdblistrr_radarrinstance WHERE id = %s', [73])
            ciphertext_before = cursor.fetchone()[0]

        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_to])
        NewRadarr = executor.loader.project_state([self.migrate_to]).apps.get_model('mdblistrr', 'RadarrInstance')
        upgraded = NewRadarr.objects.get(pk=73)
        self.assertEqual(upgraded.name, 'Existing')
        self.assertEqual(upgraded.url, 'http://existing')
        self.assertEqual(upgraded.apikey, 'encrypted-value')
        self.assertEqual(upgraded.quality_profile, '12')
        self.assertEqual(upgraded.root_folder, '/movies')
        self.assertTrue(upgraded.enable_queue_import)
        self.assertEqual(upgraded.created_at, created_at)
        self.assertTrue(upgraded.is_library_source)
        self.assertFalse(upgraded.is_ondemand_target)
        with connection.cursor() as cursor:
            cursor.execute('SELECT apikey FROM mdblistrr_radarrinstance WHERE id = %s', [73])
            self.assertEqual(cursor.fetchone()[0], ciphertext_before)


class ArrInstanceModelAndFormTests(EncryptionKeyMixin, TestCase):
    def test_radarr_role_defaults_and_optional_import_configuration(self):
        instance = RadarrInstance(name='read-only', url='http://r', apikey='key')
        instance.full_clean()
        self.assertTrue(instance.is_library_source)
        self.assertFalse(instance.is_ondemand_target)
        self.assertIsNone(instance.quality_profile)
        self.assertIsNone(instance.root_folder)

    def test_queue_import_validation_is_identical_for_both_arr_models(self):
        for model in (RadarrInstance, SonarrInstance):
            for profile, root in [(None, '/media'), ('', '/media'), ('   ', '/media'), ('0', '/media'), ('1', None), ('1', ''), ('1', '   '), ('1', '0')]:
                with self.subTest(model=model.__name__, profile=profile, root=root):
                    instance = model(name='arr', url='http://arr', apikey='key', enable_queue_import=True,
                                     quality_profile=profile, root_folder=root)
                    with self.assertRaises(ValidationError):
                        instance.full_clean()
            model(name='arr', url='http://arr', apikey='key', enable_queue_import=True,
                  quality_profile='1', root_folder='/media').full_clean()

    def _form_data(self, **updates):
        data = {'name': 'arr', 'url': 'http://arr', 'apikey': 'key', 'is_library_source': 'on'}
        data.update(updates)
        return data

    def test_forms_share_role_labels_order_and_queue_validation(self):
        expected = ['is_library_source', 'is_ondemand_target', 'enable_queue_import']
        for form_class in (RadarrInstanceForm, SonarrInstanceForm):
            form = form_class()
            self.assertEqual([name for name in form.fields if name in expected], expected)
            self.assertEqual(form.fields['is_library_source'].label, 'Permanent library source')
            self.assertEqual(form.fields['is_ondemand_target'].label, 'On-Demand target')
            self.assertEqual(form.fields['enable_queue_import'].label, 'Enable MDBList queue import')
            invalid = form_class(data=self._form_data(enable_queue_import='on', quality_profile='0', root_folder='0'))
            self.assertFalse(invalid.is_valid())
            self.assertIn('quality_profile', invalid.errors)
            self.assertIn('root_folder', invalid.errors)

    def test_radarr_form_saves_independent_roles_without_queue_import(self):
        form = RadarrInstanceForm(data=self._form_data(is_library_source='', is_ondemand_target='on'))
        self.assertTrue(form.is_valid(), form.errors)
        saved = form.save()
        self.assertFalse(saved.is_library_source)
        self.assertTrue(saved.is_ondemand_target)
        self.assertFalse(saved.enable_queue_import)


class RadarrRoleAwareSyncTests(EncryptionKeyMixin, TestCase):
    def create_radarr(self, name, **roles):
        values = dict(name=name, url=f'http://{name}', apikey='key')
        values.update(roles)
        return RadarrInstance.objects.create(**values)

    def test_first_and_all_scopes_filter_role_before_ordering(self):
        self.create_radarr('target', is_library_source=False, is_ondemand_target=True)
        first = self.create_radarr('first', is_library_source=True)
        both = self.create_radarr('both', is_library_source=True, is_ondemand_target=True)
        self.create_radarr('neither', is_library_source=False, is_ondemand_target=False)
        self.assertEqual(get_radarr_sync_instances(), [first])
        Preferences.set_value('sync_instance_scope', 'all')
        self.assertEqual(get_radarr_sync_instances(), [first, both])

    def test_both_role_selection_matches_sonarr(self):
        radarr = self.create_radarr('r', is_library_source=True, is_ondemand_target=True)
        sonarr = SonarrInstance.objects.create(name='s', url='http://s', apikey='key', is_library_source=True, is_ondemand_target=True)
        self.assertEqual(get_radarr_sync_instances(), [radarr])
        self.assertEqual(get_sonarr_sync_instances(), [sonarr])

    @patch('mdblistrr.cron.get_mdblistarr')
    @patch('mdblistrr.cron.reset_mdblistarr')
    def test_no_source_fails_closed_without_upload(self, reset, get_service):
        self.create_radarr('target', is_library_source=False, is_ondemand_target=True)
        service = get_service.return_value
        service.mdblist = Mock()
        result = post_radarr_payload(force=True)
        self.assertEqual(result, {'response': 'No Radarr source instances configured'})
        self.assertFalse(any(call[0][0].startswith('post_') for call in service.mdblist.method_calls))


class ArrQueueRuntimeParityTests(EncryptionKeyMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.radarr = RadarrInstance.objects.create(
            name='r', url='http://r', apikey='key', quality_profile='1', root_folder='/movies',
            enable_queue_import=True,
        )
        self.sonarr = SonarrInstance.objects.create(
            name='s', url='http://s', apikey='key', quality_profile='2', root_folder='/shows',
            enable_queue_import=True,
        )

    def queue(self):
        return [
            {'mediatype': 'movie', 'instanceid': self.radarr.id, 'title': 'Movie', 'tmdbid': 10},
            {'mediatype': 'show', 'instanceid': self.sonarr.id, 'title': 'Show', 'tvdbid': 20},
        ]

    def run_queue(self, queue=None):
        service = Mock()
        service.mdblist.get_mdblist_queue.return_value = self.queue() if queue is None else queue
        service.get_radarr_quality_profile.return_value = '1'
        service.get_radarr_root_folder.return_value = '/movies'
        service.get_sonarr_quality_profile.return_value = '2'
        service.get_sonarr_root_folder.return_value = '/shows'
        with patch('mdblistrr.cron.time.sleep'), patch('mdblistrr.cron.reset_mdblistarr'), \
             patch('mdblistrr.cron.get_mdblistarr', return_value=service), \
             patch('mdblistrr.cron.RadarrAPI.post_movie', return_value={'title': 'Movie'}) as post_movie, \
             patch('mdblistrr.cron.SonarrAPI.post_show', return_value={'title': 'Show'}) as post_show:
            result = get_mdblist_queue_to_arr()
        return result, post_movie, post_show

    def test_valid_configuration_proceeds_for_both_products(self):
        Preferences.set_value('enable_mdblist_queue_processing', '1')
        result, post_movie, post_show = self.run_queue()
        self.assertEqual(result, {'result': 200})
        post_movie.assert_called_once()
        post_show.assert_called_once()

    def test_invalid_configuration_blocks_each_product_runtime_write(self):
        Preferences.set_value('enable_mdblist_queue_processing', '1')
        self.radarr.quality_profile = '   '
        self.radarr.save(update_fields=['quality_profile'])
        self.sonarr.root_folder = '0'
        self.sonarr.save(update_fields=['root_folder'])
        _, post_movie, post_show = self.run_queue()
        post_movie.assert_not_called()
        post_show.assert_not_called()

    def test_roles_alone_and_per_instance_opt_in_do_not_authorize_writes(self):
        Preferences.set_value('enable_mdblist_queue_processing', '1')
        for source, target in ((True, False), (False, True), (True, True), (False, False)):
            self.radarr.is_library_source = source
            self.radarr.is_ondemand_target = target
            self.radarr.enable_queue_import = False
            self.radarr.save(update_fields=['is_library_source', 'is_ondemand_target', 'enable_queue_import'])
            self.sonarr.is_library_source = source
            self.sonarr.is_ondemand_target = target
            self.sonarr.enable_queue_import = False
            self.sonarr.save(update_fields=['is_library_source', 'is_ondemand_target', 'enable_queue_import'])
            with self.subTest(source=source, target=target):
                _, post_movie, post_show = self.run_queue()
                post_movie.assert_not_called()
                post_show.assert_not_called()

    def test_global_opt_in_is_required(self):
        Preferences.set_value('enable_mdblist_queue_processing', '0')
        result, post_movie, post_show = self.run_queue()
        self.assertEqual(result['message'], 'MDBList queue processing disabled')
        post_movie.assert_not_called()
        post_show.assert_not_called()


@override_settings(ALLOWED_HOSTS=['testserver'])
class ArrPurposeTemplateTests(EncryptionKeyMixin, TestCase):
    def setUp(self):
        super().setUp()
        user = get_user_model().objects.create_user('staff', password='pw', is_staff=True, is_superuser=True)
        self.client.force_login(user)

    @patch('mdblistrr.views.get_mdblistarr')
    def test_both_products_share_purpose_partial_and_never_render_saved_secrets(self, get_service):
        get_service.return_value.mdblist = None
        radarr = RadarrInstance.objects.create(name='r', url='http://r', apikey='radarr-secret', is_library_source=False, is_ondemand_target=True)
        sonarr = SonarrInstance.objects.create(name='s', url='http://s', apikey='sonarr-secret', is_library_source=True, is_ondemand_target=False)
        session = self.client.session
        session['active_radarr_id'] = str(radarr.id)
        session['active_sonarr_id'] = str(sonarr.id)
        session.save()
        response = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertEqual(html.count('class="arr-instance-purpose"'), 2)
        self.assertGreaterEqual(html.count('Permanent library source'), 2)
        self.assertGreaterEqual(html.count('On-Demand target'), 2)
        self.assertEqual(html.count('Enable MDBList queue import'), 2)
        self.assertNotIn('radarr-secret', html)
        self.assertNotIn('sonarr-secret', html)
        self.assertNotIn('Enable Radarr reconciliation', html)
        self.assertNotIn('Run Radarr reconciliation now', html)

        parser = ElementCollector()
        parser.feed(html)
        ids = [attrs['id'] for _, attrs in parser.elements if attrs.get('id', '').startswith(('id_radarr_', 'id_sonarr_'))]
        self.assertTrue(ids)
        self.assertFalse([element_id for element_id, count in Counter(ids).items() if count > 1])

        elements_by_id = {attrs['id']: attrs for _, attrs in parser.elements if 'id' in attrs}
        label_targets = {attrs['for'] for tag, attrs in parser.elements if tag == 'label' and 'for' in attrs}
        for product in ('radarr', 'sonarr'):
            for field in ('name', 'url', 'apikey', 'is_library_source', 'is_ondemand_target',
                          'enable_queue_import', 'quality_profile', 'root_folder'):
                element_id = f'id_{product}_{field}'
                self.assertIn(element_id, elements_by_id)
                self.assertIn(element_id, label_targets)

        self.assertNotIn('checked', elements_by_id['id_radarr_is_library_source'])
        self.assertIn('checked', elements_by_id['id_radarr_is_ondemand_target'])
        self.assertIn('checked', elements_by_id['id_sonarr_is_library_source'])
        self.assertNotIn('checked', elements_by_id['id_sonarr_is_ondemand_target'])

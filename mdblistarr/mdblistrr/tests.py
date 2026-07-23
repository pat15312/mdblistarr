import io
import os
import subprocess
import sys
import tempfile
import threading
from contextlib import contextmanager, redirect_stdout, redirect_stderr
from pathlib import Path
from unittest.mock import patch, Mock

from cryptography.fernet import Fernet
from django.contrib.auth import get_user_model
from django.core.management import call_command, CommandError
from django.db import connection
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from . import crypto
from .arr import MdblistAPI, RadarrAPI, SonarrAPI
from .connect import sanitize_text
from .models import Preferences, RadarrInstance, SonarrInstance, SonarrCleanupCandidate
from .services import get_mdblistarr, reset_mdblistarr
from .admin_state import usable_administrator_exists
from .forms import InitialAdminSetupForm

KEY = Fernet.generate_key().decode()
WRONG_KEY = Fernet.generate_key().decode()

@contextmanager
def env(**updates):
    old = {k: os.environ.get(k) for k in updates}
    for k, v in updates.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    crypto._fernet = None
    try:
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        crypto._fernet = None
        reset_mdblistarr()

@override_settings(ALLOWED_HOSTS=['testserver'])
class SecurityTests(TestCase):
    def setUp(self):
        self.env = env(MDBLISTARR_ENCRYPTION_KEY=KEY, MDBLISTARR_ADMIN_USERNAME=None, MDBLISTARR_ADMIN_PASSWORD=None, MDBLISTARR_ADMIN_PASSWORD_FILE=None)
        self.env.__enter__()
    def tearDown(self):
        self.env.__exit__(None, None, None)

    def raw_pref(self, name):
        with connection.cursor() as c:
            c.execute('select value from mdblistrr_preferences where name=%s', [name])
            row = c.fetchone()
        return row[0] if row else None

    def test_secret_preferences_encrypt_and_decrypt_all_types_after_reset(self):
        for name, value in {
            'mdblist_apikey': 'mdb-secret-key',
            'mdblist_access_token': 'oauth-access-token-secret',
            'mdblist_refresh_token': 'oauth-refresh-token-secret',
        }.items():
            Preferences.set_secret(name, value)
            raw = self.raw_pref(name)
            self.assertTrue(raw.startswith(crypto.PREFIX))
            self.assertNotIn(value, raw)
            crypto._fernet = None
            self.assertEqual(Preferences.get_secret(name), value)
        Preferences.set_value('sync_hour', '7')
        self.assertEqual(Preferences.get_value('sync_hour'), '7')

    def test_services_and_clients_receive_plaintext_mdb_tokens(self):
        Preferences.set_secret('mdblist_apikey', 'plain-mdb-key')
        reset_mdblistarr()
        self.assertEqual(get_mdblistarr().mdblist.apikey, 'plain-mdb-key')
        Preferences.clear_secret('mdblist_apikey')
        Preferences.set_secret('mdblist_access_token', 'access-plain')
        Preferences.set_secret('mdblist_refresh_token', 'refresh-plain')
        Preferences.set_value('mdblist_token_expires_at', '9999999999')
        reset_mdblistarr()
        api = get_mdblistarr().mdblist
        self.assertEqual(api.access_token, 'access-plain')
        self.assertEqual(api.refresh_token, 'refresh-plain')

    def test_oauth_refresh_stores_encrypted_and_uses_decrypted_refresh(self):
        api = MdblistAPI(access_token='old-access', refresh_token='refresh-plain', token_expires_at=1, client_id='client')
        with patch('mdblistrr.arr._requests.post') as post:
            post.return_value.json.return_value = {'access_token': 'new-access', 'refresh_token': 'new-refresh', 'expires_in': 3600}
            api._ensure_valid_token()
        self.assertEqual(post.call_args.kwargs['data']['refresh_token'], 'refresh-plain')
        self.assertEqual(Preferences.get_secret('mdblist_access_token'), 'new-access')
        self.assertEqual(Preferences.get_secret('mdblist_refresh_token'), 'new-refresh')
        self.assertTrue(self.raw_pref('mdblist_access_token').startswith(crypto.PREFIX))

    def test_oauth_disconnect_sends_plaintext_token_not_ciphertext(self):
        Preferences.set_secret('mdblist_access_token', 'disconnect-token')
        User = get_user_model(); User.objects.create_user('staff', password='pw', is_staff=True, is_superuser=True)
        self.client.login(username='staff', password='pw')
        with patch('mdblistrr.views._requests.post') as post:
            self.client.post('/oauth/disconnect')
        self.assertEqual(post.call_args.kwargs['data']['token'], 'disconnect-token')

    def test_encrypt_command_requires_valid_key_and_detects_tampering(self):
        with env(MDBLISTARR_ENCRYPTION_KEY=None):
            with self.assertRaises(CommandError):
                call_command('encrypt_secrets')
        Preferences.set_secret('mdblist_apikey', 'secret')
        with env(MDBLISTARR_ENCRYPTION_KEY=WRONG_KEY):
            with self.assertRaises(CommandError):
                call_command('encrypt_secrets')
        with connection.cursor() as c:
            c.execute("update mdblistrr_preferences set value=%s where name='mdblist_apikey'", [crypto.PREFIX + 'tampered'])
        with self.assertRaises(CommandError):
            call_command('encrypt_secrets')

    def test_mixed_plaintext_and_encrypted_migration_idempotent(self):
        radarr = RadarrInstance.objects.create(name='r1', url='http://r', apikey='initial-radarr', quality_profile='1', root_folder='/m')
        sonarr = SonarrInstance.objects.create(name='s1', url='http://s', apikey='initial-sonarr', quality_profile='1', root_folder='/t')
        Preferences.set_secret('mdblist_apikey', 'initial-pref')
        Preferences.set_secret('mdblist_access_token', 'already-encrypted')
        encrypted_access_before = self.raw_pref('mdblist_access_token')

        with connection.cursor() as c:
            c.execute("UPDATE mdblistrr_radarrinstance SET apikey=%s WHERE id=%s", ['plain-radarr', radarr.id])
            c.execute("UPDATE mdblistrr_sonarrinstance SET apikey=%s WHERE id=%s", ['plain-sonarr', sonarr.id])
            c.execute("UPDATE mdblistrr_preferences SET value=%s WHERE name=%s", ['plain-pref', 'mdblist_apikey'])

            c.execute("SELECT apikey FROM mdblistrr_radarrinstance WHERE id=%s", [radarr.id])
            self.assertEqual(c.fetchone()[0], 'plain-radarr')
            c.execute("SELECT apikey FROM mdblistrr_sonarrinstance WHERE id=%s", [sonarr.id])
            self.assertEqual(c.fetchone()[0], 'plain-sonarr')
            c.execute("SELECT value FROM mdblistrr_preferences WHERE name=%s", ['mdblist_apikey'])
            self.assertEqual(c.fetchone()[0], 'plain-pref')

        call_command('encrypt_secrets')

        with connection.cursor() as c:
            c.execute("SELECT apikey FROM mdblistrr_radarrinstance WHERE id=%s", [radarr.id])
            radarr_ciphertext = c.fetchone()[0]
            c.execute("SELECT apikey FROM mdblistrr_sonarrinstance WHERE id=%s", [sonarr.id])
            sonarr_ciphertext = c.fetchone()[0]
            c.execute("SELECT value FROM mdblistrr_preferences WHERE name=%s", ['mdblist_apikey'])
            pref_ciphertext = c.fetchone()[0]

        for plaintext, ciphertext in [
            ('plain-radarr', radarr_ciphertext),
            ('plain-sonarr', sonarr_ciphertext),
            ('plain-pref', pref_ciphertext),
        ]:
            self.assertTrue(ciphertext.startswith(crypto.PREFIX))
            self.assertNotIn(plaintext, ciphertext)

        self.assertEqual(Preferences.get_secret('mdblist_apikey'), 'plain-pref')
        self.assertEqual(RadarrInstance.objects.get(id=radarr.id).apikey, 'plain-radarr')
        self.assertEqual(SonarrInstance.objects.get(id=sonarr.id).apikey, 'plain-sonarr')
        self.assertEqual(self.raw_pref('mdblist_access_token'), encrypted_access_before)

        call_command('encrypt_secrets')
        with connection.cursor() as c:
            c.execute("SELECT apikey FROM mdblistrr_radarrinstance WHERE id=%s", [radarr.id])
            self.assertEqual(c.fetchone()[0], radarr_ciphertext)
            c.execute("SELECT apikey FROM mdblistrr_sonarrinstance WHERE id=%s", [sonarr.id])
            self.assertEqual(c.fetchone()[0], sonarr_ciphertext)
            c.execute("SELECT value FROM mdblistrr_preferences WHERE name=%s", ['mdblist_apikey'])
            self.assertEqual(c.fetchone()[0], pref_ciphertext)
            c.execute("SELECT value FROM mdblistrr_preferences WHERE name=%s", ['mdblist_access_token'])
            self.assertEqual(c.fetchone()[0], encrypted_access_before)

    def test_admin_bootstrap_restart_legacy_and_password_file(self):
        User = get_user_model()
        call_command('secure_startup')
        with tempfile.NamedTemporaryFile('w', delete=False) as f:
            f.write('safe-password\n')
            path = f.name
        try:
            with env(MDBLISTARR_ADMIN_USERNAME='owner', MDBLISTARR_ADMIN_PASSWORD_FILE=path):
                call_command('secure_startup')
                owner = User.objects.get(username='owner')
                self.assertTrue(owner.check_password('safe-password'))
                owner.set_password('changed'); owner.save()
                call_command('secure_startup')
                self.assertTrue(User.objects.get(username='owner').check_password('changed'))
        finally:
            os.unlink(path)
        User.objects.all().delete()
        legacy = User.objects.create_superuser('admin', password='admin')
        with env(MDBLISTARR_ADMIN_USERNAME='owner', MDBLISTARR_ADMIN_PASSWORD='new-safe'):
            call_command('secure_startup')
        legacy = User.objects.get(id=legacy.id)
        self.assertFalse(legacy.is_active)
        self.assertFalse(legacy.has_usable_password())
        self.assertFalse(self.client.login(username='admin', password='admin'))
        User.objects.all().delete()
        changed = User.objects.create_superuser('admin', password='changed-password')
        call_command('secure_startup')
        self.assertTrue(User.objects.get(id=changed.id).is_active)

    def test_auth_exact_statuses_and_no_nonstaff_loop(self):
        response = self.client.get('/', follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.resolver_match.url_name, 'setup')
        response = self.client.post('/set_active_tab/', data='{}', content_type='application/json')
        self.assertEqual(response.status_code, 503)
        User = get_user_model()
        User.objects.create_user('user', password='pw')
        self.client.login(username='user', password='pw')
        response = self.client.get('/', follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.resolver_match.url_name, 'setup')
        self.client.logout()
        User.objects.create_user('staff', password='pw', is_staff=True, is_superuser=True)
        self.assertTrue(self.client.login(username='staff', password='pw'))
        self.assertEqual(self.client.get('/').status_code, 200)

    def test_login_success_failure_logout(self):
        get_user_model().objects.create_user('staff', password='pw', is_staff=True, is_superuser=True)
        self.assertEqual(self.client.post('/accounts/login/', {'username': 'staff', 'password': 'bad'}).status_code, 200)
        response = self.client.post('/accounts/login/', {'username': 'staff', 'password': 'pw'}, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.resolver_match.url_name, 'home_view')
        self.client.post('/accounts/logout/')
        self.assertEqual(self.client.get('/').status_code, 302)

    def test_csrf_state_changing_endpoints(self):
        get_user_model().objects.create_user('staff', password='pw', is_staff=True, is_superuser=True)
        c = Client(enforce_csrf_checks=True)
        c.login(username='staff', password='pw')
        for path in ['/set_active_tab/', '/oauth/device/start', '/oauth/device/poll', '/oauth/disconnect', '/test_radarr_connection/', '/test_sonarr_connection/']:
            self.assertEqual(c.post(path, data='{}', content_type='application/json').status_code, 403)
        self.assertEqual(c.get('/test_radarr_connection/').status_code, 405)

    def test_arr_blank_edit_preserves_saved_key_and_html_masks_keys(self):
        r = RadarrInstance.objects.create(name='r', url='http://r', apikey='saved-radarr', quality_profile='1', root_folder='/m')
        s = SonarrInstance.objects.create(name='s', url='http://s', apikey='saved-sonarr', quality_profile='1', root_folder='/t')
        get_user_model().objects.create_user('staff', password='pw', is_staff=True, is_superuser=True)
        self.client.login(username='staff', password='pw')
        with patch('mdblistrr.services.MDBListarr.test_radarr_connection', return_value={'status': True, 'version': 'ok'}):
            self.client.post('/', {'form_type':'radarr_save','instance_id':str(r.id),'name':'r','url':'http://r','apikey':'','quality_profile':'1','root_folder':'/m'})
        self.assertEqual(RadarrInstance.objects.get(id=r.id).apikey, 'saved-radarr')
        html = self.client.get('/').content.decode()
        self.assertNotIn('saved-radarr', html)
        self.assertNotIn('saved-sonarr', html)

    def test_mdblist_form_candidate_key_validation_and_blank_preserves(self):
        Preferences.set_secret('mdblist_apikey', 'existing-valid')
        with patch('mdblistrr.views.MdblistAPI.test_api', return_value=False):
            form = __import__('mdblistrr.views', fromlist=['MDBListForm']).MDBListForm({'mdblist_apikey':'bad','sync_instance_scope':'first','sync_hour':'1'}, oauth_connected=False)
            self.assertFalse(form.is_valid())
        with patch('mdblistrr.views.MdblistAPI.test_api', return_value=True):
            form = __import__('mdblistrr.views', fromlist=['MDBListForm']).MDBListForm({'mdblist_apikey':'good','sync_instance_scope':'first','sync_hour':'1'}, oauth_connected=False)
            self.assertTrue(form.is_valid())
        get_user_model().objects.create_user('staff', password='pw', is_staff=True, is_superuser=True)
        self.client.login(username='staff', password='pw')
        self.client.post('/', {'form_type':'mdblist','mdblist_apikey':'','sync_instance_scope':'first','sync_hour':'1'})
        self.assertEqual(Preferences.get_secret('mdblist_apikey'), 'existing-valid')

    def test_exception_sanitizing_and_arr_headers(self):
        secret = 'abcdefghijklmnopqrstuvwxyz123456'
        self.assertNotIn(secret, sanitize_text(f'failed apikey={secret} bearer {secret}'))
        sonarr = SonarrAPI(url='http://sonarr', apikey=secret)
        with patch.object(sonarr.connect, 'get_json', return_value={'status': 1, 'json': {'instanceName':'s','version':'1'}}) as get_json:
            sonarr.get_status()
        self.assertEqual(get_json.call_args.kwargs['headers']['X-Api-Key'], secret)
        self.assertNotIn('apikey', get_json.call_args.kwargs.get('params') or {})
        radarr = RadarrAPI(url='http://radarr', apikey=secret)
        with patch.object(radarr.connect, 'get_json', side_effect=Exception(f'boom {secret}')):
            result = radarr.get_status()
        self.assertNotIn(secret, result['message'])

    def test_sonarr_seasonpass_uses_nested_v3_payload_without_monitoring_options(self):
        sonarr = SonarrAPI(url='http://sonarr', apikey='secret-key')
        with patch.object(sonarr.connect, 'post_json', return_value={'status_code': 202}) as post_json:
            result = sonarr.post_seasonpass(20, [(10, False), (11, True)])
        self.assertEqual(result, {'status_code': 202})
        self.assertEqual(post_json.call_args.args[0], 'http://sonarr/api/v3/seasonpass')
        payload = post_json.call_args.kwargs['json']
        self.assertEqual(payload, {'series': [{'id': 20, 'seasons': [{'seasonNumber': 10, 'monitored': False}, {'seasonNumber': 11, 'monitored': True}]}]})
        self.assertNotIn('monitoringOptions', payload)
        self.assertNotIn('seriesId', payload)
        self.assertNotIn('seasonNumber', payload)


    def test_sonarr_series_editor_monitor_payload_validation(self):
        sonarr = SonarrAPI(url='http://sonarr', apikey='secret-key')
        with patch.object(sonarr.connect, 'put_json', return_value={'status_code': 202}) as put_json:
            self.assertEqual(sonarr.put_series_monitor([123], True), {'status_code': 202})
        self.assertEqual(put_json.call_args.args[0], 'http://sonarr/api/v3/series/editor')
        self.assertEqual(put_json.call_args.kwargs['json'], {'seriesIds': [123], 'monitored': True})
        self.assertEqual(set(put_json.call_args.kwargs['json']), {'seriesIds', 'monitored'})
        with patch.object(sonarr.connect, 'put_json', return_value={'status_code': 202}) as put_json:
            sonarr.put_series_monitor([123], False)
        self.assertEqual(put_json.call_args.kwargs['json'], {'seriesIds': [123], 'monitored': False})
        for bad in ([], [0], [-1], [True], ['123'], [None]):
            with patch.object(sonarr.connect, 'put_json') as put_json:
                self.assertIn('error', sonarr.put_series_monitor(bad, True))
                put_json.assert_not_called()

    def test_background_code_uses_decrypted_credentials(self):
        r = RadarrInstance.objects.create(name='r', url='http://r', apikey='radarr-background', quality_profile='1', root_folder='/m')
        self.assertEqual(RadarrAPI(instance_id=r.id).apikey, 'radarr-background')

class RuntimeSecretBootstrapTests(TestCase):
    def test_bootstrap_generates_persistent_secrets_idempotently_and_silently(self):
        from mdblistrr.runtime_secrets import bootstrap_runtime_secrets
        with tempfile.TemporaryDirectory() as d:
            secret_dir = os.path.join(d, 'secrets')
            with env(MDBLISTARR_SECRET_DIR=secret_dir, DJANGO_SECRET_KEY=None, DJANGO_SECRET_KEY_FILE=None, MDBLISTARR_ENCRYPTION_KEY=None, MDBLISTARR_ENCRYPTION_KEY_FILE=None):
                with patch('mdblistrr.runtime_secrets.DEFAULT_SECRET_DIR', __import__('pathlib').Path(secret_dir)), patch('mdblistrr.runtime_secrets.DEFAULT_SECRET_FILES', {'DJANGO_SECRET_KEY': __import__('pathlib').Path(secret_dir)/'django_secret_key','MDBLISTARR_ENCRYPTION_KEY': __import__('pathlib').Path(secret_dir)/'mdblistarr_encryption_key'}):
                    bootstrap_runtime_secrets()
                    dk = open(os.path.join(secret_dir,'django_secret_key')).read().strip()
                    ek = open(os.path.join(secret_dir,'mdblistarr_encryption_key')).read().strip()
                    self.assertGreater(len(dk), 40); Fernet(ek.encode())
                    if os.name == 'posix':
                        self.assertEqual(oct(os.stat(secret_dir).st_mode & 0o777), '0o700')
                        self.assertEqual(oct(os.stat(os.path.join(secret_dir,'django_secret_key')).st_mode & 0o777), '0o600')
                    bootstrap_runtime_secrets()
                    self.assertEqual(open(os.path.join(secret_dir,'django_secret_key')).read().strip(), dk)
                    self.assertEqual(open(os.path.join(secret_dir,'mdblistarr_encryption_key')).read().strip(), ek)

    def test_secret_precedence_and_invalid_default_fail_closed(self):
        from mdblistrr.runtime_secrets import resolve_secret, SecretResolutionError
        with tempfile.TemporaryDirectory() as d:
            default = os.path.join(d, 'default')
            filep = os.path.join(d, 'file')
            open(filep,'w').write('from-file\n')
            with env(DJANGO_SECRET_KEY='from-env', DJANGO_SECRET_KEY_FILE=filep):
                self.assertEqual(resolve_secret('DJANGO_SECRET_KEY', default_path=default), 'from-file')
            with env(DJANGO_SECRET_KEY='from-env', DJANGO_SECRET_KEY_FILE=None):
                self.assertEqual(resolve_secret('DJANGO_SECRET_KEY', default_path=default), 'from-env')
            bad = os.path.join(d, 'badkey'); open(bad,'w').write('bad')
            with env(MDBLISTARR_ENCRYPTION_KEY=None, MDBLISTARR_ENCRYPTION_KEY_FILE=None):
                with self.assertRaises(SecretResolutionError):
                    resolve_secret('MDBLISTARR_ENCRYPTION_KEY', default_path=bad, required=True)
                self.assertEqual(open(bad).read(), 'bad')

    def test_setup_flow_creates_single_admin_and_then_disables_setup(self):
        User = get_user_model()
        response = self.client.get('/', follow=False)
        self.assertEqual(response.status_code, 302); self.assertIn('/setup/', response['Location'])
        self.assertEqual(self.client.get('/accounts/login/').status_code, 302)
        self.assertEqual(self.client.get('/setup/').status_code, 200)
        self.assertContains(self.client.post('/setup/', {'username':'owner','password1':'password','password2':'password'}), 'too common', status_code=200)
        response = self.client.post('/setup/', {'username':'owner','password1':'Safe-Password-12345','password2':'Safe-Password-12345'}, follow=False)
        self.assertEqual(response.status_code, 302); self.assertEqual(User.objects.filter(is_superuser=True,is_staff=True,is_active=True).count(), 1)
        user = User.objects.get(username='owner'); self.assertTrue(user.check_password('Safe-Password-12345'))
        self.assertEqual(self.client.get('/setup/').status_code, 302)
        self.client.logout(); self.assertEqual(self.client.post('/setup/', {'username':'other','password1':'Safe-Password-12345','password2':'Safe-Password-12345'}).status_code, 302)
        self.assertFalse(User.objects.filter(username='other').exists())

    def test_setup_required_json_then_auth_json_statuses(self):
        self.assertEqual(self.client.post('/set_active_tab/', data='{}', content_type='application/json').status_code, 503)
        User = get_user_model(); User.objects.create_user('staff', password='pw', is_staff=True, is_superuser=True)
        self.assertEqual(self.client.post('/set_active_tab/', data='{}', content_type='application/json').status_code, 401)
        User.objects.create_user('user', password='pw')
        self.client.login(username='user', password='pw')
        self.assertEqual(self.client.post('/set_active_tab/', data='{}', content_type='application/json').status_code, 403)

    def test_secret_file_failures_generation_precedence_and_management_discovery(self):
        from mdblistrr.runtime_secrets import resolve_secret, SecretResolutionError
        with tempfile.TemporaryDirectory() as d:
            missing = os.path.join(d, 'missing')
            default = os.path.join(d, 'default')
            with env(DJANGO_SECRET_KEY='env-value', DJANGO_SECRET_KEY_FILE=missing):
                with self.assertRaises(SecretResolutionError):
                    resolve_secret('DJANGO_SECRET_KEY', default_path=default, generate=True, required=True)
                self.assertFalse(os.path.exists(default))
            empty = os.path.join(d, 'empty'); open(empty, 'w').close()
            with env(DJANGO_SECRET_KEY_FILE=empty, DJANGO_SECRET_KEY=None):
                with self.assertRaises(SecretResolutionError):
                    resolve_secret('DJANGO_SECRET_KEY', default_path=default, generate=True, required=True)
                self.assertEqual(open(empty).read(), '')
            with env(DJANGO_SECRET_KEY='direct-value', DJANGO_SECRET_KEY_FILE=None):
                self.assertEqual(resolve_secret('DJANGO_SECRET_KEY', default_path=default, generate=True), 'direct-value')
                self.assertFalse(os.path.exists(default))
            filep = os.path.join(d, 'file'); open(filep, 'w').write('file-value\n')
            with env(DJANGO_SECRET_KEY='direct-value', DJANGO_SECRET_KEY_FILE=filep):
                self.assertEqual(resolve_secret('DJANGO_SECRET_KEY', default_path=default, generate=True), 'file-value')
            key_file = os.path.join(d, 'fernet')
            open(key_file, 'w').write(KEY + '\n')
            with env(MDBLISTARR_ENCRYPTION_KEY=None, MDBLISTARR_ENCRYPTION_KEY_FILE=None):
                with patch('mdblistrr.runtime_secrets.DEFAULT_SECRET_FILES', {'MDBLISTARR_ENCRYPTION_KEY': Path(key_file)}):
                    Preferences.objects.create(name='mdblist_apikey', value='plain-default-discovery')
                    call_command('encrypt_secrets')
                    self.assertTrue(Preferences.objects.get(name='mdblist_apikey').value.startswith(crypto.PREFIX))

    def test_no_clobber_concurrent_generation_and_link_failure_cleanup(self):
        from mdblistrr.runtime_secrets import resolve_secret, SecretResolutionError
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'secrets' / 'django_secret_key'
            errors = []
            results = []
            def worker():
                try:
                    results.append(resolve_secret('DJANGO_SECRET_KEY', default_path=path, generate=True, required=True))
                except Exception as exc:
                    errors.append(exc)
            with env(DJANGO_SECRET_KEY=None, DJANGO_SECRET_KEY_FILE=None):
                threads = [threading.Thread(target=worker) for _ in range(8)]
                for thread in threads: thread.start()
                for thread in threads: thread.join()
            self.assertFalse(errors)
            self.assertEqual(len(set(results)), 1)
            self.assertEqual(path.read_text().strip(), results[0])
            if os.name == 'posix':
                self.assertEqual(oct(path.parent.stat().st_mode & 0o777), '0o700')
                self.assertEqual(oct(path.stat().st_mode & 0o777), '0o600')
            self.assertFalse(list(path.parent.glob(f'.{path.name}.*')))

            path.write_text('original-secret\n')
            with patch('mdblistrr.runtime_secrets.os.link', side_effect=OSError('link unsupported')):
                with self.assertRaises(SecretResolutionError):
                    # Remove the file after the pre-check so creation is attempted, then
                    # ensure a failed safe publish never replaces an existing value.
                    existing = path.read_text()
                    path.unlink()
                    try:
                        resolve_secret('DJANGO_SECRET_KEY', default_path=path, generate=True, required=True)
                    finally:
                        path.write_text(existing)
            self.assertEqual(path.read_text(), 'original-secret\n')
            self.assertFalse(list(path.parent.glob(f'.{path.name}.*')))

    def test_both_generated_secret_file_permissions_and_values_not_printed(self):
        from mdblistrr.runtime_secrets import bootstrap_runtime_secrets
        with tempfile.TemporaryDirectory() as d:
            secret_dir = Path(d) / 'secrets'
            defaults = {'DJANGO_SECRET_KEY': secret_dir / 'django_secret_key', 'MDBLISTARR_ENCRYPTION_KEY': secret_dir / 'mdblistarr_encryption_key'}
            out, err = io.StringIO(), io.StringIO()
            with env(DJANGO_SECRET_KEY=None, DJANGO_SECRET_KEY_FILE=None, MDBLISTARR_ENCRYPTION_KEY=None, MDBLISTARR_ENCRYPTION_KEY_FILE=None):
                with patch('mdblistrr.runtime_secrets.DEFAULT_SECRET_FILES', defaults), redirect_stdout(out), redirect_stderr(err):
                    bootstrap_runtime_secrets()
            django_value = defaults['DJANGO_SECRET_KEY'].read_text().strip()
            fernet_value = defaults['MDBLISTARR_ENCRYPTION_KEY'].read_text().strip()
            self.assertNotIn(django_value, out.getvalue() + err.getvalue())
            self.assertNotIn(fernet_value, out.getvalue() + err.getvalue())
            if os.name == 'posix':
                self.assertEqual(oct(secret_dir.stat().st_mode & 0o777), '0o700')
                self.assertEqual(oct(defaults['DJANGO_SECRET_KEY'].stat().st_mode & 0o777), '0o600')
                self.assertEqual(oct(defaults['MDBLISTARR_ENCRYPTION_KEY'].stat().st_mode & 0o777), '0o600')

    def test_reset_db_semantics_preserve_generated_keys(self):
        from mdblistrr.runtime_secrets import bootstrap_runtime_secrets
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / 'db.sqlite3'
            db.write_text('db')
            secret_dir = Path(d) / 'secrets'
            defaults = {'DJANGO_SECRET_KEY': secret_dir / 'django_secret_key', 'MDBLISTARR_ENCRYPTION_KEY': secret_dir / 'mdblistarr_encryption_key'}
            with env(DJANGO_SECRET_KEY=None, DJANGO_SECRET_KEY_FILE=None, MDBLISTARR_ENCRYPTION_KEY=None, MDBLISTARR_ENCRYPTION_KEY_FILE=None):
                with patch('mdblistrr.runtime_secrets.DEFAULT_SECRET_FILES', defaults):
                    bootstrap_runtime_secrets()
            before = {name: path.read_text() for name, path in defaults.items()}
            db.unlink()
            self.assertEqual({name: path.read_text() for name, path in defaults.items()}, before)

    def test_admin_state_helper_password_usability_cases_and_static_path(self):
        User = get_user_model()
        inactive = User.objects.create_superuser('inactive', password='pw'); inactive.is_active = False; inactive.save()
        self.assertFalse(usable_administrator_exists())
        User.objects.create_user('staffonly', password='pw', is_staff=True)
        self.assertFalse(usable_administrator_exists())
        User.objects.create_user('superonly', password='pw', is_superuser=True)
        self.assertFalse(usable_administrator_exists())
        unusable = User.objects.create_user('unusable', is_staff=True, is_superuser=True); unusable.set_unusable_password(); unusable.save()
        self.assertFalse(usable_administrator_exists())
        User.objects.create_user('valid', password='pw', is_staff=True, is_superuser=True)
        self.assertTrue(usable_administrator_exists())
        self.assertNotEqual(self.client.get('/static/example.css').status_code, 302)

    def test_setup_form_validation_csrf_methods_and_unavailable_cases(self):
        User = get_user_model()
        long_username = 'u' * (User._meta.get_field('username').max_length + 1)
        invalid_cases = [
            ({'username':'bad space','password1':'Safe-Password-12345','password2':'Safe-Password-12345'}, 'username'),
            ({'username':long_username,'password1':'Safe-Password-12345','password2':'Safe-Password-12345'}, 'username'),
            ({'username':'owner','password1':'Safe-Password-12345','password2':'Different-Password-12345'}, 'password2'),
            ({'username':'similarname','password1':'similarname','password2':'similarname'}, 'password2'),
            ({'username':'common','password1':'password','password2':'password'}, 'password2'),
            ({'username':'numeric','password1':'1234567890123','password2':'1234567890123'}, 'password2'),
        ]
        for data, field in invalid_cases:
            form = InitialAdminSetupForm(data)
            self.assertFalse(form.is_valid(), data)
            self.assertIn(field, form.errors)
        User.objects.create_user('duplicate')
        form = InitialAdminSetupForm({'username':'duplicate','password1':'Safe-Password-12345','password2':'Safe-Password-12345'})
        self.assertFalse(form.is_valid()); self.assertIn('username', form.errors)
        form = InitialAdminSetupForm({'username':'validowner','password1':'Safe-Password-12345','password2':'Safe-Password-12345'})
        self.assertTrue(form.is_valid(), form.errors)
        c = Client(enforce_csrf_checks=True)
        self.assertEqual(c.post('/setup/', {'username':'csrf','password1':'Safe-Password-12345','password2':'Safe-Password-12345'}).status_code, 403)
        self.assertEqual(self.client.put('/setup/').status_code, 405)
        User.objects.filter(username='duplicate').delete()
        with env(MDBLISTARR_ADMIN_USERNAME='headless', MDBLISTARR_ADMIN_PASSWORD='Safe-Password-12345'):
            call_command('secure_startup')
        self.assertEqual(self.client.get('/setup/').status_code, 302)
        count = User.objects.filter(is_superuser=True, is_staff=True, is_active=True).count()
        self.client.post('/setup/', {'username':'late','password1':'Safe-Password-12345','password2':'Safe-Password-12345'})
        self.assertEqual(User.objects.filter(is_superuser=True, is_staff=True, is_active=True).count(), count)

@override_settings(ALLOWED_HOSTS=['testserver'])
class SetupConcurrencyTests(TransactionTestCase):
    reset_sequences = True

    def test_concurrent_setup_claim_creates_exactly_one_admin(self):
        results = []
        errors = []
        with tempfile.TemporaryDirectory() as d:
            with patch('mdblistrr.views.SETUP_LOCK_PATH', os.path.join(d, '.initial-setup.lock')):
                def submit(username):
                    try:
                        client = Client()
                        response = client.post('/setup/', {
                            'username': username,
                            'password1': 'Safe-Password-12345',
                            'password2': 'Safe-Password-12345',
                        })
                        results.append((username, response.status_code))
                    except Exception as exc:
                        errors.append(exc)
                threads = [threading.Thread(target=submit, args=(name,)) for name in ('owner1', 'owner2')]
                for thread in threads: thread.start()
                for thread in threads: thread.join()
        self.assertFalse(errors)
        self.assertEqual(len(results), 2)
        self.assertTrue(all(status in (302, 303) for _, status in results), results)
        User = get_user_model()
        admins = list(User.objects.filter(is_active=True, is_staff=True, is_superuser=True))
        self.assertEqual(len(admins), 1)
        self.assertIn(admins[0].username, {'owner1', 'owner2'})
        self.assertEqual(User.objects.count(), 1)

class SonarrOnDemandDecisionTests(TestCase):
    def ep(self, sid=1, season=1, num=1, has=False, mon=False, air='2020-01-01T00:00:00Z'):
        return {'id': sid, 'seasonNumber': season, 'episodeNumber': num, 'hasFile': has, 'monitored': mon, 'airDateUtc': air}

    def test_american_dad_partial_retention_is_incomplete_and_reconciles(self):
        from .sonarr_reconcile import determine_series_completeness, calculate_episode_monitoring
        source = [self.ep(season=s, num=e, has=s <= 10, mon=s <= 10, sid=s*100+e) for s in range(1,22) for e in range(1,3)]
        target = [self.ep(season=s, num=e, has=False, mon=False, sid=s*100+e) for s in range(1,22) for e in range(1,3)]
        complete = determine_series_completeness(source)
        self.assertFalse(complete['complete'])
        self.assertEqual(complete['missing'], 22)
        stats = calculate_episode_monitoring(source, target, search_newly_eligible=True)
        self.assertEqual(len(stats.monitor_true_ids), 22)
        self.assertEqual(len(stats.monitor_false_ids), 0)
        self.assertEqual(set(stats.search_ids), set(stats.monitor_true_ids))

    def test_fully_retained_completely_absent_partial_future_and_specials(self):
        from .sonarr_reconcile import determine_series_completeness, calculate_episode_monitoring
        self.assertTrue(determine_series_completeness([self.ep(has=True), self.ep(sid=2, num=2, has=True)])['complete'])
        self.assertFalse(determine_series_completeness([self.ep(has=False)])['complete'])
        self.assertFalse(determine_series_completeness([self.ep(has=True), self.ep(sid=2, num=2, has=False)])['complete'])
        future = [self.ep(has=True), self.ep(sid=2, num=2, has=False, air='2999-01-01T00:00:00Z')]
        self.assertTrue(determine_series_completeness(future)['complete'])
        specials = [self.ep(season=0, has=False), self.ep(sid=2, has=True)]
        self.assertTrue(determine_series_completeness(specials)['complete'])
        self.assertFalse(determine_series_completeness(specials, include_specials=True)['complete'])
        stats = calculate_episode_monitoring([self.ep(has=False)], [self.ep(mon=False)])
        self.assertEqual(stats.monitor_true_ids, [1])

    def test_no_relevant_and_malformed_fail_closed(self):
        from .sonarr_reconcile import determine_series_completeness, calculate_episode_monitoring
        self.assertFalse(determine_series_completeness([self.ep(air='2999-01-01')])['complete'])
        self.assertTrue(determine_series_completeness({'bad': 'shape'})['malformed'])
        self.assertEqual(calculate_episode_monitoring([{}], [self.ep()]).failures, 1)

    def test_desired_series_monitoring_and_wanted_missing_ids(self):
        from .sonarr_reconcile import calculate_episode_monitoring
        wanted = calculate_episode_monitoring([], [self.ep(sid=1, has=False)])
        self.assertTrue(wanted.desired_series_monitoring)
        self.assertEqual(wanted.wanted_missing_episode_ids, [1])
        partial = calculate_episode_monitoring([self.ep(sid=10, num=1, has=True), self.ep(sid=11, num=2, has=False)], [self.ep(sid=1, num=1, has=False), self.ep(sid=2, num=2, has=False)])
        self.assertTrue(partial.desired_series_monitoring)
        self.assertEqual(partial.wanted_missing_episode_ids, [2])
        full = calculate_episode_monitoring([self.ep(sid=10, has=True)], [self.ep(sid=1, has=False)])
        self.assertFalse(full.desired_series_monitoring)
        self.assertFalse(calculate_episode_monitoring([], [self.ep(sid=1, air='2999-01-01T00:00:00Z')]).desired_series_monitoring)
        self.assertFalse(calculate_episode_monitoring([], [self.ep(sid=1, air=None)]).desired_series_monitoring)
        self.assertFalse(calculate_episode_monitoring([], [self.ep(sid=1, season=0)]).desired_series_monitoring)
        self.assertTrue(calculate_episode_monitoring([], [self.ep(sid=1, season=0)], include_specials=True).desired_series_monitoring)
        invalid = calculate_episode_monitoring([], [self.ep(sid='bad'), self.ep(sid=2, has=True), self.ep(sid=3, has=False)])
        self.assertEqual(invalid.failures, 1)

@override_settings(ALLOWED_HOSTS=['testserver'])
class SonarrSafetyTests(TestCase):
    def setUp(self):
        self.env = env(MDBLISTARR_ENCRYPTION_KEY=KEY)
        self.env.__enter__()
        self.source = SonarrInstance.objects.create(name='source', url='http://source', apikey='src', is_library_source=True)
        self.target = SonarrInstance.objects.create(name='target', url='http://target', apikey='tgt', is_library_source=False, is_ondemand_target=True)

    def tearDown(self):
        self.env.__exit__(None, None, None)

    def test_queue_disabled_makes_no_requests(self):
        from .cron import get_mdblist_queue_to_arr
        Preferences.set_value('enable_mdblist_queue_processing', '0')
        with patch('mdblistrr.services.MDBListarr._get_config'), patch('mdblistrr.arr.MdblistAPI.get_mdblist_queue') as q, patch('mdblistrr.arr.SonarrAPI.post_show') as post:
            res = get_mdblist_queue_to_arr()
        self.assertEqual(res['message'], 'MDBList queue processing disabled')
        q.assert_not_called(); post.assert_not_called()

    def test_queue_import_requires_profile_and_root(self):
        from django.core.exceptions import ValidationError
        self.target.enable_queue_import = True
        with self.assertRaises(ValidationError):
            self.target.full_clean()
        self.target.quality_profile = '1'; self.target.root_folder = '/tv'
        self.target.full_clean()

    def test_reconciliation_writes_only_target_and_is_idempotent(self):
        from .cron import reconcile_sonarr_ondemand
        Preferences.set_value('sonarr_reconciliation_enabled','1')
        Preferences.set_value('sonarr_reconciliation_source_id', str(self.source.id))
        Preferences.set_value('sonarr_reconciliation_target_id', str(self.target.id))
        series = [{'id': 10, 'tvdbId': 111, 'monitored': True, 'seasons': [{'seasonNumber': 1, 'monitored': False}]}]
        eps_source = [{'id':1,'seasonNumber':1,'episodeNumber':1,'hasFile':True,'airDateUtc':'2020-01-01T00:00:00Z'}]
        eps_target = [{'id':2,'seasonNumber':1,'episodeNumber':1,'hasFile':False,'monitored':True,'airDateUtc':'2020-01-01T00:00:00Z'}]
        def init(api_self, instance_id=None, **kw): api_self.instance_id = instance_id
        with patch('mdblistrr.cron.SonarrAPI.__init__', init), \
             patch('mdblistrr.cron.SonarrAPI.get_series', return_value=series), \
             patch('mdblistrr.cron.SonarrAPI.get_episodes', side_effect=[eps_source, eps_target]), \
             patch('mdblistrr.cron.SonarrAPI.put_episode_monitor', return_value={'ok': True}) as put, \
             patch('mdblistrr.cron.SonarrAPI.post_seasonpass', return_value={'status_code': 202}), \
             patch('mdblistrr.cron.SonarrAPI.put_series_monitor', return_value={'status_code': 202}):
            res = reconcile_sonarr_ondemand(force=True)
        self.assertEqual(res['result'], 200)
        put.assert_called_once_with([2], False)

class SonarrReviewFixTests(TestCase):
    def ep(self, sid=1, season=1, num=1, has=False, mon=False, air='2020-01-01T00:00:00Z'):
        return {'id': sid, 'seasonNumber': season, 'episodeNumber': num, 'hasFile': has, 'monitored': mon, 'airDateUtc': air}

    def test_missing_dates_are_unscheduled_not_malformed_for_completeness(self):
        from .sonarr_reconcile import determine_series_completeness
        result = determine_series_completeness([self.ep(has=True), self.ep(sid=2, has=False, air=None)])
        self.assertFalse(result['malformed'])
        self.assertTrue(result['complete'])
        self.assertEqual(result['unscheduled_ignored'], 1)

    def test_invalid_dates_still_fail_closed_for_completeness(self):
        from .sonarr_reconcile import determine_series_completeness
        result = determine_series_completeness([self.ep(has=True), self.ep(sid=2, has=False, air='not-a-date')])
        self.assertTrue(result['malformed'])
        self.assertFalse(result['complete'])


    def test_undated_target_episode_is_unmonitored_without_search_and_valid_peer_processed(self):
        from .sonarr_reconcile import calculate_episode_monitoring
        target = [
            {'id': 1, 'seasonNumber': 1, 'episodeNumber': 1, 'hasFile': False, 'monitored': True, 'airDateUtc': None, 'airDate': None},
            {'id': 2, 'seasonNumber': 1, 'episodeNumber': 2, 'hasFile': False, 'monitored': False, 'airDateUtc': None, 'airDate': None},
            {'id': 3, 'seasonNumber': 1, 'episodeNumber': 3, 'hasFile': False, 'monitored': False, 'airDateUtc': '2020-01-01T00:00:00Z'},
        ]
        stats = calculate_episode_monitoring([], target, search_newly_eligible=True)
        self.assertEqual(stats.failures, 0)
        self.assertEqual(stats.malformed_episodes, 0)
        self.assertEqual(stats.unscheduled_episodes_ignored, 2)
        self.assertEqual(stats.monitor_false_ids, [1])
        self.assertEqual(stats.monitor_true_ids, [3])
        self.assertEqual(stats.search_ids, [3])

    def test_blank_air_dates_are_unscheduled_and_do_not_search(self):
        from .sonarr_reconcile import calculate_episode_monitoring
        stats = calculate_episode_monitoring([], [{'id': 1, 'seasonNumber': 1, 'episodeNumber': 1, 'hasFile': False, 'monitored': False, 'airDateUtc': ' ', 'airDate': ''}], search_newly_eligible=True)
        self.assertEqual(stats.failures, 0)
        self.assertEqual(stats.monitor_true_ids, [])
        self.assertEqual(stats.monitor_false_ids, [])
        self.assertEqual(stats.search_ids, [])
        self.assertEqual(stats.episodes_unchanged, 1)

    def test_later_valid_air_date_becomes_normally_eligible(self):
        from .sonarr_reconcile import calculate_episode_monitoring
        undated = {'id': 1, 'seasonNumber': 1, 'episodeNumber': 1, 'hasFile': False, 'monitored': False, 'airDateUtc': None, 'airDate': None}
        aired = dict(undated, airDateUtc='2020-01-01T00:00:00Z')
        self.assertEqual(calculate_episode_monitoring([], [undated], search_newly_eligible=True).monitor_true_ids, [])
        stats = calculate_episode_monitoring([], [aired], search_newly_eligible=True)
        self.assertEqual(stats.monitor_true_ids, [1])
        self.assertEqual(stats.search_ids, [1])

    def test_non_empty_invalid_target_date_remains_malformed(self):
        from .sonarr_reconcile import calculate_episode_monitoring
        stats = calculate_episode_monitoring([], [{'id': 1, 'seasonNumber': 1, 'episodeNumber': 1, 'hasFile': False, 'monitored': False, 'airDateUtc': None, 'airDate': 'not-a-date'}])
        self.assertEqual(stats.failures, 1)
        self.assertEqual(stats.malformed_episodes, 1)
        self.assertEqual(stats.monitor_true_ids, [])
        self.assertEqual(stats.monitor_false_ids, [])

    def test_invalid_season_episode_values_are_malformed_not_raised(self):
        from .sonarr_reconcile import calculate_episode_monitoring, determine_series_completeness
        result = determine_series_completeness([self.ep(season='bad')])
        self.assertTrue(result['malformed'])
        stats = calculate_episode_monitoring([], [self.ep(season='bad')])
        self.assertEqual(stats.failures, 1)
        self.assertEqual(stats.malformed_episodes, 1)

    def test_transport_empty_non_2xx_and_invalid_json_fail(self):
        from .connect import Connect
        class Response:
            def __init__(self, status_code, text, body=None):
                self.status_code = status_code; self.text = text; self.content = text.encode(); self.headers = {}; self.body = body
            def json(self):
                if self.body is not None:
                    return self.body
                raise ValueError('bad json')
        c = Connect()
        with patch.object(c, 'put', return_value=Response(500, '')):
            self.assertEqual(c.put_json('http://x')['error'], 'Empty response from server')
        with patch.object(c, 'put', return_value=Response(500, 'not json')):
            self.assertEqual(c.put_json('http://x')['error'], 'Invalid PUT response')

    def test_non_2xx_json_lists_and_scalars_are_error_dicts_but_2xx_lists_pass_through(self):
        from .connect import Connect
        class Response:
            def __init__(self, status_code, body):
                self.status_code = status_code; self.body = body; self.text = 'json'; self.content = b'json'; self.headers = {}
            def json(self):
                return self.body
        c = Connect()
        with patch.object(c, 'put', return_value=Response(500, [{'msg': 'bad'}])):
            res = c.put_json('http://x')
            self.assertEqual(res['error'], 'HTTP request failed')
            self.assertEqual(res['status_code'], 500)
            self.assertIn('bad', res['decoded_response'])
        with patch.object(c, 'post', return_value=Response(500, [{'msg': 'bad'}])):
            res = c.post_json('http://x')
            self.assertEqual(res['error'], 'HTTP request failed')
            self.assertEqual(res['status_code'], 500)
        with patch.object(c, 'post', return_value=Response(503, 'temporarily unavailable')):
            res = c.post_json('http://x')
            self.assertEqual(res['error'], 'HTTP request failed')
            self.assertEqual(res['status_code'], 503)
            self.assertIn('temporarily unavailable', res['decoded_response'])
        with patch.object(c, 'put', return_value=Response(200, [{'msg': 'ok'}])):
            self.assertEqual(c.put_json('http://x'), [{'msg': 'ok'}])
        with patch.object(c, 'post', return_value=Response(201, [{'msg': 'ok'}])):
            self.assertEqual(c.post_json('http://x'), [{'msg': 'ok'}])

@override_settings(ALLOWED_HOSTS=['testserver'])
class SonarrReconciliationIntegrationReviewTests(TestCase):
    def setUp(self):
        self.env = env(MDBLISTARR_ENCRYPTION_KEY=KEY)
        self.env.__enter__()
        self.source = SonarrInstance.objects.create(name='source', url='http://source', apikey='src', is_library_source=True)
        self.target = SonarrInstance.objects.create(name='target', url='http://target', apikey='tgt', is_library_source=False, is_ondemand_target=True)
        Preferences.set_value('sonarr_reconciliation_enabled','1')
        Preferences.set_value('sonarr_reconciliation_source_id', str(self.source.id))
        Preferences.set_value('sonarr_reconciliation_target_id', str(self.target.id))
        Preferences.set_value('sonarr_search_newly_eligible', '1')

    def tearDown(self):
        self.env.__exit__(None, None, None)

    def _run(self, source_series, target_series, episodes_by_series, monitor_res={'status':'ok','status_code':202}, search_res={'status':'ok','status_code':201}, season_res={'status':'ok','status_code':202}):
        from .cron import reconcile_sonarr_ondemand
        def init(api_self, instance_id=None, **kw): api_self.instance_id = instance_id
        def get_series(api_self): return source_series if api_self.instance_id == self.source.id else target_series
        def get_episodes(api_self, series_id): return episodes_by_series[series_id]
        with patch('mdblistrr.cron.SonarrAPI.__init__', init), \
             patch('mdblistrr.cron.SonarrAPI.get_series', get_series), \
             patch('mdblistrr.cron.SonarrAPI.get_episodes', get_episodes), \
             patch('mdblistrr.cron.SonarrAPI.put_episode_monitor', return_value=monitor_res) as put, \
             patch('mdblistrr.cron.SonarrAPI.post_seasonpass', return_value=season_res), \
             patch('mdblistrr.cron.SonarrAPI.put_series_monitor', return_value={'status_code': 202}), \
             patch('mdblistrr.cron.SonarrAPI.trigger_episode_search', return_value=search_res) as search:
            res = reconcile_sonarr_ondemand(force=True)
        return res, put, search

    def test_target_only_series_monitors_aired_regulars_and_unmonitors_future_specials(self):
        target_series = [{'id': 20, 'tvdbId': 222, 'monitored': False, 'seasons': [{'seasonNumber': 0, 'monitored': False}, {'seasonNumber': 1, 'monitored': False}]}]
        eps = [
            {'id': 1, 'seasonNumber': 1, 'episodeNumber': 1, 'hasFile': False, 'monitored': False, 'airDateUtc': '2020-01-01T00:00:00Z'},
            {'id': 2, 'seasonNumber': 1, 'episodeNumber': 2, 'hasFile': False, 'monitored': True, 'airDateUtc': '2999-01-01T00:00:00Z'},
            {'id': 3, 'seasonNumber': 0, 'episodeNumber': 1, 'hasFile': False, 'monitored': True, 'airDateUtc': '2020-01-01T00:00:00Z'},
        ]
        res, put, search = self._run([], target_series, {20: eps})
        self.assertEqual(res['result'], 200)
        put.assert_any_call([1], True)
        put.assert_any_call([2, 3], False)
        search.assert_called_once_with([1])

    def test_malformed_target_episode_date_fails_without_writes(self):
        target_series = [{'id': 20, 'tvdbId': 222, 'monitored': False, 'seasons': [{'seasonNumber': 0, 'monitored': False}, {'seasonNumber': 1, 'monitored': False}]}]
        eps = [{'id': 1, 'seasonNumber': 1, 'episodeNumber': 1, 'hasFile': False, 'monitored': False, 'airDateUtc': 'invalid'}]
        res, put, search = self._run([], target_series, {20: eps})
        self.assertEqual(res['result'], 207)
        put.assert_not_called(); search.assert_not_called()

    def test_failed_monitor_responses_are_partial_failures_and_no_search_after_failed_true(self):
        target_series = [{'id': 20, 'tvdbId': 222, 'monitored': False, 'seasons': [{'seasonNumber': 0, 'monitored': False}, {'seasonNumber': 1, 'monitored': False}]}]
        eps = [{'id': 1, 'seasonNumber': 1, 'episodeNumber': 1, 'hasFile': False, 'monitored': False, 'airDateUtc': '2020-01-01T00:00:00Z'}]
        for failure in ({'error': 'Invalid PUT response', 'status_code': 500}, {'errorMessage': 'Sonarr rejected request'}):
            res, put, search = self._run([], target_series, {20: eps}, monitor_res=failure)
            self.assertEqual(res['result'], 207)
            self.assertGreater(res['failures'], 0)
            put.assert_called_once_with([1], True)
            search.assert_not_called()

    def test_failed_episode_search_command_is_partial_failure_after_successful_monitor(self):
        target_series = [{'id': 20, 'tvdbId': 222, 'monitored': False, 'seasons': [{'seasonNumber': 0, 'monitored': False}, {'seasonNumber': 1, 'monitored': False}]}]
        eps = [{'id': 1, 'seasonNumber': 1, 'episodeNumber': 1, 'hasFile': False, 'monitored': False, 'airDateUtc': '2020-01-01T00:00:00Z'}]
        res, put, search = self._run([], target_series, {20: eps}, search_res={'errorMessage': 'search failed'})
        self.assertEqual(res['result'], 207)
        put.assert_called_once_with([1], True)
        search.assert_called_once_with([1])

    def test_successful_monitor_to_true_is_followed_by_expected_search(self):
        target_series = [{'id': 20, 'tvdbId': 222, 'monitored': False, 'seasons': [{'seasonNumber': 0, 'monitored': False}, {'seasonNumber': 1, 'monitored': False}]}]
        eps = [{'id': 1, 'seasonNumber': 1, 'episodeNumber': 1, 'hasFile': False, 'monitored': False, 'airDateUtc': '2020-01-01T00:00:00Z'}]
        res, put, search = self._run([], target_series, {20: eps})
        self.assertEqual(res['result'], 200)
        put.assert_called_once_with([1], True)
        search.assert_called_once_with([1])

@override_settings(ALLOWED_HOSTS=['testserver'])
class SonarrSeriesResponseValidationTests(TestCase):
    def setUp(self):
        self.env = env(MDBLISTARR_ENCRYPTION_KEY=KEY)
        self.env.__enter__()
        self.source = SonarrInstance.objects.create(name='source', url='http://source', apikey='src', is_library_source=True)
        self.target = SonarrInstance.objects.create(name='target', url='http://target', apikey='tgt', is_library_source=False, is_ondemand_target=True)
        Preferences.set_value('sonarr_reconciliation_enabled','1')
        Preferences.set_value('sonarr_reconciliation_source_id', str(self.source.id))
        Preferences.set_value('sonarr_reconciliation_target_id', str(self.target.id))
        Preferences.set_value('sonarr_search_newly_eligible', '1')

    def tearDown(self):
        self.env.__exit__(None, None, None)

    def _run_with_series(self, source_series, target_series):
        from .cron import reconcile_sonarr_ondemand
        def init(api_self, instance_id=None, **kw): api_self.instance_id = instance_id
        def get_series(api_self): return source_series if api_self.instance_id == self.source.id else target_series
        with patch('mdblistrr.cron.SonarrAPI.__init__', init), \
             patch('mdblistrr.cron.SonarrAPI.get_series', get_series), \
             patch('mdblistrr.cron.SonarrAPI.get_episodes') as episodes, \
             patch('mdblistrr.cron.SonarrAPI.put_episode_monitor') as put, \
             patch('mdblistrr.cron.SonarrAPI.trigger_episode_search') as search:
            res = reconcile_sonarr_ondemand(force=True)
        return res, episodes, put, search

    def test_source_list_error_sentinel_aborts_before_fetch_or_writes(self):
        res, episodes, put, search = self._run_with_series(
            [{'result': 'Error connecting to Sonarr API'}],
            [{'id': 20, 'tvdbId': 222}],
        )
        self.assertEqual(res['result'], 502)
        self.assertIn('source_series_item_0_api_error', res['message'])
        episodes.assert_not_called(); put.assert_not_called(); search.assert_not_called()

    def test_source_dict_error_aborts_safely(self):
        res, episodes, put, search = self._run_with_series(
            {'error': 'connection_failed'},
            [{'id': 20, 'tvdbId': 222}],
        )
        self.assertEqual(res['result'], 502)
        self.assertEqual(res['message'], 'source_series_not_list')
        episodes.assert_not_called(); put.assert_not_called(); search.assert_not_called()

    def test_mixed_valid_and_error_or_malformed_source_list_aborts(self):
        for bad_item in ({'result': 'Error connecting to Sonarr API'}, {'id': 11}, 'not-a-dict'):
            res, episodes, put, search = self._run_with_series(
                [{'id': 10, 'tvdbId': 111}, bad_item],
                [{'id': 20, 'tvdbId': 222}],
            )
            self.assertEqual(res['result'], 502)
            episodes.assert_not_called(); put.assert_not_called(); search.assert_not_called()

    def test_malformed_target_series_response_aborts_without_writes(self):
        for target_series in ({'error': 'target failed'}, [{'id': 20}], [{'result': 'Error connecting to Sonarr API'}]):
            res, episodes, put, search = self._run_with_series(
                [{'id': 10, 'tvdbId': 111}],
                target_series,
            )
            self.assertEqual(res['result'], 502)
            episodes.assert_not_called(); put.assert_not_called(); search.assert_not_called()

    def test_missing_null_or_non_list_target_seasons_are_partial_failures(self):
        from .cron import reconcile_sonarr_ondemand
        target_eps = [{'id': 1, 'seasonNumber': 1, 'episodeNumber': 1, 'hasFile': False, 'monitored': True, 'airDateUtc': '2020-01-01T00:00:00Z'}]
        def init(api_self, instance_id=None, **kw): api_self.instance_id = instance_id
        for target_series in (
            [{'id': 20, 'tvdbId': 222}],
            [{'id': 20, 'tvdbId': 222, 'monitored': False, 'seasons': None}],
            [{'id': 20, 'tvdbId': 222, 'monitored': False, 'seasons': {'seasonNumber': 1, 'monitored': True}}],
        ):
            def get_series(api_self): return [] if api_self.instance_id == self.source.id else target_series
            with patch('mdblistrr.cron.SonarrAPI.__init__', init), \
                 patch('mdblistrr.cron.SonarrAPI.get_series', get_series), \
                 patch('mdblistrr.cron.SonarrAPI.get_episodes', return_value=target_eps), \
                 patch('mdblistrr.cron.SonarrAPI.put_episode_monitor') as put, \
                 patch('mdblistrr.cron.SonarrAPI.post_seasonpass') as seasonpass, \
                 patch('mdblistrr.cron.SonarrAPI.trigger_episode_search') as search:
                res = reconcile_sonarr_ondemand(force=True)
            self.assertEqual(res['result'], 207)
            put.assert_not_called()
            seasonpass.assert_not_called()
            search.assert_not_called()

    def test_empty_source_list_remains_valid_target_only_reconciliation(self):
        from .cron import reconcile_sonarr_ondemand
        target_series = [{'id': 20, 'tvdbId': 222, 'monitored': False, 'seasons': [{'seasonNumber': 0, 'monitored': False}, {'seasonNumber': 1, 'monitored': False}]}]
        target_eps = [{'id': 1, 'seasonNumber': 1, 'episodeNumber': 1, 'hasFile': False, 'monitored': False, 'airDateUtc': '2020-01-01T00:00:00Z'}]
        def init(api_self, instance_id=None, **kw): api_self.instance_id = instance_id
        def get_series(api_self): return [] if api_self.instance_id == self.source.id else target_series
        def get_episodes(api_self, series_id): return target_eps
        with patch('mdblistrr.cron.SonarrAPI.__init__', init), \
             patch('mdblistrr.cron.SonarrAPI.get_series', get_series), \
             patch('mdblistrr.cron.SonarrAPI.get_episodes', get_episodes), \
             patch('mdblistrr.cron.SonarrAPI.put_episode_monitor', return_value={'status': 'ok', 'status_code': 202}) as put, \
             patch('mdblistrr.cron.SonarrAPI.post_seasonpass', return_value={'status': 'ok', 'status_code': 202}), \
             patch('mdblistrr.cron.SonarrAPI.put_series_monitor', return_value={'status_code': 202}), \
             patch('mdblistrr.cron.SonarrAPI.trigger_episode_search', return_value={'status': 'ok', 'status_code': 201}) as search:
            res = reconcile_sonarr_ondemand(force=True)
        self.assertEqual(res['result'], 200)
        put.assert_called_once_with([1], True)
        search.assert_called_once_with([1])

    def test_valid_source_and_target_response_still_work(self):
        from .cron import reconcile_sonarr_ondemand
        source_series = [{'id': 10, 'tvdbId': 111, 'monitored': True, 'seasons': [{'seasonNumber': 1, 'monitored': True}]}]
        target_series = [{'id': 20, 'tvdbId': 111, 'monitored': True, 'seasons': [{'seasonNumber': 1, 'monitored': True}]}]
        episodes_by_series = {
            10: [{'id': 10, 'seasonNumber': 1, 'episodeNumber': 1, 'hasFile': True, 'airDateUtc': '2020-01-01T00:00:00Z'}],
            20: [{'id': 20, 'seasonNumber': 1, 'episodeNumber': 1, 'hasFile': False, 'monitored': True, 'airDateUtc': '2020-01-01T00:00:00Z'}],
        }
        def init(api_self, instance_id=None, **kw): api_self.instance_id = instance_id
        def get_series(api_self): return source_series if api_self.instance_id == self.source.id else target_series
        def get_episodes(api_self, series_id): return episodes_by_series[series_id]
        with patch('mdblistrr.cron.SonarrAPI.__init__', init), \
             patch('mdblistrr.cron.SonarrAPI.get_series', get_series), \
             patch('mdblistrr.cron.SonarrAPI.get_episodes', get_episodes), \
             patch('mdblistrr.cron.SonarrAPI.put_episode_monitor', return_value={'status': 'ok', 'status_code': 202}) as put, \
             patch('mdblistrr.cron.SonarrAPI.post_seasonpass', return_value={'status': 'ok', 'status_code': 202}), \
             patch('mdblistrr.cron.SonarrAPI.put_series_monitor', return_value={'status_code': 202}), \
             patch('mdblistrr.cron.SonarrAPI.trigger_episode_search') as search:
            res = reconcile_sonarr_ondemand(force=True)
        self.assertEqual(res['result'], 200)
        put.assert_called_once_with([20], False)
        search.assert_not_called()

@override_settings(ALLOWED_HOSTS=['testserver'])
class SonarrSeasonMonitoringReconciliationTests(TestCase):
    def setUp(self):
        self.env = env(MDBLISTARR_ENCRYPTION_KEY=KEY)
        self.env.__enter__()
        self.source = SonarrInstance.objects.create(name='source-season', url='http://source', apikey='src', is_library_source=True)
        self.target = SonarrInstance.objects.create(name='target-season', url='http://target', apikey='tgt', is_library_source=False, is_ondemand_target=True)
        Preferences.set_value('sonarr_reconciliation_enabled','1')
        Preferences.set_value('sonarr_reconciliation_source_id', str(self.source.id))
        Preferences.set_value('sonarr_reconciliation_target_id', str(self.target.id))
        Preferences.set_value('sonarr_search_newly_eligible', '0')
        Preferences.set_value('sonarr_include_specials', '0')

    def tearDown(self):
        self.env.__exit__(None, None, None)

    def ep(self, sid, season=1, num=1, has=False, mon=False, air='2020-01-01T00:00:00Z'):
        return {'id': sid, 'seasonNumber': season, 'episodeNumber': num, 'hasFile': has, 'monitored': mon, 'airDateUtc': air}

    def run_reconcile(self, source_series, target_series, episodes_by_series, monitor_res={'status_code': 202}, season_res={'status_code': 202}):
        from .cron import reconcile_sonarr_ondemand
        def init(api_self, instance_id=None, **kw): api_self.instance_id = instance_id
        def get_series(api_self): return source_series if api_self.instance_id == self.source.id else target_series
        def get_episodes(api_self, series_id): return episodes_by_series[series_id]
        with patch('mdblistrr.cron.SonarrAPI.__init__', init), \
             patch('mdblistrr.cron.SonarrAPI.get_series', get_series), \
             patch('mdblistrr.cron.SonarrAPI.get_episodes', get_episodes), \
             patch('mdblistrr.cron.SonarrAPI.put_episode_monitor', return_value=monitor_res) as put, \
             patch('mdblistrr.cron.SonarrAPI.post_seasonpass', return_value=season_res) as seasonpass, \
             patch('mdblistrr.cron.SonarrAPI.put_series_monitor', return_value={'status_code': 202}), \
             patch('mdblistrr.cron.SonarrAPI.trigger_episode_search') as search:
            res = reconcile_sonarr_ondemand(force=True)
        return res, put, seasonpass, search

    def test_all_desired_false_unmonitors_currently_monitored_season(self):
        source_series = [{'id': 10, 'tvdbId': 100}]
        target_series = [{'id': 20, 'tvdbId': 100, 'monitored': False, 'seasons': [{'seasonNumber': 1, 'monitored': True}]}]
        res, put, seasonpass, search = self.run_reconcile(source_series, target_series, {10: [self.ep(1, has=True)], 20: [self.ep(2, mon=True)]})
        self.assertEqual(res['result'], 200)
        put.assert_called_once_with([2], False)
        seasonpass.assert_called_once_with(20, [(1, False)])
        search.assert_not_called()

    def test_any_desired_true_monitors_currently_unmonitored_season(self):
        target_series = [{'id': 20, 'tvdbId': 200, 'monitored': False, 'seasons': [{'seasonNumber': 1, 'monitored': False}]}]
        res, put, seasonpass, _ = self.run_reconcile([], target_series, {20: [self.ep(1, mon=False)]})
        self.assertEqual(res['result'], 200)
        put.assert_called_once_with([1], True)
        seasonpass.assert_called_once_with(20, [(1, True)])

    def test_spongebob_style_complete_permanent_season_unmonitors_episodes_and_season(self):
        source_series = [{'id': 10, 'tvdbId': 75886}]
        target_series = [{'id': 20, 'tvdbId': 75886, 'monitored': False, 'seasons': [{'seasonNumber': 10, 'monitored': True}]}]
        source_eps = [self.ep(i, season=10, num=i, has=True) for i in range(1, 72)]
        target_eps = [self.ep(100+i, season=10, num=i, mon=True) for i in range(1, 72)]
        res, put, seasonpass, _ = self.run_reconcile(source_series, target_series, {10: source_eps, 20: target_eps})
        self.assertEqual(res['result'], 200)
        put.assert_called_once_with([e['id'] for e in target_eps], False)
        seasonpass.assert_called_once_with(20, [(10, False)])

    def test_future_only_season_is_false(self):
        target_series = [{'id': 20, 'tvdbId': 201, 'monitored': False, 'seasons': [{'seasonNumber': 1, 'monitored': True}]}]
        res, put, seasonpass, _ = self.run_reconcile([], target_series, {20: [self.ep(1, air='2999-01-01T00:00:00Z', mon=True)]})
        self.assertEqual(res['result'], 200)
        put.assert_called_once_with([1], False)
        seasonpass.assert_called_once_with(20, [(1, False)])

    def test_unscheduled_only_season_is_false(self):
        target_series = [{'id': 20, 'tvdbId': 202, 'monitored': False, 'seasons': [{'seasonNumber': 1, 'monitored': True}]}]
        res, put, seasonpass, _ = self.run_reconcile([], target_series, {20: [self.ep(1, air=None, mon=True)]})
        self.assertEqual(res['result'], 200)
        put.assert_called_once_with([1], False)
        seasonpass.assert_called_once_with(20, [(1, False)])

    def test_specials_follow_include_specials_preference(self):
        target_series = [{'id': 20, 'tvdbId': 203, 'monitored': False, 'seasons': [{'seasonNumber': 0, 'monitored': True}]}]
        res, put, seasonpass, _ = self.run_reconcile([], target_series, {20: [self.ep(1, season=0, mon=True)]})
        self.assertEqual(res['result'], 200)
        seasonpass.assert_called_once_with(20, [(0, False)])
        Preferences.set_value('sonarr_include_specials', '1')
        target_series[0]['seasons'][0]['monitored'] = False
        res, put, seasonpass, _ = self.run_reconcile([], target_series, {20: [self.ep(1, season=0, mon=False)]})
        self.assertEqual(res['result'], 200)
        seasonpass.assert_called_once_with(20, [(0, True)])

    def test_correct_season_flags_are_not_written_again(self):
        target_series = [{'id': 20, 'tvdbId': 204, 'monitored': False, 'seasons': [{'seasonNumber': 1, 'monitored': True}, {'seasonNumber': 2, 'monitored': False}]}]
        eps = [self.ep(1, season=1, mon=True), self.ep(2, season=2, air='2999-01-01T00:00:00Z', mon=False)]
        res, put, seasonpass, _ = self.run_reconcile([], target_series, {20: eps})
        self.assertEqual(res['result'], 200)
        put.assert_not_called()
        seasonpass.assert_not_called()

    def test_changed_seasons_are_batched_per_series(self):
        target_series = [{'id': 20, 'tvdbId': 208, 'monitored': False, 'seasons': [{'seasonNumber': 10, 'monitored': True}, {'seasonNumber': 11, 'monitored': False}]}]
        eps = [self.ep(1, season=10, mon=True, air='2999-01-01T00:00:00Z'), self.ep(2, season=11, mon=False)]
        res, put, seasonpass, _ = self.run_reconcile([], target_series, {20: eps})
        self.assertEqual(res['result'], 200)
        put.assert_any_call([2], True)
        put.assert_any_call([1], False)
        seasonpass.assert_called_once_with(20, [(10, False), (11, True)])

    def test_failed_episode_monitor_batch_prevents_season_updates_for_series(self):
        target_series = [{'id': 20, 'tvdbId': 205, 'monitored': False, 'seasons': [{'seasonNumber': 1, 'monitored': False}]}]
        res, put, seasonpass, _ = self.run_reconcile([], target_series, {20: [self.ep(1, mon=False)]}, monitor_res={'status_code': 500})
        self.assertEqual(res['result'], 207)
        put.assert_called_once_with([1], True)
        seasonpass.assert_not_called()

    def test_failed_season_update_is_partial_failure(self):
        target_series = [{'id': 20, 'tvdbId': 206, 'monitored': False, 'seasons': [{'seasonNumber': 1, 'monitored': False}]}]
        res, put, seasonpass, _ = self.run_reconcile([], target_series, {20: [self.ep(1)]}, season_res={'status_code': 500})
        self.assertEqual(res['result'], 207)
        self.assertEqual(res['failures'], 1)
        seasonpass.assert_called_once_with(20, [(1, True)])

    def test_no_source_writes_deletes_or_unrelated_series_changes(self):
        source_series = [{'id': 10, 'tvdbId': 207}]
        target_series = [{'id': 20, 'tvdbId': 207, 'monitored': False, 'seasons': [{'seasonNumber': 1, 'monitored': True}]}]
        res, put, seasonpass, _ = self.run_reconcile(source_series, target_series, {10: [self.ep(1, has=True)], 20: [self.ep(2, mon=True)]})
        self.assertEqual(res['result'], 200)
        self.assertEqual(put.call_count, 1)
        self.assertEqual(seasonpass.call_count, 1)

class SonarrDuplicateCleanupTests(TestCase):
    def setUp(self):
        os.environ['MDBLISTARR_ENCRYPTION_KEY'] = Fernet.generate_key().decode('ascii')
        self.target = SonarrInstance.objects.create(name='target-cleanup', url='http://target', apikey='tgt', is_ondemand_target=True, is_library_source=False)

    def ep(self, sid, season=1, num=1, has=True, mon=False, file_id=10, air='2020-01-01T00:00:00Z'):
        return {'id': sid, 'seasonNumber': season, 'episodeNumber': num, 'hasFile': has, 'monitored': mon, 'episodeFileId': file_id, 'airDateUtc': air}

    def test_delete_episode_files_uses_bulk_endpoint_body_and_headers(self):
        from .arr import SonarrAPI
        with patch('mdblistrr.arr.Connect.delete_json', return_value={'status':'ok','status_code':204}) as delete:
            api = SonarrAPI(instance_id=self.target.id)
            res = api.delete_episode_files([123, 124])
        self.assertEqual(res['status_code'], 204)
        args, kwargs = delete.call_args
        self.assertEqual(args[0], 'http://target/api/v3/episodefile/bulk')
        self.assertEqual(kwargs['json'], {'episodeFileIds': [123, 124]})
        self.assertEqual(kwargs['headers']['X-Api-Key'], 'tgt')

    def test_connect_delete_empty_2xx_success_and_non_2xx_failure(self):
        from .connect import Connect
        c = Connect()
        ok = type('R', (), {'status_code': 204, 'text': '', 'headers': {}, 'content': b'', 'json': lambda self: {}})()
        bad = type('R', (), {'status_code': 500, 'text': '', 'headers': {}, 'content': b'', 'json': lambda self: {}})()
        with patch.object(c, 'delete', return_value=ok):
            self.assertEqual(c.delete_json('http://x')['status'], 'ok')
        with patch.object(c, 'delete', return_value=bad):
            self.assertIn('error', c.delete_json('http://x'))

    def test_single_file_eligible_only_with_permanent_duplicate_reason(self):
        from .sonarr_reconcile import calculate_episode_monitoring
        from .sonarr_cleanup import eligible_episode_file_ids
        src = [self.ep(1, has=True)]
        tgt = [self.ep(2, has=True, mon=False, file_id=55)]
        stats = calculate_episode_monitoring(src, tgt)
        self.assertEqual(eligible_episode_file_ids(src, tgt, stats), {55: [[1,1]]})
        src[0]['hasFile'] = False
        stats = calculate_episode_monitoring(src, tgt)
        self.assertEqual(eligible_episode_file_ids(src, tgt, stats), {})

    def test_unmonitored_future_unscheduled_special_and_malformed_fail_closed(self):
        from .sonarr_reconcile import calculate_episode_monitoring
        from .sonarr_cleanup import eligible_episode_file_ids
        cases = [
            self.ep(2, mon=False, air='2999-01-01T00:00:00Z'),
            self.ep(2, mon=False, air=None),
            self.ep(2, season=0, mon=False),
            {'id': 2, 'seasonNumber': 'bad', 'episodeNumber': 1, 'hasFile': True, 'monitored': False, 'episodeFileId': 55},
        ]
        for tgt_ep in cases:
            src = [self.ep(1, season=tgt_ep.get('seasonNumber', 1), num=tgt_ep.get('episodeNumber', 1), has=True)] if tgt_ep.get('seasonNumber') != 'bad' else []
            stats = calculate_episode_monitoring(src, [tgt_ep])
            self.assertEqual(eligible_episode_file_ids(src, [tgt_ep], stats), {})

    def test_multi_episode_file_requires_every_linked_episode_duplicate(self):
        from .sonarr_reconcile import calculate_episode_monitoring
        from .sonarr_cleanup import eligible_episode_file_ids
        tgt = [self.ep(1, num=1, file_id=77), self.ep(2, num=2, file_id=77)]
        src = [self.ep(11, num=1, has=True), self.ep(12, num=2, has=True)]
        stats = calculate_episode_monitoring(src, tgt)
        self.assertEqual(eligible_episode_file_ids(src, tgt, stats), {77: [[1,1],[1,2]]})
        src[1]['hasFile'] = False
        stats = calculate_episode_monitoring(src, tgt)
        self.assertEqual(eligible_episode_file_ids(src, tgt, stats), {})

    def test_candidate_lifecycle_dry_run_limit_and_real_delete(self):
        from .sonarr_reconcile import calculate_episode_monitoring
        from .sonarr_cleanup import process_cleanup_for_series
        src = [self.ep(1, has=True)]
        tgt = [self.ep(2, file_id=88)]
        stats = calculate_episode_monitoring(src, tgt)
        class TargetAPI:
            def __init__(self): self.calls = 0
            def delete_episode_files(self, ids): return {'status':'ok','status_code':204}
            def get_episodes(self, sid):
                self.calls += 1
                return tgt if self.calls == 1 else [{'id':2,'seasonNumber':1,'episodeNumber':1,'hasFile':False,'monitored':False,'episodeFileId':88}]
        api = TargetAPI()
        source_api = type('S', (), {'get_episodes': lambda self, sid: src})()
        c = process_cleanup_for_series(target_instance=self.target, tvdb_id=1, target_series_id=2, source_episodes=src, target_episodes=tgt, stats=stats, target_api=api, source_api=source_api, source_series_id=2, cleanup_enabled=True, dry_run=True, grace_hours=0, remaining_delete_budget=25)
        self.assertEqual(c.cleanup_candidates_new, 1)
        c = process_cleanup_for_series(target_instance=self.target, tvdb_id=1, target_series_id=2, source_episodes=src, target_episodes=tgt, stats=stats, target_api=api, source_api=source_api, source_series_id=2, cleanup_enabled=True, dry_run=True, grace_hours=0, remaining_delete_budget=25)
        self.assertEqual(c.cleanup_would_delete, 1)
        c = process_cleanup_for_series(target_instance=self.target, tvdb_id=1, target_series_id=2, source_episodes=src, target_episodes=tgt, stats=stats, target_api=api, source_api=source_api, source_series_id=2, cleanup_enabled=True, dry_run=False, grace_hours=0, remaining_delete_budget=25)
        self.assertEqual(c.cleanup_files_deleted, 1)
        self.assertEqual(SonarrCleanupCandidate.objects.get(episode_file_id=88).status, SonarrCleanupCandidate.STATUS_DELETED)

class SonarrDuplicateCleanupSafetyHardeningTests(TestCase):
    def setUp(self):
        os.environ['MDBLISTARR_ENCRYPTION_KEY'] = Fernet.generate_key().decode('ascii')
        self.source = SonarrInstance.objects.create(name='source-hardening', url='http://source', apikey='src', is_library_source=True)
        self.target = SonarrInstance.objects.create(name='target-hardening', url='http://target', apikey='tgt', is_ondemand_target=True, is_library_source=False)

    def ep(self, sid, season=1, num=1, has=True, mon=False, file_id=10, air='2020-01-01T00:00:00Z'):
        return {'id': sid, 'seasonNumber': season, 'episodeNumber': num, 'hasFile': has, 'monitored': mon, 'episodeFileId': file_id, 'airDateUtc': air}

    def stats(self, src, tgt):
        from .sonarr_reconcile import calculate_episode_monitoring
        return calculate_episode_monitoring(src, tgt)

    def ready_candidate(self, file_id=10, keys=None, series_id=20):
        now = timezone.now() - timezone.timedelta(hours=2)
        return SonarrCleanupCandidate.objects.create(target_instance=self.target, tvdb_id=1, target_series_id=series_id, episode_file_id=file_id, linked_episode_keys=keys or [[1,1]], reason='permanent_duplicate', status=SonarrCleanupCandidate.STATUS_READY, first_eligible_at=now, last_confirmed_at=now, ready_at=now)

    def test_grouping_malformed_or_inconsistent_linked_records_blocks_file(self):
        from .sonarr_cleanup import eligible_episode_file_ids
        src = [self.ep(1, num=1), self.ep(2, num=2)]
        valid = self.ep(11, num=1, file_id=55)
        for extra in [
            {'id': 12, 'seasonNumber': 'bad', 'episodeNumber': 2, 'hasFile': True, 'monitored': False, 'episodeFileId': 55},
            self.ep(12, num=2, has=False, file_id=55),
            self.ep(12, num=2, mon=True, file_id=55),
            self.ep(12, num=1, file_id=55),
        ]:
            tgt = [valid, extra]
            self.assertEqual(eligible_episode_file_ids(src, tgt, self.stats(src, tgt)), {})

    def test_revalidation_blocks_delete_for_source_or_target_changes_and_uncertainty(self):
        from .sonarr_cleanup import process_cleanup_for_series
        cases = [
            ([self.ep(1, has=False)], [self.ep(2, file_id=80)], 'cancelled'),
            ([self.ep(1)], [self.ep(2, file_id=80, mon=True)], 'cancelled'),
            ([self.ep(1)], [self.ep(2, file_id=81)], 'cancelled'),
            ({'error': 'source down'}, [self.ep(2, file_id=80)], 'ready'),
            ([self.ep(1)], {'error': 'target down'}, 'ready'),
            ([{'id': 1, 'seasonNumber': 'bad'}], [self.ep(2, file_id=80)], 'ready'),
        ]
        for i, (fresh_src, fresh_tgt, expected) in enumerate(cases):
            SonarrCleanupCandidate.objects.all().delete()
            self.ready_candidate(file_id=80)
            deletes = []
            class API:
                def __init__(self, eps): self.eps = eps
                def get_episodes(self, sid): return self.eps
                def delete_episode_files(self, ids): deletes.append(ids); return {'status':'ok','status_code':204}
            src = [self.ep(1)]
            tgt = [self.ep(2, file_id=80)]
            c = process_cleanup_for_series(target_instance=self.target, tvdb_id=1, target_series_id=20, source_episodes=src, target_episodes=tgt, stats=self.stats(src, tgt), target_api=API(fresh_tgt), source_api=API(fresh_src), source_series_id=10, cleanup_enabled=True, dry_run=False, grace_hours=0, remaining_delete_budget=1)
            self.assertEqual(deletes, [], msg=i)
            self.assertEqual(SonarrCleanupCandidate.objects.get(episode_file_id=80).status, expected, msg=i)
            if expected == 'ready':
                self.assertEqual(c.cleanup_failures, 1)

    def test_changed_fully_eligible_linked_set_resets_grace(self):
        from .sonarr_cleanup import process_cleanup_for_series
        old = timezone.now() - timezone.timedelta(hours=2)
        cand = SonarrCleanupCandidate.objects.create(target_instance=self.target, tvdb_id=1, target_series_id=20, episode_file_id=90, linked_episode_keys=[[1,1]], reason='permanent_duplicate', status=SonarrCleanupCandidate.STATUS_READY, first_eligible_at=old, last_confirmed_at=old, ready_at=old)
        src = [self.ep(1, num=1), self.ep(2, num=2)]
        tgt = [self.ep(11, num=1, file_id=90), self.ep(12, num=2, file_id=90)]
        class API:
            def get_episodes(self, sid): return tgt if sid == 20 else src
            def delete_episode_files(self, ids): raise AssertionError('no delete')
        c = process_cleanup_for_series(target_instance=self.target, tvdb_id=1, target_series_id=20, source_episodes=src, target_episodes=tgt, stats=self.stats(src, tgt), target_api=API(), source_api=API(), source_series_id=10, cleanup_enabled=True, dry_run=False, grace_hours=24, remaining_delete_budget=1)
        cand.refresh_from_db()
        self.assertEqual(cand.status, SonarrCleanupCandidate.STATUS_PENDING)
        self.assertEqual(cand.linked_episode_keys, [[1,1],[1,2]])
        self.assertIsNone(cand.ready_at)
        self.assertEqual(c.cleanup_candidates_pending, 1)

    def test_terminal_reappearing_file_resets_grace_and_cancelled_clears_fields(self):
        from .sonarr_cleanup import process_cleanup_for_series
        old = timezone.now() - timezone.timedelta(days=3)
        for status in [SonarrCleanupCandidate.STATUS_DELETED, SonarrCleanupCandidate.STATUS_ALREADY_ABSENT, SonarrCleanupCandidate.STATUS_CANCELLED]:
            SonarrCleanupCandidate.objects.all().delete()
            SonarrCleanupCandidate.objects.create(target_instance=self.target, tvdb_id=1, target_series_id=20, episode_file_id=99, linked_episode_keys=[[1,1]], reason='permanent_duplicate', status=status, first_eligible_at=old, last_confirmed_at=old, ready_at=old, deleted_at=old, cancelled_at=old, last_error='stale')
            src = [self.ep(1)]
            tgt = [self.ep(2, file_id=99)]
            class API:
                def get_episodes(self, sid): return tgt if sid == 20 else src
                def delete_episode_files(self, ids): raise AssertionError('no delete')
            process_cleanup_for_series(target_instance=self.target, tvdb_id=1, target_series_id=20, source_episodes=src, target_episodes=tgt, stats=self.stats(src, tgt), target_api=API(), source_api=API(), source_series_id=10, cleanup_enabled=True, dry_run=False, grace_hours=24, remaining_delete_budget=1)
            cand = SonarrCleanupCandidate.objects.get(episode_file_id=99)
            self.assertEqual(cand.status, SonarrCleanupCandidate.STATUS_PENDING)
            self.assertIsNone(cand.ready_at)
            self.assertIsNone(cand.deleted_at)
            self.assertIsNone(cand.cancelled_at)
            self.assertEqual(cand.last_error, '')

    def test_attempt_budget_consumed_on_failures_and_stale_post_verify(self):
        from .sonarr_cleanup import process_cleanup_for_series
        scenarios = [({'error':'boom'}, [self.ep(2, file_id=70)], 1), ({'status':'ok','status_code':204}, [self.ep(2, file_id=70)], 1), ({'status':'ok','status_code':204}, {'error':'bad verify'}, 1)]
        for res, post_eps, expected_attempts in scenarios:
            SonarrCleanupCandidate.objects.all().delete()
            self.ready_candidate(file_id=70)
            deletes = []
            src = [self.ep(1)]
            tgt = [self.ep(2, file_id=70)]
            class API:
                def __init__(self): self.calls = 0
                def get_episodes(self, sid):
                    self.calls += 1
                    return tgt if self.calls == 1 else post_eps
                def delete_episode_files(self, ids): deletes.append(ids); return res
            target_api = API()
            class SourceAPI:
                def get_episodes(self, sid): return src
            c = process_cleanup_for_series(target_instance=self.target, tvdb_id=1, target_series_id=20, source_episodes=src, target_episodes=tgt, stats=self.stats(src, tgt), target_api=target_api, source_api=SourceAPI(), source_series_id=10, cleanup_enabled=True, dry_run=False, grace_hours=0, remaining_delete_budget=1)
            self.assertEqual(len(deletes), 1)
            self.assertEqual(c.delete_attempts_consumed, expected_attempts)
            if isinstance(post_eps, list):
                self.assertEqual(c.cleanup_failures, 1)

    def test_api_hardening_delete_response_shapes_and_bad_ids(self):
        from .connect import Connect
        from .arr import SonarrAPI
        c = Connect()
        class R:
            def __init__(self, status, text, payload=None): self.status_code=status; self.text=text; self.content=text.encode(); self.headers={}; self.payload=payload
            def json(self): return self.payload
        with patch.object(c, 'delete', return_value=R(200, '{"ok": true}', {'ok': True})):
            self.assertEqual(c.delete_json('http://x')['status_code'], 200)
        with patch.object(c, 'delete', return_value=R(200, '[1]', [1])):
            self.assertEqual(c.delete_json('http://x')['json'], [1])
        with patch.object(c, 'delete', side_effect=__import__('requests').exceptions.ConnectionError('apiKey=secret')):
            self.assertNotIn('secret', c.delete_json('http://x?apikey=secret')['exception'])
        api = SonarrAPI(instance_id=self.target.id)
        with patch('mdblistrr.arr.Connect.delete_json') as delete:
            self.assertIn('error', api.delete_episode_files([]))
            self.assertIn('errorMessage', api.delete_episode_files(['bad']))
            delete.assert_not_called()

class SonarrCleanupGlobalBudgetTests(TestCase):
    def setUp(self):
        os.environ['MDBLISTARR_ENCRYPTION_KEY'] = Fernet.generate_key().decode('ascii')
        self.target = SonarrInstance.objects.create(name='target-budget', url='http://target', apikey='tgt', is_ondemand_target=True, is_library_source=False)

    def ep(self, sid, season=1, num=1, has=True, mon=False, file_id=10, air='2020-01-01T00:00:00Z'):
        return {'id': sid, 'seasonNumber': season, 'episodeNumber': num, 'hasFile': has, 'monitored': mon, 'episodeFileId': file_id, 'airDateUtc': air}

    def ready(self, file_id, series_id, num=1):
        now = timezone.now() - timezone.timedelta(hours=2)
        SonarrCleanupCandidate.objects.create(target_instance=self.target, tvdb_id=series_id, target_series_id=series_id, episode_file_id=file_id, linked_episode_keys=[[1,num]], reason='permanent_duplicate', status=SonarrCleanupCandidate.STATUS_READY, first_eligible_at=now, last_confirmed_at=now, ready_at=now)

    def test_two_series_share_one_global_limit_and_defer_later_ready(self):
        from .sonarr_reconcile import calculate_episode_monitoring
        from .sonarr_cleanup import process_cleanup_for_series
        deletes = []
        class SourceAPI:
            def __init__(self, src): self.src = src
            def get_episodes(self, sid): return self.src[sid]
        class TargetAPI:
            def __init__(self, tgt): self.tgt = tgt; self.calls = {}
            def get_episodes(self, sid):
                self.calls[sid] = self.calls.get(sid, 0) + 1
                if self.calls[sid] % 2 == 1:
                    return self.tgt[sid]
                return [dict(ep, hasFile=False) for ep in self.tgt[sid]]
            def delete_episode_files(self, ids): deletes.append(ids); return {'status':'ok','status_code':204}
        src = {10: [self.ep(1, file_id=901)], 11: [self.ep(2, file_id=902)], 12: [self.ep(3, file_id=903)]}
        tgt = {20: [self.ep(20, file_id=100)], 21: [self.ep(21, file_id=101)], 22: [self.ep(22, file_id=102)]}
        for file_id, series_id in [(100,20),(101,21),(102,22)]:
            self.ready(file_id, series_id)
        remaining = 2
        deferred = 0
        target_api = TargetAPI(tgt)
        source_api = SourceAPI(src)
        for src_id, series_id in [(10,20),(11,21),(12,22)]:
            stats = calculate_episode_monitoring(src[src_id], tgt[series_id])
            c = process_cleanup_for_series(target_instance=self.target, tvdb_id=series_id, target_series_id=series_id, source_episodes=src[src_id], target_episodes=tgt[series_id], stats=stats, target_api=target_api, source_api=source_api, source_series_id=src_id, cleanup_enabled=True, dry_run=False, grace_hours=0, remaining_delete_budget=remaining)
            remaining -= c.delete_attempts_consumed
            deferred += c.cleanup_deferred_by_limit
        self.assertEqual(len(deletes), 2)
        self.assertEqual(remaining, 0)
        self.assertEqual(deferred, 1)

    def test_no_failure_path_exceeds_cap(self):
        from .sonarr_reconcile import calculate_episode_monitoring
        from .sonarr_cleanup import process_cleanup_for_series
        deletes = []
        class API:
            def get_episodes(self, sid): return [self_outer.ep(1, file_id=200)]
            def delete_episode_files(self, ids): deletes.append(ids); return {'error':'failed'}
        self_outer = self
        self.ready(200, 20)
        src = [self.ep(1, file_id=901)]
        tgt = [self.ep(2, file_id=200)]
        c = process_cleanup_for_series(target_instance=self.target, tvdb_id=20, target_series_id=20, source_episodes=src, target_episodes=tgt, stats=calculate_episode_monitoring(src, tgt), target_api=API(), source_api=API(), source_series_id=10, cleanup_enabled=True, dry_run=False, grace_hours=0, remaining_delete_budget=1)
        self.assertEqual(len(deletes), 1)
        self.assertEqual(c.delete_attempts_consumed, 1)
        c2 = process_cleanup_for_series(target_instance=self.target, tvdb_id=20, target_series_id=20, source_episodes=src, target_episodes=tgt, stats=calculate_episode_monitoring(src, tgt), target_api=API(), source_api=API(), source_series_id=10, cleanup_enabled=True, dry_run=False, grace_hours=0, remaining_delete_budget=0)
        self.assertEqual(len(deletes), 1)
        self.assertEqual(c2.cleanup_deferred_by_limit, 1)

class SonarrCleanupFinalSafetyTests(TestCase):
    def setUp(self):
        os.environ['MDBLISTARR_ENCRYPTION_KEY'] = Fernet.generate_key().decode('ascii')
        self.target = SonarrInstance.objects.create(name='target-final', url='http://target', apikey='tgt', is_ondemand_target=True, is_library_source=False)

    def ep(self, sid, season=1, num=1, has=True, mon=False, file_id=10, air='2020-01-01T00:00:00Z'):
        return {'id': sid, 'seasonNumber': season, 'episodeNumber': num, 'hasFile': has, 'monitored': mon, 'episodeFileId': file_id, 'airDateUtc': air}

    def stats(self, src, tgt):
        from .sonarr_reconcile import calculate_episode_monitoring
        return calculate_episode_monitoring(src, tgt)

    def ready(self, file_id, keys=None):
        now = timezone.now() - timezone.timedelta(hours=2)
        return SonarrCleanupCandidate.objects.create(target_instance=self.target, tvdb_id=1, target_series_id=20, episode_file_id=file_id, linked_episode_keys=keys or [[1,1]], reason='permanent_duplicate', status=SonarrCleanupCandidate.STATUS_READY, first_eligible_at=now, last_confirmed_at=now, ready_at=now)

    def test_delete_connection_error_is_one_http_call_one_attempt_and_stops(self):
        from .sonarr_cleanup import process_cleanup_for_series
        from .arr import SonarrAPI
        self.ready(300)
        self.ready(301, [[1,2]])
        src = [self.ep(1, num=1), self.ep(2, num=2)]
        tgt = [self.ep(11, num=1, file_id=300), self.ep(12, num=2, file_id=301)]
        class SourceAPI:
            def get_episodes(self, sid): return src
        target_api = SonarrAPI(instance_id=self.target.id)
        target_api.get_episodes = lambda sid: tgt
        with patch.object(target_api.connect.session, 'delete', side_effect=__import__('requests').exceptions.ConnectionError('apikey=secret')) as session_delete:
            c = process_cleanup_for_series(target_instance=self.target, tvdb_id=1, target_series_id=20, source_episodes=src, target_episodes=tgt, stats=self.stats(src, tgt), target_api=target_api, source_api=SourceAPI(), source_series_id=10, cleanup_enabled=True, dry_run=False, grace_hours=0, remaining_delete_budget=2)
        self.assertEqual(session_delete.call_count, 1)
        self.assertEqual(c.delete_attempts_consumed, 1)
        self.assertTrue(c.stop_deletes_for_run)
        self.assertEqual(c.cleanup_deferred_by_limit, 1)
        self.assertEqual(SonarrCleanupCandidate.objects.get(episode_file_id=300).status, SonarrCleanupCandidate.STATUS_READY)

    def test_uncertain_predelete_stops_second_ready_candidate_without_delete(self):
        from .sonarr_cleanup import process_cleanup_for_series
        self.ready(310)
        self.ready(311, [[1,2]])
        src = [self.ep(1, num=1), self.ep(2, num=2)]
        tgt = [self.ep(11, num=1, file_id=310), self.ep(12, num=2, file_id=311)]
        deletes = []
        class SourceAPI:
            def get_episodes(self, sid): return [{'result': 'Error connecting to Sonarr API'}]
        class TargetAPI:
            def get_episodes(self, sid): return tgt
            def delete_episode_files(self, ids): deletes.append(ids); return {'status':'ok'}
        c = process_cleanup_for_series(target_instance=self.target, tvdb_id=1, target_series_id=20, source_episodes=src, target_episodes=tgt, stats=self.stats(src, tgt), target_api=TargetAPI(), source_api=SourceAPI(), source_series_id=10, cleanup_enabled=True, dry_run=False, grace_hours=0, remaining_delete_budget=2)
        self.assertEqual(deletes, [])
        self.assertTrue(c.stop_deletes_for_run)
        self.assertEqual(c.cleanup_failures, 1)
        self.assertEqual(c.cleanup_deferred_by_limit, 1)
        self.assertEqual(SonarrCleanupCandidate.objects.get(episode_file_id=310).status, SonarrCleanupCandidate.STATUS_READY)

    def test_post_delete_validation_shapes_and_valid_absence(self):
        from .sonarr_cleanup import process_cleanup_for_series
        invalid_posts = [
            [{'result': 'Error connecting to Sonarr API'}],
            ['not-dict'],
            [{'id': 1, 'seasonNumber': 'bad', 'episodeNumber': 1, 'hasFile': False, 'monitored': False}],
            [{'id': 1, 'seasonNumber': 1, 'episodeNumber': 1, 'monitored': False}],
            [{'id': 1, 'seasonNumber': 1, 'episodeNumber': 1, 'hasFile': True, 'monitored': False}],
        ]
        src = [self.ep(1)]
        tgt = [self.ep(2, file_id=320)]
        for post in invalid_posts:
            SonarrCleanupCandidate.objects.all().delete()
            self.ready(320)
            class TargetAPI:
                def __init__(self): self.calls = 0
                def get_episodes(self, sid): self.calls += 1; return tgt if self.calls == 1 else post
                def delete_episode_files(self, ids): return {'status':'ok','status_code':204}
            class SourceAPI:
                def get_episodes(self, sid): return src
            c = process_cleanup_for_series(target_instance=self.target, tvdb_id=1, target_series_id=20, source_episodes=src, target_episodes=tgt, stats=self.stats(src, tgt), target_api=TargetAPI(), source_api=SourceAPI(), source_series_id=10, cleanup_enabled=True, dry_run=False, grace_hours=0, remaining_delete_budget=1)
            self.assertEqual(c.cleanup_failures, 1)
            self.assertTrue(c.stop_deletes_for_run)
            self.assertEqual(SonarrCleanupCandidate.objects.get(episode_file_id=320).status, SonarrCleanupCandidate.STATUS_READY)
        SonarrCleanupCandidate.objects.all().delete()
        self.ready(320)
        class ValidTargetAPI:
            def __init__(self): self.calls = 0
            def get_episodes(self, sid): self.calls += 1; return tgt if self.calls == 1 else [dict(tgt[0], hasFile=False, episodeFileId=0)]
            def delete_episode_files(self, ids): return {'status':'ok','status_code':204}
        class SourceAPI:
            def get_episodes(self, sid): return src
        c = process_cleanup_for_series(target_instance=self.target, tvdb_id=1, target_series_id=20, source_episodes=src, target_episodes=tgt, stats=self.stats(src, tgt), target_api=ValidTargetAPI(), source_api=SourceAPI(), source_series_id=10, cleanup_enabled=True, dry_run=False, grace_hours=0, remaining_delete_budget=1)
        self.assertEqual(c.cleanup_files_deleted, 1)

    def test_monitored_must_be_exact_false_and_duplicate_keys_block_all_files(self):
        from .sonarr_cleanup import eligible_episode_file_ids
        src = [self.ep(1, num=1), self.ep(2, num=2)]
        for bad_monitored in [{}, {'monitored': None}, {'monitored': 0}]:
            ep = self.ep(11, file_id=330)
            ep.update(bad_monitored)
            if bad_monitored == {}:
                ep.pop('monitored')
            self.assertEqual(eligible_episode_file_ids(src, [ep], self.stats(src, [ep])), {})
        same_file = [self.ep(11, num=1, file_id=331), self.ep(12, num=1, file_id=331)]
        self.assertEqual(eligible_episode_file_ids(src, same_file, self.stats(src, same_file)), {})
        different_files = [self.ep(11, num=1, file_id=332), self.ep(12, num=1, file_id=333)]
        self.assertEqual(eligible_episode_file_ids(src, different_files, self.stats(src, different_files)), {})

class SonarrCleanupLinkedAbsenceTests(TestCase):
    def setUp(self):
        os.environ['MDBLISTARR_ENCRYPTION_KEY'] = Fernet.generate_key().decode('ascii')
        self.target = SonarrInstance.objects.create(name='target-linked', url='http://target', apikey='tgt', is_ondemand_target=True, is_library_source=False)

    def ep(self, sid, season=1, num=1, has=True, mon=False, file_id=10, series_id=20, air='2020-01-01T00:00:00Z'):
        return {'id': sid, 'seriesId': series_id, 'seasonNumber': season, 'episodeNumber': num, 'hasFile': has, 'monitored': mon, 'episodeFileId': file_id, 'airDateUtc': air}

    def stats(self, src, tgt):
        from .sonarr_reconcile import calculate_episode_monitoring
        return calculate_episode_monitoring(src, tgt)

    def ready(self, file_id=400, keys=None, status=None):
        now = timezone.now() - timezone.timedelta(hours=2)
        return SonarrCleanupCandidate.objects.create(target_instance=self.target, tvdb_id=1, target_series_id=20, episode_file_id=file_id, linked_episode_keys=keys or [[1,1]], reason='permanent_duplicate', status=status or SonarrCleanupCandidate.STATUS_READY, first_eligible_at=now, last_confirmed_at=now, ready_at=now, deleted_at=now if status == SonarrCleanupCandidate.STATUS_DELETED else None)

    def test_empty_or_partial_predelete_response_is_uncertain_not_absent(self):
        from .sonarr_cleanup import process_cleanup_for_series
        cases = [[], [self.ep(11, num=1, file_id=400)]]
        for fresh_tgt in cases:
            SonarrCleanupCandidate.objects.all().delete()
            self.ready(400, [[1,1],[1,2]])
            src = [self.ep(1, num=1, series_id=10), self.ep(2, num=2, series_id=10)]
            tgt = [self.ep(11, num=1, file_id=400), self.ep(12, num=2, file_id=400)]
            deletes = []
            class SourceAPI:
                def get_episodes(self, sid): return src
            class TargetAPI:
                def get_episodes(self, sid): return fresh_tgt
                def delete_episode_files(self, ids): deletes.append(ids); return {'status':'ok'}
            c = process_cleanup_for_series(target_instance=self.target, tvdb_id=1, target_series_id=20, source_episodes=src, target_episodes=tgt, stats=self.stats(src, tgt), target_api=TargetAPI(), source_api=SourceAPI(), source_series_id=10, cleanup_enabled=True, dry_run=False, grace_hours=0, remaining_delete_budget=1)
            self.assertEqual(deletes, [])
            self.assertEqual(c.cleanup_failures, 1)
            self.assertEqual(SonarrCleanupCandidate.objects.get(episode_file_id=400).status, SonarrCleanupCandidate.STATUS_READY)

    def test_empty_or_partial_postdelete_response_is_uncertain_not_deleted(self):
        from .sonarr_cleanup import process_cleanup_for_series
        cases = [[], [self.ep(11, num=1, file_id=410)]]
        for post_tgt in cases:
            SonarrCleanupCandidate.objects.all().delete()
            self.ready(410, [[1,1],[1,2]])
            src = [self.ep(1, num=1, series_id=10), self.ep(2, num=2, series_id=10)]
            tgt = [self.ep(11, num=1, file_id=410), self.ep(12, num=2, file_id=410)]
            class SourceAPI:
                def get_episodes(self, sid): return src
            class TargetAPI:
                def __init__(self): self.calls = 0
                def get_episodes(self, sid): self.calls += 1; return tgt if self.calls == 1 else post_tgt
                def delete_episode_files(self, ids): return {'status':'ok','status_code':204}
            c = process_cleanup_for_series(target_instance=self.target, tvdb_id=1, target_series_id=20, source_episodes=src, target_episodes=tgt, stats=self.stats(src, tgt), target_api=TargetAPI(), source_api=SourceAPI(), source_series_id=10, cleanup_enabled=True, dry_run=False, grace_hours=0, remaining_delete_budget=1)
            self.assertEqual(c.delete_attempts_consumed, 1)
            self.assertEqual(c.cleanup_failures, 1)
            self.assertEqual(SonarrCleanupCandidate.objects.get(episode_file_id=410).status, SonarrCleanupCandidate.STATUS_READY)

    def test_valid_multi_episode_absence_confirms_deleted_or_already_absent(self):
        from .sonarr_cleanup import process_cleanup_for_series
        src = [self.ep(1, num=1, series_id=10), self.ep(2, num=2, series_id=10)]
        tgt = [self.ep(11, num=1, file_id=420), self.ep(12, num=2, file_id=420)]
        post = [dict(tgt[0], hasFile=False, episodeFileId=0), dict(tgt[1], hasFile=False, episodeFileId=0)]
        class SourceAPI:
            def get_episodes(self, sid): return src
        class TargetAPI:
            def __init__(self, result): self.calls = 0; self.result = result
            def get_episodes(self, sid): self.calls += 1; return tgt if self.calls == 1 else post
            def delete_episode_files(self, ids): return self.result
        self.ready(420, [[1,1],[1,2]])
        c = process_cleanup_for_series(target_instance=self.target, tvdb_id=1, target_series_id=20, source_episodes=src, target_episodes=tgt, stats=self.stats(src, tgt), target_api=TargetAPI({'status':'ok','status_code':204}), source_api=SourceAPI(), source_series_id=10, cleanup_enabled=True, dry_run=False, grace_hours=0, remaining_delete_budget=1)
        self.assertEqual(c.cleanup_files_deleted, 1)
        SonarrCleanupCandidate.objects.all().delete()
        self.ready(420, [[1,1],[1,2]])
        c = process_cleanup_for_series(target_instance=self.target, tvdb_id=1, target_series_id=20, source_episodes=src, target_episodes=tgt, stats=self.stats(src, tgt), target_api=TargetAPI({'error':'failed'}), source_api=SourceAPI(), source_series_id=10, cleanup_enabled=True, dry_run=False, grace_hours=0, remaining_delete_budget=1)
        self.assertEqual(c.cleanup_files_already_absent, 1)

    def test_series_id_mismatch_is_uncertain(self):
        from .sonarr_cleanup import process_cleanup_for_series
        self.ready(430)
        src = [self.ep(1, series_id=10)]
        tgt = [self.ep(11, file_id=430)]
        bad_target = [self.ep(11, file_id=430, series_id=999)]
        class SourceAPI:
            def get_episodes(self, sid): return src
        class TargetAPI:
            def get_episodes(self, sid): return bad_target
            def delete_episode_files(self, ids): raise AssertionError('no delete')
        c = process_cleanup_for_series(target_instance=self.target, tvdb_id=1, target_series_id=20, source_episodes=src, target_episodes=tgt, stats=self.stats(src, tgt), target_api=TargetAPI(), source_api=SourceAPI(), source_series_id=10, cleanup_enabled=True, dry_run=False, grace_hours=0, remaining_delete_budget=1)
        self.assertEqual(c.cleanup_failures, 1)
        self.assertEqual(SonarrCleanupCandidate.objects.get(episode_file_id=430).status, SonarrCleanupCandidate.STATUS_READY)

    def test_deleted_candidate_remains_deleted_when_linked_episodes_remain_absent(self):
        from .sonarr_cleanup import process_cleanup_for_series
        deleted = self.ready(440, status=SonarrCleanupCandidate.STATUS_DELETED)
        original_deleted_at = deleted.deleted_at
        src = [self.ep(1, series_id=10)]
        tgt = [self.ep(11, has=False, file_id=0)]
        class API:
            def get_episodes(self, sid): return tgt if sid == 20 else src
            def delete_episode_files(self, ids): raise AssertionError('no delete')
        c = process_cleanup_for_series(target_instance=self.target, tvdb_id=1, target_series_id=20, source_episodes=src, target_episodes=tgt, stats=self.stats(src, tgt), target_api=API(), source_api=API(), source_series_id=10, cleanup_enabled=True, dry_run=False, grace_hours=0, remaining_delete_budget=1)
        deleted.refresh_from_db()
        self.assertEqual(deleted.status, SonarrCleanupCandidate.STATUS_DELETED)
        self.assertEqual(deleted.deleted_at, original_deleted_at)
        self.assertEqual(c.cleanup_files_already_absent, 0)
        self.assertFalse(any('already_absent' in event for event in c.events))

class SonarrCleanupCompleteSeriesAndSeasonZeroTests(TestCase):
    def setUp(self):
        os.environ['MDBLISTARR_ENCRYPTION_KEY'] = Fernet.generate_key().decode('ascii')
        self.target = SonarrInstance.objects.create(name='target-complete-series', url='http://target', apikey='tgt', is_ondemand_target=True, is_library_source=False)

    def ep(self, sid, season=1, num=1, has=True, mon=False, file_id=10, series_id=20, air='2020-01-01T00:00:00Z'):
        return {'id': sid, 'seriesId': series_id, 'seasonNumber': season, 'episodeNumber': num, 'hasFile': has, 'monitored': mon, 'episodeFileId': file_id, 'airDateUtc': air}

    def ready(self, file_id=500, keys=None):
        now = timezone.now() - timezone.timedelta(hours=2)
        return SonarrCleanupCandidate.objects.create(target_instance=self.target, tvdb_id=1, target_series_id=20, episode_file_id=file_id, linked_episode_keys=keys or [[1,1],[1,2]], reason='permanent_duplicate', status=SonarrCleanupCandidate.STATUS_READY, first_eligible_at=now, last_confirmed_at=now, ready_at=now)

    def test_unrelated_episode_retaining_candidate_file_blocks_absence_pre_and_post_delete(self):
        from .sonarr_cleanup import process_cleanup_for_series
        from .sonarr_reconcile import calculate_episode_monitoring
        src = [self.ep(1, num=1, series_id=10), self.ep(2, num=2, series_id=10), self.ep(3, num=3, series_id=10), self.ep(4, num=4, series_id=10)]
        initial = [self.ep(11, num=1, file_id=500), self.ep(12, num=2, file_id=500), self.ep(13, num=3, file_id=777), self.ep(14, num=4, file_id=502)]
        unrelated_still_active = [dict(initial[0], hasFile=False, episodeFileId=0), dict(initial[1], hasFile=False, episodeFileId=0), self.ep(13, num=3, file_id=500), self.ep(14, num=4, file_id=502)]
        deletes = []
        self.ready(500)
        class SourceAPI:
            def get_episodes(self, sid): return src
        class PreTargetAPI:
            def get_episodes(self, sid): return unrelated_still_active
            def delete_episode_files(self, ids): deletes.append(ids); return {'status':'ok'}
        c = process_cleanup_for_series(target_instance=self.target, tvdb_id=1, target_series_id=20, source_episodes=src, target_episodes=initial, stats=calculate_episode_monitoring(src, initial), target_api=PreTargetAPI(), source_api=SourceAPI(), source_series_id=10, cleanup_enabled=True, dry_run=False, grace_hours=0, remaining_delete_budget=2)
        self.assertEqual(deletes, [])
        self.assertEqual(c.cleanup_files_deleted, 0)
        self.assertNotEqual(SonarrCleanupCandidate.objects.get(episode_file_id=500).status, SonarrCleanupCandidate.STATUS_DELETED)
        SonarrCleanupCandidate.objects.all().delete()
        self.ready(500)
        self.ready(501, [[1,3]])
        self.ready(502, [[1,4]])
        class PostTargetAPI:
            def __init__(self): self.calls = 0
            def get_episodes(self, sid): self.calls += 1; return initial if self.calls == 1 else unrelated_still_active
            def delete_episode_files(self, ids): deletes.append(ids); return {'status':'ok','status_code':204}
        deletes.clear()
        c = process_cleanup_for_series(target_instance=self.target, tvdb_id=1, target_series_id=20, source_episodes=src, target_episodes=initial, stats=calculate_episode_monitoring(src, initial), target_api=PostTargetAPI(), source_api=SourceAPI(), source_series_id=10, cleanup_enabled=True, dry_run=False, grace_hours=0, remaining_delete_budget=2)
        self.assertEqual(deletes, [[500]])
        self.assertEqual(SonarrCleanupCandidate.objects.get(episode_file_id=500).status, SonarrCleanupCandidate.STATUS_READY)
        self.assertEqual(c.cleanup_failures, 1)
        self.assertTrue(c.stop_deletes_for_run)
        self.assertEqual(c.cleanup_deferred_by_limit, 1)
        valid_absence = [dict(initial[0], hasFile=False, episodeFileId=0), dict(initial[1], hasFile=False, episodeFileId=0), dict(initial[2], episodeFileId=777), dict(initial[3], episodeFileId=502)]
        SonarrCleanupCandidate.objects.all().delete()
        self.ready(500)
        class ValidTargetAPI:
            def __init__(self): self.calls = 0
            def get_episodes(self, sid): self.calls += 1; return initial if self.calls == 1 else valid_absence
            def delete_episode_files(self, ids): return {'status':'ok','status_code':204}
        c = process_cleanup_for_series(target_instance=self.target, tvdb_id=1, target_series_id=20, source_episodes=src, target_episodes=initial, stats=calculate_episode_monitoring(src, initial), target_api=ValidTargetAPI(), source_api=SourceAPI(), source_series_id=10, cleanup_enabled=True, dry_run=False, grace_hours=0, remaining_delete_budget=1)
        self.assertEqual(c.cleanup_files_deleted, 1)

    def test_season_zero_keys_are_supported_and_invalid_linked_keys_fail_closed(self):
        from .sonarr_cleanup import candidate_file_state, eligible_episode_file_ids
        from .sonarr_reconcile import calculate_episode_monitoring
        cand = self.ready(600, [[0,1]])
        special = [self.ep(1, season=0, num=1, file_id=600)]
        self.assertEqual(candidate_file_state(special, cand, expected_series_id=20), 'active')
        source_special = [self.ep(10, season=0, num=1, file_id=900, series_id=10)]
        stats = calculate_episode_monitoring(source_special, special, include_specials=True)
        self.assertEqual(eligible_episode_file_ids(source_special, special, stats), {600: [[0,1]]})
        stats_disabled = calculate_episode_monitoring(source_special, special, include_specials=False)
        self.assertEqual(eligible_episode_file_ids(source_special, special, stats_disabled), {})
        for bad_keys in ([[[-1,1]], [['bad',1]], [[0,1],[0,1]]]):
            cand.linked_episode_keys = bad_keys
            self.assertEqual(candidate_file_state(special, cand, expected_series_id=20), 'uncertain')

@override_settings(ALLOWED_HOSTS=['testserver'])
class SonarrSeriesMonitoringInitialSearchTests(TestCase):
    def setUp(self):
        self.env = env(MDBLISTARR_ENCRYPTION_KEY=KEY)
        self.env.__enter__()
        self.source = SonarrInstance.objects.create(name='source-series', url='http://source', apikey='src', is_library_source=True)
        self.target = SonarrInstance.objects.create(name='target-series', url='http://target', apikey='tgt', is_ondemand_target=True)
        Preferences.set_value('sonarr_reconciliation_enabled','1')
        Preferences.set_value('sonarr_reconciliation_source_id', str(self.source.id))
        Preferences.set_value('sonarr_reconciliation_target_id', str(self.target.id))
        Preferences.set_value('sonarr_search_newly_eligible', '1')

    def tearDown(self):
        self.env.__exit__(None, None, None)

    def ep(self, sid, season=1, num=1, has=False, mon=True, air='2020-01-01T00:00:00Z'):
        return {'id': sid, 'seasonNumber': season, 'episodeNumber': num, 'hasFile': has, 'monitored': mon, 'airDateUtc': air}

    def test_the_dropout_unmonitored_series_gets_initial_missing_search_once_in_order(self):
        from .cron import reconcile_sonarr_ondemand
        calls = []
        target_series = [{'id': 20, 'tvdbId': 368117, 'monitored': False, 'seasons': [{'seasonNumber': 1, 'monitored': True}]}]
        target_eps = [self.ep(i, num=i) for i in range(1, 9)]
        def init(api_self, instance_id=None, **kw): api_self.instance_id = instance_id
        def get_series(api_self): return [] if api_self.instance_id == self.source.id else target_series
        def get_episodes(api_self, series_id): return target_eps
        def put_episode(ids, monitored): calls.append('episode'); return {'status_code': 202}
        def seasonpass(series_id, seasons): calls.append('season'); return {'status_code': 202}
        def series_monitor(ids, monitored): calls.append('series'); return {'status_code': 202}
        def search(ids): calls.append('search'); return {'status_code': 201}
        with patch('mdblistrr.cron.SonarrAPI.__init__', init), \
             patch('mdblistrr.cron.SonarrAPI.get_series', get_series), \
             patch('mdblistrr.cron.SonarrAPI.get_episodes', get_episodes), \
             patch('mdblistrr.cron.SonarrAPI.put_episode_monitor', side_effect=put_episode) as put, \
             patch('mdblistrr.cron.SonarrAPI.post_seasonpass', side_effect=seasonpass) as season, \
             patch('mdblistrr.cron.SonarrAPI.put_series_monitor', side_effect=series_monitor) as series, \
             patch('mdblistrr.cron.SonarrAPI.trigger_episode_search', side_effect=search) as search_mock, \
             patch('mdblistrr.cron.process_cleanup_for_series', return_value=type('C', (), {'delete_attempts_consumed':0,'stop_deletes_for_run':False,'cleanup_failures':0,'events':[], 'cleanup_candidates_new':0,'cleanup_candidates_pending':0,'cleanup_candidates_ready':0,'cleanup_candidates_cancelled':0,'cleanup_would_delete':0,'cleanup_files_deleted':0,'cleanup_files_already_absent':0,'cleanup_deferred_by_limit':0})()) as cleanup:
            res = reconcile_sonarr_ondemand(force=True)
        self.assertEqual(res['result'], 200)
        put.assert_not_called(); season.assert_not_called()
        series.assert_called_once_with([20], True)
        search_mock.assert_called_once_with(list(range(1, 9)))
        cleanup.assert_called_once()
        self.assertEqual(calls, ['series', 'search'])

    def test_unchanged_monitored_series_does_not_repeat_initial_search(self):
        from .cron import reconcile_sonarr_ondemand
        target_series = [{'id': 20, 'tvdbId': 1, 'monitored': True, 'seasons': [{'seasonNumber': 1, 'monitored': True}]}]
        target_eps = [self.ep(1)]
        def init(api_self, instance_id=None, **kw): api_self.instance_id = instance_id
        with patch('mdblistrr.cron.SonarrAPI.__init__', init), \
             patch('mdblistrr.cron.SonarrAPI.get_series', lambda api_self: [] if api_self.instance_id == self.source.id else target_series), \
             patch('mdblistrr.cron.SonarrAPI.get_episodes', return_value=target_eps), \
             patch('mdblistrr.cron.SonarrAPI.put_episode_monitor') as put, \
             patch('mdblistrr.cron.SonarrAPI.post_seasonpass') as season, \
             patch('mdblistrr.cron.SonarrAPI.put_series_monitor') as series, \
             patch('mdblistrr.cron.SonarrAPI.trigger_episode_search') as search, \
             patch('mdblistrr.cron.process_cleanup_for_series', return_value=type('C', (), {'delete_attempts_consumed':0,'stop_deletes_for_run':False,'cleanup_failures':0,'events':[], 'cleanup_candidates_new':0,'cleanup_candidates_pending':0,'cleanup_candidates_ready':0,'cleanup_candidates_cancelled':0,'cleanup_would_delete':0,'cleanup_files_deleted':0,'cleanup_files_already_absent':0,'cleanup_deferred_by_limit':0})()):
            res = reconcile_sonarr_ondemand(force=True)
        self.assertEqual(res['result'], 200)
        put.assert_not_called(); season.assert_not_called(); series.assert_not_called(); search.assert_not_called()

    def test_malformed_series_monitored_fails_closed(self):
        from .cron import reconcile_sonarr_ondemand
        target_series = [{'id': 20, 'tvdbId': 1, 'monitored': 'false', 'seasons': [{'seasonNumber': 1, 'monitored': True}]}]
        def init(api_self, instance_id=None, **kw): api_self.instance_id = instance_id
        with patch('mdblistrr.cron.SonarrAPI.__init__', init), \
             patch('mdblistrr.cron.SonarrAPI.get_series', lambda api_self: [] if api_self.instance_id == self.source.id else target_series), \
             patch('mdblistrr.cron.SonarrAPI.get_episodes') as episodes, \
             patch('mdblistrr.cron.SonarrAPI.put_episode_monitor') as put, \
             patch('mdblistrr.cron.SonarrAPI.post_seasonpass') as season, \
             patch('mdblistrr.cron.SonarrAPI.put_series_monitor') as series, \
             patch('mdblistrr.cron.SonarrAPI.trigger_episode_search') as search:
            res = reconcile_sonarr_ondemand(force=True)
        self.assertEqual(res['result'], 207)
        episodes.assert_not_called(); put.assert_not_called(); season.assert_not_called(); series.assert_not_called(); search.assert_not_called()

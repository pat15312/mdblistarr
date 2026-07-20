import os
import tempfile
from contextlib import contextmanager
from unittest.mock import patch, Mock

from cryptography.fernet import Fernet
from django.contrib.auth import get_user_model
from django.core.management import call_command, CommandError
from django.db import connection
from django.test import Client, TestCase, override_settings

from . import crypto
from .arr import MdblistAPI, RadarrAPI, SonarrAPI
from .connect import sanitize_text
from .models import Preferences, RadarrInstance, SonarrInstance
from .services import get_mdblistarr, reset_mdblistarr

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
        self.assertFalse(User.objects.get(id=legacy.id).is_active)
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

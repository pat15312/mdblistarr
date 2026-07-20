import os, tempfile
from django.contrib.auth import get_user_model
from django.core.management import call_command, CommandError
from django.test import TestCase, override_settings, Client
from django.db import connection
from cryptography.fernet import Fernet
from .crypto import PREFIX, decrypt, encrypt
from .models import Preferences, RadarrInstance, SonarrInstance

KEY = Fernet.generate_key().decode()

@override_settings(ALLOWED_HOSTS=['testserver'])
class SecurityTests(TestCase):
    def setUp(self):
        os.environ['MDBLISTARR_ENCRYPTION_KEY'] = KEY
        from . import crypto; crypto._fernet = None
    def test_auth_required_and_staff_only(self):
        self.assertEqual(self.client.get('/').status_code, 302)
        User=get_user_model()
        u=User.objects.create_user('user', password='pw')
        self.client.login(username='user', password='pw')
        self.assertEqual(self.client.get('/').status_code, 302)
        s=User.objects.create_user('staff', password='pw', is_staff=True)
        self.client.login(username='staff', password='pw')
        self.assertNotEqual(self.client.get('/').status_code, 302)
    def test_login_logout(self):
        get_user_model().objects.create_user('staff', password='pw', is_staff=True)
        self.assertTrue(self.client.login(username='staff', password='pw'))
        self.client.post('/accounts/logout/')
        self.assertEqual(self.client.get('/').status_code, 302)
    def test_encrypts_raw_storage_and_decrypts(self):
        r=RadarrInstance.objects.create(name='r', url='http://r', apikey='radarr-secret', quality_profile='1', root_folder='/m')
        s=SonarrInstance.objects.create(name='s', url='http://s', apikey='sonarr-secret', quality_profile='1', root_folder='/t')
        Preferences.objects.create(name='mdblist_apikey', value='mdb-secret')
        with connection.cursor() as c:
            c.execute('select apikey from mdblistrr_radarrinstance where id=%s',[r.id]); raw_r=c.fetchone()[0]
            c.execute('select apikey from mdblistrr_sonarrinstance where id=%s',[s.id]); raw_s=c.fetchone()[0]
            c.execute("select value from mdblistrr_preferences where name='mdblist_apikey'"); raw_p=c.fetchone()[0]
        self.assertTrue(raw_r.startswith(PREFIX)); self.assertNotIn('radarr-secret', raw_r)
        self.assertTrue(raw_s.startswith(PREFIX)); self.assertNotIn('sonarr-secret', raw_s)
        self.assertTrue(raw_p.startswith(PREFIX)); self.assertNotIn('mdb-secret', raw_p)
        self.assertEqual(RadarrInstance.objects.get(id=r.id).apikey, 'radarr-secret')
    def test_encrypt_command_migrates_plaintext_idempotently(self):
        with connection.cursor() as c:
            c.execute("insert into mdblistrr_radarrinstance(name,url,apikey,quality_profile,root_folder,created_at) values('r','http://r','plain','1','/',CURRENT_TIMESTAMP)")
        call_command('encrypt_secrets'); call_command('encrypt_secrets')
        self.assertEqual(RadarrInstance.objects.get(name='r').apikey, 'plain')
    def test_bootstrap_no_admin_admin_and_password_file(self):
        User=get_user_model()
        with self.assertRaises(CommandError): call_command('secure_startup')
        with tempfile.NamedTemporaryFile('w', delete=False) as f:
            f.write('safe-password\n'); path=f.name
        os.environ['MDBLISTARR_ADMIN_USERNAME']='owner'; os.environ['MDBLISTARR_ADMIN_PASSWORD_FILE']=path
        call_command('secure_startup')
        self.assertTrue(User.objects.get(username='owner').check_password('safe-password'))
        self.assertFalse(User.objects.filter(username='admin').exists())
        os.unlink(path); os.environ.pop('MDBLISTARR_ADMIN_PASSWORD_FILE',None); os.environ.pop('MDBLISTARR_ADMIN_USERNAME',None)
    def test_legacy_admin_disabled_changed_preserved(self):
        User=get_user_model(); legacy=User.objects.create_superuser('admin', password='admin')
        os.environ['MDBLISTARR_ADMIN_USERNAME']='owner'; os.environ['MDBLISTARR_ADMIN_PASSWORD']='safe-password'
        call_command('secure_startup')
        self.assertFalse(User.objects.get(id=legacy.id).is_active)
        User.objects.all().delete(); changed=User.objects.create_superuser('admin', password='changed-password')
        call_command('secure_startup')
        self.assertTrue(User.objects.get(id=changed.id).is_active)
    def test_csrf_post_required(self):
        User=get_user_model(); User.objects.create_user('staff', password='pw', is_staff=True)
        c=Client(enforce_csrf_checks=True); c.login(username='staff', password='pw')
        self.assertEqual(c.post('/set_active_tab/', data='{}', content_type='application/json').status_code, 403)
        self.assertEqual(c.get('/test_radarr_connection/').status_code, 405)

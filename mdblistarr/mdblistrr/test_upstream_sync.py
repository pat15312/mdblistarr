import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

from django.test import SimpleTestCase

from .arr import RadarrAPI, SonarrAPI


class ArrPathPrefixTests(SimpleTestCase):
    def test_sonarr_normalizes_trailing_slashes_and_preserves_path_prefix(self):
        self.assertEqual(SonarrAPI(url="http://sonarr:8989", apikey="key").url, "http://sonarr:8989")
        self.assertEqual(SonarrAPI(url="http://sonarr:8989/", apikey="key").url, "http://sonarr:8989")
        api = SonarrAPI(url="https://example.com/sonarr/", apikey="key")
        self.assertEqual(api.url, "https://example.com/sonarr")
        api.connect = Mock()
        api.get_status()
        self.assertEqual(api.connect.get_json.call_args.args[0], "https://example.com/sonarr/api/v3/system/status")

    def test_radarr_normalizes_trailing_slashes_and_preserves_path_prefix(self):
        api = RadarrAPI(url="example.com/radarr/", apikey="key")
        self.assertEqual(api.url, "http://example.com/radarr")
        api.connect = Mock()
        api.get_status()
        self.assertEqual(api.connect.get_json.call_args.args[0], "http://example.com/radarr/api/v3/system/status")


class EnvironmentSettingsTests(SimpleTestCase):
    settings_file = Path(__file__).resolve().parents[1] / "mdblist" / "settings.py"

    def load_values(self, **values):
        names = {
            "DJANGO_ALLOWED_HOSTS", "ALLOWED_HOSTS", "CSRF_TRUSTED_ORIGINS",
            "DJANGO_SECURE_PROXY_SSL_HEADER", "TRUST_PROXY_HEADERS", "TZ",
        }
        env = os.environ.copy()
        for name in names:
            env.pop(name, None)
        env.update({name: value for name, value in values.items() if value is not None})
        env["DJANGO_SECRET_KEY"] = "test-only-settings-secret"
        code = (
            "import importlib.util,json;"
            f"s=importlib.util.spec_from_file_location('sync_settings',{str(self.settings_file)!r});"
            "m=importlib.util.module_from_spec(s);s.loader.exec_module(m);"
            "print(json.dumps({'hosts':m.ALLOWED_HOSTS,'csrf':m.CSRF_TRUSTED_ORIGINS,"
            "'proxy':getattr(m,'SECURE_PROXY_SSL_HEADER',None),"
            "'forwarded_host':getattr(m,'USE_X_FORWARDED_HOST',False),'tz':m.TIME_ZONE}))"
        )
        return json.loads(subprocess.check_output(
            [sys.executable, "-c", code],
            cwd=self.settings_file.parent.parent,
            env=env,
            text=True,
        ))

    def test_allowed_hosts_prefers_existing_fork_variable(self):
        result = self.load_values(DJANGO_ALLOWED_HOSTS="fork.example, localhost", ALLOWED_HOSTS="upstream.example")
        self.assertEqual(result["hosts"], ["fork.example", "localhost"])

    def test_allowed_hosts_accepts_upstream_compatibility_alias(self):
        self.assertEqual(self.load_values(ALLOWED_HOSTS="one.example; two.example")["hosts"], ["one.example", "two.example"])

    def test_csrf_origins_accept_commas_and_semicolons(self):
        result = self.load_values(CSRF_TRUSTED_ORIGINS="https://one.example; https://two.example,http://three.example:5353")
        self.assertEqual(result["csrf"], ["https://one.example", "https://two.example", "http://three.example:5353"])

    def test_proxy_headers_are_not_trusted_by_default(self):
        result = self.load_values()
        self.assertIsNone(result["proxy"])
        self.assertFalse(result["forwarded_host"])

    def test_proxy_headers_require_explicit_opt_in(self):
        result = self.load_values(TRUST_PROXY_HEADERS="true")
        self.assertEqual(result["proxy"], ["HTTP_X_FORWARDED_PROTO", "https"])
        self.assertTrue(result["forwarded_host"])
        explicit = self.load_values(
            TRUST_PROXY_HEADERS="true",
            DJANGO_SECURE_PROXY_SSL_HEADER="HTTP_X_FORWARDED_SCHEME,https",
        )
        self.assertEqual(explicit["proxy"], ["HTTP_X_FORWARDED_SCHEME", "https"])

    def test_timezone_defaults_to_utc_and_accepts_tz(self):
        self.assertEqual(self.load_values()["tz"], "UTC")
        self.assertEqual(self.load_values(TZ="Europe/London")["tz"], "Europe/London")

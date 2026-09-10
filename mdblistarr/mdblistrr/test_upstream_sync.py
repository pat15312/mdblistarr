import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, TestCase

from .arr import RadarrAPI, SonarrAPI
from .cron import post_radarr_payload, post_sonarr_payload
from .models import Preferences


class CollectionBatchLimitTests(TestCase):
    collected_at = '2020-01-02T00:00:00Z'

    def setUp(self):
        Preferences.set_value('sync_hour', '0')
        Preferences.set_value('sync_library_status', '1')

    def assert_collection_batches(self, method, media, expected, sizes):
        batches = [call.args[0][media] for call in method.call_args_list]
        self.assertEqual([len(batch) for batch in batches], sizes)
        self.assertEqual([item for batch in batches for item in batch], expected)

    def check_sync(self, product, count, sizes):
        added_ids = range(1, count + 1)
        removed_ids = range(count + 1, count * 2 + 1)
        api = Mock()
        if product == 'radarr':
            media, id_key = 'movies', 'tmdb'
            sync = post_radarr_payload
            api_class = 'RadarrAPI'
            api.get_movies.return_value = [
                {'tmdbId': item_id, 'hasFile': item_id <= count,
                 'movieFile': {'dateAdded': self.collected_at}}
                for item_id in range(1, count * 2 + 1)
            ]
            api.get_exclusions.return_value = []
            expected_add = [
                {'ids': {id_key: item_id}, 'collected_at': self.collected_at}
                for item_id in added_ids
            ]
        else:
            media, id_key = 'shows', 'tvdb'
            sync = post_sonarr_payload
            api_class = 'SonarrAPI'
            api.get_series.return_value = [
                {'id': item_id, 'tvdbId': item_id}
                for item_id in range(1, count * 2 + 1)
            ]
            api.get_import_list_exclusions.return_value = []
            api.get_episode_files.side_effect = lambda item_id: (
                [{'id': item_id, 'dateAdded': self.collected_at}]
                if item_id <= count else []
            )
            api.get_episodes.side_effect = lambda item_id: [
                {'seasonNumber': 1, 'episodeNumber': number,
                 'airDateUtc': '2020-01-01T00:00:00Z',
                 'hasFile': item_id <= count, 'episodeFileId': item_id}
                for number in (1, 2)
            ]
            expected_add = [
                {'ids': {id_key: item_id}, 'seasons': [
                    {'number': 1, 'episodes': [
                        {'number': number, 'collected_at': self.collected_at}
                        for number in (1, 2)
                    ]}
                ]}
                for item_id in added_ids
            ]

        mdblist = Mock()
        mdblist.post_arr_payload.return_value = {'response': 'Ok'}
        mdblist.post_collection.return_value = {'updated': {media: 0}}
        mdblist.post_collection_remove.return_value = {'removed': {media: 0}}
        with (
            patch('mdblistrr.cron.reset_mdblistarr'),
            patch('mdblistrr.cron.get_mdblistarr', return_value=Mock(mdblist=mdblist)),
            patch(f'mdblistrr.cron.get_{product}_sync_instances', return_value=[Mock(id=1)]),
            patch(f'mdblistrr.cron.{api_class}', return_value=api),
        ):
            self.assertEqual(sync(force=True), {'response': 'Ok'})

        self.assert_collection_batches(mdblist.post_collection, media, expected_add, sizes)
        self.assert_collection_batches(
            mdblist.post_collection_remove, media,
            [{'ids': {id_key: item_id}} for item_id in removed_ids], sizes,
        )

    def test_radarr_collection_additions_and_removals_respect_batch_limit(self):
        for count, sizes in ((0, []), (200, [200]), (201, [200, 1]), (401, [200, 200, 1])):
            with self.subTest(count=count):
                self.check_sync('radarr', count, sizes)

    def test_sonarr_collection_additions_and_removals_preserve_episode_payloads(self):
        for count, sizes in ((0, []), (200, [200]), (201, [200, 1]), (401, [200, 200, 1])):
            with self.subTest(count=count):
                self.check_sync('sonarr', count, sizes)


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

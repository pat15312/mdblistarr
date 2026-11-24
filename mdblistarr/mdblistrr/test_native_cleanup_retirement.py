"""Retirement preserves historical intent and native submit-once recovery."""
import os
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from . import test_blacklist
from .models import JellyfinCleanup, Preferences, TitleCleanupCandidate, TitleCleanupEvent


class NativeCleanupRetirementTests(TestCase):
    # Reuse fixtures without inheriting (and rerunning) the existing test suite.
    setup_product = test_blacklist.TitleLifecycleTests.setup_product
    process = test_blacklist.TitleLifecycleTests.process

    def clear_candidates(self):
        JellyfinCleanup.objects.all().delete()
        TitleCleanupEvent.objects.all().delete()
        TitleCleanupCandidate.objects.all().delete()

    def legacy_evidence(self, candidate, status):
        row = JellyfinCleanup.objects.create(
            candidate=candidate, status=status,
            binding={'target_id': self.target.pk, 'root': '/retired/synthetic'},
            directories={'original': ['synthetic'], 'remaining': ['synthetic']},
            original_item_ids=['original-item'], original_series_ids=['original-series'],
            refresh_requested_at=timezone.now(), refresh_status='original-status',
            completed_at=timezone.now() if status == 'complete' else None,
            last_error='Original uncertain evidence must survive retirement.',
        )
        event = TitleCleanupEvent.objects.create(
            candidate=candidate, phase='jellyfin', outcome='original_retired_intent',
            evidence={'original_id': row.pk, 'status': status},
        )
        return (row.pk, JellyfinCleanup.objects.values().get(pk=row.pk),
                event.pk, TitleCleanupEvent.objects.values().get(pk=event.pk))

    def assert_legacy_unchanged(self, snapshot):
        row_id, row, event_id, event = snapshot
        self.assertEqual(JellyfinCleanup.objects.values().get(pk=row_id), row)
        self.assertEqual(TitleCleanupEvent.objects.values().get(pk=event_id), event)

    def test_every_retired_state_and_original_event_survive_native_processing(self):
        statuses = [value for value, _ in JellyfinCleanup._meta.get_field('status').choices]
        for product in ('sonarr', 'radarr'):
            for status in statuses:
                with self.subTest(product=product, status=status):
                    candidate = self.setup_product(product, with_file=False)
                    snapshot = self.legacy_evidence(candidate, status)
                    result = self.process()
                    self.assert_legacy_unchanged(snapshot)
                    candidate.refresh_from_db()
                    if status == 'complete':
                        self.assertEqual(result.attempts, 1)
                        self.assertEqual(candidate.status, 'deleted')
                    else:
                        self.assertEqual(result.attempts, 0)
                        self.assertTrue(result.stop)
                        self.assertEqual(self.api.deleted, [])
                        self.assertIn('unfinished evidence', candidate.last_error)
                    self.clear_candidates()

    def test_retired_pending_work_allows_only_original_native_read_only_recovery(self):
        for product in ('sonarr', 'radarr'):
            with self.subTest(product=product):
                candidate = self.setup_product(product)
                self.api.uncertain = True
                self.assertEqual(self.process().attempts, 1)
                snapshot = self.legacy_evidence(candidate, 'directory_submitting')
                original_intents = list(candidate.events.filter(
                    outcome='intent_persisted').values())
                self.assertEqual(self.process().attempts, 0)
                candidate.refresh_from_db()
                self.assertEqual(candidate.status, 'uncertain')
                self.api.files = []
                recovered = self.process()
                self.assertEqual(recovered.attempts, 0)
                candidate.refresh_from_db()
                self.assertEqual(candidate.status, 'files_removed')
                self.assertEqual(self.process().attempts, 0)
                self.assertEqual(len(self.api.deleted), 1)
                self.assertEqual(list(candidate.events.filter(
                    outcome='intent_persisted').values()), original_intents)
                self.assert_legacy_unchanged(snapshot)
                self.clear_candidates()

    def test_old_environment_cannot_enable_retired_calls_or_override_native_controls(self):
        legacy_environment = {
            'MDBLISTARR_JELLYFIN_ENABLED': '1',
            'MDBLISTARR_JELLYFIN_URL': 'http://retired.invalid',
            'MDBLISTARR_JELLYFIN_API_KEY_FILE': '/retired/secret',
            'MDBLISTARR_JELLYFIN_ROOTS': '{"sonarr":{"root":"/retired/series",'
                '"library_id":"' + 'a' * 32 + '"},"radarr":{"root":"/retired/movies",'
                '"library_id":"' + 'b' * 32 + '"}}',
        }
        with patch.dict(os.environ, legacy_environment), \
                patch('requests.sessions.Session.request', side_effect=AssertionError('Unexpected network call')), \
                patch('os.rmdir', side_effect=AssertionError('Unexpected directory removal')):
            for product in ('sonarr', 'radarr'):
                with self.subTest(product=product):
                    candidate = self.setup_product(product, with_file=False)
                    Preferences.set_value('blacklist_cleanup_enabled', '0')
                    self.assertEqual(self.process().attempts, 0)
                    Preferences.set_value('blacklist_cleanup_enabled', '1')
                    Preferences.set_value('blacklist_cleanup_dry_run', '1')
                    self.assertEqual(self.process().attempts, 0)
                    self.assertEqual(self.api.deleted, [])
                    Preferences.set_value('blacklist_cleanup_dry_run', '0')
                    self.assertEqual(self.process().attempts, 1)
                    candidate.refresh_from_db()
                    self.assertEqual(candidate.status, 'deleted')
                    self.assertFalse(JellyfinCleanup.objects.exists())
                    self.clear_candidates()

    def test_accepted_but_present_native_request_is_never_resent_and_recovers_without_next_stage(self):
        for product in ('sonarr', 'radarr'):
            for with_file in (True, False):
                with self.subTest(product=product, with_file=with_file):
                    candidate = self.setup_product(product, with_file=with_file)
                    native_request = self.api.cleanup_request

                    def accepted_request(method, resource, params=None, payload=None):
                        if method == 'DELETE':
                            self.api.deleted.append((resource, params))
                            return {'status_code': 202, 'data': None}
                        return native_request(method, resource, params=params, payload=payload)

                    self.api.cleanup_request = accepted_request
                    result = self.process()
                    self.assertEqual(result.attempts, 1)
                    self.assertTrue(result.stop)
                    candidate.refresh_from_db()
                    self.assertEqual(candidate.status, 'uncertain')
                    original_intents = list(candidate.events.filter(outcome='intent_persisted').values())
                    self.assertEqual(self.process().attempts, 0)
                    self.assertEqual(len(self.api.deleted), 1)
                    if with_file:
                        self.api.files = []
                    else:
                        self.api.row = None
                    self.assertEqual(self.process().attempts, 0)
                    candidate.refresh_from_db()
                    self.assertEqual(candidate.status, 'files_removed' if with_file else 'deleted')
                    self.assertEqual(len(self.api.deleted), 1)
                    self.assertEqual(list(candidate.events.filter(
                        outcome='intent_persisted').values()), original_intents)
                    self.clear_candidates()

    def test_final_native_recycle_bin_check_rejects_changed_or_malformed_configuration(self):
        invalid_configs = [None, [], {}, {'recycleBin': None},
                           {'recycleBin': False}, {'recycleBin': '/recycle'}]
        for product in ('sonarr', 'radarr'):
            for invalid in invalid_configs:
                with self.subTest(product=product, config=invalid):
                    candidate = self.setup_product(product)
                    native_request = self.api.cleanup_request
                    config_reads = []

                    def changed_config(method, resource, params=None, payload=None):
                        if method == 'GET' and resource == 'config/mediamanagement':
                            config_reads.append(resource)
                            return {'status_code': 200, 'data': (
                                {'deleteEmptyFolders': True, 'recycleBin': ''}
                                if len(config_reads) == 1 else invalid)}
                        return native_request(method, resource, params=params, payload=payload)

                    self.api.cleanup_request = changed_config
                    self.assertEqual(self.process().attempts, 0)
                    self.assertEqual(len(config_reads), 2)
                    self.assertEqual(self.api.deleted, [])
                    self.assertFalse(candidate.events.filter(outcome='intent_persisted').exists())
                    candidate.refresh_from_db()
                    self.assertTrue(candidate.last_error)
                    self.clear_candidates()

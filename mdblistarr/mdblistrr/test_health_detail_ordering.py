from datetime import datetime, timedelta, timezone as dt_timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from lxml import html

from .health_details import build_health_details
from .models import (Preferences, SonarrInstance, RadarrInstance,
    SonarrCleanupCandidate, RadarrCleanupCandidate, TitleCleanupCandidate)


class CleanupDetailOrderingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = get_user_model().objects.create_superuser('sorting-admin', password='password')
        cls.targets = {}
        for product, model in (('sonarr', SonarrInstance), ('radarr', RadarrInstance)):
            source = model.objects.create(name='Permanent', url='http://source', apikey='test')
            cls.targets[product] = model.objects.create(name='On Demand', url='http://target',
                apikey='test', is_library_source=False, is_ondemand_target=True)
            Preferences.set_value(f'{product}_reconciliation_source_id', str(source.pk))
            Preferences.set_value(f'{product}_reconciliation_target_id', str(cls.targets[product].pk))

    def setUp(self):
        self.serial = 0
        self.now = datetime(2026, 7, 1, 12, 0, 0, tzinfo=dt_timezone.utc)

    def candidate(self, product, metric, title, timestamp, *, whole=False, episodes=None):
        self.serial += 1
        number = self.serial
        common = dict(status=metric, target_title=title, first_eligible_at=self.now,
                      last_confirmed_at=self.now, deleted_at=None)
        common['first_eligible_at' if metric == 'pending' else 'deleted_at'] = timestamp
        if whole:
            return TitleCleanupCandidate.objects.create(product=product,
                target_instance_id=self.targets[product].pk, target_fingerprint='a' * 64,
                target_media_id=number, external_id=number, **common)
        if product == 'sonarr':
            return SonarrCleanupCandidate.objects.create(target_instance=self.targets[product],
                tvdb_id=number, target_series_id=number, episode_file_id=number,
                linked_episode_keys=[[1, 1]] if episodes is None else episodes, **common)
        return RadarrCleanupCandidate.objects.create(target_instance=self.targets[product],
            tmdb_id=number, source_movie_id=number, source_movie_file_id=number,
            target_movie_id=number, movie_file_id=number, **common)

    def refs(self, rows):
        return [('title' if isinstance(row, TitleCleanupCandidate) else 'cleanup', row.pk)
                for row in rows]

    def page_refs(self, product, metric, page=1):
        return [(entry['kind'], entry['id']) for entry in
                build_health_details(product, 'cleanup', metric, page)['page_obj']]

    def test_dates_display_precision_titles_and_both_cleanup_kinds(self):
        for product in ('sonarr', 'radarr'):
            for metric in ('pending', 'deleted'):
                with self.subTest(product=product, metric=metric):
                    # Insertion order and microseconds deliberately disagree with display order.
                    old = self.candidate(product, metric, 'Aardvark', self.now - timedelta(seconds=1))
                    zulu = self.candidate(product, metric, 'Zulu', self.now.replace(microsecond=900000))
                    beta = self.candidate(product, metric, 'beta', self.now, whole=True)
                    alpha = self.candidate(product, metric, '  alpha\t title  ', self.now.replace(microsecond=1))
                    upper = self.candidate(product, metric, 'ALPHA TITLE', self.now, whole=True)
                    blank = self.candidate(product, metric, '', self.now, whole=True)
                    whitespace = self.candidate(product, metric, ' \t\n', self.now)
                    newest = self.candidate(product, metric, 'Newest', self.now + timedelta(seconds=1), whole=True)
                    expected = [newest, alpha, upper, beta, zulu, whitespace, blank, old]
                    if metric == 'deleted':
                        unknown = self.candidate(product, metric, '', None)
                        known = self.candidate(product, metric, 'Known', None, whole=True)
                        expected += [known, unknown]
                    self.assertEqual(self.page_refs(product, metric), self.refs(expected))

    def test_numeric_episodes_multi_episode_files_and_whole_series(self):
        for metric in ('pending', 'deleted'):
            with self.subTest(metric=metric):
                whole = self.candidate('sonarr', metric, 'Example', self.now, whole=True)
                s10 = self.candidate('sonarr', metric, 'Example', self.now, episodes=[[10, 1]])
                s2e2 = self.candidate('sonarr', metric, 'Example', self.now, episodes=[[2, 2]])
                s1e10 = self.candidate('sonarr', metric, 'example', self.now, episodes=[[1, 10]])
                # The season and episode must come from the same earliest pair, not separate minima.
                multi = self.candidate('sonarr', metric, 'Example', self.now, episodes=[[2, 1], [1, 2], [1, 10]])
                s2e1 = self.candidate('sonarr', metric, 'Example', self.now, episodes=[[2, 1]])
                s1e1 = self.candidate('sonarr', metric, 'Example', self.now, episodes=[[1, 1]])
                duplicate = self.candidate('sonarr', metric, 'Example', self.now, episodes=[[1, 1]])
                special = self.candidate('sonarr', metric, 'Example', self.now, episodes=[[0, 3]])
                malformed = [self.candidate('sonarr', metric, 'Example', self.now, episodes=keys)
                             for keys in ([], [[False, 1]], [[1, 1], ['bad', 2]], {'invalid': [1, 2]})]
                expected = [special, s1e1, duplicate, multi, s1e10, s2e1, s2e2, s10, *malformed, whole]
                self.assertEqual(self.page_refs('sonarr', metric), self.refs(expected))
                self.assertEqual(self.page_refs('sonarr', metric), self.refs(expected))

    def test_unicode_case_insensitive_display_titles(self):
        for product in ('sonarr', 'radarr'):
            for metric in ('pending', 'deleted'):
                with self.subTest(product=product, metric=metric):
                    later = self.candidate(product, metric, 'Éclair Z', self.now)
                    first = self.candidate(product, metric, 'éclair A', self.now, whole=True)
                    self.assertEqual(self.page_refs(product, metric), self.refs([first, later]))

    def test_complete_union_is_sorted_before_pagination(self):
        for product in ('sonarr', 'radarr'):
            for metric in ('pending', 'deleted'):
                with self.subTest(product=product, metric=metric):
                    rows = [self.candidate(product, metric, f'Title {number:02d}', self.now,
                                          whole=number % 2 == 0) for number in reversed(range(54))]
                    expected = self.refs(list(reversed(rows)))
                    actual = []
                    for page, size in ((1, 25), (2, 25), (3, 4)):
                        with CaptureQueriesContext(connection) as queries:
                            result = build_health_details(product, 'cleanup', metric, page)['page_obj']
                        self.assertEqual((result.paginator.count, len(result)), (54, size))
                        actual += [(entry['kind'], entry['id']) for entry in result]
                        # Only a bounded reference page is fetched before hydrating its records.
                        selects = [q['sql'].upper() for q in queries if 'UNION ALL' in q['sql'].upper()
                                   and 'COUNT(' not in q['sql'].upper()]
                        self.assertEqual(len(selects), 1)
                        self.assertIn(f'LIMIT {size}', selects[0])
                        if page > 1:
                            self.assertIn(f'OFFSET {(page - 1) * 25}', selects[0])
                    self.assertEqual(actual, expected)
                    self.assertEqual(len(set(actual)), 54)

    def test_numeric_episode_order_crosses_page_boundary(self):
        for metric in ('pending', 'deleted'):
            rows = [self.candidate('sonarr', metric, 'Example', self.now, episodes=[[1, number]])
                    for number in reversed(range(1, 29))]
            self.assertEqual(self.page_refs('sonarr', metric) + self.page_refs('sonarr', metric, 2),
                             self.refs(list(reversed(rows))))

    def test_rendering_remains_read_only_and_preserves_display_and_other_ordering(self):
        self.client.force_login(self.staff)
        for product in ('sonarr', 'radarr'):
            old = self.candidate(product, 'deleted', 'Older', self.now - timedelta(seconds=1))
            new = self.candidate(product, 'deleted', 'Newer', self.now, whole=True)
            # Ready is outside the requested change and retains ID ordering.
            ready = [self.candidate(product, 'ready', title, self.now) for title in ('Zulu', 'Alpha')]
            before = list(TitleCleanupCandidate.objects.values())
            with patch('requests.sessions.Session.request', side_effect=AssertionError('network')):
                with CaptureQueriesContext(connection) as queries, timezone.override(ZoneInfo('Europe/London')):
                    response = self.client.get(reverse('arr_health_details_view', args=[product, 'cleanup', 'deleted']))
                self.assertEqual(self.page_refs(product, 'ready'), self.refs(ready))
            self.assertEqual(response.status_code, 200)
            doc = html.fromstring(response.content)
            self.assertEqual(doc.xpath('//tbody/tr/td[2]//strong/text()'), ['Newer', 'Older'])
            self.assertContains(response, '2026-07-01 13:00:00 BST')
            self.assertContains(response, 'Blacklisted')
            self.assertContains(response, 'Permanent duplicate')
            self.assertContains(response, 'Deleted at')
            self.assertEqual(list(TitleCleanupCandidate.objects.values()), before)
            self.assertFalse(any(q['sql'].lstrip().split()[0].upper() in
                                 ('UPDATE', 'INSERT', 'DELETE', 'REPLACE') for q in queries))
            old.refresh_from_db(); new.refresh_from_db()
            self.assertEqual((old.status, new.status), ('deleted', 'deleted'))

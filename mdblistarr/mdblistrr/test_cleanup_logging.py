from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from .cron import (
    MAX_CLEANUP_SERIES_SUMMARIES,
    MAX_CLEANUP_SERIES_TITLE_LENGTH,
    build_cleanup_series_summary,
    log_cleanup_series_summaries,
)


def counters(**overrides):
    values = {
        'cleanup_candidates_ready': 0,
        'cleanup_would_delete': 0,
        'cleanup_files_deleted': 0,
        'cleanup_files_already_absent': 0,
        'cleanup_deferred_by_limit': 0,
        'cleanup_failures': 0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class CleanupSeriesSummaryTests(SimpleTestCase):
    def show(self, title='Example Series', series_id=678, tvdb_id=12345):
        return {'title': title, 'id': series_id, 'tvdbId': tvdb_id}

    def test_dry_run_summary_has_one_safe_human_readable_counter_line(self):
        summary = build_cleanup_series_summary(
            self.show(), counters(cleanup_candidates_ready=3, cleanup_would_delete=3), True, True)
        self.assertEqual(
            summary['message'],
            'Sonarr cleanup series="Example Series" tvdb=12345 sonarr_series=678 '
            'mode=dry_run ready=3 would_delete=3 deleted=0 already_absent=0 deferred=0 failures=0')
        self.assertNotIn('episodeFileId', summary['message'])
        self.assertNotIn('/downloads/', summary['message'])

    def test_dry_run_requires_would_delete_activity(self):
        self.assertIsNone(build_cleanup_series_summary(self.show(), counters(), True, True))
        self.assertIsNone(build_cleanup_series_summary(
            self.show(), counters(cleanup_candidates_ready=3), True, True))

    def test_disabled_summary_requires_ready_and_claims_no_deletion(self):
        summary = build_cleanup_series_summary(
            self.show(), counters(cleanup_candidates_ready=2), False, True)
        self.assertIn('mode=disabled ready=2 would_delete=0 deleted=0', summary['message'])
        self.assertIsNone(build_cleanup_series_summary(self.show(), counters(), False, True))

    def test_live_summary_reports_every_cleanup_outcome(self):
        summary = build_cleanup_series_summary(self.show(), counters(
            cleanup_candidates_ready=5, cleanup_files_deleted=2,
            cleanup_files_already_absent=1, cleanup_deferred_by_limit=1,
            cleanup_failures=1), True, False)
        self.assertIn(
            'mode=live ready=5 would_delete=0 deleted=2 already_absent=1 deferred=1 failures=1',
            summary['message'])

    def test_live_summary_does_not_report_pending_candidate_only(self):
        self.assertIsNone(build_cleanup_series_summary(self.show(), counters(), True, False))

    def test_title_fallback_whitespace_quotes_length_and_secrets(self):
        for missing in (None, '', '  '):
            summary = build_cleanup_series_summary(
                self.show(missing), counters(cleanup_would_delete=1), True, True)
            self.assertIn('series="Unknown series"', summary['message'])
        title = 'Line one\n  "quoted"   apikey=super-secret-value ' + ('x' * 300)
        summary = build_cleanup_series_summary(
            self.show(title), counters(cleanup_would_delete=1), True, True)
        displayed = summary['message'].split('series="', 1)[1].split('" tvdb=', 1)[0]
        self.assertNotIn('\n', displayed)
        self.assertNotIn('"', displayed)
        self.assertNotIn('super-secret-value', displayed)
        self.assertLessEqual(len(displayed), MAX_CLEANUP_SERIES_TITLE_LENGTH)

    def test_multiple_series_counters_match_aggregate(self):
        summaries = [
            build_cleanup_series_summary(self.show('A', 1), counters(cleanup_would_delete=18), True, True),
            build_cleanup_series_summary(self.show('B', 2), counters(cleanup_would_delete=8), True, True),
        ]
        total = sum(int(item['message'].split('would_delete=', 1)[1].split()[0]) for item in summaries)
        self.assertEqual(total, 26)

    @patch('mdblistrr.cron.save_log')
    def test_logging_is_deterministic_bounded_and_truncated(self, save_log):
        summaries = []
        count = MAX_CLEANUP_SERIES_SUMMARIES + 37
        for index in reversed(range(count)):
            summaries.append(build_cleanup_series_summary(
                self.show(f'Series {index:03}', index + 1, index + 1000),
                counters(cleanup_would_delete=1), True, True))

        log_cleanup_series_summaries(2, summaries)

        self.assertEqual(save_log.call_count, MAX_CLEANUP_SERIES_SUMMARIES + 1)
        messages = [call.args[2] for call in save_log.call_args_list]
        self.assertIn('series="Series 000"', messages[0])
        self.assertIn('series="Series 099"', messages[99])
        self.assertEqual(
            messages[-1],
            'Sonarr cleanup series summaries truncated reported=100 additional=37')

    @patch('mdblistrr.cron.save_log')
    def test_failures_use_warning_and_success_and_truncation_use_info(self, save_log):
        success = build_cleanup_series_summary(
            self.show('Success', 1), counters(cleanup_files_deleted=1), True, False)
        failure = build_cleanup_series_summary(
            self.show('Failure', 2), counters(cleanup_failures=1), True, False)
        log_cleanup_series_summaries(2, [success, failure])
        statuses = {call.args[2]: call.args[1] for call in save_log.call_args_list}
        self.assertEqual(statuses[success['message']], 1)
        self.assertEqual(statuses[failure['message']], 2)


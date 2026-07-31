from django.test import TestCase
from django.utils import timezone
from .models import SonarrInstance, SonarrEpisodeSearchCandidate as Candidate, SonarrEpisodeSearchCommand as Command
from .sonarr_search import submit_pending_search_candidates, validate_episode_search_command, validate_command_list

class EpisodeSearchLifecycleTests(TestCase):
    def setUp(self):
        self.target = SonarrInstance.objects.create(name='target', url='http://target', apikey='', is_library_source=False, is_ondemand_target=True)
        self.now = timezone.now()
    def candidate(self, episode_id=1):
        return Candidate.objects.create(target_instance=self.target, target_series_id=20, target_episode_id=episode_id, tvdb_id=10, season_number=1, episode_number=episode_id, first_eligible_at=self.now, last_confirmed_at=self.now)
    def resource(self, **changes):
        value = {'id':7, 'name':'EpisodeSearch', 'status':'queued', 'result':'unknown', 'queued':self.now.isoformat(), 'body':{'name':'EpisodeSearch','episodeIds':[1]}}
        value.update(changes); return value
    def test_attempt_and_snapshot_exist_before_the_single_post(self):
        candidate = self.candidate(); outer = self
        class API:
            calls = 0
            def trigger_episode_search(self, ids):
                self.calls += 1; command = Command.objects.get()
                outer.assertEqual(command.status, 'submitting')
                outer.assertEqual(list(command.candidate_links.values_list('target_episode_id', flat=True)), [1])
                outer.assertEqual(Candidate.objects.get(pk=candidate.pk).current_command, command)
                return {'status_code':201, **outer.resource()}
        api=API(); counts, events, failed = submit_pending_search_candidates(target_api=api, target_instance=self.target, target_series_id=20, now=self.now)
        self.assertFalse(failed); self.assertEqual((api.calls, counts['submitted']), (1,1))
        candidate.refresh_from_db(); self.assertEqual((candidate.status,candidate.attempt_count), ('submitted',1)); self.assertNotIn('[1]', events[0])
    def test_malformed_success_is_ambiguous_and_protected(self):
        self.candidate()
        class API:
            def trigger_episode_search(self, ids): return {'status_code':201, 'id':True, 'name':'EpisodeSearch'}
        submit_pending_search_candidates(target_api=API(), target_instance=self.target, target_series_id=20)
        self.assertEqual(Command.objects.get().status, 'ambiguous'); self.assertEqual(Candidate.objects.get().status, 'submitted')
    def test_http_rejection_remains_pending_but_has_audit_attempt(self):
        self.candidate()
        class API:
            def trigger_episode_search(self, ids): return {'status_code':500}
        submit_pending_search_candidates(target_api=API(), target_instance=self.target, target_series_id=20)
        self.assertEqual(Command.objects.get().status, 'superseded'); self.assertEqual(Candidate.objects.get().status, 'pending')
    def test_strict_validation_and_unknown_result(self):
        self.assertEqual(validate_episode_search_command(self.resource(), [1], 7)['result'], 'unknown')
        for resource in (self.resource(id=True), self.resource(status='new-state'), self.resource(body={'name':'EpisodeSearch','episodeIds':[1,1]}), self.resource(name='SeriesSearch')):
            with self.subTest(resource=resource), self.assertRaises(ValueError): validate_episode_search_command(resource, [1], 7)
    def test_duplicate_command_ids_fail_closed(self):
        with self.assertRaises(ValueError): validate_command_list([self.resource(), self.resource()])
    def test_batches_have_independent_command_records(self):
        for episode_id in range(1, 249): self.candidate(episode_id)
        class API:
            command_id=0
            def trigger_episode_search(self, ids):
                self.command_id += 1
                return {'status_code':201,'id':self.command_id,'name':'EpisodeSearch'}
        counts, _, failed = submit_pending_search_candidates(target_api=API(), target_instance=self.target, target_series_id=20)
        self.assertFalse(failed); self.assertEqual(counts['submitted'],248)
        self.assertEqual([c.candidate_links.count() for c in Command.objects.order_by('id')], [100,100,48])

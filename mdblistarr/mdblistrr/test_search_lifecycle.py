from datetime import timedelta
from types import SimpleNamespace
from django.test import TestCase
from django.utils import timezone
from .models import (SonarrInstance, SonarrEpisodeSearchCandidate as Candidate,
    SonarrEpisodeSearchCommand as Command, SonarrEpisodeSearchCommandCandidate as Link)
from .sonarr_search import (submit_pending_search_candidates, validate_episode_search_command,
    validate_command_list, reconcile_search_commands_for_series, poll_episode_search_commands,
    update_search_candidates_for_series, COMMAND_FALLBACK_LIMIT)


class FakeAPI:
    def __init__(self, owner=None):
        self.owner = owner
        self.posts = []
        self.next_id = 100
        self.list_response = []
        self.individual = {}
        self.get_calls = []
    def trigger_episode_search(self, ids):
        self.posts.append(list(ids)); self.next_id += 1
        return {'status_code': 201, 'id': self.next_id, 'name': 'EpisodeSearch'}
    def get_commands(self): return self.list_response
    def get_command(self, command_id):
        self.get_calls.append(command_id)
        return self.individual.get(command_id, {'status_code': 404})


class EpisodeSearchLifecycleTests(TestCase):
    def setUp(self):
        self.target = SonarrInstance.objects.create(name='target', url='http://target', apikey='', is_library_source=False, is_ondemand_target=True)
        self.now = timezone.now()
    def candidate(self, episode_id=1, status='pending', attempt=0, current=None, due=None):
        return Candidate.objects.create(target_instance=self.target, target_series_id=20,
            target_episode_id=episode_id, tvdb_id=10, season_number=1,
            episode_number=episode_id, status=status, attempt_count=attempt,
            current_command=current, retry_not_before=due,
            first_eligible_at=self.now, last_confirmed_at=self.now)
    def command(self, command_id=7, status='queued', attempt=1, processed=None, episode_ids=(1,)):
        command = Command.objects.create(target_instance=self.target, target_series_id=20,
            sonarr_command_id=command_id, status=status, sonarr_status=status,
            submission_attempted_at=self.now, queued_at=self.now, attempt_number=attempt,
            terminal_at=self.now if status in ('completed','failed','aborted','cancelled','orphaned') else None,
            outcome_reconciled_at=processed)
        for episode_id in episode_ids:
            candidate = Candidate.objects.filter(target_instance=self.target, target_episode_id=episode_id).first() or self.candidate(episode_id, 'submitted', attempt)
            candidate.current_command=command; candidate.status='submitted'; candidate.attempt_count=attempt; candidate.save()
            Link.objects.create(command=command,candidate=candidate,target_episode_id=episode_id)
        return command
    def resource(self, command_id=7, status='queued', ids=(1,), result='unknown', queued=None):
        return {'id':command_id,'name':'EpisodeSearch','status':status,'result':result,
            'queued':(queued or self.now).isoformat(),'body':{'name':'EpisodeSearch','episodeIds':list(ids)}}
    def reconcile(self, command, resource, episodes=None, eligible=(1,), now=None, max_retries=3):
        parsed=validate_episode_search_command(resource)
        return reconcile_search_commands_for_series(target_instance=self.target,target_series_id=20,
            command_map={resource['id']:(resource,parsed)},poll_failed=False,
            target_episodes=episodes or [{'id':1,'hasFile':False,'lastSearchTime':None}],
            eligible_episode_ids=eligible,max_retries=max_retries,retry_delay_minutes=30,now=now or self.now)

    def wanted_stats(self, episode_id=1):
        return SimpleNamespace(wanted_missing_episode_ids=[episode_id], desired_by_key={(1, episode_id): True}, reason_by_key={(1, episode_id): 'wanted'})

    def test_attempt_and_snapshot_exist_before_single_post(self):
        candidate=self.candidate(); outer=self
        class API(FakeAPI):
            def trigger_episode_search(self, ids):
                self.posts.append(ids); command=Command.objects.get()
                outer.assertEqual(command.status,'submitting')
                outer.assertEqual(list(command.candidate_links.values_list('target_episode_id',flat=True)),[1])
                outer.assertEqual(Candidate.objects.get(pk=candidate.pk).current_command,command)
                return {'status_code':201,'id':7,'name':'EpisodeSearch'}
        api=API(); counts,events,failed=submit_pending_search_candidates(target_api=api,target_instance=self.target,target_series_id=20,now=self.now)
        self.assertFalse(failed); self.assertEqual((len(api.posts),counts['submitted']),(1,1))
        candidate.refresh_from_db(); self.assertEqual((candidate.status,candidate.attempt_count),('submitted',1)); self.assertNotIn('[1]',events[0])

    def test_remonitored_completed_candidate_starts_clean_initial_lifecycle(self):
        completed=self.command(status='completed'); candidate=Candidate.objects.get()
        historical_link_ids=list(candidate.command_links.values_list('id',flat=True))
        update_search_candidates_for_series(target_instance=self.target,tvdb_id=10,target_series_id=20,
            target_episodes=[{'id':1,'seasonNumber':1,'episodeNumber':1,'lastSearchTime':None}],
            stats=self.wanted_stats(),applied_monitor_true_ids=[1],series_monitored_confirmed=True,now=self.now+timedelta(hours=1))
        candidate.refresh_from_db()
        self.assertEqual((candidate.status,candidate.attempt_count,candidate.current_command_id,candidate.retry_not_before),('pending',0,None,None))
        self.assertEqual(list(candidate.command_links.values_list('id',flat=True)),historical_link_ids)
        api=FakeAPI(); submit_pending_search_candidates(target_api=api,target_instance=self.target,target_series_id=20,now=self.now+timedelta(hours=1))
        new=Command.objects.exclude(pk=completed.pk).get(); self.assertEqual((new.attempt_number,new.retry_of_id),(1,None))

    def test_cancelled_historical_candidate_resets_without_deleting_history(self):
        old=self.command(status='cancelled',processed=self.now); candidate=Candidate.objects.get()
        candidate.status='cancelled'; candidate.cancelled_at=self.now; candidate.retry_not_before=self.now; candidate.save()
        update_search_candidates_for_series(target_instance=self.target,tvdb_id=10,target_series_id=20,
            target_episodes=[{'id':1,'seasonNumber':1,'episodeNumber':1,'lastSearchTime':None}],
            stats=self.wanted_stats(),series_monitored_confirmed=True,now=self.now+timedelta(hours=1))
        candidate.refresh_from_db(); self.assertEqual((candidate.status,candidate.attempt_count,candidate.current_command_id),('pending',0,None))
        self.assertTrue(candidate.command_links.filter(command=old).exists())

    def test_pending_retry_satisfied_by_last_search_clears_retry_state(self):
        failed=self.command(status='failed',processed=self.now); candidate=Candidate.objects.get()
        candidate.status='pending'; candidate.retry_not_before=self.now+timedelta(hours=2); candidate.last_error='retry scheduled'; candidate.save()
        searched=self.now+timedelta(minutes=1)
        update_search_candidates_for_series(target_instance=self.target,tvdb_id=10,target_series_id=20,
            target_episodes=[{'id':1,'seasonNumber':1,'episodeNumber':1,'lastSearchTime':searched.isoformat()}],
            stats=self.wanted_stats(),series_monitored_confirmed=True,now=self.now+timedelta(minutes=2))
        candidate.refresh_from_db(); self.assertEqual((candidate.status,candidate.retry_not_before,candidate.last_error),('submitted',None,'')); self.assertEqual(candidate.current_command,failed)

    def test_queued_and_started_stay_submitted_and_log_only_transition(self):
        for status in ('queued','started'):
            with self.subTest(status=status):
                Candidate.objects.all().delete(); Command.objects.all().delete()
                command=self.command(status='queued'); resource=self.resource(status=status)
                _,events,unsafe=self.reconcile(command,resource)
                self.assertFalse(unsafe); self.assertEqual(Candidate.objects.get().status,'submitted')
                _,events2,_=self.reconcile(command,resource,now=self.now+timedelta(minutes=1))
                self.assertEqual(events2,[])

    def test_completed_missing_is_satisfied_and_never_retried(self):
        command=self.command(); resource=self.resource(status='completed',result='successful')
        self.reconcile(command,resource)
        self.assertEqual((Command.objects.get().status,Candidate.objects.get().status),('completed','submitted'))
        api=FakeAPI(); submit_pending_search_candidates(target_api=api,target_instance=self.target,target_series_id=20,now=self.now+timedelta(hours=1))
        self.assertEqual(api.posts,[])

    def test_all_official_results_have_deliberate_semantics(self):
        for status,result,expected,unsafe in (
            ('queued','unknown','queued',False),('started','indeterminate','started',False),
            ('completed','successful','completed',False),('completed','unknown','completed',False),
            ('completed','unsuccessful','failed',False),('completed','indeterminate','ambiguous',True)):
            with self.subTest(status=status,result=result):
                Candidate.objects.all().delete(); Command.objects.all().delete()
                command=self.command(); resource=self.resource(status=status,result=result)
                _,_,got_unsafe=self.reconcile(command,resource)
                self.assertEqual(Command.objects.get().status,expected); self.assertEqual(got_unsafe,unsafe)
        with self.assertRaises(ValueError): validate_episode_search_command(self.resource(status='future_status'))

    def test_each_explicit_terminal_status_requeues_once(self):
        for status in ('failed','aborted','cancelled','orphaned'):
            with self.subTest(status=status):
                Candidate.objects.all().delete(); Command.objects.all().delete()
                command=self.command(); resource=self.resource(status=status)
                counts,events,_=self.reconcile(command,resource)
                candidate=Candidate.objects.get(); deadline=candidate.retry_not_before
                self.assertEqual((candidate.status,deadline),('pending',self.now+timedelta(minutes=30)))
                counts2,events2,_=self.reconcile(command,resource,now=self.now+timedelta(minutes=15))
                candidate.refresh_from_db(); self.assertEqual(candidate.retry_not_before,deadline)
                self.assertEqual(counts2['search_candidates_requeued'],0); self.assertEqual(events2,[])

    def test_retry_deadline_survives_15_minute_run_then_posts_once_at_30(self):
        command=self.command(); failed=self.resource(status='failed')
        self.reconcile(command,failed,now=self.now)
        deadline=Candidate.objects.get().retry_not_before
        self.reconcile(command,failed,now=self.now+timedelta(minutes=15))
        self.assertEqual(Candidate.objects.get().retry_not_before,deadline)
        api=FakeAPI(); submit_pending_search_candidates(target_api=api,target_instance=self.target,target_series_id=20,now=self.now+timedelta(minutes=30))
        self.assertEqual(api.posts,[[1]])
        submit_pending_search_candidates(target_api=api,target_instance=self.target,target_series_id=20,now=self.now+timedelta(minutes=45))
        self.assertEqual(api.posts,[[1]])
        retry=Command.objects.get(sonarr_command_id=101); self.assertEqual((retry.retry_of, retry.attempt_number),(command,2))

    def test_search_disabled_equivalent_leaves_due_pending_until_submission_enabled(self):
        command=self.command(); self.reconcile(command,self.resource(status='failed'))
        candidate=Candidate.objects.get(); deadline=candidate.retry_not_before
        # Disabled reconciliation deliberately does not call the submission function.
        self.assertEqual(candidate.status,'pending'); self.assertEqual(candidate.retry_not_before,deadline)
        api=FakeAPI(); submit_pending_search_candidates(target_api=api,target_instance=self.target,target_series_id=20,now=deadline)
        self.assertEqual(api.posts,[[1]])

    def test_terminal_candidate_outcomes_are_independent_and_clear_stale_state(self):
        command=self.command(episode_ids=(1,2,3,4)); stale=self.now+timedelta(days=1)
        for c in Candidate.objects.all(): c.retry_not_before=stale; c.last_error='old'; c.save()
        episodes=[{'id':1,'hasFile':True,'lastSearchTime':None},
            {'id':2,'hasFile':False,'lastSearchTime':self.now.isoformat()},
            {'id':3,'hasFile':False,'lastSearchTime':None},
            {'id':4,'hasFile':False,'lastSearchTime':None}]
        self.reconcile(command,self.resource(status='failed',ids=(1,2,3,4)),episodes=episodes,eligible=(3,4),max_retries=0)
        by_id={c.target_episode_id:c for c in Candidate.objects.all()}
        self.assertEqual((by_id[1].status,by_id[1].retry_not_before,by_id[1].last_error),('submitted',None,''))
        self.assertEqual((by_id[2].status,by_id[2].retry_not_before,by_id[2].last_error),('submitted',None,''))
        self.assertEqual((by_id[3].status,by_id[3].retry_not_before),('failed',None))
        self.assertEqual((by_id[4].status,by_id[4].retry_not_before),('failed',None))

    def test_ineligible_terminal_candidate_is_cancelled_cleanly(self):
        command=self.command(); self.reconcile(command,self.resource(status='failed'),eligible=())
        candidate=Candidate.objects.get(); self.assertEqual((candidate.status,candidate.retry_not_before,candidate.last_error),('cancelled',None,'')); self.assertIsNotNone(candidate.cancelled_at)

    def test_lineage_safe_grouping_separates_initial_prior_commands_and_attempts(self):
        first=self.command(7,'failed',1,self.now,(1,)); second=self.command(8,'failed',2,self.now,(2,))
        c1=Candidate.objects.get(target_episode_id=1); c2=Candidate.objects.get(target_episode_id=2)
        for c in (c1,c2): c.status='pending'; c.retry_not_before=self.now; c.save()
        self.candidate(3)
        api=FakeAPI(); submit_pending_search_candidates(target_api=api,target_instance=self.target,target_series_id=20,now=self.now)
        self.assertEqual(api.posts,[[1],[2],[3]])
        created=list(Command.objects.filter(sonarr_command_id__gte=101).order_by('sonarr_command_id'))
        self.assertEqual([(c.retry_of_id,c.attempt_number,c.candidate_links.count()) for c in created],[(first.id,2,1),(second.id,3,1),(None,1,1)])
        self.assertEqual(list(Candidate.objects.order_by('target_episode_id').values_list('attempt_count',flat=True)),[2,3,1])

    def test_same_compatible_history_retains_100_100_48_batches(self):
        for episode_id in range(1,249): self.candidate(episode_id)
        api=FakeAPI(); submit_pending_search_candidates(target_api=api,target_instance=self.target,target_series_id=20)
        self.assertEqual([len(ids) for ids in api.posts],[100,100,48])

    def test_ambiguous_adoption_requires_exact_unique_match(self):
        candidate=self.candidate(status='submitted'); local=Command.objects.create(target_instance=self.target,target_series_id=20,status='ambiguous',submission_attempted_at=self.now)
        Link.objects.create(command=local,candidate=candidate,target_episode_id=1); candidate.current_command=local; candidate.save()
        resource=self.resource(command_id=9)
        reconcile_search_commands_for_series(target_instance=self.target,target_series_id=20,command_map={9:(resource,validate_episode_search_command(resource))},poll_failed=False,target_episodes=[{'id':1}],eligible_episode_ids=[1],now=self.now)
        local.refresh_from_db(); self.assertEqual(local.sonarr_command_id,9)
        # A second local attempt sees zero unclaimed matches and remains ambiguous.
        other=Command.objects.create(target_instance=self.target,target_series_id=20,status='ambiguous',submission_attempted_at=self.now)
        Link.objects.create(command=other,candidate=candidate,target_episode_id=1)
        _,_,unsafe=reconcile_search_commands_for_series(target_instance=self.target,target_series_id=20,command_map={9:(resource,validate_episode_search_command(resource))},poll_failed=False,target_episodes=[{'id':1}],eligible_episode_ids=[1],now=self.now)
        other.refresh_from_db(); self.assertEqual(other.status,'ambiguous'); self.assertTrue(unsafe)

    def test_multiple_adoption_matches_remain_ambiguous(self):
        candidate=self.candidate(status='submitted'); local=Command.objects.create(target_instance=self.target,target_series_id=20,status='ambiguous',submission_attempted_at=self.now)
        Link.objects.create(command=local,candidate=candidate,target_episode_id=1)
        resources=[self.resource(command_id=i) for i in (9,10)]
        command_map={r['id']:(r,validate_episode_search_command(r)) for r in resources}
        counts,_,unsafe=reconcile_search_commands_for_series(target_instance=self.target,target_series_id=20,command_map=command_map,poll_failed=False,target_episodes=[{'id':1}],eligible_episode_ids=[1],now=self.now)
        self.assertTrue(unsafe); self.assertEqual(counts['search_commands_ambiguous'],1); self.assertIsNone(Command.objects.get().sonarr_command_id)

    def test_missing_command_logs_once_and_never_retries_after_grace(self):
        command=self.command(); counts,events,unsafe=reconcile_search_commands_for_series(target_instance=self.target,target_series_id=20,command_map={},poll_failed=False,target_episodes=[{'id':1,'hasFile':False,'lastSearchTime':None}],eligible_episode_ids=[1],missing_grace_hours=24,now=self.now)
        self.assertTrue(unsafe); self.assertEqual(len(events),1)
        _,events2,unsafe2=reconcile_search_commands_for_series(target_instance=self.target,target_series_id=20,command_map={},poll_failed=False,target_episodes=[{'id':1,'hasFile':False,'lastSearchTime':None}],eligible_episode_ids=[1],missing_grace_hours=24,now=self.now+timedelta(hours=25))
        self.assertTrue(unsafe2); self.assertEqual(events2,[]); self.assertEqual(Candidate.objects.get().status,'submitted')

    def test_missing_command_can_complete_by_file_or_last_search_evidence(self):
        for evidence in ({'id':1,'hasFile':True,'lastSearchTime':None},{'id':1,'hasFile':False,'lastSearchTime':self.now.isoformat()}):
            with self.subTest(evidence=evidence):
                Candidate.objects.all().delete(); Command.objects.all().delete(); command=self.command()
                reconcile_search_commands_for_series(target_instance=self.target,target_series_id=20,command_map={},poll_failed=False,target_episodes=[evidence],eligible_episode_ids=[1],now=self.now)
                self.assertEqual(Command.objects.get().status,'completed')

    def test_bounded_individual_fallback_and_404_absence(self):
        for i in range(1,COMMAND_FALLBACK_LIMIT+3): self.command(command_id=i,episode_ids=(i,))
        api=FakeAPI(); api.individual[1]=self.resource(command_id=1,ids=(1,))
        mapped,failed,count=poll_episode_search_commands(api,self.target)
        self.assertFalse(failed); self.assertIn(1,mapped); self.assertEqual(count,COMMAND_FALLBACK_LIMIT); self.assertEqual(len(api.get_calls),COMMAND_FALLBACK_LIMIT)

    def test_fallback_rotates_fairly_across_twenty_five_absent_commands(self):
        for i in range(1,26): self.command(command_id=i,episode_ids=(i,))
        api=FakeAPI()
        _,failed1,count1=poll_episode_search_commands(api,self.target)
        first=list(api.get_calls); api.get_calls=[]
        _,failed2,count2=poll_episode_search_commands(api,self.target)
        second=list(api.get_calls); api.get_calls=[]
        terminal=self.resource(command_id=25,status='failed',ids=(25,))
        api.individual[25]=terminal
        mapped,failed3,count3=poll_episode_search_commands(api,self.target)
        third=list(api.get_calls)
        self.assertEqual((failed1,failed2,failed3),(False,False,False))
        self.assertEqual((count1,count2,count3),(10,10,5))
        self.assertEqual((first,second,third),(list(range(1,11)),list(range(11,21)),list(range(21,26))))
        self.assertEqual(set(first+second+third),set(range(1,26)))
        counts,_,unsafe=reconcile_search_commands_for_series(target_instance=self.target,target_series_id=20,
            command_map=mapped,poll_failed=False,target_episodes=[{'id':i,'hasFile':False,'lastSearchTime':None} for i in range(1,26)],
            eligible_episode_ids=list(range(1,26)),now=self.now+timedelta(hours=1))
        discovered=Command.objects.get(sonarr_command_id=25); self.assertEqual(discovered.status,'failed'); self.assertIsNotNone(discovered.outcome_reconciled_at)
        self.assertEqual(counts['search_candidates_requeued'],1); self.assertTrue(unsafe)  # other absent commands remain fail-closed

    def test_malformed_list_and_polling_outage_fail_closed(self):
        api=FakeAPI(); api.list_response={'error':'down'}
        mapped,failed,count=poll_episode_search_commands(api,self.target)
        self.assertTrue(failed); self.assertEqual((mapped,count),({},0))
        command=self.command(); counts,events,unsafe=reconcile_search_commands_for_series(target_instance=self.target,target_series_id=20,command_map={},poll_failed=True,target_episodes=[{'id':1}],eligible_episode_ids=[1])
        self.assertTrue(unsafe); self.assertEqual(counts['search_command_poll_failures'],1); self.assertEqual(Candidate.objects.get().status,'submitted')

    def test_strict_validation_rejects_malformed_and_duplicate_list_ids(self):
        self.assertEqual(validate_episode_search_command(self.resource(result='indeterminate'),[1],7)['result'],'indeterminate')
        for resource in (self.resource(command_id=True),self.resource(status='new-state'),self.resource(ids=(1,1)),dict(self.resource(),name='SeriesSearch')):
            with self.subTest(resource=resource),self.assertRaises(ValueError): validate_episode_search_command(resource,[1],7)
        with self.assertRaises(ValueError): validate_command_list([self.resource(),self.resource()])

    def test_malformed_success_is_ambiguous_and_protected(self):
        self.candidate()
        class API(FakeAPI):
            def trigger_episode_search(self,ids): self.posts.append(ids); return {'status_code':201,'id':True,'name':'EpisodeSearch'}
        api=API(); submit_pending_search_candidates(target_api=api,target_instance=self.target,target_series_id=20)
        self.assertEqual(Command.objects.get().status,'ambiguous'); self.assertEqual(Candidate.objects.get().status,'submitted')

from unittest.mock import Mock
from django.test import SimpleTestCase
from .arr import RadarrAPI
from .radarr_reconcile import calculate_movie_monitoring, validate_movie_response

def movie(mid, tmdb, has=False, monitored=False, available=True, title='Movie'):
    return {'id':mid,'tmdbId':tmdb,'hasFile':has,'monitored':monitored,'isAvailable':available,'title':title}

class RadarrDecisionTests(SimpleTestCase):
    def test_matrix_counters_and_tmdb_matching(self):
        source=[movie(1,10,True,title='Different'),movie(2,20,False),movie(3,99,True,title='Same')]
        target=[movie(101,10,monitored=True,title='Target'),movie(102,20),movie(103,30),movie(104,40,True,True),movie(105,50,available=False),movie(106,60,title='Same')]
        result=calculate_movie_monitoring(source,target)
        self.assertEqual(result.monitor_true_ids,[102,103,106]); self.assertEqual(result.monitor_false_ids,[101,104])
        self.assertEqual((result.movies_compared,result.movies_target_only,result.movies_unchanged),(2,4,1))
        self.assertEqual((result.permanent_files_present,result.target_files_present,result.eligible_missing,result.unavailable),(1,1,3,1))
        self.assertEqual(result.failures,0)

class RadarrValidationTests(SimpleTestCase):
    def test_empty_valid_and_structures_fail_closed(self):
        self.assertEqual(validate_movie_response([]),(True,'')); self.assertFalse(validate_movie_response({})[0]); self.assertFalse(validate_movie_response([None])[0])
    def test_strict_fields_errors_status_and_duplicates(self):
        for field in ('hasFile','monitored','isAvailable'):
            item=movie(1,2); item[field]='false'; self.assertFalse(validate_movie_response([item],target=True)[0])
        for item in (movie(True,2),movie(1,0)):
            self.assertFalse(validate_movie_response([item])[0])
        item=movie(1,2); item['error']='bad'; self.assertFalse(validate_movie_response([item])[0])
        item=movie(1,2); item['status_code']=500; self.assertFalse(validate_movie_response([item])[0])
        self.assertFalse(validate_movie_response([movie(1,2),movie(1,3)])[0]); self.assertFalse(validate_movie_response([movie(1,2),movie(2,2)])[0])

class RadarrMonitorApiTests(SimpleTestCase):
    def api(self):
        api=object.__new__(RadarrAPI); api.url='http://radarr'; api.apikey='secret'; api.connect=Mock(); api.connect.put_json.return_value={}; return api
    def test_rejects_unsafe_arguments(self):
        api=self.api()
        for ids, monitored in [([],True),([True],True),([0],True),([-1],True),([1],'true')]: self.assertIn('error',api.put_movie_monitor(ids,monitored))
        api.connect.put_json.assert_not_called()
    def test_v3_movie_editor_contract(self):
        api=self.api(); api.put_movie_monitor([3,1],False); args,kwargs=api.connect.put_json.call_args
        self.assertEqual(args[0],'http://radarr/api/v3/movie/editor'); self.assertEqual(kwargs['json'],{'movieIds':[3,1],'monitored':False})

import fcntl, json, os, tempfile
from datetime import timedelta
os.environ.setdefault('MDBLISTARR_ENCRYPTION_KEY', 'MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=')
from unittest.mock import patch, call
from django.test import TestCase, Client
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone
from .cron import reconcile_radarr_ondemand, reconcile_radarr_ondemand_task
from .forms import RadarrReconciliationForm
from .models import (Preferences, RadarrInstance, RadarrMovieSearchCandidate,
    RadarrMovieSearchCommand, RadarrMovieSearchCommandCandidate, RadarrCleanupCandidate)

def cleanup_movie(mid, tmdb, file_id, *, monitored=False, edition=None):
    return {**movie(mid,tmdb,True,monitored), 'movieFileId':file_id,
        'movieFile':{'id':file_id,'movieId':mid,'edition':edition}}

class RadarrOrchestrationTests(TestCase):
    def setUp(self):
        self.source=RadarrInstance.objects.create(name='Source',url='http://source',apikey='source-key',is_library_source=True)
        self.target=RadarrInstance.objects.create(name='Target',url='http://target',apikey='target-key',is_library_source=False,is_ondemand_target=True)
        for k,v in {'enabled':'1','source_id':self.source.id,'target_id':self.target.id,'interval_minutes':'15'}.items():
            Preferences.set_value('radarr_reconciliation_'+k,str(v))
        fd,self.lock=tempfile.mkstemp(); os.close(fd); self.addCleanup(lambda: os.path.exists(self.lock) and os.unlink(self.lock))
    def apis(self,src=None,tgt=None):
        a,b=Mock(),Mock(); a.get_movies.return_value=[] if src is None else src; b.get_movies.return_value=[] if tgt is None else tgt; b.put_movie_monitor.return_value={}; return a,b
    def invoke(self,a,b,force=True):
        with patch('mdblistrr.cron.RADARR_RECONCILE_LOCK_PATH',self.lock),patch('mdblistrr.cron.RadarrAPI',side_effect=[a,b]): return reconcile_radarr_ondemand(force)
    def test_disabled_even_force_and_interval_skip_make_no_calls(self):
        Preferences.set_value('radarr_reconciliation_enabled','0')
        with patch('mdblistrr.cron.RadarrAPI') as factory:
            self.assertIn('disabled',reconcile_radarr_ondemand(True)['message']); factory.assert_not_called()
        Preferences.set_value('radarr_reconciliation_enabled','1'); now=timezone.now().replace(minute=1)
        with patch('mdblistrr.cron.timezone.now',return_value=now),patch('mdblistrr.cron.RadarrAPI') as factory:
            self.assertEqual(reconcile_radarr_ondemand(False)['message'],'Not scheduled interval'); factory.assert_not_called()
        a,b=self.apis(); self.assertEqual(self.invoke(a,b,True)['result'],200)
    def test_role_and_same_instance_safety(self):
        for obj,field in ((self.source,'is_library_source'),(self.target,'is_ondemand_target')):
            setattr(obj,field,False); obj.save(update_fields=[field])
            with patch('mdblistrr.cron.RadarrAPI') as factory:
                self.assertEqual(reconcile_radarr_ondemand(True)['result'],400); factory.assert_not_called()
            setattr(obj,field,True); obj.save(update_fields=[field])
        self.source.is_ondemand_target=True; self.source.save(update_fields=['is_ondemand_target']); Preferences.set_value('radarr_reconciliation_target_id',str(self.source.id))
        with patch('mdblistrr.cron.RadarrAPI') as factory:
            self.assertEqual(reconcile_radarr_ondemand(True)['message'],'source_target_same'); factory.assert_not_called()
    def test_malformed_source_target_duplicate_and_error_make_no_writes(self):
        bads=({},[movie(1,1),movie(2,1)],[{'error':'bad'}])
        for bad in bads:
            a,b=self.apis(bad,[]); self.assertEqual(self.invoke(a,b)['result'],502); b.put_movie_monitor.assert_not_called()
            a,b=self.apis([],bad); self.assertEqual(self.invoke(a,b)['result'],502); b.put_movie_monitor.assert_not_called()
    def test_orchestration_fetches_once_target_only_and_no_search_delete(self):
        a,b=self.apis([movie(1,10,True)],[movie(101,10,monitored=True),movie(102,20),movie(103,30,available=False)])
        result=self.invoke(a,b); self.assertEqual(result['result'],200); a.get_movies.assert_called_once(); b.get_movies.assert_called_once()
        self.assertEqual(b.put_movie_monitor.call_args_list,[call([102],True),call([101],False)])
        a.put_movie_monitor.assert_not_called()
        for api in (a,b): api.trigger_movie_search.assert_not_called(); api.post_movie.assert_not_called(); api.delete_movie.assert_not_called()
    def test_sorted_bounded_true_and_false_batches(self):
        targets=[movie(i,10000+i) for i in range(250,0,-1)]+[movie(i,20000+i,True,True) for i in range(550,300,-1)]
        a,b=self.apis([],targets); result=self.invoke(a,b); calls=b.put_movie_monitor.call_args_list
        self.assertEqual([len(c.args[0]) for c in calls],[100,100,50,100,100,50]); self.assertEqual(calls[0].args[0],list(range(1,101))); self.assertEqual(calls[3].args[0],list(range(301,401)))
        self.assertEqual(result['counters']['movies_newly_monitored'],250); self.assertEqual(result['counters']['movies_newly_unmonitored'],250)
    def test_partial_failure_207_counters_and_json(self):
        a,b=self.apis([],[movie(i,10000+i) for i in range(1,102)]); b.put_movie_monitor.side_effect=[{'error':'bad'},{}]; result=self.invoke(a,b)
        self.assertEqual(result['result'],207); self.assertEqual(result['counters']['movies_newly_monitored'],1); self.assertEqual(result['counters']['monitor_update_failures'],100); self.assertEqual(result['counters']['failures'],1); json.dumps(result)
        self.assertEqual(RadarrMovieSearchCandidate.objects.count(), 1)

    def test_production_like_candidates_then_one_controlled_search(self):
        targets=[{**movie(i,10000+i,monitored=True),'lastSearchTime':None} for i in range(1,48)]
        targets += [{**movie(i,10000+i,available=False),'lastSearchTime':None} for i in range(48,51)]
        source,target_api=self.apis([],targets)
        first=self.invoke(source,target_api)
        self.assertEqual(first['result'],200); self.assertEqual(RadarrMovieSearchCandidate.objects.filter(status='pending').count(),47)
        target_api.trigger_movies_search.assert_not_called()
        Preferences.set_value('radarr_search_newly_eligible','1')
        source,target_api=self.apis([],targets)
        target_api.trigger_movies_search.return_value={'status_code':201,'id':77,'name':'MoviesSearch','body':{'name':'MoviesSearch','movieIds':list(range(1,48))},'status':'queued','result':'unknown'}
        second=self.invoke(source,target_api)
        target_api.trigger_movies_search.assert_called_once_with(list(range(1,48)))
        self.assertEqual(second['counters']['search_candidates_submitted'],47)
        self.assertEqual(second['counters']['initial_searches_triggered'],47)
        self.assertEqual(RadarrMovieSearchCommandCandidate.objects.count(),47)
        source,target_api=self.apis([],targets)
        target_api.get_commands.return_value=[{'id':77,'name':'MoviesSearch','body':{'name':'MoviesSearch','movieIds':list(range(1,48))},'status':'queued','result':'unknown','queued':timezone.now().isoformat()}]
        third=self.invoke(source,target_api)
        target_api.trigger_movies_search.assert_not_called(); json.dumps(third)

    def test_malformed_last_search_blocks_only_that_movie_submission(self):
        Preferences.set_value('radarr_search_newly_eligible','1')
        malformed=RadarrMovieSearchCandidate.objects.create(target_instance=self.target,target_movie_id=1,tmdb_id=10001,first_eligible_at=timezone.now(),last_confirmed_at=timezone.now(),retry_not_before=timezone.now())
        safe=RadarrMovieSearchCandidate.objects.create(target_instance=self.target,target_movie_id=2,tmdb_id=10002,first_eligible_at=timezone.now(),last_confirmed_at=timezone.now())
        targets=[{**movie(1,10001,monitored=True),'lastSearchTime':'malformed'}, {**movie(2,10002,monitored=True),'lastSearchTime':None}]
        source,target_api=self.apis([],targets)
        target_api.trigger_movies_search.return_value={'status_code':201,'id':88,'name':'MoviesSearch','body':{'name':'MoviesSearch','movieIds':[2]},'status':'queued','result':'unknown'}
        result=self.invoke(source,target_api)
        self.assertEqual(result['result'],207); self.assertEqual(result['counters']['search_candidates_deferred'],1); self.assertEqual(result['counters']['search_failures'],1)
        target_api.trigger_movies_search.assert_called_once_with([2])
        malformed.refresh_from_db(); safe.refresh_from_db()
        self.assertEqual(malformed.status,'pending'); self.assertIsNone(malformed.current_command_id); self.assertIsNotNone(malformed.retry_not_before)
        self.assertEqual(safe.status,'submitted')

    def test_monitor_failure_preserves_existing_pending_candidate(self):
        Preferences.set_value('radarr_search_newly_eligible','1'); now=timezone.now()
        candidate=RadarrMovieSearchCandidate.objects.create(target_instance=self.target,target_movie_id=1,tmdb_id=10001,first_eligible_at=now,last_confirmed_at=now,retry_not_before=now)
        source,target_api=self.apis([],[{**movie(1,10001,monitored=False),'lastSearchTime':None}]); target_api.put_movie_monitor.return_value={'error':'failed'}
        result=self.invoke(source,target_api); candidate.refresh_from_db()
        self.assertEqual(result['result'],207); self.assertEqual(candidate.status,'pending'); self.assertIsNone(candidate.current_command_id); self.assertEqual(candidate.attempt_count,0); self.assertEqual(candidate.retry_not_before,now); target_api.trigger_movies_search.assert_not_called()

    def test_monitor_failure_preserves_due_retry_lineage(self):
        Preferences.set_value('radarr_search_newly_eligible','1'); now=timezone.now()
        predecessor=RadarrMovieSearchCommand.objects.create(target_instance=self.target,radarr_command_id=70,status='failed',submission_attempted_at=now,outcome_reconciled_at=now,attempt_number=1)
        candidate=RadarrMovieSearchCandidate.objects.create(target_instance=self.target,target_movie_id=1,tmdb_id=10001,status='pending',first_eligible_at=now,last_confirmed_at=now,current_command=predecessor,attempt_count=1,retry_not_before=now)
        source,target_api=self.apis([],[{**movie(1,10001,monitored=False),'lastSearchTime':None}]); target_api.put_movie_monitor.return_value={'error':'failed'}
        self.invoke(source,target_api); candidate.refresh_from_db(); predecessor.refresh_from_db()
        self.assertEqual(candidate.current_command_id,predecessor.id); self.assertEqual(candidate.attempt_count,1); self.assertEqual(candidate.retry_not_before,now); self.assertIsNotNone(predecessor.outcome_reconciled_at); target_api.trigger_movies_search.assert_not_called()

    def test_successful_remonitor_submits_retry_without_resetting_lineage(self):
        Preferences.set_value('radarr_search_newly_eligible','1'); now=timezone.now()
        predecessor=RadarrMovieSearchCommand.objects.create(target_instance=self.target,radarr_command_id=71,status='failed',submission_attempted_at=now,outcome_reconciled_at=now,attempt_number=1)
        candidate=RadarrMovieSearchCandidate.objects.create(target_instance=self.target,target_movie_id=1,tmdb_id=10001,status='pending',first_eligible_at=now,last_confirmed_at=now,current_command=predecessor,attempt_count=1,retry_not_before=now)
        source,target_api=self.apis([],[{**movie(1,10001,monitored=False),'lastSearchTime':None}]); target_api.trigger_movies_search.return_value={'status_code':201,'id':72,'name':'MoviesSearch','body':{'name':'MoviesSearch','movieIds':[1]},'status':'queued','result':'unknown'}
        result=self.invoke(source,target_api); candidate.refresh_from_db(); command=RadarrMovieSearchCommand.objects.get(radarr_command_id=72)
        self.assertEqual(command.retry_of_id,predecessor.id); self.assertEqual(command.attempt_number,2); self.assertEqual(candidate.attempt_count,2); self.assertEqual(result['counters']['search_retries_submitted'],1); self.assertEqual(result['counters']['initial_searches_triggered'],0)

    def test_successful_remonitor_preserves_inflight_and_ambiguous_commands(self):
        Preferences.set_value('radarr_search_newly_eligible','1'); now=timezone.now()
        for index,status in enumerate(('queued','ambiguous'),1):
            RadarrMovieSearchCandidate.objects.all().delete(); RadarrMovieSearchCommand.objects.all().delete()
            command=RadarrMovieSearchCommand.objects.create(target_instance=self.target,radarr_command_id=80 if status=='queued' else None,status=status,submission_attempted_at=now,attempt_number=1)
            candidate=RadarrMovieSearchCandidate.objects.create(target_instance=self.target,target_movie_id=index,tmdb_id=10000+index,status='submitted',first_eligible_at=now,last_confirmed_at=now,current_command=command,attempt_count=1)
            RadarrMovieSearchCommandCandidate.objects.create(command=command,candidate=candidate,target_movie_id=index)
            source,target_api=self.apis([],[{**movie(index,10000+index,monitored=False),'lastSearchTime':None}])
            target_api.get_commands.return_value=[{'id':80,'name':'MoviesSearch','body':{'name':'MoviesSearch','movieIds':[index]},'status':'queued','result':'unknown','queued':now.isoformat()}] if status=='queued' else []
            self.invoke(source,target_api); candidate.refresh_from_db()
            self.assertEqual(candidate.current_command_id,command.id); self.assertEqual(candidate.status,'submitted'); self.assertEqual(candidate.attempt_count,1); target_api.trigger_movies_search.assert_not_called()

    def test_successful_remonitor_reactivates_genuine_cancelled_cycle(self):
        now=timezone.now(); candidate=RadarrMovieSearchCandidate.objects.create(target_instance=self.target,target_movie_id=1,tmdb_id=10001,status='cancelled',first_eligible_at=now-timedelta(days=1),last_confirmed_at=now,cancelled_at=now)
        source,target_api=self.apis([],[{**movie(1,10001,monitored=False),'lastSearchTime':None}]); self.invoke(source,target_api); candidate.refresh_from_db()
        self.assertEqual(candidate.status,'pending'); self.assertIsNone(candidate.cancelled_at); self.assertEqual(candidate.attempt_count,0)
    def test_success_and_immediate_task_results_are_json_serializable(self):
        a,b=self.apis([],[movie(1,10)]); result=self.invoke(a,b); self.assertEqual(json.loads(json.dumps(result)),result)
        a,b=self.apis([],[movie(1,10)])
        with patch('mdblistrr.cron.RADARR_RECONCILE_LOCK_PATH',self.lock),patch('mdblistrr.cron.RadarrAPI',side_effect=[a,b]): task_result=reconcile_radarr_ondemand_task.enqueue()
        self.assertEqual(task_result.status.value,'SUCCESSFUL'); json.dumps(task_result.return_value)
    def test_full_mixed_cleanup_dry_run_then_safe_live_delete(self):
        Preferences.set_value('radarr_cleanup_enabled','1'); Preferences.set_value('radarr_cleanup_dry_run','1')
        Preferences.set_value('radarr_cleanup_grace_hours','0'); Preferences.set_value('radarr_cleanup_max_deletions_per_run','1')
        sources=[cleanup_movie(200+i,10000+i,20000+i) for i in range(2,111)]
        targets=[movie(1,99999),cleanup_movie(2,10002,30002),cleanup_movie(3,10003,30003,edition='IMAX'),cleanup_movie(4,10004,30004)]
        sources[1]['movieFile']['edition']='Extended'
        targets += [cleanup_movie(i,10000+i,30000+i,monitored=True) for i in range(10,111)]
        now=timezone.now(); command=RadarrMovieSearchCommand.objects.create(target_instance=self.target,status='queued',submission_attempted_at=now)
        RadarrMovieSearchCandidate.objects.create(target_instance=self.target,target_movie_id=4,tmdb_id=10004,status='submitted',first_eligible_at=now,last_confirmed_at=now,current_command=command)
        source_api,target_api=self.apis(sources,targets); target_api.put_movie_monitor.side_effect=[{}, {}, {'error':'editor failed'}]; target_api.get_commands.return_value=[{'id':77,'name':'MoviesSearch','body':{'name':'MoviesSearch','movieIds':[4]},'status':'queued','result':'unknown','queued':now.isoformat()}]
        dry=self.invoke(source_api,target_api)
        self.assertEqual(target_api.put_movie_monitor.call_args_list,[call([1],True),call(list(range(10,110)),False),call([110],False)])
        self.assertEqual(dry['counters']['cleanup_edition_conflicts'],1); self.assertEqual(dry['counters']['cleanup_would_delete'],101)
        self.assertFalse(RadarrCleanupCandidate.objects.filter(target_movie_id__in=(3,4,110)).exists()); json.dumps(dry)
        for name in ('put_movie_monitor','trigger_movies_search','post_movie','delete_movie_file'): self.assertFalse(getattr(source_api,name).called)
        target_api.delete_movie_file.assert_not_called()

        # A healthy subsequent run can delete only the first deterministic ready
        # candidate; force bypasses the interval, never dry-run or the cap.
        Preferences.set_value('radarr_cleanup_dry_run','0')
        command.status='completed'; command.outcome_reconciled_at=timezone.now(); command.save(update_fields=['status','outcome_reconciled_at'])
        live_targets=[{**item,'monitored':False} for item in targets if item['id'] != 110]
        source_api,target_api=self.apis(sources,live_targets); target_api.get_commands.return_value=[{'id':77,'name':'MoviesSearch','body':{'name':'MoviesSearch','movieIds':[4]},'status':'queued','result':'unknown','queued':now.isoformat()}]
        source_by_id={item['id']:item for item in sources}; target_by_id={item['id']:item for item in live_targets}
        source_api.get_movie.side_effect=lambda mid:{**source_by_id[mid],'status_code':200}
        target_api.get_movie.side_effect=lambda mid:{**target_by_id[mid],'status_code':200}
        source_api.get_movie_file.side_effect=lambda fid:{'id':fid,'movieId':next(item['id'] for item in sources if item.get('movieFileId')==fid),'edition':next(item['movieFile']['edition'] for item in sources if item['movieFileId']==fid),'status_code':200}
        file_calls={}
        def target_file(fid):
            file_calls[fid]=file_calls.get(fid,0)+1
            if file_calls[fid] > 1: return {'status_code':404,'error':'not found'}
            item=next(item for item in live_targets if item.get('movieFileId')==fid)
            return {'id':fid,'movieId':item['id'],'edition':item['movieFile']['edition'],'status_code':200}
        target_api.get_movie_file.side_effect=target_file; target_api.delete_movie_file.return_value={'status':'ok','status_code':204}
        live=self.invoke(source_api,target_api)
        target_api.delete_movie_file.assert_called_once_with(30002); self.assertEqual(live['counters']['cleanup_files_deleted'],1)
        self.assertGreater(live['counters']['cleanup_deferred_by_limit'],0); json.dumps(live)
        for name in ('put_movie_monitor','trigger_movies_search','post_movie','delete_movie_file'): self.assertFalse(getattr(source_api,name).called)
    def test_held_independent_lock_blocks_all_io(self):
        with open(self.lock,'a+') as held:
            fcntl.flock(held.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
            with patch('mdblistrr.cron.RADARR_RECONCILE_LOCK_PATH',self.lock),patch('mdblistrr.cron.RadarrAPI') as factory: result=reconcile_radarr_ondemand(True)
            self.assertIn('already running',result['message']); factory.assert_not_called()
        from .cron import RECONCILE_LOCK_PATH; self.assertNotEqual(self.lock,RECONCILE_LOCK_PATH)

class RadarrFormUiManualTests(TestCase):
    def setUp(self):
        self.source=RadarrInstance.objects.create(name='Source',url='http://source',apikey='source-key',is_library_source=True)
        self.target=RadarrInstance.objects.create(name='Target',url='http://target',apikey='target-key',is_library_source=False,is_ondemand_target=True)
        self.both=RadarrInstance.objects.create(name='Both',url='http://both',apikey='both-key',is_library_source=True,is_ondemand_target=True)
        U=get_user_model(); self.staff=U.objects.create_user('staff',password='pw',is_staff=True,is_superuser=True); self.user=U.objects.create_user('user',password='pw')
    def test_form_querysets_validation_default_and_preferences(self):
        form=RadarrReconciliationForm(); self.assertEqual(set(form.fields['source'].queryset),{self.source,self.both}); self.assertEqual(set(form.fields['target'].queryset),{self.target,self.both}); self.assertEqual(form.fields['interval_minutes'].initial,'15')
        self.assertTrue(form.fields['cleanup_dry_run'].initial)
        self.assertEqual((form.fields['cleanup_grace_hours'].initial,form.fields['cleanup_max_deletions_per_run'].initial),('24',25))
        self.assertEqual([Preferences.get_value(k,d) for k,d in (('radarr_cleanup_enabled','0'),('radarr_cleanup_dry_run','1'),('radarr_cleanup_grace_hours','24'),('radarr_cleanup_max_deletions_per_run','25'))],['0','1','24','25'])
        for maximum in ('0','501'):
            invalid=RadarrReconciliationForm({'interval_minutes':'15','cleanup_grace_hours':'24','cleanup_max_deletions_per_run':maximum})
            self.assertFalse(invalid.is_valid())
        self.assertFalse(RadarrReconciliationForm({'enabled':'on','interval_minutes':'15'}).is_valid()); self.assertFalse(RadarrReconciliationForm({'enabled':'on','source':self.both.id,'target':self.both.id,'interval_minutes':'15'}).is_valid()); disabled=RadarrReconciliationForm({'interval_minutes':'15'}); self.assertTrue(disabled.is_valid()); disabled.save_preferences()
        enabled=RadarrReconciliationForm({'enabled':'on','source':self.source.id,'target':self.target.id,'interval_minutes':'30'}); self.assertTrue(enabled.is_valid()); enabled.save_preferences()
        self.assertEqual([Preferences.get_value(k) for k in ('radarr_reconciliation_enabled','radarr_reconciliation_source_id','radarr_reconciliation_target_id','radarr_reconciliation_interval_minutes')],['1',str(self.source.id),str(self.target.id),'30'])
        cleanup=RadarrReconciliationForm({'enabled':'on','source':self.source.id,'target':self.target.id,'interval_minutes':'30','cleanup_enabled':'on','cleanup_dry_run':'on','cleanup_grace_hours':'48','cleanup_max_deletions_per_run':'17'})
        self.assertTrue(cleanup.is_valid()); cleanup.save_preferences()
        self.assertEqual([Preferences.get_value(k) for k in ('radarr_cleanup_enabled','radarr_cleanup_dry_run','radarr_cleanup_grace_hours','radarr_cleanup_max_deletions_per_run')],['1','1','48','17'])
    @patch('mdblistrr.views.get_mdblistarr')
    def test_ui_persisted_selection_no_future_controls_secrets_or_duplicate_ids(self,service):
        service.return_value.get_radarr_quality_profile_choices.return_value=[]; service.return_value.get_radarr_root_folder_choices.return_value=[]
        Preferences.set_value('radarr_reconciliation_source_id',str(self.source.id)); Preferences.set_value('radarr_reconciliation_target_id',str(self.target.id)); self.client.force_login(self.staff); html=self.client.get('/').content.decode()
        self.assertIn('Run Radarr reconciliation now',html); self.assertIn(f'<option value="{self.source.id}" selected>Source</option>',html); self.assertIn(f'<option value="{self.target.id}" selected>Target</option>',html)
        self.assertIn('Search newly eligible missing movies', html)
        self.assertIn('MoviesSearch commands:', html)
        self.assertIn('Destructive cleanup safety:',html); self.assertIn('Enable automatic duplicate-file cleanup',html)
        self.assertIn('Dry-run cleanup',html); self.assertIn('Maximum file deletions per reconciliation run',html)
        for absent in ('force-delete','Force delete','safety bypass','source-key','target-key'): self.assertNotIn(absent,html)
        ids=[x.split('"',1)[0] for x in html.split(' id="')[1:]]; self.assertEqual(len(ids),len(set(ids)))
    def test_manual_methods_permissions_force_disabled_and_csrf(self):
        rec,sync=reverse('run_radarr_reconciliation_now'),reverse('run_radarr_library_sync_now'); self.client.force_login(self.user); self.assertEqual(self.client.post(rec, content_type='application/json').status_code,403); self.assertEqual(self.client.post(sync, content_type='application/json').status_code,403)
        self.client.force_login(self.staff); self.assertEqual(self.client.get(rec).status_code,405); self.assertEqual(self.client.get(sync).status_code,405)
        with patch('mdblistrr.cron.reconcile_radarr_ondemand',return_value={'result':200}) as run: self.assertEqual(self.client.post(rec).status_code,302); run.assert_called_once_with(force=True)
        with patch('mdblistrr.cron.post_radarr_payload',return_value={'response':'Ok'}) as run: self.assertEqual(self.client.post(sync).status_code,302); run.assert_called_once_with(force=True)
        Preferences.set_value('radarr_reconciliation_enabled','0'); self.assertIn('disabled',reconcile_radarr_ondemand(True)['message'])
        csrf=Client(enforce_csrf_checks=True); csrf.force_login(self.staff); self.assertEqual(csrf.post(rec).status_code,403); self.assertEqual(csrf.post(sync).status_code,403)

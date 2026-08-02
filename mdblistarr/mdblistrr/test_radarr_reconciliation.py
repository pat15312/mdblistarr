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
os.environ.setdefault('MDBLISTARR_ENCRYPTION_KEY', 'MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=')
from unittest.mock import patch, call
from django.test import TestCase, Client
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone
from .cron import reconcile_radarr_ondemand, reconcile_radarr_ondemand_task
from .forms import RadarrReconciliationForm
from .models import (Preferences, RadarrInstance, RadarrMovieSearchCandidate,
    RadarrMovieSearchCommand, RadarrMovieSearchCommandCandidate)

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
    def test_success_and_immediate_task_results_are_json_serializable(self):
        a,b=self.apis([],[movie(1,10)]); result=self.invoke(a,b); self.assertEqual(json.loads(json.dumps(result)),result)
        a,b=self.apis([],[movie(1,10)])
        with patch('mdblistrr.cron.RADARR_RECONCILE_LOCK_PATH',self.lock),patch('mdblistrr.cron.RadarrAPI',side_effect=[a,b]): task_result=reconcile_radarr_ondemand_task.enqueue()
        self.assertEqual(task_result.status.value,'SUCCESSFUL'); json.dumps(task_result.return_value)
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
        self.assertFalse(RadarrReconciliationForm({'enabled':'on','interval_minutes':'15'}).is_valid()); self.assertFalse(RadarrReconciliationForm({'enabled':'on','source':self.both.id,'target':self.both.id,'interval_minutes':'15'}).is_valid()); disabled=RadarrReconciliationForm({'interval_minutes':'15'}); self.assertTrue(disabled.is_valid()); disabled.save_preferences()
        enabled=RadarrReconciliationForm({'enabled':'on','source':self.source.id,'target':self.target.id,'interval_minutes':'30'}); self.assertTrue(enabled.is_valid()); enabled.save_preferences()
        self.assertEqual([Preferences.get_value(k) for k in ('radarr_reconciliation_enabled','radarr_reconciliation_source_id','radarr_reconciliation_target_id','radarr_reconciliation_interval_minutes')],['1',str(self.source.id),str(self.target.id),'30'])
    @patch('mdblistrr.views.get_mdblistarr')
    def test_ui_persisted_selection_no_future_controls_secrets_or_duplicate_ids(self,service):
        service.return_value.get_radarr_quality_profile_choices.return_value=[]; service.return_value.get_radarr_root_folder_choices.return_value=[]
        Preferences.set_value('radarr_reconciliation_source_id',str(self.source.id)); Preferences.set_value('radarr_reconciliation_target_id',str(self.target.id)); self.client.force_login(self.staff); html=self.client.get('/').content.decode()
        self.assertIn('Run Radarr reconciliation now',html); self.assertIn(f'<option value="{self.source.id}" selected>Source</option>',html); self.assertIn(f'<option value="{self.target.id}" selected>Target</option>',html)
        self.assertIn('Search newly eligible missing movies', html)
        self.assertIn('MoviesSearch commands:', html)
        for absent in ('Radarr cleanup','force-delete','source-key','target-key'): self.assertNotIn(absent,html)
        ids=[x.split('"',1)[0] for x in html.split(' id="')[1:]]; self.assertEqual(len(ids),len(set(ids)))
    def test_manual_methods_permissions_force_disabled_and_csrf(self):
        rec,sync=reverse('run_radarr_reconciliation_now'),reverse('run_radarr_library_sync_now'); self.client.force_login(self.user); self.assertEqual(self.client.post(rec, content_type='application/json').status_code,403); self.assertEqual(self.client.post(sync, content_type='application/json').status_code,403)
        self.client.force_login(self.staff); self.assertEqual(self.client.get(rec).status_code,405); self.assertEqual(self.client.get(sync).status_code,405)
        with patch('mdblistrr.cron.reconcile_radarr_ondemand',return_value={'result':200}) as run: self.assertEqual(self.client.post(rec).status_code,302); run.assert_called_once_with(force=True)
        with patch('mdblistrr.cron.post_radarr_payload',return_value={'response':'Ok'}) as run: self.assertEqual(self.client.post(sync).status_code,302); run.assert_called_once_with(force=True)
        Preferences.set_value('radarr_reconciliation_enabled','0'); self.assertIn('disabled',reconcile_radarr_ondemand(True)['message'])
        csrf=Client(enforce_csrf_checks=True); csrf.force_login(self.staff); self.assertEqual(csrf.post(rec).status_code,403); self.assertEqual(csrf.post(sync).status_code,403)

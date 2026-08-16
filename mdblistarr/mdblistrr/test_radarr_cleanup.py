import os
os.environ.setdefault('MDBLISTARR_ENCRYPTION_KEY', 'MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=')
from unittest.mock import Mock
from django.test import SimpleTestCase, TestCase
from django.utils import timezone
from .arr import RadarrAPI
from .models import (RadarrInstance, RadarrCleanupCandidate, RadarrMovieSearchCandidate, RadarrMovieSearchCommand)
from .radarr_cleanup import (normalize_edition, editions_compatible, validate_movie_file,
    validate_movie_for_cleanup, derive_cleanup_evidence, process_radarr_cleanup)

def movie(mid,tmdb,fid,edition=None,monitored=False):
    return {'id':mid,'tmdbId':tmdb,'hasFile':True,'movieFileId':fid,'monitored':monitored,
        'isAvailable':True,'title':'Safe','movieFile':{'id':fid,'movieId':mid,'edition':edition}}

class EditionAndValidationTests(SimpleTestCase):
    def test_normalization_matrix(self):
        for a,b in ((None,None),('', '  '),("Director's Cut"," director's   CUT ")):
            self.assertEqual(editions_compatible(a,b)[0],'compatible')
        for a,b in ((None,"Director's Cut"),("Director's Cut",None),("Theatrical","Director's Cut"),('Extended','IMAX')):
            self.assertEqual(editions_compatible(a,b)[0],'conflict')
        self.assertEqual(normalize_edition(1),None); self.assertEqual(editions_compatible(1,None)[0],'malformed')
    def test_strict_movie_file(self):
        self.assertTrue(validate_movie_file({'id':4,'movieId':2,'edition':None},expected_file_id=4,expected_movie_id=2)[0])
        for bad in ({'id':True,'movieId':2,'edition':None},{'id':4,'movieId':3,'edition':None},{'id':4,'movieId':2,'edition':3},{'id':4,'movieId':2,'edition':None,'status_code':500},{'error':'x'}):
            self.assertFalse(validate_movie_file(bad,expected_file_id=4,expected_movie_id=2)[0])
    def test_cleanup_movie_contract(self):
        good=movie(2,9,4); self.assertTrue(validate_movie_for_cleanup(good,target=True,require_nested=True)[0])
        for key,value in (('id',True),('tmdbId',0),('hasFile','true'),('monitored','false'),('movieFileId',0)):
            bad=dict(good); bad[key]=value; self.assertFalse(validate_movie_for_cleanup(bad,target=True,require_nested=True)[0])
    def test_evidence_tmdb_and_editions(self):
        state,evidence,_=derive_cleanup_evidence(movie(1,9,3,"Director's Cut"),movie(2,9,4," director's  cut"))
        self.assertEqual(state,'eligible'); self.assertEqual((evidence.tmdb_id,evidence.movie_file_id),(9,4))
        self.assertEqual(derive_cleanup_evidence(movie(1,8,3),movie(2,9,4))[0],'ineligible')
        self.assertEqual(derive_cleanup_evidence(movie(1,9,3,'IMAX'),movie(2,9,4,'Extended'))[0],'conflict')

class RadarrExactApiTests(SimpleTestCase):
    def api(self):
        api=object.__new__(RadarrAPI); api.url='http://target'; api.apikey='secret'; api.connect=Mock(); return api
    def test_exact_get_routes_and_validation(self):
        api=self.api(); api.connect.get_json_with_status.return_value={'status_code':404,'error':'x'}
        self.assertEqual(api.get_movie(7)['status_code'],404); self.assertIn('/api/v3/movie/7',api.connect.get_json_with_status.call_args.args[0])
        api.get_movie_file(8); self.assertIn('/api/v3/moviefile/8',api.connect.get_json_with_status.call_args.args[0])
        for value in (True,0,-1,'1'): self.assertIn('error',api.get_movie(value)); self.assertIn('error',api.get_movie_file(value))
    def test_exact_nonretrying_delete(self):
        api=self.api(); api.connect.delete_json.return_value={'status':'ok','status_code':204}
        self.assertEqual(api.delete_movie_file(8)['status_code'],204)
        self.assertEqual(api.connect.delete_json.call_count,1); self.assertEqual(api.connect.delete_json.call_args.args[0],'http://target/api/v3/moviefile/8')
        for value in (True,0,-1,'8'): self.assertIn('error',api.delete_movie_file(value))

class CandidateLifecycleTests(TestCase):
    def setUp(self):
        self.target=RadarrInstance.objects.create(name='target',url='http://target',apikey='x',is_library_source=False,is_ondemand_target=True)
        self.source_movie=movie(1,9,3); self.target_movie=movie(2,9,4)
    def cleanup(self,**kw):
        defaults=dict(target_instance=self.target,source_movies=[self.source_movie],target_movies=[self.target_movie],
            confirmed_unmonitored_ids={2},monitoring_blocked_ids=set(),source_api=Mock(),target_api=Mock(),grace_hours=24,max_deletions=25)
        defaults.update(kw); return process_radarr_cleanup(**defaults)
    def test_disabled_maintains_pending_without_delete(self):
        target_api=Mock(); out=self.cleanup(target_api=target_api)
        self.assertEqual(out.cleanup_candidates_new,1); self.assertEqual(RadarrCleanupCandidate.objects.get().status,'pending'); target_api.delete_movie_file.assert_not_called()
    def test_dry_run_ready_does_not_consume_budget(self):
        self.cleanup(); cand=RadarrCleanupCandidate.objects.get(); cand.first_eligible_at=timezone.now()-timezone.timedelta(days=2); cand.save()
        target_api=Mock(); out=self.cleanup(target_api=target_api,cleanup_enabled=True,dry_run=True)
        self.assertEqual(out.cleanup_would_delete,1); self.assertEqual(out.delete_attempts_consumed,0); target_api.delete_movie_file.assert_not_called()
    def test_monitor_block_preserves_candidate(self):
        self.cleanup(); before=RadarrCleanupCandidate.objects.get().first_eligible_at
        out=self.cleanup(confirmed_unmonitored_ids=set(),monitoring_blocked_ids={2},cleanup_enabled=True,dry_run=False)
        self.assertEqual(RadarrCleanupCandidate.objects.get().first_eligible_at,before); self.assertEqual(out.cleanup_safety_deferred,1)
    def test_edition_conflict_cancels(self):
        self.cleanup(); self.target_movie=movie(2,9,4,'IMAX'); self.source_movie=movie(1,9,3,'Extended'); out=self.cleanup()
        self.assertEqual(out.cleanup_edition_conflicts,1); self.assertEqual(RadarrCleanupCandidate.objects.get().status,'cancelled')
    def test_target_only_creates_nothing(self):
        out=self.cleanup(source_movies=[]); self.assertEqual(out.cleanup_candidates_new,0); self.assertFalse(RadarrCleanupCandidate.objects.exists())

class DestructiveLifecycleTests(TestCase):
    def setUp(self):
        self.target=RadarrInstance.objects.create(name='target-live',url='http://target',apikey='x',is_library_source=False,is_ondemand_target=True)
    def snapshots(self, count=1):
        sources=[movie(i,1000+i,2000+i) for i in range(1,count+1)]
        targets=[movie(100+i,1000+i,3000+i) for i in range(1,count+1)]
        return sources,targets
    def ready(self, sources, targets):
        now=timezone.now()-timezone.timedelta(days=2)
        for source,target in zip(sources,targets):
            RadarrCleanupCandidate.objects.create(target_instance=self.target,tmdb_id=target['tmdbId'],source_movie_id=source['id'],source_movie_file_id=source['movieFileId'],target_movie_id=target['id'],movie_file_id=target['movieFileId'],source_edition='',target_edition='',first_eligible_at=now,last_confirmed_at=now,ready_at=now,status='ready')
    def invoke(self,sources,targets,source_api,target_api,**kw):
        return process_radarr_cleanup(target_instance=self.target,source_movies=sources,target_movies=targets,
            confirmed_unmonitored_ids={m['id'] for m in targets},monitoring_blocked_ids=set(),source_api=source_api,target_api=target_api,
            cleanup_enabled=True,dry_run=False,grace_hours=24,max_deletions=25,**kw)
    def valid_apis(self,sources,targets,delete_result=None):
        source_api,target_api=Mock(),Mock()
        source_by_id={m['id']:m for m in sources}; target_by_id={m['id']:m for m in targets}
        source_api.get_movie.side_effect=lambda mid:{**source_by_id[mid],'status_code':200}
        target_api.get_movie.side_effect=lambda mid:{**target_by_id[mid],'status_code':200}
        source_api.get_movie_file.side_effect=lambda fid:{'id':fid,'movieId':next(m['id'] for m in sources if m['movieFileId']==fid),'edition':None,'status_code':200}
        calls={}
        def target_file(fid):
            calls[fid]=calls.get(fid,0)+1
            if calls[fid]>1:return {'status_code':404,'error':'not found'}
            return {'id':fid,'movieId':next(m['id'] for m in targets if m['movieFileId']==fid),'edition':None,'status_code':200}
        target_api.get_movie_file.side_effect=target_file
        target_api.delete_movie_file.return_value={'status':'ok','status_code':204} if delete_result is None else delete_result
        return source_api,target_api,calls
    def test_successful_live_cleanup_exact_and_source_read_only(self):
        sources,targets=self.snapshots(); self.ready(sources,targets); source_api,target_api,_=self.valid_apis(sources,targets)
        out=self.invoke(sources,targets,source_api,target_api); cand=RadarrCleanupCandidate.objects.get()
        self.assertEqual(cand.status,'deleted'); self.assertEqual(out.cleanup_files_deleted,1)
        source_api.get_movie.assert_called_once_with(1); source_api.get_movie_file.assert_called_once_with(2001)
        target_api.get_movie.assert_called_once_with(101); target_api.delete_movie_file.assert_called_once_with(3001)
        self.assertFalse(hasattr(target_api,'delete_movie') and target_api.delete_movie.called)
        for name in ('put_movie_monitor','trigger_movies_search','post_movie','delete_movie_file'): self.assertFalse(getattr(source_api,name).called)
    def test_delete_error_then_absent_is_already_absent_without_retry(self):
        sources,targets=self.snapshots(); self.ready(sources,targets); source_api,target_api,_=self.valid_apis(sources,targets,{'error':'race','status_code':500})
        out=self.invoke(sources,targets,source_api,target_api)
        self.assertEqual(RadarrCleanupCandidate.objects.get().status,'already_absent'); self.assertEqual(out.cleanup_files_already_absent,1); self.assertEqual(target_api.delete_movie_file.call_count,1)
    def test_delete_success_file_remains_stops_later_and_counters_safety(self):
        sources,targets=self.snapshots(3); self.ready(sources,targets); source_api,target_api,_=self.valid_apis(sources,targets)
        target_api.get_movie_file.side_effect=lambda fid:{'id':fid,'movieId':next(m['id'] for m in targets if m['movieFileId']==fid),'edition':None,'status_code':200}
        out=self.invoke(sources,targets,source_api,target_api)
        self.assertEqual(target_api.delete_movie_file.call_count,1); self.assertTrue(out.stop_deletes_for_run); self.assertEqual(out.cleanup_failures,1)
        self.assertEqual(out.cleanup_deferred_by_limit,0); self.assertEqual(out.cleanup_safety_deferred,3)
    def test_malformed_post_delete_verification_stops(self):
        sources,targets=self.snapshots(2); self.ready(sources,targets); source_api,target_api,_=self.valid_apis(sources,targets)
        sequence=[{'id':3001,'movieId':101,'edition':None,'status_code':200},{'garbage':True}]
        target_api.get_movie_file.side_effect=lambda fid: sequence.pop(0) if fid==3001 else {'id':fid,'movieId':102,'edition':None,'status_code':200}
        out=self.invoke(sources,targets,source_api,target_api)
        self.assertEqual(target_api.delete_movie_file.call_count,1); self.assertTrue(out.stop_deletes_for_run); self.assertEqual(out.cleanup_safety_deferred,2)
    def test_global_budget_30_exactly_25_and_five_limit_deferred(self):
        sources,targets=self.snapshots(30); self.ready(sources,targets); source_api,target_api,_=self.valid_apis(sources,targets)
        out=self.invoke(sources,targets,source_api,target_api)
        self.assertEqual(target_api.delete_movie_file.call_count,25); self.assertEqual(out.delete_attempts_consumed,25); self.assertEqual(out.cleanup_deferred_by_limit,5); self.assertEqual(out.cleanup_safety_deferred,0)
        self.assertEqual([c.args[0] for c in target_api.delete_movie_file.call_args_list],list(range(3001,3026)))
    def test_uncertain_third_attempt_stops_fourth_and_safety_defers_rest(self):
        sources,targets=self.snapshots(30); self.ready(sources,targets); source_api,target_api,_=self.valid_apis(sources,targets)
        calls={}
        def target_file(fid):
            calls[fid]=calls.get(fid,0)+1
            movie_id=next(m['id'] for m in targets if m['movieFileId']==fid)
            if calls[fid]>1 and fid in (3001,3002): return {'status_code':404,'error':'not found'}
            return {'id':fid,'movieId':movie_id,'edition':None,'status_code':200}
        target_api.get_movie_file.side_effect=target_file
        out=self.invoke(sources,targets,source_api,target_api)
        self.assertEqual(target_api.delete_movie_file.call_count,3); self.assertTrue(out.stop_deletes_for_run)
        self.assertEqual(out.cleanup_deferred_by_limit,0); self.assertEqual(out.cleanup_safety_deferred,28)

    def test_predelete_uncertainty_stops_later_calls(self):
        sources,targets=self.snapshots(2); self.ready(sources,targets); source_api,target_api,_=self.valid_apis(sources,targets)
        source_api.get_movie.side_effect=[{'error':'transport'}, {**sources[1],'status_code':200}]
        out=self.invoke(sources,targets,source_api,target_api)
        self.assertEqual(target_api.delete_movie_file.call_count,0); self.assertTrue(out.stop_deletes_for_run); self.assertEqual(out.cleanup_failures,1); self.assertEqual(out.cleanup_safety_deferred,2); self.assertEqual(out.cleanup_deferred_by_limit,0)
    def test_search_lifecycle_blockers_and_completed_history(self):
        blockers=('submitting','queued','started','ambiguous','unavailable','failed')
        for status in blockers:
            with self.subTest(status=status):
                RadarrCleanupCandidate.objects.all().delete(); RadarrMovieSearchCandidate.objects.all().delete(); RadarrMovieSearchCommand.objects.all().delete()
                sources,targets=self.snapshots(); self.ready(sources,targets); source_api,target_api,_=self.valid_apis(sources,targets)
                command=RadarrMovieSearchCommand.objects.create(target_instance=self.target,status=status,submission_attempted_at=timezone.now(),outcome_reconciled_at=None)
                RadarrMovieSearchCandidate.objects.create(target_instance=self.target,target_movie_id=101,tmdb_id=1001,status='submitted',first_eligible_at=timezone.now(),last_confirmed_at=timezone.now(),current_command=command)
                out=self.invoke(sources,targets,source_api,target_api)
                target_api.delete_movie_file.assert_not_called(); self.assertEqual(out.cleanup_safety_deferred,1)
        RadarrCleanupCandidate.objects.all().delete(); RadarrMovieSearchCandidate.objects.all().delete(); RadarrMovieSearchCommand.objects.all().delete()
        sources,targets=self.snapshots(); self.ready(sources,targets); source_api,target_api,_=self.valid_apis(sources,targets)
        command=RadarrMovieSearchCommand.objects.create(target_instance=self.target,status='completed',submission_attempted_at=timezone.now(),outcome_reconciled_at=timezone.now())
        RadarrMovieSearchCandidate.objects.create(target_instance=self.target,target_movie_id=101,tmdb_id=1001,status='submitted',first_eligible_at=timezone.now(),last_confirmed_at=timezone.now(),current_command=command)
        self.invoke(sources,targets,source_api,target_api); target_api.delete_movie_file.assert_called_once_with(3001)
    def test_source_file_replacement_resets_grace_without_delete(self):
        sources,targets=self.snapshots(); self.ready(sources,targets); sources[0]['movieFileId']=999; sources[0]['movieFile']={'id':999,'movieId':1,'edition':None}
        source_api,target_api,_=self.valid_apis(sources,targets); before=RadarrCleanupCandidate.objects.get().first_eligible_at
        out=self.invoke(sources,targets,source_api,target_api); cand=RadarrCleanupCandidate.objects.get()
        self.assertEqual((cand.status,cand.source_movie_file_id),('pending',999)); self.assertGreater(cand.first_eligible_at,before); target_api.delete_movie_file.assert_not_called()
    def test_target_replacement_retires_old_and_new_has_fresh_grace(self):
        sources,targets=self.snapshots(); self.ready(sources,targets); old=RadarrCleanupCandidate.objects.get(); targets[0]=movie(101,1001,9999)
        target_api=Mock(); target_api.get_movie_file.return_value={'status_code':404,'error':'not found'}
        out=process_radarr_cleanup(target_instance=self.target,source_movies=sources,target_movies=targets,confirmed_unmonitored_ids={101},monitoring_blocked_ids=set(),source_api=Mock(),target_api=target_api,cleanup_enabled=False,grace_hours=24)
        old.refresh_from_db(); new=RadarrCleanupCandidate.objects.get(movie_file_id=9999)
        self.assertEqual(old.status,'already_absent'); self.assertEqual(new.status,'pending'); self.assertNotEqual(old.first_eligible_at,new.first_eligible_at); self.assertEqual(out.cleanup_files_already_absent,1)
    def test_external_disappearance_is_already_absent_no_delete(self):
        sources,targets=self.snapshots(); self.ready(sources,targets); targets[0]={**targets[0],'hasFile':False,'movieFileId':0}; target_api=Mock(); target_api.get_movie_file.return_value={'status_code':404,'error':'not found'}
        out=process_radarr_cleanup(target_instance=self.target,source_movies=sources,target_movies=targets,confirmed_unmonitored_ids={101},monitoring_blocked_ids=set(),source_api=Mock(),target_api=target_api,cleanup_enabled=False)
        self.assertEqual(RadarrCleanupCandidate.objects.get().status,'already_absent'); self.assertEqual(out.cleanup_files_already_absent,1); target_api.delete_movie_file.assert_not_called()
    def test_source_file_disappears_cancels_target_untouched(self):
        sources,targets=self.snapshots(); self.ready(sources,targets); source_api,target_api,_=self.valid_apis(sources,targets); source_api.get_movie.return_value={**sources[0],'hasFile':False,'movieFileId':0,'status_code':200}; source_api.get_movie.side_effect=None
        out=self.invoke(sources,targets,source_api,target_api)
        self.assertEqual(RadarrCleanupCandidate.objects.get().status,'cancelled'); target_api.delete_movie_file.assert_not_called()

class TargetMovieDisappearanceTests(TestCase):
    def setUp(self):
        self.target=RadarrInstance.objects.create(name='target-missing',url='http://target',apikey='x',is_library_source=False,is_ondemand_target=True)
    def process_missing(self, exact, *, live=False):
        sources=[movie(1,1001,2001)]; targets=[movie(101,1001,3001)]
        now=timezone.now()-timezone.timedelta(days=2)
        RadarrCleanupCandidate.objects.create(target_instance=self.target,tmdb_id=1001,source_movie_id=1,source_movie_file_id=2001,target_movie_id=101,movie_file_id=3001,source_edition='',target_edition='',first_eligible_at=now,last_confirmed_at=now,ready_at=now,status='ready')
        target_api=Mock(); target_api.get_movie_file.return_value=exact
        out=process_radarr_cleanup(target_instance=self.target,source_movies=sources,target_movies=[],
            confirmed_unmonitored_ids=set(),monitoring_blocked_ids=set(),source_api=Mock(),target_api=target_api,
            cleanup_enabled=live,dry_run=not live,grace_hours=24,max_deletions=25)
        return out,target_api,RadarrCleanupCandidate.objects.get()
    def test_missing_movie_and_exact_404_is_already_absent(self):
        out,api,cand=self.process_missing({'status_code':404,'error':'not found'})
        self.assertEqual(cand.status,'already_absent'); self.assertEqual(out.cleanup_files_already_absent,1)
        api.get_movie_file.assert_called_once_with(3001); api.delete_movie_file.assert_not_called()
    def test_missing_movie_but_exact_file_exists_cancels_without_delete(self):
        out,api,cand=self.process_missing({'id':3001,'movieId':101,'edition':None,'status_code':200})
        self.assertEqual(cand.status,'cancelled'); self.assertEqual(out.cleanup_candidates_cancelled,1)
        api.delete_movie_file.assert_not_called()

    def test_lifecycle_uncertainty_counts_each_of_three_ready_candidates_once(self):
        sources=[movie(i,1000+i,2000+i) for i in range(1,4)]
        targets=[movie(101+i,1001+i,3001+i) for i in range(1,3)]
        now=timezone.now()-timezone.timedelta(days=2)
        for index in range(3):
            RadarrCleanupCandidate.objects.create(target_instance=self.target,tmdb_id=1001+index,
                source_movie_id=1+index,source_movie_file_id=2001+index,target_movie_id=101+index,
                movie_file_id=3001+index,source_edition='',target_edition='',first_eligible_at=now,
                last_confirmed_at=now,ready_at=now,status='ready')
        target_api=Mock(); target_api.get_movie_file.return_value={'status_code':503,'error':'unavailable'}
        out=process_radarr_cleanup(target_instance=self.target,source_movies=sources,target_movies=targets,
            confirmed_unmonitored_ids={102,103},monitoring_blocked_ids=set(),source_api=Mock(),target_api=target_api,
            cleanup_enabled=True,dry_run=False,grace_hours=24,max_deletions=25)
        self.assertTrue(out.stop_deletes_for_run); self.assertEqual(out.cleanup_safety_deferred,3)
        target_api.delete_movie_file.assert_not_called()
    def test_missing_movie_malformed_exact_file_stops_live_deletes(self):
        out,api,cand=self.process_missing({'status_code':503,'error':'unavailable'},live=True)
        self.assertEqual(cand.status,'ready'); self.assertEqual(out.cleanup_failures,1)
        self.assertGreaterEqual(out.cleanup_safety_deferred,1); self.assertTrue(out.stop_deletes_for_run)
        api.delete_movie_file.assert_not_called()

from unittest.mock import Mock
from django.test import SimpleTestCase, TestCase
from django.utils import timezone
from .arr import RadarrAPI
from .models import RadarrInstance, RadarrCleanupCandidate
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

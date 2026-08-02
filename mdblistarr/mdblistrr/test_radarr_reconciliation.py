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

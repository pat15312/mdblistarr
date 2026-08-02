import os
os.environ.setdefault('MDBLISTARR_ENCRYPTION_KEY', 'MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=')
from datetime import timedelta
from unittest.mock import Mock, patch
from django.test import TestCase, SimpleTestCase
from django.utils import timezone
from .arr import RadarrAPI
from .connect import Connect
from .models import RadarrInstance, RadarrMovieSearchCandidate as Candidate, RadarrMovieSearchCommand as Command, RadarrMovieSearchCommandCandidate as Link
from .radarr_search import (command_response_succeeded, poll_movie_search_commands, reconcile_movie_search_commands, submit_pending_search_candidates, update_movie_search_candidates, validate_command_list, validate_movie_search_command)

def movie(mid,last=None,has=False): return {'id':mid,'tmdbId':1000+mid,'monitored':True,'isAvailable':True,'hasFile':has,'lastSearchTime':last}
def resource(cid,ids,status='queued',result=''): return {'id':cid,'name':'MoviesSearch','body':{'name':'MoviesSearch','movieIds':ids},'status':status,'result':result,'queued':timezone.now().isoformat()}

class ApiTests(SimpleTestCase):
 def api(self):
  a=object.__new__(RadarrAPI); a.url='http://r'; a.apikey='key'; a.connect=Mock(); return a
 def test_exact_post_and_invalid_inputs(self):
  a=self.api(); a.trigger_movies_search([3,1]); self.assertEqual(a.connect.post_json_once.call_args.kwargs['json'],{'name':'MoviesSearch','movieIds':[3,1]}); a.connect.post_json.assert_not_called(); a.connect.get_json.assert_not_called()
  for bad in (None,3,[],[True],['1'],[0],[-1],[1,1],list(range(101))):
   b=self.api(); self.assertIn('error',b.trigger_movies_search(bad)); b.connect.post_json_once.assert_not_called()
 def test_get_command_status_helper(self):
  a=self.api(); a.connect.get_json_with_status.return_value={'status_code':404}; self.assertEqual(a.get_command(7)['status_code'],404)
  for bad in (True,0,-1,'1'): self.assertIn('error',a.get_command(bad))
 def test_http_status_preserved(self):
  c=Connect()
  for code,text,payload in ((200,'{}',resource(7,[1])),(404,'{"x":1}',{'x':1}),(404,'',None)):
   response=Mock(status_code=code,text=text,headers={'content-type':'application/json'}); response.json.return_value=payload
   with patch.object(c,'get',return_value=response): result=c.get_json_with_status('http://r/api/v3/command/7')
   self.assertEqual(result['status_code'],code)
   if code==200:self.assertEqual(validate_movie_search_command(result)['id'],7)
 def test_validators_fail_closed_and_ignore_unrelated(self):
  good={'status_code':201,**resource(1,[1,2])}; self.assertTrue(command_response_succeeded(good,[2,1]))
  for bad in ({**good,'id':True},{**good,'status_code':500},{**good,'name':'RssSync'},{**good,'body':{}},{**good,'body':{'name':'MoviesSearch','movieIds':[1,1]}}): self.assertFalse(command_response_succeeded(bad,[1,2]))
  self.assertEqual(set(validate_command_list([resource(1,[1]),{'id':2,'name':'RefreshMovie','body':'bad'}])),{1})
  with self.assertRaises(ValueError):validate_command_list([resource(1,[1]),resource(1,[1])])

class LifecycleTests(TestCase):
 def setUp(self): self.target=RadarrInstance.objects.create(name='t',url='http://t',apikey='k',is_library_source=False,is_ondemand_target=True); self.now=timezone.now()
 def cand(self,mid=1,status='pending',**kw): return Candidate.objects.create(target_instance=self.target,target_movie_id=mid,tmdb_id=1000+mid,status=status,first_eligible_at=self.now,last_confirmed_at=self.now,**kw)
 def command(self,mid,cid,status='queued'):
  c=Command.objects.create(target_instance=self.target,radarr_command_id=cid,status=status,submission_attempted_at=self.now); x=Candidate.objects.filter(target_movie_id=mid).first() or self.cand(mid); Link.objects.create(command=c,candidate=x,target_movie_id=mid); x.current_command=c;x.status='submitted';x.save();return c
 def test_candidate_discovery_manual_cancel_reactivate(self):
  counts,_,_=update_movie_search_candidates(target_instance=self.target,target_movies=[movie(1)],eligible_movie_ids=[1],confirmed_monitored_ids=[1],now=self.now); self.assertEqual(counts['search_candidates_new'],1)
  c=Candidate.objects.get(); update_movie_search_candidates(target_instance=self.target,target_movies=[movie(1,self.now.isoformat())],eligible_movie_ids=[1],confirmed_monitored_ids=[1],now=self.now); c.refresh_from_db();self.assertEqual(c.status,'submitted')
  c.status='pending';c.save();update_movie_search_candidates(target_instance=self.target,target_movies=[movie(1)],eligible_movie_ids=[],confirmed_monitored_ids=[],now=self.now);c.refresh_from_db();self.assertEqual(c.status,'cancelled')
  update_movie_search_candidates(target_instance=self.target,target_movies=[movie(1)],eligible_movie_ids=[1],confirmed_monitored_ids=[1],now=self.now+timedelta(minutes=1));c.refresh_from_db();self.assertEqual(c.status,'pending')
 def test_historical_skipped_new_monitor_cycle_created(self):
  old=(self.now-timedelta(days=1)).isoformat(); update_movie_search_candidates(target_instance=self.target,target_movies=[movie(1,old)],eligible_movie_ids=[1],confirmed_monitored_ids=[1]);self.assertFalse(Candidate.objects.exists())
  update_movie_search_candidates(target_instance=self.target,target_movies=[movie(1,old)],eligible_movie_ids=[1],confirmed_monitored_ids=[1],newly_monitored_ids=[1]);self.assertEqual(Candidate.objects.get().status,'pending')
 def test_malformed_search_deferred_not_cancelled(self):
  c=self.cand(retry_not_before=self.now+timedelta(hours=1)); counts,events,failed=update_movie_search_candidates(target_instance=self.target,target_movies=[movie(1,'broken')],eligible_movie_ids=[1],confirmed_monitored_ids=[1],now=self.now+timedelta(minutes=1));c.refresh_from_db();self.assertTrue(failed);self.assertEqual(c.status,'pending');self.assertIsNotNone(c.retry_not_before);self.assertEqual(counts['search_candidates_deferred'],1);self.assertIn('deferred',events[0])
  Candidate.objects.all().delete();update_movie_search_candidates(target_instance=self.target,target_movies=[movie(2,'bad')],eligible_movie_ids=[2],confirmed_monitored_ids=[2]);self.assertFalse(Candidate.objects.exists())
 def test_persisted_before_single_post_and_ambiguous(self):
  self.cand();api=Mock()
  def post(ids): self.assertEqual(Command.objects.count(),1);self.assertEqual(Link.objects.count(),1);self.assertEqual(Candidate.objects.get().status,'submitted');return {'status_code':201,'name':'MoviesSearch'}
  api.trigger_movies_search.side_effect=post;submit_pending_search_candidates(target_api=api,target_instance=self.target,now=self.now);self.assertEqual(Command.objects.get().status,'ambiguous');self.assertEqual(Candidate.objects.get().status,'submitted');api.trigger_movies_search.assert_called_once();submit_pending_search_candidates(target_api=api,target_instance=self.target,now=self.now);self.assertEqual(api.trigger_movies_search.call_count,1)
 def test_definite_rejection_restores_pending(self):
  c=self.cand();api=Mock();api.trigger_movies_search.return_value={'status_code':400,'error':'no'};submit_pending_search_candidates(target_api=api,target_instance=self.target,now=self.now);c.refresh_from_db();self.assertEqual(c.status,'pending');self.assertEqual(c.attempt_count,0);self.assertEqual(Command.objects.get().status,'superseded');api.trigger_movies_search.assert_called_once()
 def test_initial_terminal_status_mapping(self):
  for n,(status,result,expected) in enumerate((('completed','successful','completed'),('completed','unsuccessful','failed'),('completed','indeterminate','ambiguous'),('failed','unsuccessful','failed'),('aborted','unsuccessful','aborted'),('cancelled','unsuccessful','cancelled'),('orphaned','unsuccessful','orphaned')),1):
   Candidate.objects.all().delete();Command.objects.all().delete();self.cand(n);api=Mock();api.trigger_movies_search.return_value={'status_code':201,**resource(10+n,[n],status,result)};submit_pending_search_candidates(target_api=api,target_instance=self.target,now=self.now);self.assertEqual(Command.objects.get().status,expected)
 def test_404_fallback_does_not_block_other_outcome(self):
  missing=self.command(1,10);other=self.command(2,11);api=Mock();api.get_commands.return_value=[resource(11,[2],'completed','successful')];api.get_command.return_value={'status_code':404,'error':'gone'};mapped,failed,count=poll_movie_search_commands(api,self.target);self.assertFalse(failed);self.assertEqual(count,1)
  counters,_,unsafe=reconcile_movie_search_commands(target_instance=self.target,command_map=mapped,poll_failed=False,target_movies=[movie(1),movie(2)],eligible_movie_ids=[1,2],now=self.now);missing.refresh_from_db();other.refresh_from_db();self.assertEqual(missing.status,'unavailable');self.assertEqual(other.status,'completed');self.assertEqual(counters['search_commands_completed'],1);self.assertTrue(unsafe)
 def test_batching_250(self):
  Candidate.objects.bulk_create([Candidate(target_instance=self.target,target_movie_id=i,tmdb_id=1000+i,first_eligible_at=self.now,last_confirmed_at=self.now) for i in range(1,251)]);api=Mock();ids=iter((1,2,3));api.trigger_movies_search.side_effect=lambda batch:{'status_code':201,**resource(next(ids),batch)};counts,_,_=submit_pending_search_candidates(target_api=api,target_instance=self.target,now=self.now);self.assertEqual(counts['submitted'],250);self.assertEqual([len(c.args[0]) for c in api.trigger_movies_search.call_args_list],[100,100,50]);self.assertEqual([c.candidate_links.count() for c in Command.objects.order_by('id')],[100,100,50])

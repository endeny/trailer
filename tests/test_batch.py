import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).parents[1]/'scripts'))
import json
import tempfile
import unittest
from prepare_popular import snapshot
from warm_popular import load_plan,run_batch,BATCH_SECONDS
from test_cache_trailer import S3

class Batch(unittest.TestCase):
    def test_popular_snapshot_deduplicates_in_order(self):
        result=snapshot(3,lambda page:{'results':[{'id':1,'title':'one'},{'id':2,'title':'two'}] if page==1 else [{'id':2},{'id':3}]})
        self.assertEqual([v['tmdb_id'] for v in result['items']],[1,2,3])

    def test_plan_rejects_duplicates_and_non_movie_ids(self):
        with tempfile.TemporaryDirectory() as temp:
            plan=Path(temp)/'plan.json'
            for items in [[{'media_type':'person','tmdb_id':1}],[{'media_type':'movie','tmdb_id':1}]*2]:
                plan.write_text(json.dumps({'items':items}))
                with self.assertRaises(ValueError):load_plan(plan,'en')

    def run_fixture(self,count,states,existing=None,clock=lambda:0):
        s3=S3();items=[{'media_type':'movie','tmdb_id':i,'language':'en'} for i in range(1,count+1)]
        key='trailer-control/v1/batches/test.json'
        if existing:s3.objects[key]=json.dumps(existing).encode()
        calls=[]
        def execute(item):
            calls.append(item['tmdb_id']);state=states[item['tmdb_id']]
            s3.objects[f"trailers/v1/movie/{item['tmdb_id']}/en/metadata.json"]=json.dumps(state).encode()
            return 0 if state['status'] in ('ready','not_found') else 1
        with tempfile.TemporaryDirectory() as temp:
            report=run_batch(s3,'media',items,'test',Path(temp)/'report.json',execute=execute,clock=clock)
            self.assertEqual(report,json.loads(s3.objects[key]))
        return report,calls

    def test_three_auth_failures_pause_without_spending_remaining_queue(self):
        report,calls=self.run_fixture(5,{i:{'status':'failed','error':'youtube_auth_required'} for i in range(1,6)})
        self.assertEqual(calls,[1,2,3]);self.assertEqual(report['status'],'paused')
        self.assertEqual(report['counts'],{'ready':0,'not_found':0,'failed':3,'pending':2})

    def test_explicitly_expired_cookies_pause_after_first_attempt(self):
        report,calls=self.run_fixture(5,{i:{'status':'failed','error':'youtube_cookies_expired'} for i in range(1,6)})
        self.assertEqual(calls,[1]);self.assertEqual(report['pause_reason'],'youtube_cookies_expired')
        self.assertEqual(report['counts']['pending'],4)

    def test_resume_skips_completed_and_retries_only_remaining(self):
        first,_=self.run_fixture(4,{i:{'status':'failed','error':'youtube_auth_required'} for i in range(1,5)})
        first['items'][0]['status']='ready'
        second,calls=self.run_fixture(4,{i:{'status':'ready','storage':'hf','variants':{'720p':{}}} for i in range(1,5)},existing=first)
        self.assertEqual(calls,[2,3,4]);self.assertEqual(second['counts']['ready'],4);self.assertEqual(second['status'],'completed')

    def test_r2_only_readiness_is_not_counted_as_hf_success(self):
        report,_=self.run_fixture(1,{1:{'status':'ready'}})
        self.assertEqual(report['counts']['ready'],0);self.assertEqual(report['counts']['failed'],1)

    def test_time_budget_checkpoints_before_starting_another_video(self):
        times=iter([0,BATCH_SECONDS])
        report,calls=self.run_fixture(2,{},clock=lambda:next(times))
        self.assertEqual(calls,[]);self.assertEqual(report['pause_reason'],'time_budget')
        self.assertEqual(report['counts']['pending'],2)

    def test_not_found_is_terminal_and_does_not_block_other_movies(self):
        report,calls=self.run_fixture(2,{1:{'status':'not_found'},2:{'status':'ready','storage':'hf','variants':{}}})
        self.assertEqual(calls,[1,2]);self.assertEqual(report['status'],'completed');self.assertEqual(report['counts']['not_found'],1)

class TVBatch(unittest.TestCase):
    def test_tv_plan_has_separate_namespace_from_same_movie_id(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'plan.json'
            path.write_text(json.dumps({'items':[{'media_type':'tv','tmdb_id':1399}]}))
            items,tv_id=load_plan(path,'en')
            self.assertEqual(items[0]['media_type'],'tv');self.assertTrue(tv_id.startswith('popular-tv-'))
            path.write_text(json.dumps({'items':[{'media_type':'movie','tmdb_id':1399}]}))
            self.assertNotEqual(tv_id,load_plan(path,'en')[1])

    def test_tv_batch_reads_tv_metadata(self):
        s3=S3();item={'media_type':'tv','tmdb_id':1399,'language':'en'}
        def execute(identity):
            self.assertEqual(identity['media_type'],'tv')
            s3.objects['trailers/v1/tv/1399/en/metadata.json']=json.dumps({'status':'ready','storage':'hf','variants':{'1080p':{}}}).encode()
            return 0
        with tempfile.TemporaryDirectory() as temp:
            report=run_batch(s3,'media',[item],'tv-test',Path(temp)/'report.json',execute=execute)
        self.assertEqual(report['counts']['ready'],1)
        self.assertEqual(report['items'][0]['qualities'],['1080p'])

    def test_tv_snapshot_uses_name_and_preserves_tv_type(self):
        plan=snapshot(1,lambda page:{'results':[{'id':1399,'name':'Game of Thrones'}]},media_type='tv')
        self.assertEqual(plan['items'][0],{'media_type':'tv','tmdb_id':1399,'title':'Game of Thrones'})

if __name__=='__main__':unittest.main()

import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).parents[1]/'scripts'))
import unittest
import tempfile
from unittest.mock import Mock
from hf_storage import HFStore, HFStorageError, REPO_LIMIT_BYTES
from hf_job import run_hf_job
from test_cache_trailer import S3
from cache_trailer import JobError
from download_variants import download_variants

class HF(unittest.TestCase):
    def test_hf_budget_blocks_downloader(self):
        store=Mock();store.lookup.return_value=None
        store.ensure_capacity.side_effect=HFStorageError('hf_storage_budget_exceeded')
        downloader=Mock();s3=S3()
        self.assertEqual(run_hf_job(s3,'media','movie','1','en',store=store,downloader=downloader),1)
        downloader.assert_not_called();self.assertEqual(s3.states[-1]['error'],'hf_storage_budget_exceeded')
        self.assertEqual(s3.uploads,[])

    def test_hf_cache_precedes_download_and_legacy_r2(self):
        store=Mock();store.lookup.return_value={'storage':'hf','variants':{'720p':{}},'hf_revision':'b'*40}
        downloader=Mock();s3=S3(True)
        self.assertEqual(run_hf_job(s3,'media','movie','1','en',store=store,downloader=downloader),0)
        downloader.assert_not_called();self.assertEqual(s3.states[-1]['storage'],'hf');self.assertEqual(s3.uploads,[])

    def test_upload_failure_does_not_publish_ready_or_upload_r2_video(self):
        store=Mock();store.lookup.return_value=None;store.upload.side_effect=HFStorageError('hf_upload_failed')
        s3=S3();candidate={'youtube_id':'12345678901'}
        result=run_hf_job(s3,'media','movie','1','en',store=store,resolver=lambda *a:[candidate],downloader=lambda *a:({'720p':{}},30))
        self.assertEqual(result,1);self.assertFalse(any(s['status']=='ready' for s in s3.states));self.assertEqual(s3.uploads,[])

    def test_atomic_commit_uses_immutable_paths_and_publishes_revision(self):
        api=Mock();api.list_repo_tree.return_value=[];api.create_commit.return_value.oid='b'*40
        store=HFStore('saltpi/iplay-trailers','secret',api=api,download=Mock())
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'720p.mp4';path.write_bytes(b'video')
            state=store.upload('trailers/v1/movie/1/en',{'media_type':'movie','tmdb_id':1,'language':'en'},{'youtube_id':'12345678901'},
                               {'720p':{'file':path,'width':1280,'height':720}},30)
        self.assertEqual(state['hf_revision'],'b'*40);ops=api.create_commit.call_args.kwargs['operations'];self.assertEqual(len(ops),2)
        self.assertIn(state['variants']['720p']['sha256'],ops[0].path_in_repo)
        self.assertNotIn('secret',str(state))

    def test_budget_reads_actual_tree_sizes(self):
        api=Mock();api.list_repo_tree.return_value=[Mock(size=REPO_LIMIT_BYTES)]
        with self.assertRaisesRegex(HFStorageError,'budget'):
            HFStore('saltpi/iplay-trailers','secret',api=api,download=Mock()).ensure_capacity(1)

    def test_native_720_source_never_advertises_1080(self):
        import json
        def command(args,timeout):
            if args[0]=='yt-dlp':
                if '--dump-single-json' in args:return json.dumps({'duration':30})
                Path(args[args.index('-o')+1].replace('%(ext)s','mkv')).write_bytes(b'source');return ''
            if args[0]=='ffmpeg':Path(args[-1]).write_bytes(b'encoded');return ''
            return json.dumps({'streams':[{'codec_type':'video','codec_name':'h264','width':1280,'height':720}]})
        with tempfile.TemporaryDirectory() as temp:
            variants,_=download_variants({'youtube_id':'12345678901'},Path(temp),command,JobError)
            self.assertEqual(list(variants),['720p'])

    def test_native_1080_source_produces_two_real_sizes(self):
        import json
        def command(args,timeout):
            if args[0]=='yt-dlp':
                if '--dump-single-json' in args:return json.dumps({'duration':30})
                Path(args[args.index('-o')+1].replace('%(ext)s','mkv')).write_bytes(b'source');return ''
            if args[0]=='ffmpeg':Path(args[-1]).write_bytes(b'encoded');return ''
            width,height=(1280,720) if args[-1].endswith('720p.mp4') else (1920,1080)
            return json.dumps({'streams':[{'codec_type':'video','codec_name':'h264','width':width,'height':height}]})
        with tempfile.TemporaryDirectory() as temp:
            variants,_=download_variants({'youtube_id':'12345678901'},Path(temp),command,JobError)
            self.assertEqual(list(variants),['720p','1080p'])
            self.assertEqual(variants['1080p']['height'],1080)


class Migration(unittest.TestCase):
    def test_youtube_auth_failure_migrates_existing_real_identity_without_upscaling(self):
        from unittest.mock import patch
        import json
        s3=S3(True);s3.download_file=lambda bucket,key,path:Path(path).write_bytes(b'legacy')
        store=Mock();store.lookup.return_value=None
        store.upload.return_value={'storage':'hf','variants':{'720p':{}},'hf_revision':'b'*40}
        probe=json.dumps({'streams':[{'codec_type':'video','codec_name':'h264','width':1280,'height':720}],'format':{'duration':'30'}})
        with patch('cache_trailer.command',return_value=probe):
            result=run_hf_job(s3,'media','movie','1','en',store=store,resolver=lambda *a:[{}],
                              downloader=Mock(side_effect=JobError('youtube_auth_required')))
        self.assertEqual(result,0);self.assertEqual(s3.states[-1]['storage'],'hf')
        self.assertEqual(list(store.upload.call_args.args[3]),['720p']);self.assertEqual(s3.uploads,[])

    def test_failed_refresh_preserves_existing_hf_snapshot(self):
        store=Mock();store.lookup.return_value={'storage':'hf','variants':{'720p':{}},'hf_revision':'b'*40}
        s3=S3()
        result=run_hf_job(s3,'media','movie','1','en',store=store,resolver=lambda *a:[{}],
                          downloader=Mock(side_effect=JobError('youtube_auth_required')),refresh=True)
        self.assertEqual(result,1);self.assertEqual(s3.states[-1]['status'],'ready')
        self.assertEqual(s3.states[-1]['hf_revision'],'b'*40)

if __name__=='__main__':unittest.main()

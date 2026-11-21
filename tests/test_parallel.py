import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).parents[1]/'scripts'))
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from unittest.mock import patch
from hf_job import run_hf_job
from test_cache_trailer import S3

class Parallel(unittest.TestCase):
    def test_movie_and_tv_download_in_parallel_but_commits_are_serialized(self):
        locks={};registry=threading.Lock();local=threading.local();barrier=threading.Barrier(2)
        commit_key='trailer-control/v1/download-lock.json'
        @contextmanager
        def lease(s3,bucket,key=commit_key):
            with registry:lock=locks.setdefault(key,threading.Lock())
            with lock:
                previous=getattr(local,'global_commit',False)
                if key==commit_key:local.global_commit=True
                try:yield
                finally:local.global_commit=previous
        parent=self
        class Store:
            def lookup(self,base):return None
            def ensure_capacity(self,reserve):pass
            def upload(self,base,identity,candidate,variants,duration):
                parent.assertTrue(getattr(local,'global_commit',False))
                time.sleep(.02)
                return {'storage':'hf','variants':{'720p':{}},'hf_revision':'a'*40}
        def download(candidate,directory):
            self.assertFalse(getattr(local,'global_commit',False))
            barrier.wait(timeout=3) # A global download lock would deadlock this pair.
            return {'720p':{}},30
        def run(kind):
            return run_hf_job(S3(),'media',kind,'1399','en',store=Store(),resolver=lambda *a:[{}],downloader=download)
        with patch('cache_trailer.storage_lease',lease),ThreadPoolExecutor(max_workers=2) as pool:
            futures=[pool.submit(run,kind) for kind in ['movie','tv']]
            self.assertEqual([future.result(timeout=5) for future in futures],[0,0])
        self.assertIn('trailer-control/v1/identities/movie/1399/en.json',locks)
        self.assertIn('trailer-control/v1/identities/tv/1399/en.json',locks)

if __name__=='__main__':unittest.main()

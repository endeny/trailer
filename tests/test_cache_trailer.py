import importlib.util
import json
import io
from pathlib import Path
import unittest
import tempfile
from unittest.mock import patch
from botocore.exceptions import ClientError

spec = importlib.util.spec_from_file_location('cache_trailer', Path(__file__).parents[1] / 'scripts/cache_trailer.py')
job = importlib.util.module_from_spec(spec)
spec.loader.exec_module(job)


class S3:
    def __init__(self, cached=False):
        self.cached = cached
        self.states = []
        self.uploads = []
        self.objects = {}
        self.copies = []
    def head_object(self, **kwargs):
        if self.cached:
            return {'ContentLength': 123}
        raise ClientError({'Error': {'Code': '404'}}, 'HeadObject')
    def put_object(self, **kwargs):
        key = kwargs['Key']
        if kwargs.get('IfNoneMatch') == '*' and key in self.objects:
            raise ClientError({'Error': {'Code':'PreconditionFailed'}}, 'PutObject')
        self.objects[key] = kwargs['Body']
        if key.endswith('/metadata.json'): self.states.append(json.loads(kwargs['Body']))
    def get_object(self, **kwargs):
        if kwargs['Key'] not in self.objects:
            raise ClientError({'Error': {'Code':'NoSuchKey'}}, 'GetObject')
        return {'Body':io.BytesIO(self.objects[kwargs['Key']]),'ETag':'"test"'}
    def delete_object(self, **kwargs): self.objects.pop(kwargs['Key'],None)
    def list_objects_v2(self, **kwargs): return {'Contents':[],'IsTruncated':False}
    def copy_object(self, **kwargs): self.copies.append(kwargs)
    def upload_file(self, path, bucket, key, **kwargs):
        assert Path(path).read_bytes() == b'video'
        self.uploads.append(key)


class Pipeline(unittest.TestCase):
    def test_rotated_cookie_is_classified_without_exposing_downloader_output(self):
        result = type('Result', (), {'returncode':1, 'stdout':'', 'stderr':'WARNING: cookies are no longer valid; private cookie details'})()
        with patch.object(job.subprocess, 'run', return_value=result):
            with self.assertRaisesRegex(job.JobError, '^youtube_cookies_expired$'):
                job.command(['yt-dlp'])

    def test_input_and_language_validation(self):
        self.assertEqual(job.validate('tv', '1399', 'en'), 'trailers/v1/tv/1399/en')
        for args in [('movie', '../12', 'en'), ('all', '12', 'en'), ('movie', '12', 'en-US')]:
            with self.assertRaises(job.JobError): job.validate(*args)

    def test_resolver_ranks_trailers_and_filters_wrong_language(self):
        videos = [{'site': 'YouTube', 'type': kind, 'iso_639_1': lang, 'key': key, 'official': official}
                  for kind, lang, key, official in [('Teaser', 'en', '12345678901', True), ('Trailer', 'de', '12345678902', True), ('Trailer', 'en', '12345678903', True)]]
        self.assertEqual([v['youtube_id'] for v in job.tmdb_candidates({'results': videos}, 'en')], ['12345678903', '12345678901'])
        self.assertEqual(job.kinocheck_candidates({'trailer': {'youtube_video_id': '12345678901', 'language': 'de', 'categories': ['Trailer']}}, 'en'), [])

    def test_kinocheck_uses_unfiltered_endpoint_then_filters_locally(self):
        data = {'trailer': {'youtube_video_id': '12345678901', 'language': 'en', 'categories': ['Trailer']}}
        with patch.dict(job.os.environ, {'TMDB_TOKEN': '', 'KINOCHECK_API_KEY': ''}), patch.object(job, 'request_json', return_value=data) as fetch:
            result = job.resolve('movie', '299534', 'en')
        self.assertEqual(result[0]['youtube_id'], '12345678901')
        self.assertNotIn('categories=', fetch.call_args.args[0])

    def test_default_client_is_used_before_token_dependent_mweb(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(job.os.environ, {'YOUTUBE_PO_PROVIDER': 'true'}), patch.object(job, 'download_with_client', return_value=('video', 30)) as download:
            self.assertEqual(job.download({}, Path(temp)), ('video', 30))
            self.assertEqual(download.call_args.args[2], 'default')
            self.assertEqual(download.call_count, 1)

    def test_mweb_is_a_fallback_after_default_client_failure(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(job.os.environ, {'YOUTUBE_PO_PROVIDER': 'true'}), patch.object(job, 'download_with_client', side_effect=[job.JobError('youtube_format_unavailable'), ('video', 30)]) as download:
            self.assertEqual(job.download({}, Path(temp)), ('video', 30))
            self.assertEqual([c.args[2] for c in download.call_args_list], ['default', 'mweb'])

    def test_storage_budget_blocks_download_before_resolving(self):
        s3 = S3()
        s3.list_objects_v2 = lambda **kwargs: {'Contents':[{'Size':job.STORAGE_LIMIT_BYTES}], 'IsTruncated':False}
        def resolver(*args): raise AssertionError('must not resolve when storage is full')
        self.assertEqual(job.run_job(s3, 'bucket', 'movie','1','en',resolver),1)
        self.assertEqual(s3.states[-1]['error'],'storage_budget_exceeded')
        self.assertNotIn('trailer-control/v1/download-lock.json',s3.objects)

    def test_capacity_checks_all_pages_and_reserves_output_size(self):
        s3 = S3(); pages=iter([{'Contents':[{'Size':job.STORAGE_LIMIT_BYTES-100}], 'IsTruncated':True, 'NextContinuationToken':'next'}, {'Contents':[{'Size':10}], 'IsTruncated':False}])
        s3.list_objects_v2=lambda **kwargs: next(pages)
        self.assertEqual(job.ensure_capacity(s3,'bucket',90),job.STORAGE_LIMIT_BYTES-90)

    def test_crashed_upload_lease_can_be_recovered_by_etag(self):
        s3=S3(); key='trailer-control/v1/download-lock.json'
        s3.objects[key]=json.dumps({'owner':'old','expires_at':0}).encode()
        with job.storage_lease(s3,'bucket'):
            self.assertNotEqual(json.loads(s3.objects[key])['owner'],'old')
        self.assertNotIn(key,s3.objects)

    def test_cached_media_does_not_call_external_source(self):
        s3 = S3(True)
        def resolver(*args): raise AssertionError('must not resolve cached video')
        self.assertEqual(job.run_job(s3, 'bucket', 'movie', '1', 'en', resolver), 0)
        self.assertEqual(s3.states[-1]['status'], 'ready')

    def test_missing_trailer_is_not_ready(self):
        s3 = S3()
        self.assertEqual(job.run_job(s3, 'bucket', 'movie', '1', 'en', lambda *args: []), 0)
        self.assertEqual(s3.states[-1]['status'], 'not_found')
        self.assertEqual(s3.uploads, [])

    def test_ready_is_published_after_upload(self):
        s3 = S3()
        def download(candidate, directory):
            self.assertEqual(s3.states[-1]['status'], 'running')
            path = directory / 'video.mp4'; path.write_bytes(b'video')
            return path, 30
        result = job.run_job(s3, 'bucket', 'tv', '1', 'en', lambda *args: [{'youtube_id': '12345678901', 'source': 'tmdb', 'title': 'Trailer'}], download)
        self.assertEqual(result, 0)
        self.assertEqual(s3.uploads, ['trailers/v1/tv/1/en/video.mp4'])
        self.assertEqual(s3.states[-1]['status'], 'ready')

    def test_failed_download_records_a_safe_error_and_does_not_upload(self):
        s3 = S3()
        def download(*args): raise job.JobError('youtube_auth_required')
        result = job.run_job(s3, 'bucket', 'movie', '1', 'en', lambda *args: [{}], download)
        self.assertEqual(result, 1)
        self.assertEqual(s3.states[-1]['error'], 'youtube_auth_required')
        self.assertEqual(s3.uploads, [])



class TMDBProxy(unittest.TestCase):
    def test_existing_proxy_resolves_videos_without_an_extra_tmdb_secret(self):
        video={'site':'YouTube','type':'Trailer','iso_639_1':'en','key':'12345678901','official':True}
        with patch.dict(job.os.environ, {'TMDB_TOKEN':'','TMDB_PROXY_URL':'https://tmdb.saltpi.cn'}),patch.object(job,'request_json',side_effect=[{'results':[video]},{}]) as fetch:
            result=job.resolve('movie','1726','en')
        self.assertEqual(result[0]['source'],'tmdb');self.assertEqual(result[0]['youtube_id'],'12345678901')
        self.assertIn('/3/movie/1726/videos',fetch.call_args_list[0].args[0])
        self.assertIn('X-iPlay-Attest-Trace',fetch.call_args_list[0].args[1])

    def test_expired_cookie_does_not_retry_other_download_profiles(self):
        with tempfile.TemporaryDirectory() as temp,patch.dict(job.os.environ,{'YOUTUBE_PO_PROVIDER':'true'}),patch.object(job,'download_with_client',side_effect=job.JobError('youtube_cookies_expired')) as download:
            with self.assertRaisesRegex(job.JobError,'youtube_cookies_expired'):job.download({},Path(temp))
            self.assertEqual(download.call_count,1)

if __name__ == '__main__': unittest.main()

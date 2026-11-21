import importlib.util
import json
from pathlib import Path
import unittest
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
    def head_object(self, **kwargs):
        if self.cached:
            return {'ContentLength': 123}
        raise ClientError({'Error': {'Code': '404'}}, 'HeadObject')
    def put_object(self, **kwargs):
        self.states.append(json.loads(kwargs['Body']))
    def upload_file(self, path, bucket, key, **kwargs):
        assert Path(path).read_bytes() == b'video'
        self.uploads.append(key)


class Pipeline(unittest.TestCase):
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


if __name__ == '__main__': unittest.main()

"""HF-first job orchestration. R2 is the control plane, never the new video store."""
import json
import tempfile
from pathlib import Path
from download_variants import download_variants, VARIANT_LIMITS
from hf_storage import HFStore, HFStorageError


def run_hf_job(s3, bucket, media_type, tmdb_id, language, *, store=None, resolver=None, downloader=None):
    from cache_trailer import validate, now, storage_lease, command, JobError, resolve
    import os
    base = validate(media_type, tmdb_id, language)
    identity = dict(media_type=media_type, tmdb_id=int(tmdb_id), language=language)
    resolver = resolver or resolve
    downloader = downloader or (lambda candidate, directory: download_variants(candidate, directory, command, JobError))
    def publish(status, **fields):
        state = {**identity, 'status': status, 'updated_at': now(), **fields}
        s3.put_object(Bucket=bucket, Key=f'{base}/metadata.json', Body=json.dumps(state).encode(),
                      ContentType='application/json', CacheControl='no-store')
    try:
        store = store or HFStore(os.environ.get('HF_REPO_ID'), os.environ.get('HF_TOKEN'))
        with storage_lease(s3, bucket):
            cached = store.lookup(base)
            if cached:
                publish('ready', **{k:v for k,v in cached.items() if k not in (*identity, 'status', 'updated_at')})
                print('HF cache hit; download skipped.')
                return 0
            store.ensure_capacity(sum(VARIANT_LIMITS.values()))
            publish('running')
            candidates = resolver(media_type, tmdb_id, language)
            if not candidates:
                publish('not_found', error='no_matching_trailer')
                return 0
            last_error = 'download_failed'
            for candidate in candidates:
                try:
                    with tempfile.TemporaryDirectory(prefix='trailer-hf-') as temp:
                        variants, duration = downloader(candidate, Path(temp))
                        manifest = store.upload(base, identity, candidate, variants, duration)
                        publish('ready', **{k:v for k,v in manifest.items() if k not in (*identity, 'status', 'updated_at')})
                        print('Trailer variants committed to HF; R2 manifest published.')
                        return 0
                except JobError as error:
                    last_error = str(error)
                    print(f'Candidate failed: {last_error}')
            raise JobError(last_error)
    except (HFStorageError, JobError) as error:
        publish('failed', error=str(error))
        print(f'Trailer job failed: {error}')
        return 1
    except Exception:
        publish('failed', error='storage_or_job_failed')
        print('Trailer job failed: storage_or_job_failed')
        return 1

"""HF-first job orchestration. R2 is the control plane, never the new video store."""
import json
import tempfile
from pathlib import Path
from download_variants import download_variants, VARIANT_LIMITS
from hf_storage import HFStore, HFStorageError


def run_hf_job(s3, bucket, media_type, tmdb_id, language, *, store=None, resolver=None, downloader=None, refresh=False):
    from cache_trailer import validate, now, storage_lease, command, JobError, resolve
    import os
    base = validate(media_type, tmdb_id, language)
    identity = dict(media_type=media_type, tmdb_id=int(tmdb_id), language=language)
    def migrate_existing(store):
        # A trusted legacy key identifies this exact media. Never label a 720p copy as 1080p.
        from botocore.exceptions import ClientError
        key = f'{base}/video.mp4'
        try:
            head = s3.head_object(Bucket=bucket, Key=key)
        except ClientError as error:
            if error.response['Error']['Code'] in ('404', 'NoSuchKey', 'NotFound'): return None
            raise
        if not 0 < head.get('ContentLength', 0) <= VARIANT_LIMITS['720p']:
            raise JobError('invalid_legacy_video')
        with tempfile.TemporaryDirectory(prefix='trailer-migrate-') as temp:
            output = Path(temp)/'720p.mp4'
            s3.download_file(bucket, key, str(output))
            probe = json.loads(command(['ffprobe','-v','error','-show_streams','-show_format','-of','json',str(output)],timeout=30))
            stream = next((v for v in probe.get('streams',[]) if v.get('codec_type') == 'video'),{})
            duration = float(probe.get('format',{}).get('duration',0))
            if stream.get('codec_name') != 'h264' or not 0 < stream.get('width',0) <= 1280 or not 0 < stream.get('height',0) <= 720 or not 0 < duration <= 300:
                raise JobError('invalid_legacy_video')
            manifest = store.upload(base, identity, {'source':'existing_r2'},
                {'720p':{'file':output,'width':stream['width'],'height':stream['height']}}, duration)
            manifest['migrated_from_r2'] = key
            return manifest

    resolver = resolver or resolve
    downloader = downloader or (lambda candidate, directory: download_variants(candidate, directory, command, JobError))
    def publish(status, **fields):
        state = {**identity, 'status': status, 'updated_at': now(), **fields}
        s3.put_object(Bucket=bucket, Key=f'{base}/metadata.json', Body=json.dumps(state).encode(),
                      ContentType='application/json', CacheControl='no-store')
    cached = None
    try:
        store = store or HFStore(os.environ.get('HF_REPO_ID'), os.environ.get('HF_TOKEN'))
        with storage_lease(s3, bucket):
            cached = store.lookup(base)
            if cached and not refresh:
                publish('ready', **{k:v for k,v in cached.items() if k not in (*identity, 'status', 'updated_at')})
                print('HF cache hit; download skipped.')
                return 0
            store.ensure_capacity(sum(VARIANT_LIMITS.values()))
            if not cached:
                publish('running')
            candidates = resolver(media_type, tmdb_id, language)
            if not candidates and cached:
                raise JobError('no_matching_trailer')
            if not candidates:
                migrated = migrate_existing(store)
                if migrated:
                    publish('ready', **{k:v for k,v in migrated.items() if k not in (*identity, 'status', 'updated_at')})
                    print('Existing real 720p R2 trailer migrated to HF.')
                else:
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
            if cached:
                raise JobError(last_error)
            migrated = migrate_existing(store)
            if migrated:
                publish('ready', **{k:v for k,v in migrated.items() if k not in (*identity, 'status', 'updated_at')})
                print('YouTube unavailable; existing real 720p R2 trailer migrated to HF.')
                return 0
            raise JobError(last_error)
    except (HFStorageError, JobError) as error:
        if cached:
            publish('ready', **{k:v for k,v in cached.items() if k not in (*identity, 'status', 'updated_at')}, refresh_error=str(error))
        else:
            publish('failed', error=str(error))
        print(f'Trailer job failed: {error}')
        return 1
    except Exception:
        if cached:
            publish('ready', **{k:v for k,v in cached.items() if k not in (*identity, 'status', 'updated_at')}, refresh_error='storage_or_job_failed')
        else:
            publish('failed', error='storage_or_job_failed')
        print('Trailer job failed: storage_or_job_failed')
        return 1

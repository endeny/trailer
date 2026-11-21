#!/usr/bin/env python3
"""Sequential, checkpointed HF prewarming. Stop systemic errors rather than burning 200 jobs."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from botocore.exceptions import ClientError
from cache_trailer import create_s3, validate, now

AUTH_ERRORS = {'youtube_cookies_expired','youtube_auth_required','youtube_token_required','youtube_access_denied',
               'youtube_rate_limited','youtube_js_challenge_failed'}
TERMINAL = {'ready','not_found'}
BATCH_SECONDS = 315 * 60


def load_plan(path, language):
    data = json.loads(Path(path).read_text())
    items = data.get('items')
    if not isinstance(items,list) or not 1 <= len(items) <= 200: raise ValueError('invalid_plan')
    identities, seen = [], set()
    for item in items:
        if not isinstance(item,dict) or item.get('media_type') != 'movie' or type(item.get('tmdb_id')) is not int: raise ValueError('invalid_plan_identity')
        validate('movie',item['tmdb_id'],language)
        if item['tmdb_id'] in seen: raise ValueError('duplicate_identity')
        seen.add(item['tmdb_id'])
        identities.append({'media_type':'movie','tmdb_id':item['tmdb_id'],'language':language})
    digest = hashlib.sha256(json.dumps(identities,sort_keys=True).encode()).hexdigest()[:16]
    return identities, f'popular-movies-{digest}-{language}'


def read_json(s3,bucket,key):
    try:return json.loads(s3.get_object(Bucket=bucket,Key=key)['Body'].read())
    except ClientError as error:
        if error.response['Error']['Code'] in ('404','NoSuchKey','NotFound'):return None
        raise


def execute_item(item):
    try:
        result=subprocess.run([sys.executable,str(Path(__file__).with_name('cache_trailer.py')),
                               '--media-type',item['media_type'],'--tmdb-id',str(item['tmdb_id']),
                               '--language',item['language']],capture_output=True,text=True,timeout=18*60)
        # The child hides downloader/SDK diagnostics. Do not copy its captured logs into artifacts.
        return result.returncode
    except subprocess.TimeoutExpired:return 124


def run_batch(s3,bucket,identities,batch_id,report_path,execute=execute_item,clock=time.monotonic):
    key=f'trailer-control/v1/batches/{batch_id}.json'
    old=read_json(s3,bucket,key)
    if old and [v.get('tmdb_id') for v in old.get('items',[])] != [v['tmdb_id'] for v in identities]:
        raise ValueError('checkpoint_identity_mismatch')
    report=old or {'schema_version':1,'batch_id':batch_id,'created_at':now(),
                   'items':[{**item,'status':'pending','attempts':0} for item in identities]}
    if report.get('resolver_version') != 'tmdb-proxy-v1':
        for item in report['items']:
            if item['status']=='not_found':item.update(status='pending',error=None)
    report['resolver_version']='tmdb-proxy-v1'
    report.update(status='running',run_id=os.environ.get('GITHUB_RUN_ID'),pause_reason=None)
    started, auth_failures=clock(),0
    def checkpoint():
        report['updated_at']=now()
        report['counts']={status:sum(v['status']==status for v in report['items']) for status in ['ready','not_found','failed','pending']}
        body=json.dumps(report,ensure_ascii=False,indent=2)+'\n'
        s3.put_object(Bucket=bucket,Key=key,Body=body.encode(),ContentType='application/json',CacheControl='no-store')
        Path(report_path).write_text(body)
    checkpoint()
    for rank,item in enumerate(report['items'],1):
        if item['status'] in TERMINAL:continue
        if clock()-started >= BATCH_SECONDS:
            report.update(status='paused',pause_reason='time_budget');break
        print(f"[{rank}/{len(identities)}] movie/{item['tmdb_id']}",flush=True)
        item.update(status='pending',attempts=item['attempts']+1)
        checkpoint() # A cancelled runner can safely resume this identity.
        code=execute(item)
        state=read_json(s3,bucket,f"trailers/v1/movie/{item['tmdb_id']}/{item['language']}/metadata.json") or {}
        # Never accept legacy R2 readiness as completion of an HF-first batch.
        status=state.get('status')
        if code==0 and status=='ready' and state.get('storage')=='hf':
            item.update(status='ready',qualities=sorted(state.get('variants',{})),error=None)
        elif code==0 and status=='not_found':item.update(status='not_found',error='no_matching_trailer')
        else:
            error=state.get('error') if code!=124 else 'download_or_encode_timeout'
            error=error if isinstance(error,str) and re.fullmatch('[a-z_]{1,80}',error) else 'job_failed'
            item.update(status='failed',error=error)
        auth_failures=auth_failures+1 if item.get('error') in AUTH_ERRORS else 0
        print(f"  {item['status']}"+(f": {item['error']}" if item.get('error') else ''),flush=True)
        if item.get('error')=='youtube_cookies_expired':
            report.update(status='paused',pause_reason='youtube_cookies_expired')
        elif auth_failures>=3:
            report.update(status='paused',pause_reason='youtube_auth_or_rate_limit')
        elif (item.get('error') or '').startswith(('hf_','storage_','missing_r2_')) or item.get('error')=='download_queue_busy':
            report.update(status='paused',pause_reason=item['error'])
        checkpoint()
        if report['status']=='paused':break
    else:
        report['status']='completed_with_errors' if any(v['status']=='failed' for v in report['items']) else 'completed'
    checkpoint()
    return report


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--plan',required=True);parser.add_argument('--language',default='en');parser.add_argument('--report',required=True)
    args=parser.parse_args()
    identities,batch_id=load_plan(args.plan,args.language)
    if not os.environ.get('HF_TOKEN') or not os.environ.get('HF_REPO_ID'):
        raise ValueError('hf_not_configured')
    if os.environ.get('STORAGE_BACKEND','hf')!='hf':raise ValueError('hf_required_for_batch')
    report=run_batch(create_s3(),os.environ['R2_BUCKET'],identities,batch_id,args.report)
    if os.environ.get('GITHUB_OUTPUT'):
        with open(os.environ['GITHUB_OUTPUT'],'a') as output:
            output.write(f"continue={'true' if report.get('pause_reason')=='time_budget' else 'false'}\n")
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'],'a') as summary:
            summary.write(f"## Popular movie HF prewarm\n\nBatch: `{batch_id}`\n\nState: `{report['status']}`; reason: `{report.get('pause_reason')}`\n\n")
            summary.write('\n'.join(f'- {key}: {value}' for key,value in report['counts'].items())+'\n')
    print(json.dumps({'batch_id':batch_id,'status':report['status'],'pause_reason':report.get('pause_reason'),'counts':report['counts']}))
    return 0 if report['status']=='completed' or report.get('pause_reason')=='time_budget' else 1


if __name__=='__main__':
    try:raise SystemExit(main())
    except Exception:
        print('Batch failed; check configuration and the last durable checkpoint.');raise SystemExit(1) from None

#!/usr/bin/env python3
"""Resolve a trailer and publish HF variants (or the explicit legacy R2 backend).

No external input is evaluated by a shell. Detailed downloader output is deliberately
not copied into public Actions logs or public R2 metadata (it can contain credentials).
"""
import argparse
import datetime as dt
import json
import time
import uuid
from contextlib import contextmanager
import os
from pathlib import Path
import re
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request

PREFIX = "trailers/v1"
MAX_BYTES = 40 * 1024 * 1024
MAX_DURATION = 300
STORAGE_LIMIT_BYTES = 8_000_000_000
CACHE_CONTROL = "public, max-age=86400, s-maxage=2592000"
COMMAND_DEADLINE = time.monotonic() + 17 * 60


class JobError(Exception):
    pass


def validate(media_type, tmdb_id, language):
    if media_type not in ("movie", "tv") or not re.fullmatch(r"[1-9]\d{0,9}", str(tmdb_id)) or not re.fullmatch(r"[a-z]{2}", language):
        raise JobError("invalid_input")
    return f"{PREFIX}/{media_type}/{tmdb_id}/{language}"


def request_json(url, headers=None):
    request = urllib.request.Request(url, headers={"User-Agent": "iPlay-Trailer-Cache/1.0", **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            data = json.load(response)
            if not isinstance(data, dict) or data.get("error"):
                raise JobError("source_api_failed")
            return data
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return {}
        raise JobError("source_api_failed") from None
    except (OSError, ValueError):
        raise JobError("source_api_failed") from None


def tmdb_candidates(data, language):
    videos = [v for v in data.get("results", []) if v.get("site") == "YouTube"
              and v.get("type") in ("Trailer", "Teaser") and v.get("iso_639_1") == language
              and re.fullmatch(r"[A-Za-z0-9_-]{11}", v.get("key", ""))]
    videos.sort(key=lambda v: (v.get("type") == "Trailer", v.get("official", False), v.get("published_at", "")), reverse=True)
    return [{"youtube_id": v["key"], "source": "tmdb", "title": v.get("name", ""), "language": language} for v in videos]


def kinocheck_candidates(data, language):
    videos = ([data["trailer"]] if isinstance(data.get("trailer"), dict) else []) + data.get("videos", [])
    result = []
    for v in videos:
        video_id = v.get("youtube_video_id", "")
        if v.get("language") == language and any(c in ("Trailer", "Teaser") for c in v.get("categories", [])) and re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
            result.append({"youtube_id": video_id, "source": "kinocheck", "title": v.get("title", ""), "language": language})
    return result


def resolve(media_type, tmdb_id, language):
    candidates = []
    token = os.environ.get("TMDB_TOKEN", "")
    source_failed = False
    if token:
        try:
            # The language filter controls localization; iso_639_1 below verifies the actual video language.
            data = request_json(f"https://api.themoviedb.org/3/{media_type}/{tmdb_id}/videos?language={language}", {"Authorization": f"Bearer {token}"})
            candidates.extend(tmdb_candidates(data, language))
        except JobError:
            source_failed = True
    if language in ("en", "de"):
        headers = {}
        if os.environ.get("KINOCHECK_API_KEY"):
            headers = {"X-Api-Key": os.environ["KINOCHECK_API_KEY"], "X-Api-Host": "api.kinocheck.com"}
        path = "movies" if media_type == "movie" else "shows"
        try:
            data = request_json(f"https://api.kinocheck.com/{path}?tmdb_id={tmdb_id}&language={language}", headers)
            candidates.extend(kinocheck_candidates(data, language))
        except JobError:
            source_failed = True
    if not token and language not in ("en", "de"):
        raise JobError("tmdb_token_required_for_language")
    if not candidates and source_failed:
        raise JobError("source_api_failed")
    seen = set()
    return [v for v in candidates if not (v["youtube_id"] in seen or seen.add(v["youtube_id"]))][:3]


def command(args, timeout=240):
    remaining = COMMAND_DEADLINE - time.monotonic()
    if remaining <= 0:
        raise JobError("download_or_encode_timeout")
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=min(timeout, remaining), check=False)
    except subprocess.TimeoutExpired:
        raise JobError("download_or_encode_timeout") from None
    if result.returncode:
        text = (result.stderr + result.stdout).lower()
        if "cookies are no longer valid" in text or "cookies have likely been rotated" in text:
            raise JobError("youtube_cookies_expired")
        if "sign in" in text or "cookies" in text or "not a bot" in text:
            raise JobError("youtube_auth_required")
        if "po token" in text or "po_token" in text:
            raise JobError("youtube_token_required")
        if "requested format" in text or "no video formats" in text:
            raise JobError("youtube_format_unavailable")
        if "429" in text or "too many requests" in text:
            raise JobError("youtube_rate_limited")
        if "video unavailable" in text or "video is unavailable" in text:
            raise JobError("youtube_video_unavailable")
        if "javascript" in text or "challenge" in text:
            raise JobError("youtube_js_challenge_failed")
        if "403" in text:
            raise JobError("youtube_access_denied")
        raise JobError("download_or_encode_failed")
    return result.stdout


def download_with_client(candidate, directory, client):
    base = ["yt-dlp", "--ignore-config", "--no-playlist", "--no-progress", "--no-warnings",
            "--socket-timeout", "30", "--retries", "2", "--fragment-retries", "2", "--concurrent-fragments", "1"]
    if client != "default":
        base += ["--extractor-args", "youtube:player_client=" + client]
    if os.environ.get("YOUTUBE_COOKIES_FILE"):
        base += ["--cookies", os.environ["YOUTUBE_COOKIES_FILE"]]
    url = f"https://www.youtube.com/watch?v={candidate['youtube_id']}"
    info = json.loads(command(base + ["--skip-download", "--dump-single-json", url], timeout=90))
    duration = info.get("duration")
    if info.get("is_live") or not duration or not 0 < float(duration) <= MAX_DURATION:
        raise JobError("unsupported_duration")
    source = directory / "source.%(ext)s"
    command(base + ["-f", "bv*[height<=720]+ba/b[height<=720]", "--max-filesize", "150M",
                    "--merge-output-format", "mkv", "-o", str(source), url], timeout=240)
    inputs = [p for p in directory.glob("source.*") if p.suffix not in (".part", ".ytdl")]
    if len(inputs) != 1 or not inputs[0].is_file():
        raise JobError("download_incomplete")
    output = directory / "video.mp4"
    # Conservative bitrate + final size check; native Apple playback always gets H.264/AAC and faststart.
    bitrate = min(1600, max(200, int(MAX_BYTES * 8 * 0.85 / float(duration) / 1000) - 96))
    command(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(inputs[0]),
             "-map", "0:v:0", "-map", "0:a:0?", "-t", str(MAX_DURATION),
             "-vf", "scale=w='min(1280,iw)':h='min(720,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2",
             "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-b:v", f"{bitrate}k",
             "-maxrate", f"{bitrate}k", "-bufsize", f"{bitrate * 2}k", "-c:a", "aac", "-b:a", "96k",
             "-movflags", "+faststart", str(output)], timeout=240)
    if not output.is_file() or not 0 < output.stat().st_size <= MAX_BYTES:
        raise JobError("output_size_exceeded")
    probe = json.loads(command(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(output)], timeout=30))
    if not any(s.get("codec_name") == "h264" for s in probe.get("streams", [])):
        raise JobError("invalid_output")
    return output, float(probe["format"]["duration"])



def download(candidate, directory):
    # yt-dlp defaults can expose playable HLS formats without a GVS token.
    # Forcing mweb first can discard those formats even with valid account cookies.
    profiles = ["default"]
    if os.environ.get("YOUTUBE_PO_PROVIDER") == "true":
        profiles.append("mweb")
    last_error = None
    for client in profiles:
        work = directory / client
        work.mkdir()
        try:
            return download_with_client(candidate, work, client)
        except JobError as error:
            last_error = error
            print(f"Download profile {client} failed: {error}")
    raise last_error



@contextmanager
def storage_lease(s3, bucket):
    """Serialize this service's uploads using a conditional R2 object, with crash recovery."""
    from botocore.exceptions import ClientError
    key = "trailer-control/v1/download-lock.json"
    owner = uuid.uuid4().hex
    acquired = False
    for attempt in range(61):
        payload = json.dumps({"owner": owner, "expires_at": time.time() + 1800}).encode()
        try:
            s3.put_object(Bucket=bucket, Key=key, Body=payload, IfNoneMatch="*", ContentType="application/json", CacheControl="no-store")
            acquired = True
            break
        except ClientError as error:
            if error.response["Error"]["Code"] not in ("412", "PreconditionFailed", "ConditionalRequestConflict", "409"):
                raise
        try:
            current = s3.get_object(Bucket=bucket, Key=key)
            state = json.loads(current["Body"].read())
            if float(state.get("expires_at", 0)) < time.time():
                s3.put_object(Bucket=bucket, Key=key, Body=payload, IfMatch=current["ETag"], ContentType="application/json", CacheControl="no-store")
                acquired = True
                break
        except ClientError as error:
            if error.response["Error"]["Code"] not in ("404", "NoSuchKey", "412", "PreconditionFailed", "ConditionalRequestConflict", "409"):
                raise
        if attempt < 60:
            time.sleep(5)
    if not acquired:
        raise JobError("download_queue_busy")
    try:
        yield
    finally:
        # The command deadline is under 20 minutes; the lease is valid for 30.
        # A cancelled Action leaves a lease that a later job can safely replace by ETag.
        try:
            current = s3.get_object(Bucket=bucket, Key=key)
            if json.loads(current["Body"].read()).get("owner") == owner:
                s3.delete_object(Bucket=bucket, Key=key)
        except Exception:
            pass


def ensure_capacity(s3, bucket, reserve_bytes=MAX_BYTES):
    total = 0
    args = {"Bucket": bucket}
    while True:
        page = s3.list_objects_v2(**args)
        total += sum(int(item["Size"]) for item in page.get("Contents", []))
        if total + reserve_bytes > STORAGE_LIMIT_BYTES:
            raise JobError("storage_budget_exceeded")
        if not page.get("IsTruncated"):
            break
        args["ContinuationToken"] = page["NextContinuationToken"]
    return total


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def run_job(s3, bucket, media_type, tmdb_id, language, resolver=resolve, downloader=download):
    from botocore.exceptions import ClientError
    base = validate(media_type, tmdb_id, language)
    key = f"{base}/video.mp4"
    metadata_key = f"{base}/metadata.json"
    def publish(status, **fields):
        state = {"schema_version": 1, "media_type": media_type, "tmdb_id": int(tmdb_id), "language": language,
                 "status": status, "updated_at": now(), **fields}
        s3.put_object(Bucket=bucket, Key=metadata_key, Body=json.dumps(state).encode(), ContentType="application/json", CacheControl="no-store")
    try:
        existing = s3.head_object(Bucket=bucket, Key=key)
        if existing.get("ContentLength", 0) > 0:
            if existing.get("CacheControl") != CACHE_CONTROL:
                with storage_lease(s3, bucket):
                    ensure_capacity(s3, bucket, 0)
                    s3.copy_object(Bucket=bucket, Key=key, CopySource={"Bucket": bucket, "Key": key},
                                   MetadataDirective="REPLACE", ContentType="video/mp4", CacheControl=CACHE_CONTROL)
            provenance = {}
            try:
                old = json.loads(s3.get_object(Bucket=bucket, Key=metadata_key)["Body"].read())
                provenance = {name: old[name] for name in ("youtube_id", "source", "title", "duration_seconds") if name in old}
            except ClientError as error:
                if error.response["Error"]["Code"] not in ("404", "NoSuchKey", "NotFound"):
                    raise
            publish("ready", size_bytes=existing["ContentLength"], object_key=key, **provenance)
            print("Already cached; download skipped.")
            return 0
    except ClientError as error:
        if error.response["Error"]["Code"] not in ("404", "NoSuchKey", "NotFound"):
            raise
    publish("running")
    try:
        with storage_lease(s3, bucket):
            ensure_capacity(s3, bucket)
            candidates = resolver(media_type, tmdb_id, language)
            if not candidates:
                publish("not_found", error="no_matching_trailer")
                print("No matching trailer found.")
                return 0
            last_error = "download_failed"
            for candidate in candidates:
                try:
                    with tempfile.TemporaryDirectory(prefix="trailer-") as temp:
                        output, duration = downloader(candidate, Path(temp))
                        ensure_capacity(s3, bucket, output.stat().st_size)
                        s3.upload_file(str(output), bucket, key, ExtraArgs={"ContentType": "video/mp4", "CacheControl": CACHE_CONTROL})
                        publish("ready", youtube_id=candidate["youtube_id"], source=candidate["source"], title=candidate["title"],
                                size_bytes=output.stat().st_size, duration_seconds=duration, object_key=key)
                        print("Trailer uploaded to R2.")
                        return 0
                except JobError as error:
                    last_error = str(error)
                    print(f"Candidate failed: {last_error}")
            raise JobError(last_error)
    except JobError as error:
        publish("failed", error=str(error))
        print(f"Trailer job failed: {error}")
        return 1
    except Exception:
        # SDK exceptions can include URLs/headers; do not print them into public logs.
        publish("failed", error="storage_or_job_failed")
        print("Trailer job failed: storage_or_job_failed")
        return 1


def create_s3():
    for name in ("R2_ACCOUNT_ID", "R2_BUCKET", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        if not os.environ.get(name):
            raise JobError("missing_r2_configuration")
    if not re.fullmatch(r"[a-f0-9]{32}", os.environ["R2_ACCOUNT_ID"]):
        raise JobError("invalid_r2_account_id")
    import boto3
    from botocore.config import Config
    return boto3.client("s3", endpoint_url=f"https://{os.environ['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com", region_name="auto",
                      config=Config(retries={"max_attempts": 3, "mode": "standard"}, connect_timeout=15, read_timeout=60))

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--media-type", required=True)
    parser.add_argument("--tmdb-id", required=True)
    parser.add_argument("--language", default="en")
    parser.add_argument("--refresh", action="store_true", help="Explicitly try replacing a cached HF snapshot")
    parser.add_argument("--dry-run", action="store_true", help="Resolve/download/encode without using R2")
    args = parser.parse_args()
    validate(args.media_type, args.tmdb_id, args.language)
    if args.dry_run:
        candidates = resolve(args.media_type, args.tmdb_id, args.language)
        if not candidates:
            raise JobError("no_matching_trailer")
        last_error = "download_failed"
        for candidate in candidates:
            try:
                with tempfile.TemporaryDirectory(prefix="trailer-smoke-") as temp:
                    from download_variants import download_variants
                    variants, duration = download_variants(candidate, Path(temp), command, JobError)
                    print(json.dumps({"status": "verified", "youtube_id": candidate["youtube_id"], "duration_seconds": duration,
                                      "variants": {q: {"width": v["width"], "height": v["height"], "size_bytes": v["file"].stat().st_size} for q, v in variants.items()}}))
                    return 0
            except JobError as error:
                last_error = str(error)
                print(f"Candidate failed: {last_error}")
        print(f"Download verification failed: {last_error}")
        return 1
    s3 = create_s3()
    backend = os.environ.get("STORAGE_BACKEND", "hf")
    if backend == "hf":
        from hf_job import run_hf_job
        return run_hf_job(s3, os.environ["R2_BUCKET"], args.media_type, args.tmdb_id, args.language, refresh=args.refresh)
    if backend != "r2":
        raise JobError("invalid_storage_backend")
    return run_job(s3, os.environ["R2_BUCKET"], args.media_type, args.tmdb_id, args.language)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except JobError as error:
        print(f"Trailer job failed: {error}")
        raise SystemExit(1) from None
    except Exception:
        print("Trailer job could not initialize; check inputs, R2 configuration and permissions.")
        raise SystemExit(1) from None

#!/usr/bin/env python3
"""Resolve one media trailer, download it, and publish a complete MP4 to R2.

No external input is evaluated by a shell. Detailed downloader output is deliberately
not copied into public Actions logs or public R2 metadata (it can contain credentials).
"""
import argparse
import datetime as dt
import json
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
            return json.load(response)
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
            data = request_json(f"https://api.kinocheck.com/{path}?tmdb_id={tmdb_id}&language={language}&categories=Trailer,Teaser", headers)
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
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        raise JobError("download_or_encode_timeout") from None
    if result.returncode:
        text = (result.stderr + result.stdout).lower()
        if "sign in" in text or "cookies" in text or "not a bot" in text:
            raise JobError("youtube_auth_required")
        if "po token" in text or "po_token" in text:
            raise JobError("youtube_token_required")
        if "403" in text:
            raise JobError("youtube_access_denied")
        raise JobError("download_or_encode_failed")
    return result.stdout


def download(candidate, directory):
    base = ["yt-dlp", "--ignore-config", "--no-playlist", "--no-progress", "--no-warnings",
            "--socket-timeout", "30", "--retries", "2", "--fragment-retries", "2", "--concurrent-fragments", "1"]
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
            publish("ready", size_bytes=existing["ContentLength"], object_key=key)
            print("Already cached; download skipped.")
            return 0
    except ClientError as error:
        if error.response["Error"]["Code"] not in ("404", "NoSuchKey", "NotFound"):
            raise
    publish("running")
    try:
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
                    s3.upload_file(str(output), bucket, key, ExtraArgs={"ContentType": "video/mp4", "CacheControl": "public, max-age=3600"})
                    publish("ready", youtube_id=candidate["youtube_id"], source=candidate["source"], title=candidate["title"],
                            size_bytes=output.stat().st_size, duration_seconds=duration, object_key=key)
                    print("Trailer uploaded to R2.")
                    return 0
            except JobError as error:
                last_error = str(error)
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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--media-type", required=True)
    parser.add_argument("--tmdb-id", required=True)
    parser.add_argument("--language", default="en")
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
                    path, duration = download(candidate, Path(temp))
                    print(json.dumps({"status": "verified", "youtube_id": candidate["youtube_id"], "duration_seconds": duration, "size_bytes": path.stat().st_size}))
                    return 0
            except JobError as error:
                last_error = str(error)
        print(f"Download verification failed: {last_error}")
        return 1
    for name in ("R2_ACCOUNT_ID", "R2_BUCKET", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        if not os.environ.get(name):
            raise JobError("missing_r2_configuration")
    if not re.fullmatch(r"[a-f0-9]{32}", os.environ["R2_ACCOUNT_ID"]):
        raise JobError("invalid_r2_account_id")
    import boto3
    from botocore.config import Config
    s3 = boto3.client("s3", endpoint_url=f"https://{os.environ['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com", region_name="auto",
                      config=Config(retries={"max_attempts": 3, "mode": "standard"}, connect_timeout=15, read_timeout=60))
    return run_job(s3, os.environ["R2_BUCKET"], args.media_type, args.tmdb_id, args.language)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        print("Trailer job could not initialize; check inputs, R2 configuration and permissions.")
        raise SystemExit(1) from None

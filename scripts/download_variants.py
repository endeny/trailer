"""Download at most native 1080p once, then encode bounded 720p/1080p MP4 variants."""
import json
import os
from pathlib import Path

VARIANT_LIMITS = {'720p': 40 * 1024 * 1024, '1080p': 120 * 1024 * 1024}


def download_variants(candidate, directory, command, error_type):
    clients = ['default'] + (['mweb'] if os.environ.get('YOUTUBE_PO_PROVIDER') == 'true' else [])
    last_error = None
    for client in clients:
        work = directory / client
        work.mkdir()
        try:
            base = ['yt-dlp', '--ignore-config', '--no-playlist', '--no-progress', '--no-warnings',
                    '--socket-timeout', '30', '--retries', '2', '--fragment-retries', '2', '--concurrent-fragments', '1']
            if client != 'default': base += ['--extractor-args', 'youtube:player_client='+client]
            if os.environ.get('YOUTUBE_COOKIES_FILE'): base += ['--cookies', os.environ['YOUTUBE_COOKIES_FILE']]
            url = f"https://www.youtube.com/watch?v={candidate['youtube_id']}"
            info = json.loads(command(base + ['--skip-download', '--dump-single-json', url], timeout=90))
            duration = float(info.get('duration') or 0)
            if info.get('is_live') or not 0 < duration <= 300: raise error_type('unsupported_duration')
            command(base + ['-f','bv*[height<=1080]+ba/b[height<=1080]', '--max-filesize','400M',
                            '--merge-output-format','mkv','-o',str(work/'source.%(ext)s'),url],timeout=240)
            files = [p for p in work.glob('source.*') if p.suffix not in ('.part','.ytdl')]
            if len(files) != 1: raise error_type('download_incomplete')
            probe = json.loads(command(['ffprobe','-v','error','-show_streams','-of','json',str(files[0])],timeout=30))
            video = next((s for s in probe.get('streams',[]) if s.get('codec_type') == 'video'),None)
            if not video: raise error_type('invalid_output')
            qualities = ['720p']
            if video.get('width',0) > 1280 or video.get('height',0) > 720: qualities.append('1080p')
            variants = {}
            for quality in qualities:
                width,height,peak,audio = (1280,720,1600,96) if quality == '720p' else (1920,1080,4000,128)
                limit = VARIANT_LIMITS[quality]
                bitrate = min(peak,max(200,int(limit*8*.85/duration/1000)-audio))
                output = work/f'{quality}.mp4'
                command(['ffmpeg','-hide_banner','-loglevel','error','-y','-i',str(files[0]),'-map','0:v:0','-map','0:a:0?',
                         '-t','300','-vf',f"scale=w='min({width},iw)':h='min({height},ih)':force_original_aspect_ratio=decrease:force_divisible_by=2",
                         '-c:v','libx264','-preset','veryfast','-pix_fmt','yuv420p','-b:v',f'{bitrate}k','-maxrate',f'{bitrate}k',
                         '-bufsize',f'{bitrate*2}k','-c:a','aac','-b:a',f'{audio}k','-movflags','+faststart',str(output)],timeout=240)
                if not output.is_file() or not 0 < output.stat().st_size <= limit: raise error_type('output_size_exceeded')
                encoded = json.loads(command(['ffprobe','-v','error','-show_streams','-of','json',str(output)],timeout=30))
                stream = next((s for s in encoded.get('streams',[]) if s.get('codec_type') == 'video'),{})
                if stream.get('codec_name') != 'h264': raise error_type('invalid_output')
                variants[quality] = {'file': output, 'width':stream['width'], 'height':stream['height']}
            return variants,duration
        except error_type as error:
            last_error = error
            print(f'Download profile {client} failed: {error}')
            if str(error) == 'youtube_cookies_expired': raise
    raise last_error

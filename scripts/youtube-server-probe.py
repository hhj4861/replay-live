"""Cloud-only feasibility probe; never imported by the production API.

Run inside a disposable, credential-free VM with public-only egress. Reports
contain categories/metadata only, never cookies, tokens or signed media URLs.
"""
import argparse
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import time

LIMIT = 50 * 1024**2
ROOT = Path(__file__).resolve().parents[1]


def video_url(video_id):
    if not re.fullmatch(r'[A-Za-z0-9_-]{11}', video_id):
        raise ValueError('INVALID_VIDEO_ID')
    return f'https://www.youtube.com/watch?v={video_id}'


def category(message):
    message = str(message).lower()
    for fragments, code in [
        (("confirm you're not a bot", 'confirm you’re not a bot', 'source_bot_check_required'), 'BOT_CHECK_REQUIRED'),
        (('http error 403', 'http error 429'), 'HTTP_ACCESS_REJECTED'),
        (('sign in', 'login required', 'private video'), 'LOGIN_REQUIRED'),
        (('requested format is not available',), 'FORMAT_UNAVAILABLE'),
        (('timed out', 'timeouterror'), 'TIMEOUT'),
        (('source_too_large',), 'SIZE_LIMIT'),
    ]:
        if any(fragment in message for fragment in fragments):
            return code
    return 'DOWNLOAD_FAILED'


class SafeLogger:
    def __init__(self):
        self.categories = set()
        self.provider_seen = False
        self.token_generated = False
        self.ejs_seen = False

    def debug(self, message):
        text = str(message).lower()
        self.provider_seen |= 'bgutil' in text and 'provider' in text
        self.token_generated |= 'po token' in text and ('generated' in text or 'successfully' in text)
        self.ejs_seen |= '[jsc' in text and ('solving' in text or 'challenge' in text)
        code = category(text)
        if code != 'DOWNLOAD_FAILED':
            self.categories.add(code)

    warning = error = info = debug


def reject_metadata(info, *, incomplete=False):
    duration = info.get('duration')
    if info.get('is_live') or info.get('live_status') in ('is_live', 'is_upcoming', 'post_live'):
        return 'LIVE_NOT_SUPPORTED'
    if info.get('availability') not in (None, 'public', 'unlisted'):
        return 'RESTRICTED_VIDEO'
    if duration is None and incomplete:
        return None
    if isinstance(duration, bool) or not isinstance(duration, (int, float)) or not math.isfinite(duration) or not 1 <= duration <= 120:
        return 'DURATION_LIMIT'
    return None


def media_info(path):
    result = subprocess.run(['ffprobe', '-v', 'error', '-show_streams', '-show_format', '-of', 'json', str(path)],
                            capture_output=True, timeout=15, check=True)
    data = json.loads(result.stdout)
    streams = data['streams']
    videos = [s for s in streams if s['codec_type'] == 'video']
    audios = [s for s in streams if s['codec_type'] == 'audio']
    if len(videos) != 1 or len(audios) != 1:
        raise ValueError('INVALID_STREAMS')
    duration = float(data['format']['duration'])
    if not math.isfinite(duration) or not 1 <= duration <= 120:
        raise ValueError('DURATION_LIMIT')
    return dict(duration=duration, video_codec=videos[0]['codec_name'], audio_codec=audios[0]['codec_name'],
                width=videos[0]['width'], height=videos[0]['height'])


def normalize(source, output, expected_duration):
    original = media_info(source)
    if abs(original['duration'] - expected_duration) > max(1.0, expected_duration * .02):
        raise ValueError('INCOMPLETE_DOWNLOAD')
    subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-xerror', '-i', str(source), '-map', '0:v:0', '-map', '0:a:0',
                    '-c:v', 'libx264', '-preset', 'fast', '-crf', '20', '-pix_fmt', 'yuv420p',
                    '-c:a', 'aac', '-movflags', '+faststart', '-fs', str(LIMIT), str(output)],
                   capture_output=True, timeout=90, check=True)
    if not 0 < output.stat().st_size < LIMIT:
        raise ValueError('SIZE_LIMIT')
    normalized = media_info(output)
    if abs(normalized['duration'] - expected_duration) > max(1.0, expected_duration * .02):
        raise ValueError('INCOMPLETE_TRANSCODE')
    subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-xerror', '-i', str(output), '-map', '0:v:0', '-map', '0:a:0', '-f', 'null', '-'],
                   capture_output=True, timeout=45, check=True)
    return dict(original=original, normalized=normalized, bytes=output.stat().st_size,
                sha256=hashlib.sha256(output.read_bytes()).hexdigest(), full_decode=True)


def observe_engine(events):
    """Count real calls/results, never preserve request/response secrets."""
    from yt_dlp.extractor.youtube import YoutubeIE
    from yt_dlp.extractor.youtube.pot.provider import PoTokenProvider
    from yt_dlp.extractor.youtube.jsc.provider import JsChallengeProvider
    original_player = YoutubeIE._extract_player_response
    original_token = PoTokenProvider.request_pot
    original_solve = JsChallengeProvider.bulk_solve

    def player(self, *args, **kwargs):
        response = original_player(self, *args, **kwargs)
        status = (response or {}).get('playabilityStatus', {}).get('status')
        events['player_statuses'].append(status if status in ('OK', 'LOGIN_REQUIRED', 'ERROR', 'UNPLAYABLE') else 'OTHER')
        return response

    def token(self, request):
        events['token_requests'] += 1
        response = original_token(self, request)
        if response and response.po_token:
            events['tokens_returned'] += 1
        return response

    def solve(self, requests):
        events['js_requests'] += len(requests)
        for response in original_solve(self, requests):
            if response.error is None:
                events['js_responses_ok'] += 1
            yield response

    YoutubeIE._extract_player_response = player
    PoTokenProvider.request_pot = token
    JsChallengeProvider.bulk_solve = solve


def child(mode, video_id, directory):
    # Parent supervisor enforces a deadline and aggregate disk limit even when
    # a downloader, JS provider or FFmpeg is stuck outside a progress callback.
    sys.path.insert(0, str(ROOT))
    report = dict(mode=mode, video_id=video_id, ok=False, phase='download',
                  yt_dlp_version=importlib.metadata.version('yt-dlp'))
    logger = SafeLogger()
    events = dict(token_requests=0, tokens_returned=0, js_requests=0, js_responses_ok=0,
                  player_statuses=[], media_bytes_observed=0)
    start = time.monotonic()
    try:
        if mode == 'baseline':
            from server.media_sources import download_source
            output = directory / 'baseline.mp4'
            result = download_source(dict(provider='youtube', url=video_url(video_id)), output,
                                     max_bytes=LIMIT, max_duration=120, timeout=120, check_active=lambda: None)
            report.update(result, media=media_info(output))
        else:
            from yt_dlp import YoutubeDL
            observe_engine(events)
            def progress(data):
                events['media_bytes_observed'] = max(events['media_bytes_observed'], data.get('downloaded_bytes', 0))
                if data.get('downloaded_bytes', 0) > LIMIT:
                    raise ValueError('SOURCE_TOO_LARGE')
            args = {'youtube': {'player_client': ['mweb']}} if mode.startswith('pot') else {}
            if mode.startswith('pot'):
                args['youtubepot-bgutilscript'] = {'server_home': ['/vercel/sandbox/bgutil/server']}
                args['youtubepot-bgutilhttp'] = {'disable': ['true']}
                if mode == 'pot-always':
                    args['youtube']['fetch_pot'] = ['always']
            options = dict(quiet=True, verbose=True, no_warnings=True, logger=logger, cachedir=False,
                           proxy='', cookiefile=None, cookiesfrombrowser=None, usenetrc=False,
                           noplaylist=True, retries=0, extractor_retries=0, fragment_retries=0, socket_timeout=10,
                           js_runtimes={'node': {}}, remote_components=set(), extractor_args=args,
                           format='bv*[height<=720]+ba/b[height<=720]', merge_output_format='mp4',
                           outtmpl=str(directory / 'source.%(ext)s'), max_filesize=LIMIT,
                           match_filter=reject_metadata, progress_hooks=[progress],
                           hls_prefer_native=True, concurrent_fragment_downloads=1)
            with YoutubeDL(options) as downloader:
                info = downloader.extract_info(video_url(video_id), download=True)
            rejection = reject_metadata(info or {})
            if rejection:
                raise ValueError(rejection)
            files = [p for p in directory.glob('source.*') if p.suffix in ('.mp4', '.webm', '.mkv')]
            if len(files) != 1 or not 0 < files[0].stat().st_size < LIMIT:
                raise ValueError('NO_COMPLETE_MEDIA')
            report['phase'] = 'normalize'
            report.update(normalize(files[0], directory / 'normalized.mp4', info['duration']))
        report.update(ok=True, phase='complete')
    except Exception as error:
        code = str(error)
        report['error'] = code if code in {'DURATION_LIMIT', 'LIVE_NOT_SUPPORTED', 'RESTRICTED_VIDEO', 'SIZE_LIMIT',
                                         'NO_COMPLETE_MEDIA', 'INCOMPLETE_DOWNLOAD', 'INCOMPLETE_TRANSCODE', 'INVALID_STREAMS'} else category(error)
        report['exception_type'] = type(error).__name__
    report.update(elapsed_ms=round((time.monotonic() - start) * 1000), events=events,
                  messages=sorted(logger.categories), provider_seen=logger.provider_seen,
                  token_success_log_observed=logger.token_generated, ejs_log_observed=logger.ejs_seen)
    return report


def supervise(mode, video_id):
    video_url(video_id)
    with tempfile.TemporaryDirectory(prefix='replay-youtube-probe-') as tmp:
        directory = Path(tmp)
        result_file = directory / 'result.json'
        process = subprocess.Popen([sys.executable, __file__, '--child', '--mode', mode, '--video-id', video_id,
                                    '--directory', tmp], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   start_new_session=True)
        start = time.monotonic()
        failure = None
        try:
            while process.poll() is None:
                if time.monotonic() - start > 240:
                    failure = 'TIMEOUT'
                    break
                total = sum(p.stat().st_size for p in directory.rglob('*') if p.is_file())
                if total > 3 * LIMIT:
                    failure = 'DISK_LIMIT'
                    break
                time.sleep(.2)
        finally:
            # Also stop descendants when the parent exits early.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
        if failure or not result_file.exists():
            return dict(mode=mode, video_id=video_id, ok=False, error=failure or 'PROBE_PROCESS_FAILED', exit_code=process.returncode)
        return json.loads(result_file.read_text())


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--mode', choices=['baseline', 'standard', 'pot', 'pot-always'], required=True)
    parser.add_argument('--video-id', required=True)
    parser.add_argument('--child', action='store_true')
    parser.add_argument('--directory')
    args = parser.parse_args()
    video_url(args.video_id)
    if args.child:
        report = child(args.mode, args.video_id, Path(args.directory))
        (Path(args.directory) / 'result.json').write_text(json.dumps(report))
    else:
        print(json.dumps(supervise(args.mode, args.video_id), sort_keys=True))

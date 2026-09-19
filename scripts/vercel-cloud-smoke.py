"""Verify the deployed cloud POC using local FLV output, without YouTube publishing."""
import argparse
from datetime import datetime, timezone
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import subprocess
import tempfile
import time
from urllib.parse import urljoin, urlsplit

import httpx

ROOT = Path(__file__).resolve().parent.parent
RESULT_PATH = ROOT / 'docs/vercel-cloud-smoke-result.json'


class SmokeError(Exception):
    pass


def require(condition, message):
    if not condition:
        raise SmokeError(message)


class ModuleScripts(HTMLParser):
    def __init__(self):
        super().__init__()
        self.sources = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'script' and attrs.get('type') == 'module' and attrs.get('src'):
            self.sources.append(attrs['src'])


def https_origin(value):
    parsed = urlsplit(value)
    require(parsed.scheme == 'https' and parsed.hostname and not parsed.username
            and not parsed.password and not parsed.query and not parsed.fragment
            and parsed.path in ('', '/'), 'Expected a plain HTTPS origin')
    return f'https://{parsed.netloc}'


def run(args, result):
    ui_origin = https_origin(args.ui_origin)
    if args.expected_api_origin:
        expected_origin = https_origin(args.expected_api_origin)
    else:
        runtime = json.loads((ROOT / 'data-public/vercel-runtime.json').read_text())
        expected_origin = https_origin(runtime['api_origin'])
    require(urlsplit(expected_origin).hostname.endswith('.vercel.run'), 'Expected a Vercel Sandbox API host')
    code = (ROOT / 'data-public/invite-code.txt').read_text().strip()
    sample_bytes = (ROOT / 'data/sample.mp4').read_bytes()
    require(bool(code) and bool(sample_bytes), 'Local invite code and sample are required')
    result.update(ui_origin=ui_origin, expected_api_origin=expected_origin)
    deadline = time.monotonic() + 360
    headers = {'Origin': ui_origin, 'X-Replay-Client': '1'}

    def request(client, method, url, timeout=25, **kwargs):
        remaining = deadline - time.monotonic()
        require(remaining > 0, 'Overall smoke test timeout')
        return client.request(method, url, timeout=min(timeout, remaining), **kwargs)

    def json_response(response, status, step):
        require(response.status_code == status, f'{step}: expected HTTP {status}, received {response.status_code}')
        try:
            return response.json()
        except ValueError:
            raise SmokeError(f'{step}: response was not JSON') from None

    def bootstrap(client):
        session = json_response(request(client, 'POST', f'{ui_origin}/api/session',
                                        timeout=120, json={'code': code}), 200, 'Cloud login')
        require(isinstance(session, dict) and isinstance(session.get('token'), str)
                and bool(session['token']), 'Cloud login token missing')
        api_base = session.get('api_base')
        require(api_base == f'{expected_origin}/api', 'Cloud login returned an unexpected API origin')
        for field in ('expires_at', 'sandbox_expires_at'):
            require(isinstance(session.get(field), (int, float)) and session[field] > time.time(),
                    f'Cloud login {field} invalid')
        return session

    with httpx.Client(headers=headers, follow_redirects=False) as client:
        result['step'] = 'deployed_page_and_assets'
        page = request(client, 'GET', ui_origin)
        require(page.status_code == 200, f'UI returned HTTP {page.status_code}')
        require('text/html' in page.headers.get('content-type', ''), 'UI did not return HTML')
        parser = ModuleScripts()
        parser.feed(page.text)
        require(bool(parser.sources), 'Deployed page has no module entry script')
        bundles = []
        for source in parser.sources:
            asset_url = urljoin(ui_origin + '/', source)
            require(urlsplit(asset_url).netloc == urlsplit(ui_origin).netloc
                    and urlsplit(asset_url).scheme == 'https', 'Unexpected external module entry')
            asset = request(client, 'GET', asset_url)
            require(asset.status_code == 200, f'Module entry returned HTTP {asset.status_code}')
            bundles.append(asset.text)
        bundle = '\n'.join(bundles)
        require('trycloudflare.com' not in bundle, 'Deployed bundle still references a temporary tunnel')
        require(all(marker in bundle for marker in ('sandbox_expires_at', 'api_base', 'replay-test-api-base')),
                'Deployed bundle is missing cloud session client markers')
        result.update(ui_verified=True, legacy_tunnel_absent=True, cloud_client_markers_present=True)

        result['step'] = 'bootstrap_authentication'
        wrong_code = 'smoke-invalid' if code != 'smoke-invalid' else 'smoke-invalid-alternative'
        rejected = request(client, 'POST', f'{ui_origin}/api/session', json={'code': wrong_code})
        require(rejected.status_code == 401, f'Invalid code returned HTTP {rejected.status_code}')
        result['unauthenticated_bootstrap_blocked'] = True
        owner = bootstrap(client)
        require(owner['sandbox_expires_at'] - time.time() > 240, 'Too little sandbox time remains for a complete smoke test')
        api_base = owner['api_base']
        owner_headers = {'Authorization': 'Bearer ' + owner['token']}
        result.update(api_origin=expected_origin, sandbox_expires_at=owner['sandbox_expires_at'])

        result['step'] = 'direct_api_cors_and_sample'
        unauthenticated = request(client, 'GET', f'{api_base}/media')
        require(unauthenticated.status_code == 401, 'Direct API accepted an unauthenticated request')
        preflight = request(client, 'OPTIONS', f'{api_base}/media', headers={
            'Access-Control-Request-Method': 'POST',
            'Access-Control-Request-Headers': 'content-type,x-replay-client,authorization',
        })
        require(preflight.status_code == 200 and preflight.headers.get('access-control-allow-origin') == ui_origin,
                'Direct API CORS origin check failed')
        allowed = {value.strip().lower() for value in preflight.headers.get('access-control-allow-headers', '').split(',')}
        require({'content-type', 'x-replay-client', 'authorization'} <= allowed, 'Direct API CORS headers missing')
        allowed_methods = {value.strip() for value in preflight.headers.get('access-control-allow-methods', '').split(',')}
        require('POST' in allowed_methods, 'Direct API CORS POST missing')
        media = json_response(request(client, 'GET', f'{api_base}/media', headers=owner_headers), 200, 'Shared media list')
        shared = next((item for item in media if item.get('id') == 'shared-sample'), None)
        require(shared is not None and 14.5 <= shared.get('duration', 0) <= 15.5, 'Expected the 15-second shared sample')
        preview = request(client, 'GET', f'{api_base}/media/shared-sample/preview', headers=owner_headers)
        require(preview.status_code == 200 and hashlib.sha256(preview.content).digest() == hashlib.sha256(sample_bytes).digest(),
                'Shared preview differs from the authorized local sample')
        require(preview.headers.get('access-control-allow-origin') == ui_origin, 'Preview CORS origin missing')
        result.update(direct_api_unauthenticated_blocked=True, cors_verified=True, shared_sample_verified=True)

        owned_jobs = []
        try:
            result['step'] = 'real_local_broadcast'
            job = json_response(request(client, 'POST', f'{api_base}/broadcasts', headers=owner_headers,
                                        json={'media_id': 'shared-sample', 'title': 'Vercel cloud smoke 15s', 'target': 'local'}),
                                201, 'Create local broadcast')
            owned_jobs.append(job['id'])
            states = []
            stream_deadline = time.monotonic() + 100
            while time.monotonic() < stream_deadline:
                job = json_response(request(client, 'GET', f"{api_base}/broadcasts/{job['id']}",
                                            timeout=min(15, stream_deadline - time.monotonic()),
                                            headers=owner_headers), 200, 'Broadcast progress')
                if not states or states[-1] != job['state']:
                    states.append(job['state'])
                if job['state'] in ('completed', 'failed', 'stopped'):
                    break
                time.sleep(1)
            require(job['state'] == 'completed', 'Local broadcast did not complete within 100 seconds')
            owned_jobs.remove(job['id'])
            output = request(client, 'GET', f"{api_base}/broadcasts/{job['id']}/output", headers=owner_headers, timeout=45)
            require(output.status_code == 200 and output.content.startswith(b'FLV'), 'Completed FLV output is missing or invalid')
            with tempfile.TemporaryDirectory(prefix='replay-cloud-smoke-') as directory:
                output_path = Path(directory) / 'result.flv'
                output_path.write_bytes(output.content)
                probe = subprocess.run(['ffprobe', '-v', 'error', '-protocol_whitelist', 'file,pipe', '-show_format',
                                        '-show_streams', '-of', 'json', str(output_path)],
                                       capture_output=True, timeout=20, check=False)
                require(probe.returncode == 0, 'Downloaded FLV failed ffprobe validation')
                info = json.loads(probe.stdout)
            codecs = {stream.get('codec_name') for stream in info.get('streams', [])}
            duration = float(info.get('format', {}).get('duration', 0))
            require({'h264', 'aac'} <= codecs and 14.5 <= duration <= 16, 'Downloaded FLV codecs or duration are incorrect')
            result.update(states=states, output_bytes=len(output.content), output_duration=duration, flv_probe_verified=True)

            result['step'] = 'isolation_and_schedule_cancellation'
            other = bootstrap(client)
            other_headers = {'Authorization': 'Bearer ' + other['token']}
            other_jobs = json_response(request(client, 'GET', f'{api_base}/broadcasts', headers=other_headers), 200, 'Other session jobs')
            require(other_jobs == [], 'New session can see another session broadcast list')
            for suffix in ('', '/events', '/output'):
                blocked = request(client, 'GET', f"{api_base}/broadcasts/{job['id']}{suffix}", headers=other_headers)
                require(blocked.status_code == 404, 'Cross-session broadcast access was not blocked')
            scheduled = min(time.time() + 30, owner['sandbox_expires_at'] - 185)
            require(scheduled > time.time() + 5, 'Insufficient sandbox lifetime for schedule verification')
            pending = json_response(request(client, 'POST', f'{api_base}/broadcasts', headers=owner_headers, json={
                'media_id': 'shared-sample', 'title': 'Vercel schedule cancellation', 'target': 'local',
                'scheduled_at': datetime.fromtimestamp(scheduled, timezone.utc).isoformat(),
            }), 201, 'Create scheduled broadcast')
            owned_jobs.append(pending['id'])
            blocked = request(client, 'POST', f"{api_base}/broadcasts/{pending['id']}/stop", headers=other_headers)
            require(blocked.status_code == 404, 'Other session can stop the owner broadcast')
            cancelled = json_response(request(client, 'POST', f"{api_base}/broadcasts/{pending['id']}/stop",
                                              headers=owner_headers), 200, 'Cancel scheduled broadcast')
            require(cancelled['state'] == 'stopped', 'Scheduled broadcast was not cancelled')
            owned_jobs.remove(pending['id'])
            result.update(cross_session_job_isolation=True, cross_session_stop_blocked=True, schedule_cancel_verified=True)
        finally:
            for job_id in owned_jobs:
                try:
                    cleanup = client.post(f'{api_base}/broadcasts/{job_id}/stop', headers=owner_headers, timeout=10)
                    if cleanup.status_code != 200:
                        result['cleanup_stop_failed'] = True
                except httpx.HTTPError:
                    result['cleanup_stop_failed'] = True
    result.update(result='passed', step='complete')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ui-origin', default='https://replay-live-poc.vercel.app')
    parser.add_argument('--expected-api-origin', help='Expected HTTPS sandbox origin; otherwise read provisioned runtime JSON')
    args = parser.parse_args()
    result = {'result': 'failed', 'youtube_broadcast': False,
              'checked_at': datetime.now(timezone.utc).isoformat(), 'step': 'local_configuration'}
    try:
        run(args, result)
    except SmokeError as error:
        result['error'] = str(error)
    except Exception as error:
        # Never serialize request headers, response bodies, tokens, or invite codes.
        result['error'] = f'{type(error).__name__} during {result["step"]}'
    RESULT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['result'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())

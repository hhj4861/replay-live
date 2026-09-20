"""Download on this PC, then upload directly with a single task capability."""
import hashlib
import time
from urllib.parse import urlsplit

import httpx

from .media_sources import SourceImportError, normalize_source
from .worker import output_chunks

DEFAULT_CLOUD_URL = 'https://replay-live-api.guswhd1085.workers.dev'


class DeviceImportError(SourceImportError):
    """Fixed device error codes; never expose URLs, grants or upstream bodies."""
    def __init__(self, code):
        allowed = {'DEVICE_IMPORT_REVOKED', 'DEVICE_IMPORT_INVALID',
                   'DEVICE_IMPORT_TASK_FAILED', 'DEVICE_IMPORT_UPLOAD_FAILED',
                   'DEVICE_IMPORT_COMPLETE_FAILED'}
        self.code = code if code in allowed else 'DEVICE_IMPORT_INVALID'
        ValueError.__init__(self, self.code)


def cloud_origin(value, development=False):
    parsed = urlsplit(value)
    if (parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path.strip('/') or not parsed.hostname
            or (parsed.scheme != 'https' and not (development and parsed.scheme == 'http'
                and parsed.hostname in ('127.0.0.1', 'localhost', 'testserver')))):
        raise ValueError('Use the fixed HTTPS cloud API origin (HTTP loopback only for development)')
    return value.rstrip('/')


def run_device_import(id, token, *, api, output, downloader, check_active, on_phase,
                      client=None, development=False):
    api = cloud_origin(api, development)
    owns_client = client is None
    client = client or httpx.Client(timeout=15, follow_redirects=False, trust_env=False)
    endpoint = api + '/api/device-imports/' + id
    headers = {'Authorization': 'Bearer ' + token, 'X-Replay-Client': '1'}
    deadline = time.monotonic() + 540

    def active():
        check_active()
        if time.monotonic() >= deadline:
            raise SourceImportError('SOURCE_TIMEOUT')

    def call(method, suffix, body=None):
        active()
        response = client.request(method, endpoint + suffix, headers=headers,
            **({'json': body} if body is not None else {}), timeout=60)
        if response.status_code in (401, 403, 404, 409):
            raise DeviceImportError('DEVICE_IMPORT_REVOKED')
        response.raise_for_status()
        return response.json()

    completed = False
    phase = 'requesting'
    try:
        on_phase(phase)
        task = call('GET', '/task')
        if task.get('state') == 'completed':
            completed = True
            return {'media_id': id}
        if (task.get('id') != id or not isinstance(task.get('max_bytes'), int)
                or not 0 < task['max_bytes'] <= 50 * 1024**2
                or not isinstance(task.get('max_duration'), int) or not 0 < task['max_duration'] <= 120):
            raise DeviceImportError('DEVICE_IMPORT_INVALID')
        source = normalize_source(task['source']['provider'], task['source']['url'])
        deadline = min(deadline, time.monotonic() + max(0, task['expires_at'] - time.time()))
        phase = 'downloading'
        on_phase(phase)
        result = downloader(source, output, max_bytes=task['max_bytes'], max_duration=task['max_duration'],
                            timeout=120, check_active=active)
        active()
        with output.open('rb') as handle:
            digest = hashlib.file_digest(handle, 'sha256').hexdigest()
        size = output.stat().st_size
        if not 0 < size <= task['max_bytes'] or size != result['bytes'] or digest != result['sha256']:
            raise SourceImportError('SOURCE_INCOMPLETE')
        phase = 'uploading'
        on_phase(phase)
        signed = call('POST', '/upload', {'bytes': size, 'sha256': digest})
        destination = urlsplit(signed['url'])
        host = destination.hostname or ''
        trusted = (destination.netloc == urlsplit(api).netloc
                   or (destination.netloc == 'vercel.com' and destination.path == '/api/blob/')
                   or host.endswith('.blob.vercel-storage.com')
                   or host == 'blob.vercel-storage.com' or host.endswith('.amazonaws.com'))
        if (not trusted or destination.username or destination.password or destination.fragment
                or signed.get('method') != 'PUT' or destination.scheme != urlsplit(api).scheme):
            raise DeviceImportError('DEVICE_IMPORT_INVALID')
        upload_headers = httpx.Headers(signed.get('headers', {}))
        if 'Transfer-Encoding' in upload_headers or upload_headers.get('Content-Length', str(size)) != str(size):
            raise DeviceImportError('DEVICE_IMPORT_INVALID')
        upload_headers['Content-Length'] = str(size)
        active()
        with output.open('rb') as handle:
            # The cloud task token is intentionally NOT forwarded to storage.
            response = client.put(signed['url'], headers=upload_headers,
                content=output_chunks(handle, size, task['max_bytes'], check_active=active), timeout=90)
            response.raise_for_status()
        phase = 'validating'
        on_phase(phase)
        # Completion is idempotent: a lost response must not restart the download.
        for attempt in range(3):
            try:
                result = call('POST', '/complete')
                break
            except (httpx.TransportError, httpx.HTTPStatusError):
                if attempt == 2:
                    raise
        if result.get('media', {}).get('id') != id:
            raise DeviceImportError('DEVICE_IMPORT_INVALID')
        completed = True
        return {'media_id': id}
    except Exception as error:
        code = error.code if isinstance(error, SourceImportError) else {
            'requesting': 'DEVICE_IMPORT_TASK_FAILED', 'downloading': 'SOURCE_UNAVAILABLE',
            'uploading': 'DEVICE_IMPORT_UPLOAD_FAILED', 'validating': 'DEVICE_IMPORT_COMPLETE_FAILED',
        }[phase]
        if not completed:
            try:
                client.post(endpoint + '/failure', headers=headers, json={'code': code}, timeout=10)
            except Exception:
                pass
        failure = DeviceImportError(code) if code.startswith('DEVICE_IMPORT_') else SourceImportError(code)
        raise failure from None
    finally:
        output.unlink(missing_ok=True)
        if owns_client:
            client.close()

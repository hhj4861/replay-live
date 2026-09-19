"""Offline preview mode boundaries; no browser, Google account or listeners."""
import importlib.util
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from server.auth import AuthConfig, JWTAuthenticator


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('commercial_preview', ROOT / 'scripts/commercial-preview.py')
preview = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(preview)
CLIENT_ID = '12345-previewfixture.apps.googleusercontent.com'


@pytest.fixture(autouse=True)
def empty_google_environment(monkeypatch):
    for name in preview.GOOGLE_ENV_KEYS:
        monkeypatch.delenv(name, raising=False)


def test_google_config_requires_client_id_and_explicit_access_policy(monkeypatch):
    with pytest.raises(preview.PreviewConfigurationError) as missing:
        preview.google_configuration()
    assert missing.value.code == 'PREVIEW_GOOGLE_CLIENT_ID_REQUIRED'
    monkeypatch.setenv('REPLAY_GOOGLE_CLIENT_ID', CLIENT_ID)
    with pytest.raises(preview.PreviewConfigurationError) as policy:
        preview.google_configuration()
    assert policy.value.code == 'PREVIEW_GOOGLE_ACCESS_POLICY_REQUIRED'
    monkeypatch.setenv('REPLAY_GOOGLE_ALLOWED_EMAILS', 'Fixture@Gmail.com')
    config = preview.google_configuration()
    assert config.client_id == CLIENT_ID and config.allowed_emails == ('fixture@gmail.com',)
    assert not config.allow_signups


def test_google_env_file_is_literal_restricted_and_not_retained(tmp_path, monkeypatch):
    monkeypatch.setenv('REPLAY_GOOGLE_CLIENT_ID', '12345-shell.apps.googleusercontent.com')
    before = dict(os.environ)
    file = tmp_path / '.env.google.local'
    file.write_text(f'# Public Google settings only\nREPLAY_GOOGLE_CLIENT_ID="{CLIENT_ID}"\n'
                    "REPLAY_GOOGLE_ALLOWED_EMAILS='fixture@gmail.com'\nREPLAY_GOOGLE_ALLOW_SIGNUPS=0\n")
    config = preview.google_configuration(file)
    assert config.client_id == CLIENT_ID
    assert config.allowed_emails == ('fixture@gmail.com',)
    assert dict(os.environ) == before


def test_google_admin_setting_is_server_only_literal_and_does_not_expand_admission(tmp_path, monkeypatch):
    monkeypatch.setenv('REPLAY_SITE_ADMIN_EMAILS', 'shell.admin@gmail.com')
    before = dict(os.environ)
    file = tmp_path / '.env.google.local'
    file.write_text(f'REPLAY_GOOGLE_CLIENT_ID={CLIENT_ID}\n'
                    'REPLAY_GOOGLE_ALLOWED_EMAILS=fixture@gmail.com\n'
                    'REPLAY_SITE_ADMIN_EMAILS=Fixture@Gmail.com\n')
    config = preview.google_configuration(file)
    assert config.site_admin_emails == ('fixture@gmail.com',)
    assert config.allowed_emails == ('fixture@gmail.com',)
    assert not config.allow_signups
    assert dict(os.environ) == before
    for google_config in (config, None):
        environment = preview.preview_build_environment('http://127.0.0.1:18090', Path('/example/node'), google_config)
        assert all(name not in environment for name in preview.GOOGLE_SERVER_ENV_KEYS)
        assert not any(email in str(environment) for email in ('shell.admin@gmail.com', 'fixture@gmail.com'))
    file.write_text(f'REPLAY_GOOGLE_CLIENT_ID={CLIENT_ID}\nREPLAY_SITE_ADMIN_EMAILS=fixture@gmail.com\n')
    with pytest.raises(preview.PreviewConfigurationError) as denied:
        preview.google_configuration(file)
    assert denied.value.code == 'PREVIEW_GOOGLE_ACCESS_POLICY_REQUIRED'
    assert dict(os.environ) == before


def test_invalid_google_admin_setting_is_rejected_without_retaining_or_echoing_it(tmp_path):
    before = dict(os.environ)
    file = tmp_path / '.env.google.local'
    file.write_text(f'REPLAY_GOOGLE_CLIENT_ID={CLIENT_ID}\nREPLAY_GOOGLE_ALLOWED_EMAILS=fixture@gmail.com\n'
                    'REPLAY_SITE_ADMIN_EMAILS=invalid-admin-canary\n')
    with pytest.raises(preview.PreviewConfigurationError) as invalid:
        preview.google_configuration(file)
    assert invalid.value.code == 'PREVIEW_GOOGLE_CONFIG_INVALID'
    assert 'canary' not in str(invalid.value) + invalid.value.detail
    assert dict(os.environ) == before


@pytest.mark.parametrize('contents', [
    'REPLAY_GOOGLE_CLIENT_SECRET=secret-canary',
    'REPLAY_LOGIN_PROVIDER=google',
    'REPLAY_GOOGLE_CLIENT_ID=x\nREPLAY_GOOGLE_CLIENT_ID=y',
    'REPLAY_GOOGLE_CLIENT_ID="unterminated-canary',
    'source /tmp/secret-canary',
    'REPLAY_GOOGLE_CLIENT_ID=$(touch /tmp/secret-canary)',
])
def test_bad_google_file_is_rejected_without_values_or_execution(tmp_path, contents):
    file = tmp_path / '.env.google.local'
    file.write_text(contents)
    with pytest.raises(preview.PreviewConfigurationError) as error:
        preview.google_configuration(file)
    assert 'canary' not in str(error.value)
    assert 'canary' not in error.value.detail


@pytest.mark.parametrize('value', ['true', '', 'secret-canary'])
def test_invalid_google_signup_setting_remains_fail_closed(monkeypatch, value):
    monkeypatch.setenv('REPLAY_GOOGLE_CLIENT_ID', CLIENT_ID)
    monkeypatch.setenv('REPLAY_GOOGLE_ALLOW_SIGNUPS', value)
    before = dict(os.environ)
    with pytest.raises(preview.PreviewConfigurationError) as error:
        preview.google_configuration()
    assert error.value.code == 'PREVIEW_GOOGLE_CONFIG_INVALID'
    assert dict(os.environ) == before


def test_google_build_preserves_production_auth_and_passes_only_public_id(tmp_path, monkeypatch):
    monkeypatch.setenv('REPLAY_GOOGLE_CLIENT_ID', CLIENT_ID)
    monkeypatch.setenv('REPLAY_GOOGLE_ALLOWED_EMAILS', 'fixture@gmail.com')
    monkeypatch.setenv('REPLAY_SITE_ADMIN_EMAILS', 'fixture@gmail.com')
    monkeypatch.setenv('REPLAY_GOOGLE_CLIENT_SECRET', 'secret-canary')
    config = preview.google_configuration()
    vite_file = preview.preview_build_files(tmp_path, 'http://127.0.0.1:18090', google=True)
    source = vite_file.read_text()
    assert 'transformIndexHtml' in source and '실제 Google 로그인' in source
    assert "'Referrer-Policy': 'no-referrer-when-downgrade'" in source
    assert "'Cross-Origin-Opener-Policy': 'same-origin-allow-popups'" in source
    assert 'load(id)' not in source and 'auth.ts' not in source
    assert not (tmp_path / 'preview-auth.ts').exists()
    environment = preview.preview_build_environment('http://127.0.0.1:18090', Path('/example/node'), config)
    assert environment['REPLAY_LOGIN_PROVIDER'] == 'google'
    assert environment['REPLAY_GOOGLE_CLIENT_ID'] == CLIENT_ID
    assert environment['REPLAY_API_URL'] == 'http://127.0.0.1:18090'
    assert not any(name.startswith('REPLAY_GOOGLE_') and name != 'REPLAY_GOOGLE_CLIENT_ID' for name in environment)
    assert 'REPLAY_SITE_ADMIN_EMAILS' not in environment


def test_default_preview_keeps_explicit_development_adapter_and_no_google_id(tmp_path, monkeypatch):
    monkeypatch.setenv('REPLAY_GOOGLE_CLIENT_ID', CLIENT_ID)
    vite_file = preview.preview_build_files(tmp_path, 'http://127.0.0.1:18090')
    assert 'PREVIEW_AUTH_OVERRIDE_MISSING' in vite_file.read_text()
    adapter = (tmp_path / 'preview-auth.ts').read_text()
    assert "AUTH_PROVIDER = 'development'" in adapter
    assert '/preview/session' in adapter
    assert preview.preview_build_environment('http://127.0.0.1:18090', Path('/example/node'))['REPLAY_GOOGLE_CLIENT_ID'] == ''


@pytest.mark.parametrize('google,platform_live', [(False, False), (True, False), (False, True)])
def test_preview_build_banner_links_only_file_modes_to_separate_platform_test(tmp_path, google, platform_live):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is required to evaluate the generated Vite configuration')
    config = preview.preview_build_files(tmp_path, 'http://127.0.0.1:18090', google=google, platform_live=platform_live)
    base = tmp_path / 'base.mjs'
    base.write_text('export default { plugins: [] };')
    config.write_text(config.read_text().replace(json.dumps(str(ROOT / 'web/vite.vercel.config.ts')), json.dumps(str(base))))
    script = ('const {default: config} = await import(' + json.dumps(config.as_uri()) + ');'
              'process.stdout.write(config.plugins[0].transformIndexHtml("<html><body><main>Fixture</main></body></html>"));')
    result = subprocess.run([node, '--input-type=module', '-e', script], capture_output=True, text=True,
                            check=True, timeout=10, env={})

    class Banner(HTMLParser):
        def __init__(self):
            super().__init__()
            self.links, self.text, self.notes = [], [], 0

        def handle_starttag(self, tag, attrs):
            attributes = dict(attrs)
            if tag == 'a':
                self.links.append(attributes.get('href'))
            if tag == 'aside' and attributes.get('role') == 'note':
                self.notes += 1

        def handle_data(self, data):
            self.text.append(data)

    banner = Banner()
    banner.feed(result.stdout)
    text = ' '.join(banner.text)
    assert banner.notes == 1 and '영상 보관함이 분리되어 있습니다.' in text
    assert ('실제 Google 로그인' if google else '개발용 로그인') in text
    if platform_live:
        assert banner.links == [] and '플랫폼 송출 테스트 열기' not in text
        assert '선택한 플랫폼에 실제 영상이 전송됩니다.' in text
        assert '외부 플랫폼에는 송출되지 않습니다.' not in text
    else:
        assert banner.links == ['http://127.0.0.1:13102/']
        assert '플랫폼 송출 테스트 열기' in text
        assert '파일 전용 미리보기' in text and '외부 플랫폼에는 송출되지 않습니다.' in text
        assert '선택한 플랫폼에 실제 영상이 전송됩니다.' not in text


def preview_client(*, google):
    app = FastAPI()
    auth = (SimpleNamespace(config=object()) if google else JWTAuthenticator(AuthConfig(mode='development',
        dev_token='initial-preview-token', dev_subject='local-preview-user', dev_tenant_id='local-preview')))
    web = 'http://localhost:13100' if google else 'http://127.0.0.1:13100'
    preview.install_preview_boundary(app, auth, api_port=18090, web=web, google=google)

    @app.post('/api/broadcasts')
    @app.post('/api/broadcast-batches')
    async def accepted():
        return {'accepted': True}

    return TestClient(app, base_url='http://127.0.0.1:18090', headers={'Origin': web, 'X-Replay-Client': '1'}), auth


@pytest.mark.parametrize('method', ['POST', 'OPTIONS', 'GET'])
def test_google_preview_never_issues_development_sessions(method):
    client, auth = preview_client(google=True)
    original = auth.config
    with client:
        response = client.request(method, '/preview/session')
    assert response.status_code == 404
    assert 'token' not in response.text and auth.config is original


def test_development_preview_rotates_sessions_and_requires_exact_origin():
    client, auth = preview_client(google=False)
    with client:
        first = client.post('/preview/session')
        second = client.post('/preview/session')
        foreign = client.post('/preview/session', headers={'Origin': 'http://localhost:13100'})
    assert first.status_code == second.status_code == 200
    assert first.json()['token'] != second.json()['token'] == auth.config.dev_token
    assert foreign.status_code == 403


@pytest.mark.parametrize('google', [False, True])
def test_both_preview_modes_reject_external_single_and_batch_broadcasts(google):
    client, _ = preview_client(google=google)
    with client:
        assert client.post('/api/broadcasts', json={'target': 'local'}).status_code == 200
        assert client.post('/api/broadcast-batches', json={'destinations': [{'target': 'local'}]}).status_code == 200
        assert client.post('/api/broadcasts', json={'target': 'youtube'}).status_code == 400
        assert client.post('/api/broadcast-batches', json={'destinations': [{'target': 'local'}, {'target': 'youtube'}]}).status_code == 400
        assert client.post('/api/broadcast-batches', json={'destinations': []}).status_code == 400


@pytest.mark.parametrize('arguments,code', [
    (['--google'], 'PREVIEW_GOOGLE_CLIENT_ID_REQUIRED'),
    (['--google-env-file', '.env.google.local'], 'PREVIEW_GOOGLE_MODE_REQUIRED'),
])
def test_missing_google_setup_fails_before_network_database_or_build(monkeypatch, capsys, arguments, code):
    monkeypatch.setattr(preview.sys, 'argv', ['commercial-preview.py', *arguments])
    monkeypatch.setattr(preview.signal, 'signal', lambda *_: None)

    def forbidden(*_, **__):
        raise AssertionError('No listener or build may start before configuration validation')

    monkeypatch.setattr(preview, 'require_free_ports', forbidden)
    monkeypatch.setattr(preview, 'Repository', forbidden)
    monkeypatch.setattr(preview.subprocess, 'run', forbidden)
    assert preview.main() == 1
    result = json.loads(capsys.readouterr().out)
    assert result['error'] == code and not result['ready']


def test_google_preview_bootstraps_real_auth_and_challenge_without_google_network(tmp_path, monkeypatch):
    from server.google_auth import GoogleAuthConfig, GoogleAuthenticator

    cfg = preview.Settings(mode='development', database_url='sqlite:///' + str(tmp_path / 'preview.db'),
        public_url='http://127.0.0.1:18090', origins=('http://localhost:13100',), control_token='c' * 32,
        callback_key='s' * 32, local_root=str(tmp_path), version='google-preview-test')
    repo = preview.Repository(cfg.database_url, create_schema=True)
    config = GoogleAuthConfig(CLIENT_ID, allowed_emails=('fixture@gmail.com',))
    auth = preview.preview_authenticator(repo, config)
    assert isinstance(auth, GoogleAuthenticator)

    async def forbidden(*_, **__):
        raise AssertionError('A login challenge does not contact Google')

    monkeypatch.setattr(auth._keys, '_key', forbidden)
    objects = preview.LocalStorage(tmp_path / 'objects', signing_key=cfg.callback_key,
        base_url=cfg.public_url, allow_development=True)
    keys = preview.LocalKeyringProvider({'preview': preview.Fernet.generate_key()}, 'preview', mode='development')
    app = preview.create_production_app(cfg, repository=repo, storage=objects, keys=keys, authenticator=auth)
    app.state.access_policy.migrate()
    app.state.operations.migrate()
    preview.install_preview_boundary(app, auth, api_port=18090, web=cfg.origins[0], google=True)
    with TestClient(app, base_url=cfg.public_url, headers={'Origin': cfg.origins[0], 'X-Replay-Client': '1'}) as client:
        response = client.post('/api/auth/google/challenge', json={})
        assert response.status_code == 200, response.text
        assert set(response.json()) == {'challenge_id', 'nonce', 'challenge_secret', 'expires_at'}
        assert client.post('/preview/session').status_code == 404
        assert client.get('/api/media').status_code == 401
    repo.close()


def test_shutdown_cancels_jobs_for_every_temporary_google_tenant(tmp_path):
    from sqlalchemy import select

    repo = preview.Repository('sqlite:///' + str(tmp_path / 'shutdown.db'), create_schema=True)
    for index, tenant_id in enumerate(('local-preview', 'google-preview-one', 'google-preview-two')):
        repo.add_media(tenant_id, id=f'media-{index}', name='fixture.mp4', object_key=f'{tenant_id}/fixture.mp4',
            bytes=100, sha256='a' * 64, status='ready', duration=15, width=640, height=360, fps=30)
        repo.create_job(tenant_id, media_id=f'media-{index}', title='Preview', target='local', idempotency_key='preview-fixture')
    preview.cancel_preview_jobs(repo)
    with repo.engine.connect() as connection:
        states = connection.execute(select(preview.jobs.c.state)).scalars().all()
    assert states == ['stopped'] * 3
    repo.close()

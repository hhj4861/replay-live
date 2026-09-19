"""Synthetic, offline checks; never inspect credentials or reach live services."""
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess

import boto3
import httpx
import pytest
import sqlalchemy

from server.auth import AuthConfig  # Import dependency modules before I/O guards.
from server.aws_identity import VercelAWSCredentials
from server.settings import Settings


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('commercial_preflight', ROOT / 'scripts/commercial-preflight.py')
preflight = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(preflight)


@pytest.fixture(autouse=True)
def no_external_actions(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('PREFLIGHT_EXTERNAL_ACTION_FORBIDDEN')
    for owner, names in (
        (socket, ('socket', 'create_connection', 'getaddrinfo')),
        (subprocess, ('run', 'Popen', 'check_output', 'check_call', 'call')),
        (os, ('system', 'popen')),
        (httpx, ('Client', 'AsyncClient', 'request', 'get', 'post')),
        (boto3, ('client', 'resource', 'Session')),
        (sqlalchemy, ('create_engine',)),
    ):
        for name in names:
            monkeypatch.setattr(owner, name, forbidden)


@pytest.fixture
def environment():
    return {
        'REPLAY_MODE': 'production', 'REPLAY_VERSION': 'release-8b92e0fa',
        'REPLAY_DATABASE_URL': 'postgresql+psycopg://synthetic-user:db-canary@database.synthetic.invalid/replay',
        'REPLAY_PUBLIC_URL': 'https://api.synthetic.invalid', 'REPLAY_ORIGINS': 'https://web.synthetic.invalid',
        'REPLAY_CONTROL_TOKEN': 'control-canary-' + 'a' * 40, 'REPLAY_CALLBACK_KEY': 'callback-canary-' + 'b' * 40,
        'REPLAY_BUCKET': 'synthetic-private-bucket',
        'REPLAY_KMS_KEY_ID': 'arn:aws:kms:ap-northeast-2:123456789012:key/abcd-1234',
        'REPLAY_REGION': 'ap-northeast-2', 'REPLAY_AWS_AUTH_MODE': 'vercel_oidc',
        'REPLAY_AWS_ROLE_ARN': 'arn:aws:iam::123456789012:role/synthetic-api',
        'REPLAY_AWS_OIDC_ISSUER': 'https://oidc.vercel.com/syntheticteam',
        'REPLAY_AWS_OIDC_AUDIENCE': 'https://vercel.com/syntheticteam',
        'REPLAY_AWS_OIDC_SUBJECT': 'owner:syntheticteam:project:synthetic-api:environment:preview',
        'REPLAY_AUTH_MODE': 'production', 'REPLAY_AUTH_ISSUER': 'https://identity.synthetic.invalid',
        'REPLAY_AUTH_JWKS_URL': 'https://identity.synthetic.invalid/jwks', 'REPLAY_AUTH_AUDIENCE': 'synthetic-api-audience',
        'REPLAY_AUTH_TENANT_CLAIM': 'tenant_id', 'REPLAY_AUTH_ROLES_CLAIM': 'roles',
        'REPLAY_COMMERCIAL': '1', 'REPLAY_API_URL': 'https://api.synthetic.invalid',
        'REPLAY_CONTROL_URL': 'https://api.synthetic.invalid', 'REPLAY_OIDC_AUTHORITY': 'https://identity.synthetic.invalid',
        'REPLAY_OIDC_CLIENT_ID': 'synthetic-public-client', 'REPLAY_OIDC_AUDIENCE': 'synthetic-api-audience',
        'REPLAY_OIDC_SCOPE': 'openid profile offline_access', 'CRON_SECRET': 'cron-canary-' + 'c' * 40,
        'REPLAY_WORKER_SNAPSHOT_ID': 'snap_synthetic1234', 'REPLAY_FFMPEG_PACKAGE_VERSION': '7:5.1.8-0+deb12u1',
        'REPLAY_ALERT_WEBHOOK_URL': 'https://alerts.synthetic.invalid/incoming',
        'REPLAY_ALERT_WEBHOOK_TOKEN': 'webhook-canary-' + 'd' * 40,
    }


@pytest.fixture
def repository(tmp_path, environment):
    api = {'framework': 'fastapi', 'functions': {'main.py': {
        'maxDuration': 60, 'excludeFiles': '{.env*,data/**,data-public/**,.git/**}'}}}
    web = {'framework': 'vite', 'buildCommand': 'REPLAY_COMMERCIAL=1 npm run build:vercel',
           'outputDirectory': 'dist-vercel', 'functions': {'api/dispatch.ts': {'maxDuration': 300}},
           'crons': [{'path': '/api/dispatch', 'schedule': '*/1 * * * *'}]}
    provider = environment['REPLAY_AWS_OIDC_ISSUER'].removeprefix('https://')
    trust = {'Version': '2012-10-17', 'Statement': [{'Effect': 'Allow',
        'Principal': {'Federated': 'arn:aws:iam::123456789012:oidc-provider/' + provider},
        'Action': 'sts:AssumeRoleWithWebIdentity', 'Condition': {'StringEquals': {
            provider + ':aud': environment['REPLAY_AWS_OIDC_AUDIENCE'],
            provider + ':sub': environment['REPLAY_AWS_OIDC_SUBJECT']}}}]}
    for name, document in [('vercel.json', api), ('web/vercel.json', web),
                           ('deploy/commercial/api.vercel.json', api), ('deploy/commercial/web.vercel.json', web),
                           ('deploy/commercial/aws-role-trust.json', trust)]:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(document))
    (tmp_path / 'requirements.lock').write_text('httpx==0.28.1\nboto3==1.43.90\n')
    return tmp_path


def by_code(result):
    return {item['code']: item for item in result['checks']}


def check(env, root, **kwargs):
    before = dict(os.environ)
    result = preflight.run_checks(env, repo_root=root, **kwargs)
    assert dict(os.environ) == before
    return result


def test_valid_inputs_still_do_not_claim_deployment_or_connect(environment, repository):
    result = check(environment, repository)
    assert result['ready'] and result['blocker_count'] == 0
    assert result['deployment_verified'] is False and result['ready_scope'] == 'selected_local_inputs'
    codes = by_code(result)
    for code in ('AWS_TRUST_AND_RESOURCES', 'DATABASE_AND_MIGRATIONS', 'BACKUP_RESTORE',
                 'OIDC_PROVIDER', 'WORKER_ARTIFACT_BINDING', 'ALERT_DELIVERY', 'CRON_PLAN_SUPPORT', 'DEPLOYMENT'):
        assert codes[code]['status'] == 'unchecked'
    encoded = json.dumps(result)
    for name, value in environment.items():
        if name.endswith(('TOKEN', 'KEY', 'SECRET', 'URL', 'ISSUER', 'AUDIENCE', 'SUBJECT', 'ARN', 'SNAPSHOT_ID', 'CLIENT_ID')):
            assert value not in encoded
    assert set(codes['REPLAY_CONTROL_TOKEN']) == {'code', 'status', 'detail'}


@pytest.mark.parametrize('scheme', ['postgres', 'postgresql', 'postgresql+psycopg'])
def test_provisioned_database_fallback_is_validated_offline_without_values(environment, repository, monkeypatch, scheme):
    environment.pop('REPLAY_DATABASE_URL')
    environment['DATABASE_URL'] = (scheme + '://synthetic-user:db-canary@database.synthetic.invalid/replay'
                                   '?sslmode=require&channel_binding=require')
    # The selected Mapping must remain independent from unrelated process input.
    monkeypatch.setenv('REPLAY_DATABASE_URL', 'sqlite:///:memory:')
    result = check(environment, repository)
    assert result['ready']
    codes = by_code(result)
    assert codes['DATABASE_URL']['status'] == 'pass'
    assert codes['API_RUNTIME_SETTINGS']['status'] == 'pass'
    assert 'REPLAY_DATABASE_URL' not in codes
    assert environment['DATABASE_URL'] not in json.dumps(result)


def test_preflight_explicit_database_precedence_matches_runtime(environment, repository):
    environment['DATABASE_URL'] = 'sqlite:///:memory:'
    assert check(environment, repository)['ready']
    environment['DATABASE_URL'] = environment['REPLAY_DATABASE_URL']
    environment['REPLAY_DATABASE_URL'] = ''
    result = check(environment, repository)
    assert not result['ready']
    assert by_code(result)['REPLAY_DATABASE_URL']['status'] == 'missing'
    assert by_code(result)['API_RUNTIME_SETTINGS']['status'] == 'invalid'


def test_blob_env_encryption_profile_requires_no_aws_and_keeps_values_private(environment, repository):
    import base64
    for name in list(environment):
        if name.startswith('REPLAY_AWS_') or name in ('REPLAY_BUCKET', 'REPLAY_KMS_KEY_ID', 'REPLAY_REGION', 'REPLAY_ALERT_WEBHOOK_URL'):
            environment.pop(name)
    environment.update(REPLAY_STORAGE_PROVIDER='vercel-blob', REPLAY_SECRET_PROVIDER='env-aesgcm',
        BLOB_STORE_ID='store_synthetic',
        REPLAY_BLOB_CONTROL_URL='https://web.synthetic.invalid/api/blob-control',
        REPLAY_MAX_UPLOAD_BYTES=str(50 * 1024**2), REPLAY_MAX_OUTPUT_BYTES=str(128 * 1024**2),
        REPLAY_SECRET_KEY_IDS='v1', REPLAY_SECRET_ACTIVE_KEY='v1',
        REPLAY_SECRET_KEY_V1=base64.b64encode(bytes(range(32))).decode())
    result = check(environment, repository)
    assert result['ready'], result
    assert by_code(result)['ENV_ENCRYPTION_KEYS']['status'] == 'pass'
    assert environment['REPLAY_SECRET_KEY_V1'] not in json.dumps(result)
    environment['REPLAY_SECRET_KEY_V1'] = 'invalid-secret-canary'
    assert by_code(check(environment, repository))['ENV_ENCRYPTION_KEYS']['status'] == 'invalid'


def test_queue_profile_requires_private_trigger_and_daily_recovery(environment, repository):
    environment.update(REPLAY_DISPATCH_MODE='queue', REPLAY_DISPATCH_WAKEUP_URL='https://web.synthetic.invalid/api/wake')
    target = repository / 'web/vercel.json'
    target.write_text((ROOT / 'deploy/commercial/web.free.vercel.json').read_text())
    assert check(environment, repository)['ready']
    document = json.loads(target.read_text())
    document['functions']['api/dispatch-queue.ts'].pop('experimentalTriggers')
    target.write_text(json.dumps(document))
    assert by_code(check(environment, repository))['WEB_CONFIG']['status'] == 'invalid'


def test_google_provider_does_not_require_oidc_and_does_not_claim_live_login(environment, repository):
    environment.update(REPLAY_LOGIN_PROVIDER='google', REPLAY_GOOGLE_CLIENT_ID='12345-synthetic.apps.googleusercontent.com',
                       REPLAY_GOOGLE_ALLOWED_EMAILS='synthetic@gmail.com')
    for name in list(environment):
        if name.startswith(('REPLAY_AUTH_', 'REPLAY_OIDC_')):
            environment.pop(name)
    result = check(environment, repository)
    assert result['ready'], result
    codes = by_code(result)
    assert codes['GOOGLE_ACCESS_POLICY']['status'] == 'pass'
    assert codes['GOOGLE_LIVE_LOGIN']['status'] == 'unchecked'
    assert 'OIDC_ISSUER_MATCH' not in codes
    assert environment['REPLAY_GOOGLE_ALLOWED_EMAILS'] not in json.dumps(result)
    environment['REPLAY_GOOGLE_ALLOWED_EMAILS'] = ''
    assert by_code(check(environment, repository))['GOOGLE_ACCESS_POLICY']['status'] == 'missing'
    environment['REPLAY_GOOGLE_ALLOWED_EMAILS'] = ' , '
    assert by_code(check(environment, repository))['GOOGLE_ACCESS_POLICY']['status'] == 'missing'


@pytest.mark.parametrize('changes,code', [
    ({'REPLAY_GOOGLE_CLIENT_ID': 'bad-client-canary'}, 'REPLAY_GOOGLE_CLIENT_ID'),
    ({'REPLAY_GOOGLE_CLIENT_SECRET': 'unused-secret-canary'}, 'GOOGLE_PUBLIC_CLIENT'),
    ({'REPLAY_GOOGLE_ALLOW_SIGNUPS': 'true'}, 'GOOGLE_ACCESS_POLICY'),
    ({'REPLAY_GOOGLE_ALLOWED_EMAILS': 'not-email-canary'}, 'GOOGLE_ACCESS_POLICY'),
])
def test_google_invalid_configuration_is_redacted(environment, repository, changes, code):
    environment.update(REPLAY_LOGIN_PROVIDER='google', REPLAY_GOOGLE_CLIENT_ID='12345-synthetic.apps.googleusercontent.com',
                       REPLAY_GOOGLE_ALLOWED_EMAILS='synthetic@gmail.com')
    environment.update(changes)
    result = check(environment, repository)
    assert not result['ready']
    assert by_code(result)[code]['status'] == 'invalid'
    for value in changes.values():
        if 'canary' in value:
            assert value not in json.dumps(result)


def test_unknown_login_provider_rejected_in_web_only_profile(environment, repository):
    environment['REPLAY_LOGIN_PROVIDER'] = 'unknown'
    assert by_code(check(environment, repository, profile='web'))['LOGIN_PROVIDER']['status'] == 'invalid'


def test_explicit_templates_report_selection_without_claiming_applied(environment, repository):
    (repository / 'vercel.json').unlink()
    (repository / 'web/vercel.json').write_text('{}')
    default = check(environment, repository)
    assert not default['ready']
    assert by_code(default)['API_CONFIG']['status'] == 'missing'
    assert by_code(default)['WEB_CONFIG']['status'] == 'invalid'
    explicit = check(environment, repository, api_config='deploy/commercial/api.vercel.json',
                     web_config='deploy/commercial/web.vercel.json')
    assert explicit['ready'] and not explicit['deployment_verified']
    assert explicit['targets']['api_config'] == {
        'path': str(repository / 'deploy/commercial/api.vercel.json'), 'selection': 'explicit_file'}


@pytest.mark.parametrize('name,value,code', [
    ('REPLAY_DATABASE_URL', 'sqlite:///db-canary', 'REPLAY_DATABASE_URL'),
    ('REPLAY_ORIGINS', 'https://web.synthetic.invalid,https://*.synthetic.invalid', 'REPLAY_ORIGINS'),
    ('REPLAY_ORIGINS', 'https://web.synthetic.invalid/', 'REPLAY_ORIGINS'),
    ('REPLAY_PUBLIC_URL', 'http://api.synthetic.invalid', 'REPLAY_PUBLIC_URL'),
    ('REPLAY_API_URL', 'https://other.synthetic.invalid', 'PUBLIC_API_MATCH'),
    ('REPLAY_OIDC_AUTHORITY', 'https://wrong.synthetic.invalid', 'OIDC_ISSUER_MATCH'),
    ('REPLAY_OIDC_AUDIENCE', 'wrong-audience', 'OIDC_AUDIENCE_MATCH'),
    ('REPLAY_AUTH_ROLES_CLAIM', 'tenant_id', 'API_CLAIM_NAMES'),
    ('REPLAY_AUTH_DEV_TOKEN', 'development-canary-' + 'a' * 40, 'API_AUTH_SETTINGS'),
    ('REPLAY_MODE', 'development', 'REPLAY_MODE'),
    ('REPLAY_AWS_AUTH_MODE', 'standard', 'REPLAY_AWS_AUTH_MODE'),
    ('REPLAY_AWS_OIDC_SUBJECT', 'owner:syntheticteam:project:*:environment:preview', 'AWS_IDENTITY_CONFIG'),
    ('REPLAY_AWS_OIDC_ISSUER', 'https://oidc.vercel.com/differentteam', 'AWS_IDENTITY_CONFIG'),
    ('REPLAY_CONTROL_TOKEN', 'short-canary', 'REPLAY_CONTROL_TOKEN'),
    ('REPLAY_VERSION', 'latest', 'REPLAY_VERSION'),
    ('REPLAY_VERSION', 'replace-with-immutable-commit', 'REPLAY_VERSION'),
    ('REPLAY_WORKER_SNAPSHOT_ID', 'replace-with-reviewed-snapshot-id', 'REPLAY_WORKER_SNAPSHOT_ID'),
    ('REPLAY_FFMPEG_PACKAGE_VERSION', 'latest', 'REPLAY_FFMPEG_PACKAGE_VERSION'),
    ('REPLAY_MAX_OUTPUT_BYTES', str(12 * 1024**3 + 1), 'API_RUNTIME_SETTINGS'),
    ('REPLAY_GLOBAL_CONCURRENCY', 'number-secret-canary', 'API_RUNTIME_SETTINGS'),
    ('REPLAY_CLOUD', '1', 'REPLAY_CLOUD'),
    ('REPLAY_OIDC_CLIENT_SECRET', 'client-secret-canary', 'OIDC_PUBLIC_CLIENT'),
])
def test_bad_inputs_block_with_fixed_codes_and_no_values(environment, repository, name, value, code):
    environment[name] = value
    result = check(environment, repository)
    assert not result['ready']
    assert by_code(result)[code]['status'] in ('invalid', 'missing')
    if 'canary' in value:
        assert value not in json.dumps(result)


@pytest.mark.parametrize('shared_name', ['REPLAY_CALLBACK_KEY', 'CRON_SECRET'])
def test_service_secrets_must_be_independent(environment, repository, shared_name):
    environment[shared_name] = environment['REPLAY_CONTROL_TOKEN']
    result = check(environment, repository)
    assert by_code(result)['INDEPENDENT_SERVICE_SECRETS']['status'] == 'invalid'


@pytest.mark.parametrize('mutation', ['wildcard', 'audience', 'principal', 'extra_allow', 'duplicate_key'])
def test_aws_trust_requires_exact_local_binding(environment, repository, mutation):
    path = repository / 'deploy/commercial/aws-role-trust.json'
    document = json.loads(path.read_text())
    statement = document['Statement'][0]
    provider = environment['REPLAY_AWS_OIDC_ISSUER'].removeprefix('https://')
    if mutation == 'wildcard':
        statement['Condition'] = {'StringLike': {provider + ':sub': '*'}}
    elif mutation == 'audience':
        statement['Condition']['StringEquals'][provider + ':aud'] = 'https://wrong.synthetic.invalid'
    elif mutation == 'principal':
        statement['Principal']['Federated'] = '*'
    elif mutation == 'extra_allow':
        document['Statement'].append({'Effect': 'Allow', 'Principal': '*', 'Action': '*'})
    path.write_text('{"Version":"2012-10-17","Version":"secret-canary"}' if mutation == 'duplicate_key' else json.dumps(document))
    result = check(environment, repository)
    assert by_code(result)['AWS_TRUST_CONFIG']['status'] == 'invalid'
    assert 'secret-canary' not in json.dumps(result)


@pytest.mark.parametrize('profile', ['api', 'web'])
def test_profile_scoping_without_cross_project_claim(environment, repository, profile):
    excluded = ('REPLAY_COMMERCIAL', 'REPLAY_WORKER_SNAPSHOT_ID', 'REPLAY_FFMPEG_PACKAGE_VERSION', 'CRON_SECRET') if profile == 'api' else (
        'REPLAY_DATABASE_URL', 'REPLAY_CALLBACK_KEY', 'REPLAY_AWS_ROLE_ARN', 'REPLAY_AUTH_ISSUER')
    for name in excluded:
        environment.pop(name)
    result = check(environment, repository, profile=profile)
    assert result['ready']
    assert by_code(result)['CROSS_PROJECT_CONFIGURATION']['status'] == 'unchecked'
    assert all(name not in by_code(result) for name in excluded)


def test_dotenv_is_literal_and_does_not_mutate_environment(tmp_path):
    path = tmp_path / 'synthetic.env'
    path.write_text('''# comment
export FIRST = "literal # content" # comment
SECOND='${FIRST} $(touch ignored) `ignored`'
THIRD = unquoted # comment
EMPTY=
ESCAPED="a\\tb"
''')
    before = dict(os.environ)
    parsed = preflight.parse_env_file(path)
    assert parsed == {'FIRST': 'literal # content', 'SECOND': '${FIRST} $(touch ignored) `ignored`',
                      'THIRD': 'unquoted', 'EMPTY': '', 'ESCAPED': 'a\tb'}
    assert before == dict(os.environ)


@pytest.mark.parametrize('content,detail', [
    ('SECRET=first\nSECRET=second-canary\n', 'DOTENV_DUPLICATE_KEY'),
    ('SECRET="unclosed-canary', 'DOTENV_SYNTAX_INVALID'),
    ('SECRET="value" unexpected-canary', 'DOTENV_SYNTAX_INVALID'),
    ('SECRET="bad\\q-canary"', 'DOTENV_ESCAPE_INVALID'),
    ('invalid-line-secret-canary', 'DOTENV_SYNTAX_INVALID'),
])
def test_dotenv_errors_never_echo_input(tmp_path, capsys, content, detail):
    path = tmp_path / 'synthetic.env'
    path.write_text(content)
    assert preflight.main(['--env-file', str(path)], environ={}) == 2
    output = capsys.readouterr()
    assert output.err == '' and 'canary' not in output.out
    assert json.loads(output.out)['checks'] == [{'code': 'PREFLIGHT_INPUT', 'status': 'invalid', 'detail': detail}]


def test_cli_file_is_authoritative_and_exit_codes_sanitized(environment, repository, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(preflight, 'REPO_ROOT', repository)
    # run_checks default is bound at definition, so inject only the synthetic root.
    real_run = preflight.run_checks
    monkeypatch.setattr(preflight, 'run_checks', lambda env, **kwargs: real_run(env, repo_root=repository, **kwargs))
    path = tmp_path / 'synthetic.env'
    path.write_text('\n'.join(f'{name}={value}' for name, value in environment.items()))
    assert preflight.main(['--env-file', str(path)], environ={'REPLAY_MODE': 'development'}) == 0
    valid = json.loads(capsys.readouterr().out)
    assert valid['ready'] and valid['environment_source'] == 'explicit_file'
    assert preflight.main([], environ={}) == 2
    assert not json.loads(capsys.readouterr().out)['ready']
    def fail(*args, **kwargs):
        raise RuntimeError('exception-private-key-canary')
    monkeypatch.setattr(preflight, 'run_checks', fail)
    assert preflight.main([], environ={}) == 1
    failure = capsys.readouterr()
    assert failure.err == '' and 'canary' not in failure.out
    assert json.loads(failure.out)['checks'][0]['detail'] == 'INTERNAL_FAILURE'


def test_oversized_and_missing_input_are_sanitized(tmp_path, capsys):
    path = tmp_path / 'synthetic.env'
    assert preflight.main(['--env-file', str(path)], environ={}) == 2
    assert json.loads(capsys.readouterr().out)['checks'][0]['detail'] == 'INPUT_NOT_FOUND'
    path.write_bytes(b'x' * (preflight.MAX_INPUT_BYTES + 1))
    assert preflight.main(['--env-file', str(path)], environ={}) == 2
    assert json.loads(capsys.readouterr().out)['checks'][0]['detail'] == 'INPUT_TOO_LARGE'


def test_wrong_arguments_never_echo_argument_values(capsys):
    assert preflight.main(['--profile', 'argument-secret-canary'], environ={}) == 2
    output = capsys.readouterr()
    assert output.err == '' and 'canary' not in output.out
    assert json.loads(output.out)['checks'][0]['detail'] == 'ARGUMENTS_INVALID'

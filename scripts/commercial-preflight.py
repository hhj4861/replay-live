#!/usr/bin/env python3
"""Offline commercial-input validation; never connects, deploys, or prints values.

Exit 0 means the selected local inputs pass, not that a deployment is ready.
Exit 2 reports missing/invalid inputs; exit 1 reports a sanitized internal error.
An explicit dotenv file replaces process input and is parsed without expansion.
"""
import argparse
from dataclasses import fields
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

MAX_INPUT_BYTES = 1024 * 1024
STATUSES = frozenset({'pass', 'missing', 'invalid', 'unchecked'})


class InputError(ValueError):
    """Only fixed error codes are retained, never input or exception text."""


def _read(path):
    try:
        with Path(path).open('rb') as handle:
            data = handle.read(MAX_INPUT_BYTES + 1)
        if len(data) > MAX_INPUT_BYTES:
            raise InputError('INPUT_TOO_LARGE')
        return data.decode('utf-8-sig')
    except FileNotFoundError:
        raise InputError('INPUT_NOT_FOUND') from None
    except (OSError, UnicodeError):
        raise InputError('INPUT_UNREADABLE') from None


def parse_env_file(path):
    """Parse a deliberately small dotenv grammar; no shell or interpolation."""
    result = {}
    for raw in _read(path).splitlines():
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        match = re.fullmatch(r'(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)', line)
        if not match:
            raise InputError('DOTENV_SYNTAX_INVALID')
        key, value = match.groups()
        if key in result:
            raise InputError('DOTENV_DUPLICATE_KEY')
        if value.startswith(('"', "'")):
            quote, decoded, index = value[0], [], 1
            while index < len(value):
                char = value[index]
                if char == quote:
                    trailing = value[index + 1:].strip()
                    if trailing and not trailing.startswith('#'):
                        raise InputError('DOTENV_SYNTAX_INVALID')
                    break
                if quote == '"' and char == '\\':
                    index += 1
                    if index >= len(value) or value[index] not in {'\\', '"', 'n', 'r', 't'}:
                        raise InputError('DOTENV_ESCAPE_INVALID')
                    char = {'n': '\n', 'r': '\r', 't': '\t'}.get(value[index], value[index])
                decoded.append(char)
                index += 1
            else:
                raise InputError('DOTENV_SYNTAX_INVALID')
            value = ''.join(decoded)
        else:
            value = re.split(r'\s+#', value, maxsplit=1)[0].rstrip()
        if '\x00' in value:
            raise InputError('DOTENV_SYNTAX_INVALID')
        result[key] = value
    return result


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise InputError('CONFIG_DUPLICATE_KEY')
        value[key] = item
    return value


def _json_file(path):
    try:
        value = json.loads(_read(path), object_pairs_hook=_unique_object)
    except (json.JSONDecodeError, RecursionError):
        raise InputError('CONFIG_JSON_INVALID') from None
    if not isinstance(value, dict):
        raise InputError('CONFIG_SHAPE_INVALID')
    return value


def _placeholder(value):
    lower = value.lower()
    return (lower.startswith(('replace-', 'replace_', 'changeme', 'your-'))
            or '${' in value or '$(' in value or '`' in value or '<' in value or '>' in value
            or re.search(r'(^|[./:@-])example\.(com|org|net)([/:]|$)', lower) is not None)


def _text(value):
    return bool(value) and len(value) <= 4096 and not _placeholder(value) and all(32 <= ord(c) < 127 for c in value)


def _https(value, *, origin=False):
    if not _text(value) or '*' in value or any(c.isspace() for c in value) or '\\' in value:
        return False
    try:
        parsed = urlsplit(value)
        return (parsed.scheme == 'https' and bool(parsed.hostname) and parsed.port != 0
                and not any((parsed.username, parsed.password, parsed.query, parsed.fragment))
                and (not origin or parsed.path in ('', '/')))
    except ValueError:
        return False


def _secret(value):
    return _text(value) and len(value) >= 32 and not any(c.isspace() for c in value)


def _database(value):
    from server.settings import normalize_database_url

    if not _text(value):
        return False
    try:
        parsed = urlsplit(normalize_database_url(value))
        return (parsed.scheme == 'postgresql+psycopg' and bool(parsed.hostname) and parsed.port != 0
                and bool(parsed.path.strip('/')) and not parsed.fragment)
    except ValueError:
        return False


def _claim(value):
    return _text(value) and not any(c.isspace() for c in value)


class Checks:
    def __init__(self, env):
        self.env = env
        self.items = []

    def add(self, code, status, detail):
        assert status in STATUSES
        self.items.append({'code': code, 'status': status, 'detail': detail})

    def require(self, name, predicate=_text):
        value = self.env.get(name, '')
        if not value:
            self.add(name, 'missing', 'REQUIRED_INPUT_MISSING')
            return False
        try:
            valid = isinstance(value, str) and predicate(value)
        except (ValueError, TypeError, OverflowError):
            valid = False
        self.add(name, 'pass' if valid else 'invalid', 'PRESENT_VALID' if valid else 'INPUT_INVALID')
        return bool(valid)

    def match(self, code, names):
        if not all(self.env.get(name) for name in names):
            self.add(code, 'unchecked', 'PREREQUISITES_MISSING')
        else:
            match = len({self.env[name] for name in names}) == 1
            self.add(code, 'pass' if match else 'invalid', 'INPUTS_MATCH' if match else 'INPUTS_MISMATCH')


def _manifest(checks, path, kind):
    try:
        document = _json_file(path)
        functions = document.get('functions', {})
        if not isinstance(functions, dict):
            raise InputError('CONFIG_SHAPE_INVALID')
        if kind == 'API':
            function = functions.get('main.py', {})
            valid = (document.get('framework') == 'fastapi' and isinstance(function, dict)
                     and type(function.get('maxDuration')) is int and function['maxDuration'] >= 60)
            excluded = function.get('excludeFiles', '') if isinstance(function, dict) else ''
            valid = valid and isinstance(excluded, str) and all(
                item in excluded.strip('{}').split(',') for item in ('.env*', 'data/**', 'data-public/**', '.git/**'))
        else:
            function = functions.get('api/dispatch.ts', {})
            if checks.env.get('REPLAY_DISPATCH_MODE', 'cron') == 'queue':
                consumer = functions.get('api/dispatch-queue.ts', {})
                trigger = consumer.get('experimentalTriggers', []) if isinstance(consumer, dict) else []
                scheduling = (consumer.get('maxDuration', 0) >= 300 and len(trigger) == 1
                    and isinstance(trigger[0], dict) and trigger[0].get('type') == 'queue/v2beta'
                    and trigger[0].get('topic') == 'replay-dispatch' and trigger[0].get('maxConcurrency') == 1
                    and trigger[0].get('maxDeliveries') == 5 and trigger[0].get('retryAfterSeconds') == 60
                    and any(isinstance(item, dict) and item.get('path') == '/api/wake'
                            and item.get('schedule') == '0 0 * * *' for item in document.get('crons', [])))
            else:
                scheduling = any(isinstance(item, dict) and item.get('path') == '/api/dispatch'
                                 and item.get('schedule') == '*/1 * * * *' for item in document.get('crons', []))
            valid = (document.get('framework') == 'vite'
                     and document.get('buildCommand') == 'REPLAY_COMMERCIAL=1 npm run build:vercel'
                     and document.get('outputDirectory') == 'dist-vercel'
                     and isinstance(function, dict) and type(function.get('maxDuration')) is int
                     and function['maxDuration'] >= 300 and scheduling)
        checks.add(kind + '_CONFIG', 'pass' if valid else 'invalid',
                   'SELECTED_CONFIG_MATCHES_REQUIREMENTS' if valid else 'COMMERCIAL_CONFIG_NOT_SELECTED')
    except InputError as error:
        checks.add(kind + '_CONFIG', 'missing' if error.args[0] == 'INPUT_NOT_FOUND' else 'invalid', error.args[0])
    except (TypeError, AttributeError):
        checks.add(kind + '_CONFIG', 'invalid', 'CONFIG_SHAPE_INVALID')


def _settings(checks):
    """Reuse production validation without from_env or resource construction."""
    from server.settings import Settings, database_url_from_env

    try:
        values = {}
        for field in fields(Settings):
            name = 'REPLAY_' + field.name.upper()
            if name not in checks.env:
                continue
            value = checks.env[name]
            if field.type is int:
                value = int(value)
            elif field.type is bool:
                if value not in ('0', '1'):
                    raise ValueError()
                value = value == '1'
            elif field.name == 'origins':
                value = tuple(item.strip() for item in value.split(',') if item.strip())
            values[field.name] = value
        values['database_url'] = database_url_from_env(checks.env)
        Settings(**values)
    except (ValueError, TypeError, OverflowError):
        checks.add('API_RUNTIME_SETTINGS', 'invalid', 'SETTINGS_REJECTED')
    else:
        checks.add('API_RUNTIME_SETTINGS', 'pass', 'SETTINGS_ACCEPTED_OFFLINE')


def _google(checks, *, access_policy=False):
    env = checks.env
    checks.require('REPLAY_GOOGLE_CLIENT_ID', lambda value: bool(re.fullmatch(r'[0-9]+-[A-Za-z0-9_-]+\.apps\.googleusercontent\.com', value)))
    checks.add('GOOGLE_PUBLIC_CLIENT', 'invalid' if env.get('REPLAY_GOOGLE_CLIENT_SECRET') else 'pass',
               'GOOGLE_CLIENT_SECRET_NOT_USED' if not env.get('REPLAY_GOOGLE_CLIENT_SECRET') else 'PUBLIC_CLIENT_SECRET_FORBIDDEN')
    if access_policy:
        from server.google_auth import GoogleAuthConfig
        try:
            if env.get('REPLAY_GOOGLE_ALLOW_SIGNUPS', '0') not in ('0', '1'):
                raise ValueError()
            config = GoogleAuthConfig(client_id=env.get('REPLAY_GOOGLE_CLIENT_ID', ''),
                allowed_emails=tuple(item.strip() for item in env.get('REPLAY_GOOGLE_ALLOWED_EMAILS', '').split(',') if item.strip()),
                allowed_subjects=tuple(item.strip() for item in env.get('REPLAY_GOOGLE_ALLOWED_SUBJECTS', '').split(',') if item.strip()),
                allow_signups=env.get('REPLAY_GOOGLE_ALLOW_SIGNUPS') == '1')
        except (ValueError, TypeError):
            checks.add('GOOGLE_ACCESS_POLICY', 'invalid', 'GOOGLE_SETTINGS_REJECTED')
        else:
            accepts_accounts = bool(config.allowed_emails or config.allowed_subjects or config.allow_signups)
            checks.add('GOOGLE_ACCESS_POLICY', 'pass' if accepts_accounts else 'missing',
                       'GOOGLE_ACCESS_POLICY_CONFIGURED' if accepts_accounts else 'GOOGLE_ACCESS_POLICY_DENIES_ALL')
    checks.add('GOOGLE_LIVE_LOGIN', 'unchecked', 'GOOGLE_CLIENT_ORIGINS_CONSENT_AND_LIVE_LOGIN_NOT_CHECKED')


def _api(checks, trust_path):
    from server.auth import AuthConfig
    from server.aws_identity import VercelAWSCredentials

    env = checks.env
    checks.add('LOGIN_PROVIDER', 'pass' if env.get('REPLAY_LOGIN_PROVIDER', 'oidc') in ('oidc', 'google') else 'invalid', 'LOGIN_PROVIDER_SELECTION')
    checks.require('REPLAY_MODE', lambda value: value == 'production')
    database_name = 'DATABASE_URL' if 'REPLAY_DATABASE_URL' not in env and 'DATABASE_URL' in env else 'REPLAY_DATABASE_URL'
    checks.require(database_name, _database)
    checks.require('REPLAY_PUBLIC_URL', lambda value: _https(value, origin=True))
    checks.require('REPLAY_ORIGINS', lambda value: bool(value.split(',')) and all(
        _https(item.strip(), origin=True) and urlsplit(item.strip()).path == '' for item in value.split(',')))
    checks.require('REPLAY_CONTROL_TOKEN', _secret)
    checks.require('REPLAY_CALLBACK_KEY', _secret)
    storage_provider = env.get('REPLAY_STORAGE_PROVIDER', 's3')
    secret_provider = env.get('REPLAY_SECRET_PROVIDER', 'aws-kms')
    checks.add('STORAGE_PROVIDER', 'pass' if storage_provider in ('s3', 'vercel-blob') else 'invalid', 'EXPLICIT_STORAGE_PROVIDER')
    checks.add('SECRET_PROVIDER', 'pass' if secret_provider in ('aws-kms', 'env-aesgcm') else 'invalid', 'EXPLICIT_SECRET_PROVIDER')
    if storage_provider == 's3':
        checks.require('REPLAY_BUCKET', lambda value: _text(value) and re.fullmatch(r'[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]', value))
    elif storage_provider == 'vercel-blob':
        checks.require('REPLAY_BLOB_CONTROL_URL', lambda value: _https(value) and urlsplit(value).path == '/api/blob-control')
        checks.add('BLOB_LIVE_STORAGE', 'unchecked', 'PRIVATE_ACCESS_SIGNED_UPLOAD_AND_HASH_NOT_CHECKED')
    if storage_provider == 's3' or secret_provider == 'aws-kms':
        checks.require('REPLAY_KMS_KEY_ID', lambda value: _text(value) and re.fullmatch(
            r'arn:aws(?:-us-gov|-cn)?:kms:[a-z0-9-]+:[0-9]{12}:key/[A-Za-z0-9-]+', value))
        checks.require('REPLAY_REGION', lambda value: re.fullmatch(r'[a-z]{2}(?:-gov)?-[a-z]+-[0-9]', value))
        checks.require('REPLAY_AWS_AUTH_MODE', lambda value: value == 'vercel_oidc')
        for name in ('REPLAY_AWS_ROLE_ARN', 'REPLAY_AWS_OIDC_ISSUER', 'REPLAY_AWS_OIDC_AUDIENCE', 'REPLAY_AWS_OIDC_SUBJECT'):
            checks.require(name, lambda value: _text(value) and not re.search(r'(^|[/:])(?:TEAM|ACCOUNT|KEY)(?:[/:]|$)', value))
        try:
            VercelAWSCredentials(env.get('REPLAY_AWS_ROLE_ARN', ''), env.get('REPLAY_REGION', ''),
                                 issuer=env.get('REPLAY_AWS_OIDC_ISSUER', ''), audience=env.get('REPLAY_AWS_OIDC_AUDIENCE', ''),
                                 subject=env.get('REPLAY_AWS_OIDC_SUBJECT', ''))
        except (ValueError, TypeError):
            checks.add('AWS_IDENTITY_CONFIG', 'invalid', 'IDENTITY_CONFIG_REJECTED')
        else:
            checks.add('AWS_IDENTITY_CONFIG', 'pass', 'IDENTITY_CONFIG_ACCEPTED_OFFLINE')
        _trust(checks, trust_path)
    if secret_provider == 'env-aesgcm':
        from server.secrets import EnvironmentAESGCMKeyProvider
        try:
            EnvironmentAESGCMKeyProvider.from_env(env)
        except (ValueError, TypeError):
            checks.add('ENV_ENCRYPTION_KEYS', 'invalid', 'KEY_CONFIGURATION_REJECTED')
        else:
            checks.add('ENV_ENCRYPTION_KEYS', 'pass', 'VERSIONED_KEYS_VALID_OFFLINE')
    if env.get('REPLAY_LOGIN_PROVIDER', 'oidc') == 'google':
        _google(checks, access_policy=True)
    else:
        checks.require('REPLAY_AUTH_MODE', lambda value: value == 'production')
        for name in ('REPLAY_AUTH_ISSUER', 'REPLAY_AUTH_JWKS_URL'):
            checks.require(name, _https)
        for name in ('REPLAY_AUTH_AUDIENCE', 'REPLAY_AUTH_TENANT_CLAIM', 'REPLAY_AUTH_ROLES_CLAIM'):
            checks.require(name, _claim)
        claim_names = [env.get('REPLAY_AUTH_TENANT_CLAIM'), env.get('REPLAY_AUTH_ROLES_CLAIM')]
        if env.get('REPLAY_AUTH_MEMBERSHIP_CLAIM'):
            claim_names.append(env['REPLAY_AUTH_MEMBERSHIP_CLAIM'])
        claims_distinct = len(set(claim_names)) == len(claim_names)
        checks.add('API_CLAIM_NAMES', 'pass' if claims_distinct else 'invalid',
                   'DISTINCT_CLAIM_NAMES' if claims_distinct else 'CLAIM_NAMES_COLLIDE')
        try:
            AuthConfig(mode=env.get('REPLAY_AUTH_MODE', 'production'), issuer=env.get('REPLAY_AUTH_ISSUER', ''),
                       audience=env.get('REPLAY_AUTH_AUDIENCE', ''), jwks_url=env.get('REPLAY_AUTH_JWKS_URL', ''),
                       tenant_claim=env.get('REPLAY_AUTH_TENANT_CLAIM', 'tenant_id'),
                       roles_claim=env.get('REPLAY_AUTH_ROLES_CLAIM', 'roles'),
                       membership_claim=env.get('REPLAY_AUTH_MEMBERSHIP_CLAIM') or None,
                       dev_token=env.get('REPLAY_AUTH_DEV_TOKEN'))
        except (ValueError, TypeError):
            checks.add('API_AUTH_SETTINGS', 'invalid', 'AUTH_SETTINGS_REJECTED')
        else:
            checks.add('API_AUTH_SETTINGS', 'pass', 'AUTH_SETTINGS_ACCEPTED_OFFLINE')
        if env.get('REPLAY_AUTH_MEMBERSHIP_CLAIM'):
            checks.require('REPLAY_AUTH_MEMBERSHIP_CLAIM', _claim)
    _settings(checks)
    checks.add('AWS_TRUST_AND_RESOURCES', 'unchecked', 'LIVE_STS_IAM_S3_KMS_NOT_CHECKED')
    checks.add('DATABASE_AND_MIGRATIONS', 'unchecked', 'LIVE_DATABASE_NOT_CHECKED')
    checks.add('BACKUP_RESTORE', 'unchecked', 'EXTERNAL_RESTORE_DRILL_NOT_CHECKED')
    checks.add('API_ORIGINS_DEPLOYMENT', 'unchecked', 'ACTUAL_BROWSER_ORIGIN_NOT_CHECKED')


def _trust(checks, path):
    env = checks.env
    try:
        document = _json_file(path)
        role = env.get('REPLAY_AWS_ROLE_ARN', '').split(':')
        issuer = env.get('REPLAY_AWS_OIDC_ISSUER', '')
        provider = issuer.removeprefix('https://')
        if len(role) != 6 or not provider or not issuer.startswith('https://'):
            raise InputError('IDENTITY_CONFIG_REJECTED')
        expected = {'Effect': 'Allow', 'Principal': {'Federated': f'arn:{role[1]}:iam::{role[4]}:oidc-provider/{provider}'},
                    'Action': 'sts:AssumeRoleWithWebIdentity', 'Condition': {'StringEquals': {
                        provider + ':aud': env.get('REPLAY_AWS_OIDC_AUDIENCE'),
                        provider + ':sub': env.get('REPLAY_AWS_OIDC_SUBJECT')}}}
        statements = document.get('Statement')
        if isinstance(statements, dict):
            statements = [statements]
        valid = (document.get('Version') == '2012-10-17' and isinstance(statements, list) and len(statements) == 1
                 and isinstance(statements[0], dict) and {key: value for key, value in statements[0].items() if key != 'Sid'} == expected)
        checks.add('AWS_TRUST_CONFIG', 'pass' if valid else 'invalid',
                   'SELECTED_TRUST_MATCHES_EXACTLY' if valid else 'SELECTED_TRUST_MISMATCH')
    except InputError as error:
        checks.add('AWS_TRUST_CONFIG', 'missing' if error.args[0] == 'INPUT_NOT_FOUND' else 'invalid', error.args[0])


def _web(checks, root):
    checks.add('LOGIN_PROVIDER', 'pass' if checks.env.get('REPLAY_LOGIN_PROVIDER', 'oidc') in ('oidc', 'google') else 'invalid', 'LOGIN_PROVIDER_SELECTION')
    env = checks.env
    checks.require('REPLAY_COMMERCIAL', lambda value: value == '1')
    checks.add('REPLAY_CLOUD', 'invalid' if env.get('REPLAY_CLOUD') == '1' else 'pass',
               'POC_MODE_ENABLED' if env.get('REPLAY_CLOUD') == '1' else 'POC_MODE_DISABLED')
    for name in ('REPLAY_API_URL', 'REPLAY_CONTROL_URL'):
        checks.require(name, lambda value: _https(value, origin=True) and urlsplit(value).path == '')
    checks.match('WEB_API_CONTROL_MATCH', ('REPLAY_API_URL', 'REPLAY_CONTROL_URL'))
    if env.get('REPLAY_LOGIN_PROVIDER', 'oidc') == 'google':
        _google(checks)
    else:
        checks.require('REPLAY_OIDC_AUTHORITY', _https)
        for name in ('REPLAY_OIDC_CLIENT_ID', 'REPLAY_OIDC_AUDIENCE'):
            checks.require(name, _claim)
        checks.require('REPLAY_OIDC_SCOPE', lambda value: _text(value) and 'openid' in value.split())
    checks.add('OIDC_PUBLIC_CLIENT', 'invalid' if env.get('REPLAY_OIDC_CLIENT_SECRET') else 'pass',
               'PUBLIC_CLIENT_SECRET_FORBIDDEN' if env.get('REPLAY_OIDC_CLIENT_SECRET') else 'PUBLIC_CLIENT_CONFIGURATION')
    checks.require('REPLAY_CONTROL_TOKEN', _secret)
    if env.get('REPLAY_DISPATCH_MODE', 'cron') == 'queue':
        checks.require('REPLAY_DISPATCH_WAKEUP_URL', lambda value: _https(value) and urlsplit(value).path == '/api/wake')
    if env.get('REPLAY_STORAGE_PROVIDER') == 'vercel-blob':
        checks.require('BLOB_STORE_ID', lambda value: bool(re.fullmatch(r'store_[A-Za-z0-9]+', value)))
    checks.require('CRON_SECRET', _secret)
    checks.require('REPLAY_WORKER_SNAPSHOT_ID', lambda value: _text(value) and re.fullmatch(r'snap_[A-Za-z0-9]+', value))
    checks.require('REPLAY_FFMPEG_PACKAGE_VERSION', lambda value: _text(value)
                   and re.fullmatch(r'[0-9][A-Za-z0-9.+:~_-]{0,119}', value))
    try:
        lines = [line.strip() for line in _read(root / 'requirements.lock').splitlines() if line.strip() and not line.lstrip().startswith('#')]
        valid = bool(lines) and all(re.fullmatch(r'[A-Za-z0-9_.-]+==[A-Za-z0-9.+!-]+', line) for line in lines)
        checks.add('WORKER_DEPENDENCIES_PINNED', 'pass' if valid else 'invalid',
                   'EXACT_PINS_PRESENT' if valid else 'EXACT_PINS_REQUIRED')
    except InputError as error:
        checks.add('WORKER_DEPENDENCIES_PINNED', 'missing' if error.args[0] == 'INPUT_NOT_FOUND' else 'invalid', error.args[0])
    if env.get('REPLAY_ALERT_WEBHOOK_URL'):
        checks.require('REPLAY_ALERT_WEBHOOK_URL', _https)
    else:
        checks.add('REPLAY_ALERT_WEBHOOK_URL', 'unchecked', 'OPTIONAL_ALERT_DESTINATION_NOT_SET')
    if env.get('REPLAY_ALERT_WEBHOOK_TOKEN'):
        checks.require('REPLAY_ALERT_WEBHOOK_TOKEN', _secret)
    else:
        checks.add('REPLAY_ALERT_WEBHOOK_TOKEN', 'unchecked', 'OPTIONAL_WEBHOOK_TOKEN_NOT_SET')
    checks.add('WORKER_ARTIFACT_BINDING', 'unchecked', 'LIVE_SNAPSHOT_VERSION_FFMPEG_NOT_CHECKED')
    checks.add('CRON_PLAN_SUPPORT', 'unchecked', 'LIVE_MINUTE_CRON_SUPPORT_NOT_CHECKED')
    checks.add('ALERT_DELIVERY', 'unchecked', 'EXTERNAL_WEBHOOK_DELIVERY_NOT_CHECKED')


def run_checks(env, *, profile='all', repo_root=REPO_ROOT, api_config=None, web_config=None, aws_trust_config=None):
    """Inspect copied inputs and local manifests only; no environment mutation."""
    if profile not in ('api', 'web', 'all'):
        raise InputError('PROFILE_INVALID')
    root = Path(repo_root).resolve()
    checks = Checks(dict(env))
    targets = {}
    def target(name, selected, default):
        path = Path(selected) if selected is not None else root / default
        if not path.is_absolute():
            path = root / path
        targets[name] = {'path': str(path.resolve()), 'selection': 'explicit_file' if selected is not None else 'repository_default'}
        return path
    if profile in ('api', 'all'):
        _manifest(checks, target('api_config', api_config, 'vercel.json'), 'API')
        _api(checks, target('aws_trust_config', aws_trust_config, 'deploy/commercial/aws-role-trust.json'))
    if profile in ('web', 'all'):
        _manifest(checks, target('web_config', web_config, 'web/vercel.json'), 'WEB')
        _web(checks, root)
    checks.require('REPLAY_VERSION', lambda value: _text(value) and value.lower() not in {'development', 'latest', 'main', 'master', 'head'}
                   and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,79}', value))
    secrets = ['REPLAY_CONTROL_TOKEN'] + (['REPLAY_CALLBACK_KEY'] if profile in ('api', 'all') else []) + (['CRON_SECRET'] if profile in ('web', 'all') else [])
    present = [checks.env[name] for name in secrets if checks.env.get(name)]
    checks.add('INDEPENDENT_SERVICE_SECRETS', 'invalid' if len(set(present)) != len(present) else 'pass',
               'SECRET_REUSE_FORBIDDEN' if len(set(present)) != len(present) else 'NO_REUSE_AMONG_PRESENT_INPUTS')
    if profile == 'all':
        checks.match('PUBLIC_API_MATCH', ('REPLAY_PUBLIC_URL', 'REPLAY_API_URL', 'REPLAY_CONTROL_URL'))
        if env.get('REPLAY_LOGIN_PROVIDER', 'oidc') != 'google':
            checks.match('OIDC_ISSUER_MATCH', ('REPLAY_AUTH_ISSUER', 'REPLAY_OIDC_AUTHORITY'))
            checks.match('OIDC_AUDIENCE_MATCH', ('REPLAY_AUTH_AUDIENCE', 'REPLAY_OIDC_AUDIENCE'))
    else:
        checks.add('CROSS_PROJECT_CONFIGURATION', 'unchecked', 'OTHER_PROFILE_NOT_SELECTED')
    checks.add('OIDC_PROVIDER', 'unchecked', 'LIVE_DISCOVERY_JWKS_PKCE_CLAIMS_REFRESH_NOT_CHECKED')
    checks.add('DEPLOYMENT', 'unchecked', 'LOCAL_INPUTS_ONLY_NOT_APPLIED_OR_DEPLOYED')
    blockers = sum(item['status'] in ('missing', 'invalid') for item in checks.items)
    return {'schema_version': 1, 'profile': profile, 'ready': blockers == 0, 'ready_scope': 'selected_local_inputs',
            'deployment_verified': False, 'blocker_count': blockers, 'targets': targets, 'checks': checks.items}


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise InputError('ARGUMENTS_INVALID')


def main(argv=None, *, environ=None):
    parser = _Parser(description=__doc__)
    parser.add_argument('--profile', choices=('api', 'web', 'all'), default='all')
    parser.add_argument('--env-file', help='Explicit dotenv input; does not merge with process environment')
    parser.add_argument('--api-config', help='Selected API manifest, relative to repository root or absolute')
    parser.add_argument('--web-config', help='Selected web manifest, relative to repository root or absolute')
    parser.add_argument('--aws-trust-config', help='Selected local AWS trust policy; never checks or changes IAM')
    try:
        args = parser.parse_args(argv)
        env = parse_env_file(args.env_file) if args.env_file else dict(os.environ if environ is None else environ)
        result = run_checks(env, profile=args.profile, api_config=args.api_config,
                            web_config=args.web_config, aws_trust_config=args.aws_trust_config)
        result['environment_source'] = 'explicit_file' if args.env_file else 'process_environment'
        exit_code = 0 if result['ready'] else 2
    except InputError as error:
        result = {'schema_version': 1, 'ready': False, 'ready_scope': 'selected_local_inputs', 'deployment_verified': False,
                  'checks': [{'code': 'PREFLIGHT_INPUT', 'status': 'invalid', 'detail': error.args[0]}]}
        exit_code = 2
    except Exception:
        result = {'schema_version': 1, 'ready': False, 'ready_scope': 'selected_local_inputs', 'deployment_verified': False,
                  'checks': [{'code': 'PREFLIGHT_INTERNAL', 'status': 'invalid', 'detail': 'INTERNAL_FAILURE'}]}
        exit_code = 1
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return exit_code


if __name__ == '__main__':
    raise SystemExit(main())

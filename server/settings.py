"""Explicit commercial configuration. Production never falls back to local data/auth."""
from dataclasses import dataclass, field
import os
from urllib.parse import urlsplit

from .output_policy import DEFAULT_MAX_OUTPUT_BYTES, MAX_OUTPUT_BYTES, S3_SINGLE_PUT_MAX_BYTES


def normalize_database_url(value: str) -> str:
    """Select Psycopg 3 without decoding credentials or rewriting SSL options."""
    if not isinstance(value, str):
        raise ValueError('Database URL must be a string')
    scheme, separator, remainder = value.partition('://')
    if separator and scheme in ('postgres', 'postgresql'):
        return 'postgresql+psycopg://' + remainder
    return value


def database_url_from_env(env) -> str:
    # Presence, not truthiness: an explicitly empty Replay setting must fail
    # validation instead of silently selecting a different provisioned database.
    value = env['REPLAY_DATABASE_URL'] if 'REPLAY_DATABASE_URL' in env else env.get('DATABASE_URL', '')
    return normalize_database_url(value)


@dataclass(frozen=True)
class Settings:
    mode: str = 'production'
    database_url: str = field(default='', repr=False)
    public_url: str = ''
    origins: tuple[str, ...] = ()
    control_token: str = field(default='', repr=False)
    callback_key: str = field(default='', repr=False)
    bucket: str = ''
    kms_key_id: str = ''
    storage_provider: str = 's3'
    secret_provider: str = 'aws-kms'
    blob_control_url: str = ''
    dispatch_mode: str = 'cron'
    dispatch_wakeup_url: str = ''
    region: str = 'ap-northeast-2'
    aws_auth_mode: str = 'standard'
    aws_role_arn: str = ''
    aws_oidc_issuer: str = ''
    aws_oidc_audience: str = ''
    aws_oidc_subject: str = ''
    local_root: str = 'data-commercial'
    version: str = 'development'
    max_upload_bytes: int = 500 * 1024 * 1024
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES
    max_duration: int = 4 * 3600
    max_storage_bytes: int = 10 * 1024**3
    tenant_concurrency: int = 4
    global_concurrency: int = 4
    max_pending_jobs: int = 50
    retention_days: int = 30
    validation_timeout: int = 600
    lease_seconds: int = 90
    reservation_grace: int = 120
    worker_max_seconds: int = 5 * 3600
    draining: bool = False

    def __post_init__(self):
        object.__setattr__(self, 'database_url', normalize_database_url(self.database_url))
        if self.mode not in ('production', 'development'):
            raise ValueError('REPLAY_MODE must be production or development')
        if self.aws_auth_mode not in ('standard', 'vercel_oidc'):
            raise ValueError('REPLAY_AWS_AUTH_MODE must be standard or vercel_oidc')
        if self.storage_provider not in ('s3', 'vercel-blob') or self.secret_provider not in ('aws-kms', 'env-aesgcm'):
            raise ValueError('Unsupported storage or secret provider')
        if self.dispatch_mode not in ('cron', 'queue'):
            raise ValueError('Unsupported dispatcher mode')
        if self.dispatch_mode == 'queue':
            wake = urlsplit(self.dispatch_wakeup_url)
            if (wake.scheme != 'https' or not wake.hostname or wake.username or wake.password
                    or wake.port not in (None, 443) or wake.query or wake.fragment or wake.path != '/api/wake'):
                raise ValueError('Queue dispatch requires an HTTPS wake endpoint')
        if len(self.control_token) < 32 or len(self.callback_key) < 32 or self.control_token == self.callback_key:
            raise ValueError('Independent control and callback secrets of at least 32 characters are required')
        parsed = urlsplit(self.public_url)
        if not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path.strip('/'):
            raise ValueError('REPLAY_PUBLIC_URL must be an origin')
        if not self.origins or any(urlsplit(o).path.strip('/') or '*' in o for o in self.origins):
            raise ValueError('Exact web origins are required')
        if self.mode == 'production':
            if not self.database_url.startswith('postgresql+psycopg://'):
                raise ValueError('Production requires PostgreSQL')
            if self.storage_provider == 's3' and (not self.bucket or not self.kms_key_id):
                raise ValueError('S3 production storage requires a private bucket and external KMS')
            if self.secret_provider == 'aws-kms' and not self.kms_key_id:
                raise ValueError('AWS secret encryption requires external KMS')
            if self.storage_provider == 'vercel-blob':
                bridge = urlsplit(self.blob_control_url)
                if (bridge.scheme != 'https' or not bridge.hostname or bridge.username or bridge.password
                        or bridge.port not in (None, 443) or bridge.query or bridge.fragment
                        or bridge.path != '/api/blob-control'):
                    raise ValueError('Vercel Blob requires an HTTPS control endpoint')
                if self.max_upload_bytes > 50 * 1024**2 or self.max_output_bytes > 128 * 1024**2:
                    raise ValueError('Vercel Blob upload/output limits exceed the supported adapter limits')
            if parsed.scheme != 'https' or any(urlsplit(o).scheme != 'https' for o in self.origins):
                raise ValueError('Production requires HTTPS')
            if self.version == 'development':
                raise ValueError('Production requires an immutable release version')
            if self.max_output_bytes > S3_SINGLE_PUT_MAX_BYTES:
                raise ValueError('Production output must fit the supported S3 single PUT limit')
        elif parsed.scheme not in ('http', 'https'):
            raise ValueError('Invalid public URL')
        for name in ('max_upload_bytes', 'max_output_bytes', 'max_duration', 'max_storage_bytes', 'tenant_concurrency',
                     'global_concurrency', 'max_pending_jobs', 'retention_days', 'validation_timeout', 'lease_seconds', 'reservation_grace'):
            if getattr(self, name) <= 0:
                raise ValueError(f'{name} must be positive')
        if isinstance(self.max_output_bytes, bool) or not isinstance(self.max_output_bytes, int) or self.max_output_bytes > MAX_OUTPUT_BYTES:
            raise ValueError('REPLAY_MAX_OUTPUT_BYTES must be an integer no larger than the worker file limit')
        if self.tenant_concurrency > self.global_concurrency or self.max_duration + 120 > self.worker_max_seconds:
            raise ValueError('Concurrency/runtime limits are inconsistent')

    @classmethod
    def from_env(cls):
        string_fields = ('mode', 'database_url', 'public_url', 'control_token', 'callback_key', 'bucket',
                         'kms_key_id', 'region', 'local_root', 'version', 'aws_auth_mode', 'aws_role_arn',
                         'aws_oidc_issuer', 'aws_oidc_audience', 'aws_oidc_subject', 'storage_provider',
                         'secret_provider', 'blob_control_url', 'dispatch_mode', 'dispatch_wakeup_url')
        values = {key: os.environ['REPLAY_' + key.upper()] for key in string_fields if 'REPLAY_' + key.upper() in os.environ}
        values['database_url'] = database_url_from_env(os.environ)
        for key in ('max_upload_bytes', 'max_output_bytes', 'max_duration', 'max_storage_bytes', 'tenant_concurrency', 'global_concurrency',
                    'max_pending_jobs', 'retention_days', 'validation_timeout', 'lease_seconds', 'worker_max_seconds', 'reservation_grace'):
            if 'REPLAY_' + key.upper() in os.environ:
                values[key] = int(os.environ['REPLAY_' + key.upper()])
        values['origins'] = tuple(o.strip() for o in os.getenv('REPLAY_ORIGINS', '').split(',') if o.strip())
        values['draining'] = os.getenv('REPLAY_DRAINING') == '1'
        return cls(**values)

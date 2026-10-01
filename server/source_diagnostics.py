"""Bounded failure evidence; never carry exception messages, URLs or credentials."""
from typing import Literal, get_args

from pydantic import BaseModel, Field


FailureReason = Literal['unclassified', 'network_timeout', 'tls_error', 'connection_reset',
                        'connection_error', 'http_error', 'proxy_rejected',
                        'extractor_error', 'unexpected_error']
FAILURE_REASONS = frozenset(get_args(FailureReason))


ProxyError = Literal['PORT_BLOCKED', 'SITE_PERMANENTLY_BLOCKED', 'HOST_BLOCKED',
                     'NO_USER', 'TRAFFIC_EXHAUSTED', 'THREADS_EXHAUSTED',
                     'USER_RATE_LIMIT_EXCEEDED', 'PORT_NOT_ALLOWED', 'USER_BLOCKED',
                     'INTERNAL_SERVER_ERROR', 'NO_HOST_CONNECTION', 'NO_RAY']
PROXY_ERRORS = frozenset(get_args(ProxyError))


class SourceFailure(BaseModel):
    model_config = {'extra': 'forbid', 'strict': True}
    stage: Literal['metadata', 'download', 'prepare_mp4', 'checksum']
    reason: FailureReason
    elapsed_ms: int = Field(ge=0, le=14_400_000)
    retries: int = Field(ge=0, le=2)
    http_status: int | None = Field(default=None, ge=100, le=599)
    request_retries: int | None = Field(default=None, ge=0, le=8)
    proxy_error: ProxyError | None = None

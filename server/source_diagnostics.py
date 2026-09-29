"""Bounded failure evidence; never carry exception messages, URLs or credentials."""
from typing import Literal, get_args

from pydantic import BaseModel, Field


FailureReason = Literal['unclassified', 'network_timeout', 'tls_error', 'connection_reset',
                        'connection_error', 'http_error', 'proxy_rejected',
                        'extractor_error', 'unexpected_error']
FAILURE_REASONS = frozenset(get_args(FailureReason))


class SourceFailure(BaseModel):
    model_config = {'extra': 'forbid', 'strict': True}
    stage: Literal['metadata', 'download', 'prepare_mp4', 'checksum']
    reason: FailureReason
    elapsed_ms: int = Field(ge=0, le=14_400_000)
    retries: int = Field(ge=0, le=1)
    http_status: int | None = Field(default=None, ge=100, le=599)

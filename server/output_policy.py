"""Shared local-output admission budget for the fixed encoder profile.

The estimate reserves a conservative allowance; it is also an enforced file
ceiling. Actual retained bytes are settled by the repository on completion.
"""
import math


VIDEO_BITRATE_BPS = 6_000_000
AUDIO_BITRATE_BPS = 128_000
VIDEO_BUFFER_BITS = 12_000_000
OUTPUT_OVERHEAD_PERCENT = 5
FIXED_ALLOWANCE_BYTES = 2 * 1024**2
S3_SINGLE_PUT_MAX_BYTES = 5 * 1024**3
DEFAULT_MAX_OUTPUT_BYTES = S3_SINGLE_PUT_MAX_BYTES
MAX_OUTPUT_BYTES = 32 * 1024**3


def estimate_output_bytes(duration: float) -> int:
    if isinstance(duration, bool) or not isinstance(duration, (int, float)) or not math.isfinite(duration) or not 0 < duration <= 4 * 3600:
        raise ValueError('Output duration must be positive and no more than four hours')
    encoded = duration * (VIDEO_BITRATE_BPS + AUDIO_BITRATE_BPS) / 8
    return math.ceil(encoded * (100 + OUTPUT_OVERHEAD_PERCENT) / 100) + FIXED_ALLOWANCE_BYTES

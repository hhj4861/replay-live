import os
from pathlib import Path
import subprocess
import sys

import pytest
from server.local_media import output_chunks
from server.media_runtime import MediaError, _execute, validate_media


def test_desktop_transfer_rejects_size_changes(tmp_path):
    path = tmp_path / 'video'; path.write_bytes(b'abcd')
    with path.open('rb') as stream:
        with pytest.raises(MediaError): list(output_chunks(stream, 3, 5))
    with path.open('rb') as stream:
        assert b''.join(output_chunks(stream, 4, 4)) == b'abcd'


def test_real_media_tools_enforce_cancel_timeout_and_decode(tmp_path):
    path = tmp_path / 'input.mp4'
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=s=160x90:r=10', '-f', 'lavfi', '-i', 'sine=frequency=400', '-t', '1', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', str(path)], check=True)
    probe = _execute(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'json', str(path)], timeout=5)
    assert probe[0] == 0 and probe[3] is None, probe
    assert validate_media(path)['duration'] >= 1
    cancelled = _execute(['ffmpeg', '-version'], timeout=2, should_stop=lambda: True)
    assert cancelled[-1] is True
    result = _execute(['ffmpeg', '-v', 'error', '-re', '-f', 'lavfi', '-i', 'testsrc2=s=160x90:r=10', '-f', 'null', '-'], timeout=.2)
    assert result[3] == 'STREAM_TIMEOUT'

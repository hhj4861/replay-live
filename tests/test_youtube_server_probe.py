"""Offline guard checks. These do not prove a successful YouTube download."""
import importlib.util
from pathlib import Path
import unittest
import shutil
import subprocess
import tempfile
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('probe', Path(__file__).resolve().parents[1] / 'scripts/youtube-server-probe.py')
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class ProbeGuards(unittest.TestCase):
    def test_proxy_requires_explicit_expected_provider_without_leaking_value(self):
        for value in ['', 'http://u:SECRET@localhost:823', 'http://u:SECRET@gw.dataimpulse.com:823/?token=x',
                      'http://u:SECRET@gw.dataimpulse.com:bad']:
            with patch.dict(probe.os.environ, {'REPLAY_PROBE_PROXY_URL': value}):
                with self.assertRaisesRegex(ValueError, '^PROXY_CONFIGURATION_REQUIRED$'):
                    probe.configured_proxy()
        value = 'http://test-user:test-password@gw.dataimpulse.com:823'
        with patch.dict(probe.os.environ, {'REPLAY_PROBE_PROXY_URL': value}):
            self.assertEqual(probe.configured_proxy(), value)

    def test_only_canonical_video_id(self):
        self.assertEqual(probe.video_url('GcOe4ILS6Ow'), 'https://www.youtube.com/watch?v=GcOe4ILS6Ow')
        for value in ['https://localhost/', '../secret', 'GcOe4ILS6Ow\n', '-o /tmp/test']:
            with self.assertRaises(ValueError):
                probe.video_url(value)

    def test_restricted_and_unbounded_metadata_rejected(self):
        for duration in [None, float('inf'), float('nan'), True, -1, 121]:
            self.assertIsNotNone(probe.reject_metadata({'duration': duration}))
        self.assertIsNotNone(probe.reject_metadata({'duration': 17, 'is_live': True}))
        self.assertIsNotNone(probe.reject_metadata({'duration': 17, 'availability': 'private'}))
        self.assertIsNone(probe.reject_metadata({'duration': 17, 'availability': 'public'}))

    def test_logs_do_not_retain_secret_or_source(self):
        logger = probe.SafeLogger()
        logger.error("Sign in to confirm you're not a bot https://example.com/?token=SECRET")
        self.assertIn('BOT_CHECK_REQUIRED', logger.categories)
        self.assertNotIn('SECRET', repr(vars(logger)))
        self.assertEqual(probe.category('arbitrary confidential error'), 'DOWNLOAD_FAILED')
        logger.debug("[debug] options {'socket_timeout': 10}")
        self.assertNotIn('TIMEOUT', logger.categories)

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg is required')
    def test_normalization_rejects_truncation_and_decodes_complete_media(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'fixture.mp4'
            output = Path(directory) / 'normalized.mp4'
            subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=160x90:rate=30',
                            '-f', 'lavfi', '-i', 'sine=frequency=440', '-t', '2', '-c:v', 'libx264',
                            '-c:a', 'aac', str(source)], check=True, capture_output=True, timeout=15)
            with self.assertRaisesRegex(ValueError, 'INCOMPLETE_DOWNLOAD'):
                probe.normalize(source, output, 17)
            result = probe.normalize(source, output, 2)
            self.assertTrue(result['full_decode'])
            self.assertEqual(result['normalized']['video_codec'], 'h264')
            self.assertEqual(result['normalized']['audio_codec'], 'aac')


if __name__ == '__main__':
    unittest.main()

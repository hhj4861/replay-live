#!/usr/bin/env python3
"""Local media/lock verification or an explicitly isolated database release drill."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sqlalchemy import text
from deploy.commercial.ops import check_schema, database_url, file_hash, private_json, summary
from server.media_runtime import run_stream, stream_command, validate_media
from server.repository import NotFound, Repository
from server.service import Service


def database_check(args):
    repo = Repository(database_url(args.database_env, development=args.development))
    try:
        with repo.engine.connect() as connection:
            check_schema(connection)
            before = summary(connection)
        if args.seed_fixture:
            if any(before['table_counts'][name] for name in ('replay_media', 'replay_jobs', 'replay_workers')):
                raise ValueError('Fixture requires a freshly migrated, otherwise empty database')
            items = []
            for tenant in ('release-fixture-a', 'release-fixture-b'):
                items.append(repo.add_media(tenant, id=tenant, name='synthetic.mp4', object_key=f'replay/{tenant}/media/fixture.mp4',
                    bytes=100, duration=1, width=320, height=180, fps=30, sha256=hashlib.sha256(tenant.encode()).hexdigest()))
            def create(_):
                return repo.create_job('release-fixture-a', media_id=items[0]['id'], title='Synthetic fixture', target='youtube',
                    idempotency_key='fixture-a', secret_ciphertext='synthetic-ciphertext',
                    request_fingerprint=hashlib.sha256(b'fixture-a').hexdigest())
            with ThreadPoolExecutor(max_workers=8) as pool:
                admitted = list(pool.map(create, range(8)))
            if len({job['id'] for job in admitted}) != 1:
                raise RuntimeError('Concurrent PostgreSQL idempotency failed')
            try:
                repo.get_job('release-fixture-b', admitted[0]['id'])
            except NotFound:
                pass
            else:
                raise RuntimeError('Tenant ownership boundary failed')
            repo.create_job('release-fixture-b', media_id=items[1]['id'], title='Preserved queue', target='youtube',
                idempotency_key='fixture-b', secret_ciphertext='synthetic-queued-ciphertext', scheduled=time.time() + 3600)
            with ThreadPoolExecutor(max_workers=2) as pool:
                claims = list(pool.map(lambda name: repo.claim(name), ('fixture-worker-a', 'fixture-worker-b')))
            if sum(claim is not None for claim in claims) != 1:
                raise RuntimeError('Concurrent PostgreSQL lease ownership failed')
        with repo.engine.connect() as connection:
            result = summary(connection)
            if args.assert_restored_fixture:
                if result['table_counts']['replay_media'] != 2 or result['table_counts']['replay_jobs'] != 2 or result['job_states'] != {'failed': 1, 'scheduled': 1}:
                    raise RuntimeError('Restored fixture state/count mismatch')
                failed = connection.execute(text("SELECT secret_ciphertext,lease_token,error_code FROM replay_jobs WHERE state='failed'")).one()
                queued = connection.execute(text("SELECT secret_ciphertext FROM replay_jobs WHERE state='scheduled'")).scalar_one()
                if failed[0] is not None or failed[1] is not None or failed[2] != 'RESTORE_INTERRUPTED' or queued != 'synthetic-queued-ciphertext':
                    raise RuntimeError('Restored active/queued credential policy mismatch')
        return {'database_verified': True, 'seeded_fixture': args.seed_fixture,
                'restored_fixture_verified': args.assert_restored_fixture, 'summary': result, 'live_stream_started': False}
    finally:
        repo.close()


def media_check(args):
    if not 1 <= args.soak_seconds <= 14400 or not 0 <= args.sqlite_lock_seconds <= 120:
        raise ValueError('Media duration or SQLite lock duration is outside the supported range')
    if args.soak_seconds > 60 and not args.allow_long_run:
        raise ValueError('Runs longer than 60 seconds require --allow-long-run')
    key = ''
    if args.live_youtube:
        key = os.getenv('REPLAY_YOUTUBE_STREAM_KEY', '')
        if os.getenv('REPLAY_ALLOW_LIVE_YOUTUBE') != '1' or not args.media or not re.fullmatch(r'[A-Za-z0-9_-]{10,160}', key):
            raise ValueError('Live output requires --media, --live-youtube and both explicit authorization/key environment variables')
    with tempfile.TemporaryDirectory(prefix='replay-release-verify-') as scratch:
        source = args.media or Path(scratch) / 'synthetic.mp4'
        if not args.media:
            subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-f', 'lavfi', '-i', 'testsrc2=size=320x180:rate=30',
                '-f', 'lavfi', '-i', 'sine=frequency=440', '-t', str(args.soak_seconds), '-c:v', 'libx264', '-preset', 'ultrafast',
                '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-movflags', '+faststart', str(source)], check=True, timeout=max(60, args.soak_seconds))
        metadata = validate_media(source, validation_timeout=max(300, args.soak_seconds))
        if metadata['duration'] > 60 and not args.allow_long_run:
            raise ValueError('Long media requires --allow-long-run')
        output = Path(scratch) / 'output.flv'
        target = 'rtmps://a.rtmps.youtube.com:443/live2/' + key if args.live_youtube else output
        started = time.monotonic()
        result = run_stream(stream_command(source, target), expected_duration=metadata['duration'],
                            local_output=None if args.live_youtube else output, redactions=(key,))
        if not result.complete:
            raise RuntimeError(result.error_code or 'MEDIA_CHECK_FAILED')
        report = {'media_verified': True, 'input_duration': metadata['duration'], 'progress': result.progress,
                  'elapsed_seconds': round(time.monotonic() - started, 3), 'target': 'youtube' if args.live_youtube else 'local',
                  'youtube_studio_verified': False, 'output_sha256': None if args.live_youtube else file_hash(output)}
        if args.sqlite_lock_seconds:
            service = Service(Path(scratch) / 'locked-db', sqlite_timeout=.05, poll_interval=.02)
            try:
                service.start()
                lock = sqlite3.connect(service.db_path)
                try:
                    lock.execute('BEGIN IMMEDIATE')
                    time.sleep(args.sqlite_lock_seconds)
                    report['lock_seconds'] = args.sqlite_lock_seconds
                    report['scheduler_alive_during_lock'] = service.scheduler.is_alive()
                    report['readiness_during_lock'] = service.readiness()['ready']
                finally:
                    lock.rollback()
                    lock.close()
                deadline = time.monotonic() + 3
                while not service.readiness()['ready'] and time.monotonic() < deadline:
                    time.sleep(.025)
                report['readiness_recovered'] = service.readiness()['ready']
                if not report['scheduler_alive_during_lock'] or report['readiness_during_lock'] or not report['readiness_recovered']:
                    raise RuntimeError('SQLite scheduler recovery failed')
            finally:
                service.close()
        return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database-check', action='store_true')
    parser.add_argument('--database-env', default='REPLAY_DATABASE_URL')
    parser.add_argument('--development', action='store_true')
    parser.add_argument('--seed-fixture', action='store_true')
    parser.add_argument('--assert-restored-fixture', action='store_true')
    parser.add_argument('--media', type=Path)
    parser.add_argument('--soak-seconds', type=int, default=3)
    parser.add_argument('--sqlite-lock-seconds', type=float, default=0)
    parser.add_argument('--allow-long-run', action='store_true')
    parser.add_argument('--live-youtube', action='store_true')
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    try:
        if (args.seed_fixture or args.assert_restored_fixture) and not args.database_check:
            raise ValueError('Database fixture modes require --database-check')
        report = database_check(args) if args.database_check else media_check(args)
        if args.report:
            private_json(args.report, report)
        print(json.dumps(report, ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({'error': type(exc).__name__, 'operation': 'verification_failed'}), file=sys.stderr)
        sys.exit(1)

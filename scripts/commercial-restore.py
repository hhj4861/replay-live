#!/usr/bin/env python3
"""Restore into an empty, explicitly named isolated database; no live jobs start."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from deploy.commercial.ops import database_url, restore

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('backup', type=Path)
    parser.add_argument('--database-env', default='REPLAY_RESTORE_DATABASE_URL')
    parser.add_argument('--confirm-database', required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(restore(database_url(args.database_env), args.backup, args.confirm_database)))
    except Exception as exc:
        print(json.dumps({'error': type(exc).__name__, 'operation': 'restore_failed'}), file=sys.stderr)
        sys.exit(1)

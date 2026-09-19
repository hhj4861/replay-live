#!/usr/bin/env python3
"""Consistent PostgreSQL custom-format backup and private verification manifest."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from deploy.commercial.ops import backup, database_url

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    parser.add_argument('--database-env', default='REPLAY_DATABASE_URL')
    args = parser.parse_args()
    try:
        print(json.dumps(backup(database_url(args.database_env), args.output)))
    except Exception as exc:
        print(json.dumps({'error': type(exc).__name__, 'operation': 'backup_failed'}), file=sys.stderr)
        sys.exit(1)

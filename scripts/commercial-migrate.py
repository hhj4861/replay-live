#!/usr/bin/env python3
"""Explicit schema migration; never starts the API or a scheduler."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from deploy.commercial.ops import database_url, migrate

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database-env', default='REPLAY_DATABASE_URL')
    parser.add_argument('--development', action='store_true')
    args = parser.parse_args()
    try:
        print(json.dumps(migrate(database_url(args.database_env, development=args.development), development=args.development)))
    except Exception as exc:
        print(json.dumps({'error': type(exc).__name__, 'operation': 'migration_failed'}), file=sys.stderr)
        sys.exit(1)

#!/usr/bin/env python3
"""Run the durable scheduler against a local API; credentials are environment-only."""
import argparse
import os
import time
from urllib.parse import urlsplit

import httpx


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args()
    base = os.environ.get('REPLAY_LOCAL_API_URL', 'http://127.0.0.1:13000').rstrip('/')
    url = urlsplit(base)
    token = os.environ.get('REPLAY_CONTROL_TOKEN', '')
    if (url.scheme != 'http' or url.hostname not in ('127.0.0.1', 'localhost', '::1')
            or url.username or url.password or url.query or url.fragment or url.path or len(token) < 32):
        parser.error('Set a loopback REPLAY_LOCAL_API_URL and REPLAY_CONTROL_TOKEN (at least 32 characters).')
    with httpx.Client(timeout=120, trust_env=False, follow_redirects=False) as client:
        while True:
            try:
                response = client.post(base + '/internal/automations/tick',
                                       headers={'Authorization': 'Bearer ' + token})
                response.raise_for_status()
                print('Scheduler tick completed', flush=True)
            except httpx.HTTPError:
                print('Scheduler tick failed; check local API configuration.', flush=True)
                if args.once:
                    return 1
            if args.once:
                return 0
            time.sleep(15)


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        pass

"""Owner-authorized YouTube channel discovery; refresh tokens stay encrypted."""
import base64
import hashlib
import json
import os
import re
import secrets
import time
from urllib.parse import urlencode

import httpx
from sqlalchemy import Column, Float, MetaData, String, Table, Text, delete, insert, select

from .stream_connections import StreamConnections


metadata = MetaData()
channels = Table('replay_youtube_channels', metadata,
    Column('tenant_id', String(200), primary_key=True), Column('subject_hash', String(64), primary_key=True),
    Column('channel_id', String(64), nullable=False), Column('title', String(180), nullable=False),
    Column('uploads_id', String(80), nullable=False), Column('secret_ciphertext', Text, nullable=False))
flows = Table('replay_youtube_oauth_flows', metadata,
    Column('state_hash', String(64), primary_key=True), Column('tenant_id', String(200), nullable=False),
    Column('subject_hash', String(64), nullable=False), Column('secret_ciphertext', Text, nullable=False),
    Column('expires_at', Float, nullable=False))
SCOPE = 'https://www.googleapis.com/auth/youtube.readonly'
LIVE_SCOPE = 'https://www.googleapis.com/auth/youtube.force-ssl'
grants = Table('replay_youtube_grants', metadata,
    Column('tenant_id', String(200), primary_key=True), Column('subject_hash', String(64), primary_key=True),
    Column('scopes', Text, nullable=False))


class ChannelError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class YouTubeChannel:
    def __init__(self, repo, keys, callback, *, client=None, clock=time.time):
        self.repo, self.keys, self.callback, self.clock = repo, keys, callback, clock
        self.client_id = os.getenv('REPLAY_YOUTUBE_CLIENT_ID', '')
        self.client_secret = os.getenv('REPLAY_YOUTUBE_CLIENT_SECRET', '')
        self.http = client or httpx.Client(timeout=10, follow_redirects=False, trust_env=False)

    @property
    def configured(self):
        return bool(self.client_id and self.client_secret)

    @staticmethod
    def context(owner):
        return {**owner, 'purpose': 'youtube-channel-v1'}

    @staticmethod
    def where(owner):
        return (channels.c.tenant_id == owner['tenant_id']) & (channels.c.subject_hash == owner['subject_hash'])

    def status(self, user):
        owner = StreamConnections._owner(user)
        with self.repo.engine.connect() as connection:
            row = connection.execute(select(channels).where(self.where(owner))).mappings().first()
            grant = connection.execute(select(grants.c.scopes).where(
                grants.c.tenant_id == owner['tenant_id'], grants.c.subject_hash == owner['subject_hash'])).scalar_one_or_none()
        return {'configured': self.configured, 'connected': row is not None,
                'live_authorized': bool(row and grant and LIVE_SCOPE in grant.split()),
                'channel': {k: row[k] for k in ('channel_id', 'title')} if row else None}

    def begin(self, user, *, live=False):
        if not self.configured:
            raise ChannelError('YOUTUBE_NOT_CONFIGURED')
        owner = StreamConnections._owner(user)
        scopes = SCOPE + (' ' + LIVE_SCOPE if live else '')
        state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
        with self.repo._transaction() as connection:
            connection.execute(delete(flows).where(flows.c.expires_at < self.clock()))
            connection.execute(delete(flows).where(flows.c.tenant_id == owner['tenant_id'],
                                                    flows.c.subject_hash == owner['subject_hash']))
            connection.execute(insert(flows).values(**owner, state_hash=hashlib.sha256(state.encode()).hexdigest(),
                secret_ciphertext=self.keys.encrypt(json.dumps({'verifier': verifier, 'scopes': scopes}), self.context(owner)), expires_at=self.clock()+600))
        return {'url': 'https://accounts.google.com/o/oauth2/v2/auth?' + urlencode({
            'client_id': self.client_id, 'redirect_uri': self.callback, 'response_type': 'code',
            'scope': scopes, 'access_type': 'offline', 'prompt': 'consent', 'state': state,
            'code_challenge': challenge, 'code_challenge_method': 'S256'})}

    def request(self, method, url, **kwargs):
        try:
            response = self.http.request(method, url, **kwargs)
            if response.status_code in (400, 401):
                raise ChannelError('YOUTUBE_RECONNECT_REQUIRED')
            if response.status_code == 403:
                # Expose only a fixed error code, never provider messages or credentials.
                try:
                    reasons = response.json().get('error', {}).get('errors', [])
                    if any(isinstance(item, dict) and item.get('reason') == 'liveStreamingNotEnabled' for item in reasons):
                        raise ChannelError('YOUTUBE_LIVE_NOT_ENABLED')
                except (AttributeError, TypeError, ValueError) as error:
                    if isinstance(error, ChannelError):
                        raise
                raise ChannelError('YOUTUBE_ACCESS_DENIED')
            if response.status_code != 200:
                raise ChannelError('YOUTUBE_UNAVAILABLE')
            data = response.json()
            if not isinstance(data, dict):
                raise ChannelError('YOUTUBE_UNAVAILABLE')
            return data
        except (httpx.HTTPError, ValueError) as error:
            if isinstance(error, ChannelError):
                raise
            raise ChannelError('YOUTUBE_UNAVAILABLE') from None

    def finish(self, state, code):
        if not self.configured or not re.fullmatch(r'[A-Za-z0-9_-]{40,100}', state or ''):
            raise ChannelError('YOUTUBE_INVALID_STATE')
        with self.repo._transaction() as connection:
            digest = hashlib.sha256(state.encode()).hexdigest()
            row = connection.execute(select(flows).where(flows.c.state_hash == digest).with_for_update()).mappings().first()
            if not row or row['expires_at'] <= self.clock():
                raise ChannelError('YOUTUBE_INVALID_STATE')
            connection.execute(delete(flows).where(flows.c.state_hash == digest))
        owner = {k: row[k] for k in ('tenant_id', 'subject_hash')}
        stored = self.keys.decrypt(row['secret_ciphertext'], self.context(owner))
        try:
            flow = json.loads(stored)
        except ValueError:
            flow = {'verifier': stored, 'scopes': SCOPE}
        token = self.request('POST', 'https://oauth2.googleapis.com/token', data={
            'client_id': self.client_id, 'client_secret': self.client_secret, 'grant_type': 'authorization_code',
            'redirect_uri': self.callback, 'code': code,
            'code_verifier': flow['verifier']})
        if not token.get('access_token') or not token.get('refresh_token') or not set(flow['scopes'].split()).issubset(token.get('scope', '').split()):
            raise ChannelError('YOUTUBE_RECONNECT_REQUIRED')
        items = self.request('GET', 'https://www.googleapis.com/youtube/v3/channels',
            params={'part': 'snippet,contentDetails', 'mine': 'true'},
            headers={'Authorization': 'Bearer ' + token['access_token']}).get('items', [])
        if len(items) != 1:
            raise ChannelError('YOUTUBE_CHANNEL_REQUIRED')
        try:
            channel = items[0]
            channel_id, uploads = channel['id'], channel['contentDetails']['relatedPlaylists']['uploads']
            if not re.fullmatch(r'UC[A-Za-z0-9_-]{22}', channel_id) or not re.fullmatch(r'[A-Za-z0-9_-]{10,80}', uploads):
                raise ValueError()
            title = str(channel['snippet']['title'])[:180]
        except (KeyError, TypeError, ValueError):
            raise ChannelError('YOUTUBE_CHANNEL_REQUIRED') from None
        ciphertext = self.keys.encrypt(token['refresh_token'], self.context(owner))
        with self.repo._transaction() as connection:
            self.repo._check_admission(connection, owner['tenant_id'])
            connection.execute(delete(grants).where(grants.c.tenant_id == owner['tenant_id'], grants.c.subject_hash == owner['subject_hash']))
            connection.execute(delete(channels).where(self.where(owner)))
            connection.execute(insert(channels).values(**owner, channel_id=channel_id, title=title,
                                                       uploads_id=uploads, secret_ciphertext=ciphertext))
            connection.execute(insert(grants).values(**owner, scopes=token['scope']))

    def credentials(self, user, *, live=False):
        owner = StreamConnections._owner(user)
        if live and not self.status(user)['live_authorized']:
            raise ChannelError('YOUTUBE_LIVE_PERMISSION_REQUIRED')
        with self.repo.engine.connect() as connection:
            row = connection.execute(select(channels).where(self.where(owner))).mappings().first()
        if not row:
            raise ChannelError('YOUTUBE_RECONNECT_REQUIRED')
        token = self.request('POST', 'https://oauth2.googleapis.com/token', data={
            'client_id': self.client_id, 'client_secret': self.client_secret, 'grant_type': 'refresh_token',
            'refresh_token': self.keys.decrypt(row['secret_ciphertext'], self.context(owner))})
        if not isinstance(token.get('access_token'), str):
            raise ChannelError('YOUTUBE_RECONNECT_REQUIRED')
        if live and 'scope' in token and LIVE_SCOPE not in token['scope'].split():
            raise ChannelError('YOUTUBE_LIVE_PERMISSION_REQUIRED')
        return dict(row), {'Authorization': 'Bearer ' + token['access_token']}

    def disconnect(self, user):
        owner = StreamConnections._owner(user)
        with self.repo._transaction() as connection:
            connection.execute(delete(channels).where(self.where(owner)))
            connection.execute(delete(grants).where(grants.c.tenant_id == owner['tenant_id'], grants.c.subject_hash == owner['subject_hash']))
            connection.execute(delete(flows).where(flows.c.tenant_id == owner['tenant_id'],
                                                    flows.c.subject_hash == owner['subject_hash']))

    def latest(self, user):
        row, headers = self.credentials(user)
        items = self.request('GET', 'https://www.googleapis.com/youtube/v3/playlistItems',
            params={'part': 'contentDetails', 'playlistId': row['uploads_id'], 'maxResults': 50},
            headers=headers).get('items', [])
        ids = [item.get('contentDetails', {}).get('videoId', '') for item in items]
        ids = [value for value in ids if re.fullmatch(r'[A-Za-z0-9_-]{11}', value)]
        if not ids:
            return None
        videos = self.request('GET', 'https://www.googleapis.com/youtube/v3/videos',
            params={'part': 'snippet,status,contentDetails', 'id': ','.join(ids)}, headers=headers).get('items', [])
        # Exclude our own replay archives so a public output cannot become the
        # next source and feed an endless rebroadcast loop.
        from .youtube_live import broadcasts
        with self.repo.engine.connect() as connection:
            generated = set(connection.execute(select(broadcasts.c.broadcast_id).where(
                broadcasts.c.channel_id == row['channel_id'])).scalars())
        public = [v for v in videos if v.get('id') in ids and v.get('id') not in generated and v.get('status', {}).get('privacyStatus') == 'public'
                  and v.get('snippet', {}).get('channelId') == row['channel_id']
                  and v.get('snippet', {}).get('liveBroadcastContent') == 'none'
                  and v.get('contentDetails', {}).get('duration') not in (None, 'P0D', 'PT0S')]
        if not public:
            return None
        video = max(public, key=lambda v: v['snippet'].get('publishedAt', ''))
        return {'id': video['id'], 'url': 'https://www.youtube.com/watch?v=' + video['id'],
                'title': video['snippet']['title'][:180]}

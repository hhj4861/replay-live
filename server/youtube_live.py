"""YouTube broadcast lifecycle, scoped to the authenticated channel and one durable run."""
from datetime import datetime, timezone
import hmac
import re
import time

from sqlalchemy import Boolean, Column, Float, MetaData, String, Table, insert, select, update

from .stream_connections import StreamConnections
from .youtube_channel import ChannelError

metadata = MetaData()
broadcasts = Table('replay_youtube_broadcasts', metadata,
    Column('run_id', String(32), primary_key=True), Column('tenant_id', String(200), nullable=False),
    Column('subject_hash', String(64), nullable=False), Column('channel_id', String(64), nullable=False),
    Column('stream_id', String(128), nullable=False), Column('broadcast_id', String(64)),
    Column('phase', String(32), nullable=False), Column('live_seen', Boolean, nullable=False),
    Column('created', Float, nullable=False), Column('updated', Float, nullable=False))
BASE = 'https://www.googleapis.com/youtube/v3/'


class YouTubeLive:
    def __init__(self, repo, channel, *, clock=time.time):
        self.repo, self.channel, self.clock = repo, channel, clock

    def where(self, user, run_id):
        owner = StreamConnections._owner(user)
        return ((broadcasts.c.run_id == run_id) & (broadcasts.c.tenant_id == owner['tenant_id'])
                & (broadcasts.c.subject_hash == owner['subject_hash']))

    def row(self, user, run_id):
        with self.repo.engine.connect() as connection:
            row = connection.execute(select(broadcasts).where(self.where(user, run_id))).mappings().first()
            return dict(row) if row else None

    def record(self, user, run_id, **values):
        with self.repo._transaction() as connection:
            connection.execute(update(broadcasts).where(self.where(user, run_id)).values(updated=self.clock(), **values))

    def call(self, headers, resource, *, method='GET', params=None, body=None):
        # No caller-supplied URLs and no raw provider errors/secrets escape this boundary.
        return self.channel.request(method, BASE + resource, headers=headers, params=params,
                                    **({'json': body} if body is not None else {}))

    def owned_item(self, headers, resource, identifier, channel_id, part):
        values = self.call(headers, resource, params={'part': part, 'id': identifier}).get('items', [])
        if len(values) != 1 or values[0].get('id') != identifier or values[0].get('snippet', {}).get('channelId') != channel_id:
            raise ChannelError('YOUTUBE_BROADCAST_MISSING')
        return values[0]

    def stream_for_key(self, headers, channel_id, stream_key):
        page = None
        for _ in range(10):
            result = self.call(headers, 'liveStreams', params={'part': 'snippet,cdn,status', 'mine': 'true',
                'maxResults': 50, **({'pageToken': page} if page else {})})
            for item in result.get('items', []):
                key = item.get('cdn', {}).get('ingestionInfo', {}).get('streamName', '')
                if (item.get('snippet', {}).get('channelId') == channel_id and isinstance(key, str)
                        and hmac.compare_digest(key, stream_key)):
                    if item.get('status', {}).get('streamStatus') == 'active':
                        raise ChannelError('YOUTUBE_STREAM_IN_USE')
                    identifier = item.get('id', '')
                    if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', identifier):
                        break
                    return identifier
            page = result.get('nextPageToken')
            if not page:
                break
        raise ChannelError('YOUTUBE_STREAM_KEY_MISMATCH')

    def prepare(self, user, run_id, title, destination, *, privacy='unlisted', made_for_kids=False, guard=lambda: None):
        channel, headers = self.channel.credentials(user, live=True)
        row = self.row(user, run_id)
        if row and row['channel_id'] != channel['channel_id']:
            raise ChannelError('YOUTUBE_CHANNEL_CHANGED')
        if row is None:
            stream_id = self.stream_for_key(headers, channel['channel_id'], destination['stream_key'])
            guard()
            # Persist intent BEFORE the non-idempotent API call. An ambiguous outcome
            # requires review; it must never silently create another broadcast.
            with self.repo._transaction() as connection:
                connection.execute(insert(broadcasts).values(run_id=run_id, **StreamConnections._owner(user),
                    channel_id=channel['channel_id'], stream_id=stream_id, phase='creating', live_seen=False,
                    created=self.clock(), updated=self.clock()))
            try:
                result = self.call(headers, 'liveBroadcasts', method='POST', params={'part': 'snippet,status,contentDetails'}, body={
                    'snippet': {'title': title[:100], 'description': 'Replay Live automatic broadcast ' + run_id,
                        'scheduledStartTime': datetime.fromtimestamp(self.clock()+30, timezone.utc).isoformat()},
                    'status': {'privacyStatus': privacy, 'selfDeclaredMadeForKids': made_for_kids},
                    'contentDetails': {'enableAutoStart': True, 'enableAutoStop': True, 'enableDvr': True,
                        'recordFromStart': True, 'monitorStream': {'enableMonitorStream': False}}})
            except ChannelError as error:
                if error.code == 'YOUTUBE_UNAVAILABLE':
                    raise ChannelError('YOUTUBE_CREATE_UNCONFIRMED') from None
                raise
            identifier = result.get('id', '')
            if not re.fullmatch(r'[A-Za-z0-9_-]{11}', identifier):
                raise ChannelError('YOUTUBE_CREATE_UNCONFIRMED')
            # Recording the returned id is safe even after lease expiry; creation was
            # reserved exactly once and a later coordinator can recover the binding.
            self.record(user, run_id, broadcast_id=identifier, phase='created')
            row = self.row(user, run_id)
        if not row['broadcast_id']:
            raise ChannelError('YOUTUBE_CREATE_UNCONFIRMED')
        item = self.owned_item(headers, 'liveBroadcasts', row['broadcast_id'], row['channel_id'], 'snippet,status,contentDetails')
        if row['phase'] != 'ready':
            bound = item.get('contentDetails', {}).get('boundStreamId')
            if bound and bound != row['stream_id']:
                raise ChannelError('YOUTUBE_STREAM_KEY_MISMATCH')
            if not bound:
                guard()
                self.call(headers, 'liveBroadcasts/bind', method='POST', params={
                    'id': row['broadcast_id'], 'streamId': row['stream_id'], 'part': 'id,contentDetails'})
            self.record(user, run_id, phase='ready')
        return 'https://www.youtube.com/watch?v=' + row['broadcast_id']

    def transition(self, headers, row, desired):
        try:
            self.call(headers, 'liveBroadcasts/transition', method='POST', params={
                'id': row['broadcast_id'], 'broadcastStatus': desired, 'part': 'status'})
        except ChannelError:
            # YouTube auto-start/stop may win the race with this explicit transition.
            # Read back instead of failing a broadcast already at the desired state.
            item = self.owned_item(headers, 'liveBroadcasts', row['broadcast_id'], row['channel_id'], 'snippet,status')
            state = item.get('status', {}).get('lifeCycleStatus')
            accepted = ('liveStarting', 'live', 'complete') if desired == 'live' else ('complete',)
            if state not in accepted:
                raise

    def observe(self, user, run_id, *, ended, guard=lambda: None):
        row = self.row(user, run_id)
        if not row or not row['broadcast_id']:
            raise ChannelError('YOUTUBE_CREATE_UNCONFIRMED')
        channel, headers = self.channel.credentials(user, live=True)
        if channel['channel_id'] != row['channel_id']:
            raise ChannelError('YOUTUBE_CHANNEL_CHANGED')
        item = self.owned_item(headers, 'liveBroadcasts', row['broadcast_id'], row['channel_id'], 'snippet,status,contentDetails')
        state = item.get('status', {}).get('lifeCycleStatus')
        live_seen = row['live_seen'] or state == 'live' or bool(item.get('snippet', {}).get('actualStartTime'))
        guard()
        self.record(user, run_id, live_seen=live_seen, phase=state or 'unknown')
        if state == 'complete':
            if not live_seen:
                raise ChannelError('YOUTUBE_LIVE_NOT_STARTED')
            return True
        if state == 'revoked':
            raise ChannelError('YOUTUBE_BROADCAST_REVOKED')
        if ended:
            if state == 'live':
                guard()
                self.transition(headers, row, 'complete')
            return False  # Always read back the platform state on the next tick.
        if state in ('ready', 'testing'):
            stream = self.owned_item(headers, 'liveStreams', row['stream_id'], row['channel_id'], 'snippet,status')
            if stream.get('status', {}).get('streamStatus') == 'active':
                guard()
                self.transition(headers, row, 'live')
        return False

    def public(self, user, run_id):
        row = self.row(user, run_id)
        return ({'watch_url': 'https://www.youtube.com/watch?v=' + row['broadcast_id'],
                 'state': row['phase'], 'live_confirmed': row['live_seen']} if row and row['broadcast_id'] else None)

"""Real encrypted OAuth storage + HTTP-boundary lifecycle and recovery checks."""
from urllib.parse import parse_qs, urlsplit
import json

import httpx
import pytest

from server.youtube_channel import ChannelError, LIVE_SCOPE, SCOPE
from server.youtube_live import YouTubeLive, metadata
from test_youtube_channel import channel, USER, CHANNEL_ID
from test_commercial_api import commercial

RUN = 'a' * 32
VIDEO = 'b' * 11
DESTINATION = {'target': 'youtube', 'server_url': 'rtmps://a.rtmps.youtube.com:443/live2', 'stream_key': 'key-canary'}


@pytest.fixture
def lifecycle(channel):
    oauth, repo = channel
    metadata.create_all(repo.engine)
    state = {'status': 'ready', 'bound': None, 'stream': 'inactive', 'posts': [], 'started': False,
             'reject_create': False, 'stream_owner': CHANNEL_ID}
    def transport(request):
        path = request.url.path
        if path == '/token':
            return httpx.Response(200, json={'access_token': 'access-canary', 'refresh_token': 'refresh-canary', 'scope': SCOPE + ' ' + LIVE_SCOPE})
        if path.endswith('/channels'):
            return httpx.Response(200, json={'items': [{'id': CHANNEL_ID, 'snippet': {'title': 'Test channel'},
                'contentDetails': {'relatedPlaylists': {'uploads': 'UU' + 'a'*22}}}]})
        if request.method == 'POST':
            state['posts'].append((path, dict(request.url.params), json.loads(request.content) if request.content else None))
        if path.endswith('/liveStreams'):
            return httpx.Response(200, json={'items': [{'id': 'stream-1', 'snippet': {'channelId': state['stream_owner']},
                'cdn': {'ingestionInfo': {'streamName': 'key-canary'}}, 'status': {'streamStatus': state['stream']}}]})
        if path.endswith('/bind'):
            state['bound'] = request.url.params['streamId']
            return httpx.Response(200, json={'id': VIDEO})
        if path.endswith('/transition'):
            state['status'] = request.url.params['broadcastStatus']
            if state['status'] == 'live': state['started'] = True
            if state.get('transition_race'): return httpx.Response(400, json={'error': 'already transitioned'})
            return httpx.Response(200, json={'id': VIDEO, 'status': {'lifeCycleStatus': state['status']}})
        if request.method == 'POST':
            if state['reject_create']: raise httpx.ReadTimeout('sensitive provider message')
            return httpx.Response(200, json={'id': VIDEO})
        return httpx.Response(200, json={'items': [{'id': VIDEO, 'snippet': {'channelId': CHANNEL_ID,
            **({'actualStartTime': '2026-09-29T00:00:00Z'} if state['started'] else {})},
            'status': {'lifeCycleStatus': state['status']},
            'contentDetails': {'boundStreamId': state['bound']}}]})
    oauth.http = httpx.Client(transport=httpx.MockTransport(transport))
    flow = parse_qs(urlsplit(oauth.begin(USER, live=True)['url']).query)
    assert LIVE_SCOPE in flow['scope'][0]
    oauth.finish(flow['state'][0], 'code')
    assert oauth.status(USER)['live_authorized']
    return YouTubeLive(repo, oauth), state


def test_create_bind_same_channel_and_recover_without_duplicate(lifecycle):
    live, state = lifecycle
    assert live.prepare(USER, RUN, 'Test broadcast', DESTINATION) == 'https://www.youtube.com/watch?v=' + VIDEO
    assert state['bound'] == 'stream-1'
    assert len(state['posts']) == 2
    body = state['posts'][0][2]
    assert body['status']['privacyStatus'] == 'unlisted'
    assert body['contentDetails']['enableAutoStart'] and body['contentDetails']['enableAutoStop']
    restarted = YouTubeLive(live.repo, live.channel)
    restarted.prepare(USER, RUN, 'Test broadcast', DESTINATION)
    assert len(state['posts']) == 2
    assert 'key-canary' not in json.dumps(live.row(USER, RUN))


def test_transport_completed_is_not_broadcast_complete(lifecycle):
    live, state = lifecycle
    live.prepare(USER, RUN, 'Test', DESTINATION)
    assert live.observe(USER, RUN, ended=True) is False
    assert not live.public(USER, RUN)['live_confirmed']
    assert len(state['posts']) == 2  # no live transition after the sender has ended
    state['stream'] = 'active'
    assert live.observe(USER, RUN, ended=False) is False
    assert state['status'] == 'live'
    assert live.observe(USER, RUN, ended=False) is False
    assert live.public(USER, RUN)['live_confirmed']
    assert live.observe(USER, RUN, ended=True) is False  # request end, then read back
    assert state['status'] == 'complete'
    assert live.observe(USER, RUN, ended=True) is True


def test_auto_start_and_stop_between_ticks_uses_actual_start_time(lifecycle):
    live, state = lifecycle
    live.prepare(USER, RUN, 'Test', DESTINATION)
    state.update(status='complete', started=True)
    assert live.observe(USER, RUN, ended=True) is True
    assert live.public(USER, RUN)['live_confirmed']


def test_ambiguous_create_is_never_repeated(lifecycle):
    live, state = lifecycle
    state['reject_create'] = True
    with pytest.raises(ChannelError, match='YOUTUBE_CREATE_UNCONFIRMED'):
        live.prepare(USER, RUN, 'Test', DESTINATION)
    state['reject_create'] = False
    with pytest.raises(ChannelError, match='YOUTUBE_CREATE_UNCONFIRMED'):
        live.prepare(USER, RUN, 'Test', DESTINATION)
    assert len(state['posts']) == 1


@pytest.mark.parametrize('change,code', [({'stream': 'active'}, 'YOUTUBE_STREAM_IN_USE'),
    ({'stream_owner': 'UC' + 'z'*22}, 'YOUTUBE_STREAM_KEY_MISMATCH')])
def test_no_creation_for_wrong_channel_or_active_stream(lifecycle, change, code):
    live, state = lifecycle
    state.update(change)
    with pytest.raises(ChannelError, match=code): live.prepare(USER, RUN, 'Test', DESTINATION)
    assert state['posts'] == []


def test_read_only_connection_requires_new_consent(lifecycle):
    from sqlalchemy import update
    from server.youtube_channel import grants
    live, state = lifecycle
    with live.repo._transaction() as connection:
        connection.execute(update(grants).values(scopes=SCOPE))
    assert not live.channel.status(USER)['live_authorized']
    with pytest.raises(ChannelError, match='YOUTUBE_LIVE_PERMISSION_REQUIRED'):
        live.prepare(USER, RUN, 'Test', DESTINATION)
    assert state['posts'] == []


def test_unobserved_start_must_not_pass(lifecycle):
    live, state = lifecycle
    live.prepare(USER, RUN, 'Test', DESTINATION)
    state['status'] = 'complete'
    with pytest.raises(ChannelError, match='YOUTUBE_LIVE_NOT_STARTED'): live.observe(USER, RUN, ended=True)


def test_owner_isolation(lifecycle):
    from server.auth import Principal
    live, state = lifecycle
    live.prepare(USER, RUN, 'Test', DESTINATION)
    assert live.public(Principal('other', 'alpha', ('operator',)), RUN) is None


def test_auto_transition_race_reads_back_instead_of_failing(lifecycle):
    live, state = lifecycle
    live.prepare(USER, RUN, 'Test', DESTINATION)
    state.update(stream='active', transition_race=True)
    assert live.observe(USER, RUN, ended=False) is False
    assert state['status'] == 'live'
    assert live.observe(USER, RUN, ended=True) is False
    assert live.observe(USER, RUN, ended=True) is True

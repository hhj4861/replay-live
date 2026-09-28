import hashlib
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from sqlalchemy import select

from server.auth import Principal
from server.youtube_channel import ChannelError, channels, flows, SCOPE
from test_commercial_api import commercial


@pytest.fixture
def channel(commercial):
    client, repo, objects = commercial
    client.app.state.automations.migrate()
    channel = client.app.state.automations.channel
    channel.client_id, channel.client_secret = 'test-client', 'test-secret'
    channel.http.close()
    return channel, repo


USER = Principal('alpha', 'alpha', ('operator',))
CHANNEL_ID = 'UC' + 'a'*22


def test_pkce_one_time_state_encrypted_refresh_and_owner_channel(channel):
    service, repo = channel
    params = parse_qs(urlsplit(service.begin(USER)['url']).query)
    assert params['code_challenge_method'] == ['S256']
    state = params['state'][0]
    seen = []
    def transport(request):
        seen.append(request)
        if request.url.path == '/token':
            body = parse_qs(request.content.decode())
            assert body['code_verifier'][0] != state
            import base64
            assert base64.urlsafe_b64encode(hashlib.sha256(body['code_verifier'][0].encode()).digest()).decode().rstrip('=') == params['code_challenge'][0]
            return httpx.Response(200, json={'access_token': 'access-canary', 'refresh_token': 'refresh-canary', 'scope': SCOPE})
        assert request.url.params['mine'] == 'true'
        return httpx.Response(200, json={'items': [{'id': CHANNEL_ID, 'snippet': {'title': '내 채널'},
            'contentDetails': {'relatedPlaylists': {'uploads': 'UU'+'a'*22}}}]})
    service.http = httpx.Client(transport=httpx.MockTransport(transport))
    service.finish(state, 'auth-code')
    assert service.status(USER)['channel']['title'] == '내 채널'
    assert not service.status(Principal('other', 'alpha', ('operator',)))['connected']
    with repo.engine.connect() as connection:
        row = connection.execute(select(channels)).mappings().one()
        assert 'refresh-canary' not in row['secret_ciphertext']
        assert 'refresh-canary' == service.keys.decrypt(row['secret_ciphertext'], service.context({'tenant_id': 'alpha', 'subject_hash': row['subject_hash']}))
        assert not connection.execute(select(flows)).all()
    with pytest.raises(ChannelError, match='YOUTUBE_INVALID_STATE'):
        service.finish(state, 'auth-code')
    assert len(seen) == 2


def test_latest_filters_private_live_and_other_channels(channel):
    service, repo = channel
    params = parse_qs(urlsplit(service.begin(USER)['url']).query)
    videos = [
        {'id': str(i)*11, 'snippet': {'title': '영상 '+str(i), 'channelId': CHANNEL_ID, 'liveBroadcastContent': 'none',
          'publishedAt': f'2026-09-{20+i:02}T10:00:00Z'}, 'status': {'privacyStatus': 'public'}, 'contentDetails': {'duration': 'PT3M'}}
        for i in range(1, 6)]
    videos[2]['status']['privacyStatus'] = 'private'
    videos[3]['snippet']['liveBroadcastContent'] = 'live'
    videos[4]['snippet']['channelId'] = 'UC' + 'b'*22
    def transport(request):
        if request.url.path == '/token':
            return httpx.Response(200, json={'access_token': 'access-canary', 'refresh_token': 'refresh-canary', 'scope': SCOPE})
        if request.url.path.endswith('/channels'):
            return httpx.Response(200, json={'items': [{'id': CHANNEL_ID, 'snippet': {'title': '내 채널'},
                'contentDetails': {'relatedPlaylists': {'uploads': 'UU'+'a'*22}}}]})
        if request.url.path.endswith('/playlistItems'):
            return httpx.Response(200, json={'items': [{'contentDetails': {'videoId': v['id']}} for v in videos]})
        return httpx.Response(200, json={'items': videos})
    service.http = httpx.Client(transport=httpx.MockTransport(transport))
    service.finish(params['state'][0], 'code')
    assert service.latest(USER)['id'] == '2'*11
    service.disconnect(USER)
    with pytest.raises(ChannelError, match='YOUTUBE_RECONNECT_REQUIRED'):
        service.latest(USER)


def test_expiry_and_oauth_denied_fail_without_token_leak(channel):
    service, repo = channel
    state = parse_qs(urlsplit(service.begin(USER)['url']).query)['state'][0]
    service.clock = lambda: 99999999999
    with pytest.raises(ChannelError, match='YOUTUBE_INVALID_STATE'):
        service.finish(state, 'code')
    service.http = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(400,
        json={'error_description': 'sensitive-provider-message'})))
    with pytest.raises(ChannelError, match='YOUTUBE_RECONNECT_REQUIRED') as error:
        service.request('POST', 'https://oauth2.googleapis.com/token')
    assert 'sensitive-provider-message' not in str(error)

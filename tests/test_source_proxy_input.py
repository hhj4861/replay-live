"""Loopback credentials never need to be echoed, saved, or accepted cross-origin."""
import importlib.util
import json
from pathlib import Path
import queue
import re
import threading
from types import SimpleNamespace
import urllib.error
import urllib.parse
import urllib.request


def test_proxy_form_validates_origin_nonce_and_url_before_running(monkeypatch):
    spec = importlib.util.spec_from_file_location('source_proxy_input',
        Path(__file__).parents[1] / 'scripts/source-proxy-input.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    printed, commands = queue.Queue(), []
    monkeypatch.setattr(module, 'print', lambda value, **kw: printed.put(value), raising=False)

    def run(command, **kwargs):
        commands.append((command, kwargs))
        # A runner accidentally echoing the full credential must not publish it.
        return SimpleNamespace(returncode=0, stdout=json.dumps({
            'accidental': kwargs['env']['REPLAY_DIAGNOSTIC_PROXY_URL']}))

    monkeypatch.setattr(module.subprocess, 'run', run)
    thread = threading.Thread(target=module.serve, args=(['fixed-diagnostic'], 5), daemon=True)
    thread.start()
    url = json.loads(printed.get(timeout=3))['input_url']
    origin = url.rsplit('/', 1)[0]
    with urllib.request.urlopen(url, timeout=3) as response:
        page = response.read().decode()
        assert response.headers['Referrer-Policy'] == 'same-origin'
        assert response.headers['Cache-Control'] == 'no-store'
        assert "form-action 'self'" in response.headers['Content-Security-Policy']
    nonce = re.search(r'name="csrf" value="([^"]+)"', page)[1]
    credential = 'http://synthetic:fixture@gw.dataimpulse.com:823'

    def post(source, csrf, value):
        headers = {'Content-Type': 'application/x-www-form-urlencoded'}
        if source is not None:
            headers['Origin'] = source
        request = urllib.request.Request(url, headers=headers,
            data=urllib.parse.urlencode({'csrf': csrf, 'proxy': value}).encode())
        try:
            response = urllib.request.urlopen(request, timeout=3)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            assert credential not in response.read().decode()
            return response.status

    for source in ('https://foreign.example', 'null', None):
        assert post(source, nonce, credential) == 403
    assert post(origin, 'wrong', credential) == 403
    for value in ('invalid', 'http://u:p@localhost:823',
                  'http://u:p@gw.dataimpulse.com:823/?secret=x',
                  'http://u%0a:p@gw.dataimpulse.com:823'):
        assert post(origin, nonce, value) == 400
    assert not commands
    assert post(origin, nonce, credential) == 200
    thread.join(timeout=3)
    assert not thread.is_alive()
    assert len(commands) == 1 and commands[0][0] == ['fixed-diagnostic']
    assert commands[0][1]['env']['REPLAY_DIAGNOSTIC_PROXY_URL'] == credential
    assert json.loads(printed.get(timeout=1)) == {'diagnostic_exit': 0}
    assert printed.empty()

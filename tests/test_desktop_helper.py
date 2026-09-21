import importlib.util
import json
from pathlib import Path
import plistlib
import subprocess
import sys

import pytest

SPEC = importlib.util.spec_from_file_location('desktop_helper', Path(__file__).parents[1] / 'desktop/helper.py')
helper = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(helper)


def test_launch_url_cannot_choose_endpoint_or_execute_arguments():
    for url in ['https://evil.invalid', 'replay-live-helper://start?api=https://evil.invalid', 'replay-live-helper://start/../../run', 'replay-live-helper://start --serve']:
        with pytest.raises(ValueError, match='Invalid helper'):
            helper.main(['--open', url])


def test_cancel_install_leaves_disk_and_startup_untouched(monkeypatch, tmp_path):
    monkeypatch.setattr(helper.sys, 'frozen', True, raising=False)
    monkeypatch.setattr(helper, 'bundle_root', lambda: tmp_path / 'download')
    monkeypatch.setattr(helper, 'install_root', lambda: tmp_path / 'installed')
    monkeypatch.setattr(helper, 'confirm', lambda _: False)
    helper.main([])
    assert not (tmp_path / 'installed').exists()


def test_installer_copies_bundle_before_replacement(monkeypatch, tmp_path):
    monkeypatch.setattr(helper, 'stop_installed', lambda: None)
    source = tmp_path / 'source'; source.mkdir(); (source / 'app').write_text('new')
    target = tmp_path / 'installed'; target.mkdir(); (target / 'app').write_text('old')
    helper.install(source, target)
    assert (target / 'app').read_text() == 'new'
    assert not target.with_name(target.name + '.previous').exists()


def test_decline_autostart_still_installs_and_does_not_write_run_registration(monkeypatch, tmp_path):
    monkeypatch.setattr(helper.sys, 'frozen', True, raising=False)
    source = tmp_path / 'source'; source.mkdir(); (source / 'app').write_text('bundle')
    target = tmp_path / 'installed'; flags = []
    monkeypatch.setattr(helper, 'bundle_root', lambda: source)
    monkeypatch.setattr(helper, 'install_root', lambda: target)
    monkeypatch.setattr(helper, 'stop_installed', lambda: None)
    monkeypatch.setattr(helper, 'register_protocol', lambda _: None)
    monkeypatch.setattr(helper, 'set_startup', lambda root, enabled: flags.append(enabled))
    responses = iter([True, False])
    monkeypatch.setattr(helper, 'confirm', lambda _: next(responses))
    monkeypatch.setattr(helper, 'occupied', lambda: True)
    monkeypatch.setattr(helper, 'notice', lambda _: None)
    helper.main([])
    assert flags == [False]
    assert target.is_dir()


@pytest.mark.skipif(sys.platform != 'darwin', reason='macOS plist integration')
def test_user_login_agent_quotes_paths_and_supports_disable(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setattr(subprocess, 'run', lambda *a, **k: None)
    root = tmp_path / 'Applications/Replay Live Helper.app'
    helper.set_startup(root, True)
    path = tmp_path / 'Library/LaunchAgents/app.replaylive.helper.plist'
    data = plistlib.loads(path.read_bytes())
    assert data['ProgramArguments'] == [str(root / 'Contents/MacOS/ReplayLiveHelper'), '--serve']
    assert data['RunAtLoad'] is True
    assert not data.get('KeepAlive')
    helper.set_startup(root, False)
    assert not path.exists()


def test_packaged_api_cannot_be_changed_to_untrusted_server(monkeypatch, tmp_path):
    monkeypatch.setattr(helper.sys, '_MEIPASS', str(tmp_path), raising=False)
    (tmp_path / 'helper-config.json').write_text(json.dumps({'cloud_url': 'https://evil.invalid', 'web_origin': helper.ORIGINS[0]}))
    with pytest.raises(ValueError): helper.configuration()

"""GUI-launch environment and asynchronous overlay regressions, entirely fake."""
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import google_voice_ui
from google_voice_ui import GoogleVoiceUI, UIError
from service import write_json


def node(role, name='', children=None, **fields):
    return dict(role=role, name=name, children=children or [], **fields)


@pytest.fixture
def configured(tmp_path):
    config = {
        'app': 'Google Chrome', 'window_id': 'w-test', 'pid': 1,
        'process_instance': 'test-instance',
        'url_prefix': 'https://voice.google.com/u/0/',
        'account_label': 'Test account',
        'reader': '/test/local tools/elle-read',
        'desktop': '/test/desktop tools/agent-desktop',
    }
    write_json(tmp_path/'google-voice-ui.json', config)
    return tmp_path, config


def observed_response(config):
    web = node('webarea', 'Voice - Calls', [node('button', config['account_label'])])
    tree = node('window', 'Test Chrome', [
        node('textfield', 'Address and search bar', value='voice.google.com/u/0/calls'), web,
    ])
    return {
        'status': 'complete', 'identity_verified': True,
        'target': {key: config[key] for key in ('window_id', 'pid', 'process_instance')},
        'raw_snapshot': {'data': {'tree': tree}},
    }


def test_gui_reader_uses_service_python_and_known_tool_path(configured, monkeypatch):
    home, config = configured
    python = '/test/service venv/bin/python'
    monkeypatch.setattr(google_voice_ui.sys, 'executable', python)
    monkeypatch.setenv('PATH', '/usr/bin:/bin')
    monkeypatch.setenv('CALL_TEST_SENTINEL', 'retained')
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(stdout=json.dumps(observed_response(config)))

    ui = GoogleVoiceUI(home, runner=run)
    web, _, _ = ui.raw()
    assert web['name'] == 'Voice - Calls'
    assert len(calls) == 1
    argv, options = calls[0]
    assert argv[:3] == [python, config['reader'], 'observe']
    assert argv[argv.index('--window-id')+1] == config['window_id']
    assert options['env']['PATH'].split(os.pathsep) == [
        str(Path(python).parent), str(Path(config['desktop']).parent), '/usr/bin', '/bin',
    ]
    assert options['env']['CALL_TEST_SENTINEL'] == 'retained'
    assert os.environ['PATH'] == '/usr/bin:/bin'
    assert options['capture_output'] is True and options['text'] is True


def test_nonpython_reader_is_not_passed_to_python(configured, monkeypatch):
    home, config = configured
    config['reader'] = '/test/custom/native-reader'
    write_json(home/'google-voice-ui.json', config)
    calls = []
    ui = GoogleVoiceUI(home, runner=lambda argv, **kwargs:
                       calls.append(argv) or SimpleNamespace(stdout=json.dumps(observed_response(config))))
    ui.raw()
    assert calls[0][:2] == [config['reader'], 'observe']


def test_default_reader_pins_expected_process_before_reading(configured):
    home, config = configured
    del config['reader']
    write_json(home/'google-voice-ui.json', config)
    calls = []
    ui = GoogleVoiceUI(home, runner=lambda argv, **kwargs:
                       calls.append(argv) or SimpleNamespace(stdout=json.dumps(observed_response(config))))
    ui.raw()
    argv = calls[0]
    assert argv[:3] == [google_voice_ui.sys.executable,
                        str(Path(google_voice_ui.__file__).with_name('pinned_reader.py')), 'observe']
    assert argv[argv.index('--pid')+1] == str(config['pid'])
    assert argv[argv.index('--process-instance')+1] == config['process_instance']


@pytest.mark.parametrize('backend,expected', [
    ('WINDOW_NOT_FOUND', 'google_voice_window_unavailable'),
    ('TARGET_CHANGED', 'native_window_identity_changed'),
    ('AMBIGUOUS_TARGET', 'native_window_ambiguous'),
    ('POLICY_DENIED', 'POLICY_DENIED'),
])
def test_reader_failure_keeps_actionable_cause(configured, backend, expected):
    home, _ = configured
    response = {'status':'unavailable', 'identity_verified':False, 'error':{'code':backend}}
    ui = GoogleVoiceUI(home, runner=lambda *args, **kwargs:
                       SimpleNamespace(stdout=json.dumps(response)))
    assert ui.snapshot()['evidence'] == expected


def test_hidden_native_controls_have_a_specific_remedy(configured):
    home, _ = configured
    response = {'status':'unavailable', 'identity_verified':False,
                'error':{'code':'ACTION_NOT_SUPPORTED',
                         'details':{'kind':'window_without_accessibility_element'}}}
    ui = GoogleVoiceUI(home, runner=lambda *args, **kwargs:
                       SimpleNamespace(stdout=json.dumps(response)))
    assert ui.snapshot()['evidence'] == 'google_voice_controls_unavailable'


def test_empty_gui_path_gets_system_fallback_without_mutating_parent(configured, monkeypatch):
    home, config = configured
    python = '/test/service/bin/python'
    monkeypatch.setattr(google_voice_ui.sys, 'executable', python)
    monkeypatch.delenv('PATH', raising=False)
    captured = []
    ui = GoogleVoiceUI(home, runner=lambda argv, **kwargs:
                       captured.append(kwargs['env']) or SimpleNamespace(stdout='{"ok":true}'))
    assert ui.command([config['desktop'], 'status']) == {'ok': True}
    assert captured[0]['PATH'].split(os.pathsep) == [
        '/test/service/bin', '/test/desktop tools', '/usr/bin', '/bin',
    ]
    assert 'PATH' not in os.environ


def overlay_page(opened=False):
    return node('webarea', 'Voice - Calls', [
        node('group', 'Audio settings panel') if opened else
        node('button', 'Audio settings', ref_id='audio-settings'),
    ])


def test_audio_open_waits_for_transient_paint_after_one_click(configured, monkeypatch):
    home, _ = configured
    ui = GoogleVoiceUI(home)
    monkeypatch.setattr(google_voice_ui.time, 'sleep', lambda _: None)
    observations = iter([False, False, False, True])
    clicks = []
    ui.raw = lambda **kwargs: (overlay_page(next(observations)), None, None)
    ui.click = lambda n: clicks.append(n['ref_id']) or {'ok': True}
    result = ui.audio_open()
    assert result['name'] == 'Audio settings panel'
    assert clicks == ['audio-settings']


def test_audio_open_does_not_toggle_again_when_paint_never_arrives(configured, monkeypatch):
    home, _ = configured
    ui = GoogleVoiceUI(home)
    monkeypatch.setattr(google_voice_ui.time, 'sleep', lambda _: None)
    clicks = []
    ui.raw = lambda **kwargs: (overlay_page(), None, None)
    ui.click = lambda n: clicks.append(n['ref_id']) or {'ok': True}
    with pytest.raises(UIError, match='audio_settings_did_not_open'):
        ui.audio_open()
    assert clicks == ['audio-settings']


@pytest.mark.parametrize('code', ['POLICY_DENIED', 'PERM_DENIED'])
def test_audio_open_stops_immediately_on_security_denial(configured, code):
    home, _ = configured
    ui = GoogleVoiceUI(home)
    reads, clicks = [], []
    ui.raw = lambda **kwargs: reads.append(True) or (overlay_page(), None, None)
    ui.click = lambda n: clicks.append(n['ref_id']) or {'ok': False, 'error_code': code}
    with pytest.raises(UIError, match=code):
        ui.audio_open()
    assert len(reads) == 1
    assert clicks == ['audio-settings']

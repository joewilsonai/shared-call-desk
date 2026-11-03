"""Offline audio UI regressions: every native command and observation is fake."""
import pytest

import google_voice_ui
from google_voice_ui import GoogleVoiceUI, UIError
from service import read_json, write_json
from audio_policy import VIRTUAL_ROUTES


def node(role, name='', children=None, **fields):
    return dict(role=role, name=name, children=children or [], **fields)


def panel(expanded=False):
    return node('group', 'Audio settings panel', [
        node('combobox', 'Microphone', value='Original mic', ref_id='mic',
             states=['expanded'] if expanded else []),
        node('combobox', 'Speakers', value='Original speakers', ref_id='speaker'),
    ])


def page(audio_panel=None, extras=None):
    return node('webarea', 'Voice - Calls',
                ([] if audio_panel is None else [audio_panel]) + (extras or []))


@pytest.fixture
def ui(tmp_path, monkeypatch):
    write_json(tmp_path/'google-voice-ui.json', {
        'app': 'Google Chrome', 'window_id': 'w-offline', 'pid': 1,
        'process_instance': 'offline-test',
        'url_prefix': 'https://voice.google.com/u/0/', 'account_label': 'Test account',
        'answer_label': 'Answer call', 'decline_label': 'Hang up call',
        'hangup_label': 'Hang up call',
    })
    instance = GoogleVoiceUI(tmp_path)
    def forbid_native(*args, **kwargs):
        raise AssertionError('A test attempted an unmocked native operation')
    monkeypatch.setattr(instance, 'command', forbid_native)
    monkeypatch.setattr(instance, 'raw', forbid_native)
    monkeypatch.setattr(google_voice_ui.time, 'sleep', lambda _: None)
    return instance


def command_words(args):
    """Ignore the supported global delivery flag when parsing a fake command."""
    return args[2:] if args[1] == '--headed' else args[1:]


class DelayedDialog:
    """A device list/dialog whose AX state changes several reads after Escape."""
    def __init__(self, with_list=False):
        self.phase = 'expanded' if with_list else 'panel'
        self.remaining = None
        self.destination = None
        self.commands = []
        self.reads_after_escape = 0

    def raw(self, **kwargs):
        if self.remaining is not None:
            self.reads_after_escape += 1
            self.remaining -= 1
            if self.remaining == 0:
                self.phase = self.destination
                self.remaining = None
        return page(None if self.phase == 'closed' else panel(self.phase == 'expanded')), None, None

    def command(self, args, **kwargs):
        action = command_words(args)[0]
        self.commands.append(action)
        if action == 'focus-window':
            return {'ok': True}
        assert action == 'press' and command_words(args)[1] == 'escape'
        assert self.remaining is None, 'Escape repeated before the prior state transition'
        assert self.phase != 'closed', 'Escape sent after dialog closed'
        self.destination = 'panel' if self.phase == 'expanded' else 'closed'
        self.remaining = 3
        return {'ok': True, 'data': {'disposition': {'delivery': 'delivered_unverified'}}}

    def focus(self):self.commands.append('focus_enrolled')


def test_dialog_close_waits_for_delayed_ax_without_repeating_escape(ui):
    fake = DelayedDialog()
    ui.raw, ui.command = fake.raw, fake.command
    ui.focus_enrolled=fake.focus
    ui.audio_close()
    assert fake.phase == 'closed'
    assert fake.commands == ['focus_enrolled', 'press']
    assert fake.reads_after_escape >= 3


def test_open_list_consumes_first_escape_before_dialog_escape(ui):
    fake = DelayedDialog(with_list=True)
    ui.raw, ui.command = fake.raw, fake.command
    ui.focus_enrolled=fake.focus
    ui.audio_close()
    assert fake.phase == 'closed'
    assert fake.commands == ['focus_enrolled', 'press', 'press']
    assert fake.reads_after_escape >= 6


def test_unchanged_dialog_is_not_blindly_escaped_twice(ui):
    ui.raw = lambda **kwargs: (page(panel()), None, None)
    commands = []
    ui.focus_enrolled=lambda:commands.append('focus_enrolled')
    ui.command = lambda args, **kwargs: commands.append(command_words(args)[0]) or {'ok': True}
    with pytest.raises(UIError, match='audio_panel_did_not_close'):
        ui.audio_close()
    assert commands == ['focus_enrolled', 'press']


@pytest.mark.parametrize('code', ['POLICY_DENIED', 'PERM_DENIED'])
@pytest.mark.parametrize('stage', ['focus_enrolled', 'press'])
def test_audio_close_stops_after_security_denial(ui, code, stage):
    ui.raw = lambda **kwargs: (page(panel()), None, None)
    commands = []
    def focus():
        commands.append('focus_enrolled')
        if stage=='focus_enrolled':raise UIError(code)
    ui.focus_enrolled=focus
    def command(args, **kwargs):
        commands.append(command_words(args)[0])
        return {'ok': False, 'error': {'code': code}} if command_words(args)[0] == stage else {'ok': True}
    ui.command = command
    with pytest.raises(UIError):
        ui.audio_close()
    assert commands == (['focus_enrolled'] if stage == 'focus_enrolled' else ['focus_enrolled', 'press'])


def option(name, ref):
    return node('button', name, ref_id=ref, available_actions=['Click'])


def test_device_choice_is_scoped_to_exact_list_and_exact_name(ui):
    wanted = 'Elle Call Mic (Virtual)'
    ui.audio_open = lambda: panel(expanded=True)
    choices = [
        option(wanted, 'outside-list'),
        node('list', 'Speakers', [option(wanted, 'wrong-list')]),
        node('list', 'Microphone', [
            option(wanted + ' extra', 'prefix-only'),
            option(wanted, 'right-choice'),
        ]),
    ]
    ui.raw = lambda **kwargs: (page(panel(expanded=True), choices), None, None)
    selected = []
    ui.click = lambda n: selected.append(n['ref_id']) or {'ok': True}
    ui.routes = lambda: {'Microphone': wanted if selected else 'Original mic'}
    ui.select_route('Microphone', wanted)
    assert selected == ['right-choice']


@pytest.mark.parametrize('case', ['duplicate-list', 'duplicate-option', 'prefix-only'])
def test_ambiguous_or_nonexact_device_choice_never_clicks(ui, case):
    wanted = 'Elle Call Mic (Virtual)'
    ui.audio_open = lambda: panel(expanded=True)
    candidates = [option(wanted, 'a')]
    if case == 'duplicate-option':
        candidates.append(option(wanted, 'b'))
    if case == 'prefix-only':
        candidates = [option(wanted + ' extra', 'prefix-only')]
    lists = [node('list', 'Microphone', candidates)]
    if case == 'duplicate-list':
        lists.append(node('list', 'Microphone', [option(wanted, 'other-list')]))
    ui.raw = lambda **kwargs: (page(panel(expanded=True), lists), None, None)
    clicked = []
    ui.click = lambda n: clicked.append(n)
    with pytest.raises(UIError, match='not_unique'):
        ui.select_route('Microphone', wanted)
    assert clicked == []


@pytest.mark.parametrize('code', ['POLICY_DENIED', 'PERM_DENIED'])
@pytest.mark.parametrize('stage', ['open-list', 'select-option'])
def test_device_selection_stops_after_security_denial(ui, code, stage):
    wanted = 'Elle Call Mic (Virtual)'
    ui.audio_open = lambda: panel(expanded=(stage == 'select-option'))
    ui.raw = lambda **kwargs: (page(panel(expanded=True), [
        node('list', 'Microphone', [option(wanted, 'choice')]),
    ]), None, None)
    clicked = []
    def click(n):
        clicked.append(n['ref_id'])
        return {'ok': False, 'error_code': code, 'delivery': 'not_delivered'}
    ui.click = click
    with pytest.raises(UIError, match=code):
        ui.select_route('Microphone', wanted)
    assert clicked == (['mic'] if stage == 'open-list' else ['choice'])


def test_original_routes_are_retained_before_partial_prepare_failure(ui):
    originals = {'Microphone': 'Original mic', 'Speakers': 'Original speakers'}
    ui.routes = lambda: dict(originals)
    applied = []
    def select(name, value):
        applied.append((name, value))
        if name == 'Speakers':
            raise UIError('speaker_selection_failed')
    ui.select_route = select
    with pytest.raises(UIError, match='speaker_selection_failed') as caught:
        ui.prepare_audio()
    assert caught.value.original_routes == originals
    saved = read_json(ui.home/'incoming-original-routes.json')
    assert saved['routes'] == originals
    assert saved['identity'] == ui._identity()
    assert applied[0] == ('Microphone', 'Elle Call Mic (Virtual)')
    assert len(applied) == 2


def test_preconfigured_virtual_defaults_are_verified_without_selection(ui):
    calls=[]
    ui.routes=lambda:calls.append('read') or dict(VIRTUAL_ROUTES)
    ui.audio_close=lambda:calls.append('close')
    ui.select_route=lambda *args:pytest.fail('Existing virtual defaults must not be reselected')
    receipt=ui.prepare_audio()
    assert receipt['verified'] is True and receipt['original_routes']==VIRTUAL_ROUTES
    assert calls==['read','close']
    assert read_json(ui.home/'incoming-original-routes.json')['routes']==VIRTUAL_ROUTES


@pytest.mark.parametrize('correct',[True,False])
def test_verify_audio_routes_never_changes_incorrect_or_correct_routes(ui,correct):
    actual=dict(VIRTUAL_ROUTES) if correct else {'Microphone':'Physical mic','Speakers':'Physical speakers'}
    ui.routes=lambda:actual
    closed=[];ui.audio_close=lambda:closed.append(True)
    ui.select_route=lambda *args:pytest.fail('Verification cannot select routes')
    receipt=ui.verify_audio_routes(VIRTUAL_ROUTES)
    assert receipt['verified'] is correct and receipt['actual_routes']==actual
    assert closed==[True]

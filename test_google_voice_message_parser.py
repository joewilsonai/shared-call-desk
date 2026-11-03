import copy
import json
from pathlib import Path
import pytest
from google_voice_message_parser import parse_native_inbox,parse_native_composer,walk,local_conversation_id
from messaging import TextError


@pytest.fixture
def snapshot():
    return json.loads((Path(__file__).parent/'fixtures/sms/inbox-native.json').read_text())


def parse(snapshot):
    return parse_native_inbox(snapshot,source_number='+15555550000',
        account_label='Google Account: Example Sender (sender@example.com), Google membership',
        url_prefix='https://voice.google.com/u/0/')


def row(snapshot):
    return next(node for node in walk(snapshot['data']['tree']) if node.get('role')=='checkbox' and node.get('name','').startswith('2 0 2'))


def test_actual_native_inbox_structure_parses_without_inventing_unread(snapshot):
    result=parse(snapshot)
    assert result['threads']==[{'conversation_id':local_conversation_id('+15555550000','+12025550186'),
        'recipient':'+12025550186','can_reply':True,'display_name':'+12025550186','preview':'You: Test',
        'timestamp':'Friday, May 15 2026, 3:26 AM','unread':None,'conversation_id_source':'local_source_recipient_key'}]
    assert result['unresolved_thread_count']==0
    assert set(result['controls'])=={'calls','messages','new_message'}


def test_native_refs_and_checkbox_selection_are_not_message_identity(snapshot):
    before=parse(snapshot)['threads']
    row(snapshot).update(ref_id='new-ephemeral-ref',value='1',states=['checked'])
    assert parse(snapshot)['threads']==before


@pytest.mark.parametrize('kind',['partial','account','origin'])
def test_incomplete_or_wrong_account_origin_rejected(snapshot,kind):
    if kind=='partial':snapshot['data']['complete']=False
    else:
        for node in walk(snapshot['data']['tree']):
            if kind=='account' and node.get('name','').startswith('Google Account:'):node['name']='Other account'
            if kind=='origin' and node.get('name')=='Address and search bar':node['value']='voice.google.com.evil.example/u/0/messages'
    with pytest.raises(TextError):parse(snapshot)


def test_contact_name_cannot_borrow_a_number_from_message_preview(snapshot):
    target=row(snapshot)
    label=next(n for n in target['children'] if n.get('role')=='statictext')
    label['value']=label['name']='Some Person'
    result=parse(snapshot)
    assert result['unresolved_thread_count']==1
    assert result['threads'][0]['display_name']=='Some Person'
    assert result['threads'][0]['recipient'] is None and result['threads'][0]['can_reply'] is False


def test_duplicate_recipient_rows_are_ambiguous(snapshot):
    target=next(n for n in walk(snapshot['data']['tree']) if n.get('role')=='list' and n.get('name')=='Latest messages')
    target['children'].append(copy.deepcopy(row(snapshot)))
    with pytest.raises(TextError,match='ambiguous'):parse(snapshot)

def test_conflicting_row_summary_and_child_recipient_are_not_actionable(snapshot):
    row(snapshot)['name']='2 0 2 5 5 5 0 1 9 9 . You: Test . Friday, May 15 2026, 3:26 AM .'
    result=parse(snapshot)['threads']
    assert len(result)==1 and result[0]['recipient'] is None and result[0]['can_reply'] is False

def composer(name):
    payload=json.loads((Path(__file__).parent/'fixtures/sms'/name).read_text())
    return payload,parse_native_composer(payload,source_number='+15555550000',
        account_label='Google Account: Example Sender (sender@example.com), Google membership',
        url_prefix='https://voice.google.com/u/0/')

@pytest.mark.parametrize('name',['new-native.json','recipient-native.json','suggestion-native.json'])
def test_uncommitted_typed_recipient_is_not_a_verified_recipient(name):
    _,parsed=composer(name)
    assert parsed['recipient'] is None and parsed['recipient_committed'] is False
    assert parsed['send_enabled'] is False and parsed['composer_body']==''

def test_one_recipient_row_with_duplicate_cells_is_one_recipient():
    payload,parsed=composer('thread-native.json')
    assert parsed['recipient']=='+12025550186' and parsed['recipient_committed']
    assert [m['direction'] for m in parsed['messages']]==['outgoing','incoming','outgoing']
    assert [m['body'] for m in parsed['messages']]==['test','testing','Test']
    assert parsed['messages'][0]['status']=='outgoing_observed'
    assert parse(payload)['threads'][0]['recipient']==parsed['recipient']

def test_filled_body_is_read_exactly_with_enabled_control():
    _,parsed=composer('filled-native.json')
    assert parsed['composer_body']=='Call Desk local draft check - not sent.'
    assert parsed['send_enabled'] is True

def test_multiple_recipient_rows_are_rejected():
    payload,_=composer('thread-native.json')
    group=next(n for n in walk(payload['data']['tree']) if n.get('name')=='Select recipients')
    existing=next(n for n in walk(group) if n.get('role')=='row')
    group['children'].append(copy.deepcopy(existing))
    with pytest.raises(TextError,match='group_messaging'):
        parse_native_composer(payload,source_number='+15555550000',account_label='Google Account: Example Sender (sender@example.com), Google membership',url_prefix='https://voice.google.com/u/0/')

@pytest.mark.parametrize('marker',['Sending...','Not sent','Failed to send'])
def test_pending_or_failed_indicator_prevents_outgoing_confirmation(marker):
    payload,_=composer('thread-native.json')
    details=next(n for n in walk(payload['data']['tree']) if n.get('name')=='Details')
    details['children'].append({'role':'statictext','name':marker,'value':marker})
    parsed=parse_native_composer(payload,source_number='+15555550000',account_label='Google Account: Example Sender (sender@example.com), Google membership',url_prefix='https://voice.google.com/u/0/')
    assert all(m['status']=='outgoing_unconfirmed' for m in parsed['messages'] if m['direction']=='outgoing')

def test_arbitrary_weekday_suffix_is_not_a_message_timestamp():
    payload,_=composer('thread-native.json')
    node=next(n for n in walk(payload['data']['tree']) if n.get('name','').startswith('Message from you,'))
    node['value']=node['name']='Message from you, test, Tuesday, not a date.'
    with pytest.raises(TextError,match='timestamp'):
        parse_native_composer(payload,source_number='+15555550000',account_label='Google Account: Example Sender (sender@example.com), Google membership',url_prefix='https://voice.google.com/u/0/')


def normal_thread():
    payload,_=composer('thread-native.json')
    details=next(n for n in walk(payload['data']['tree']) if n.get('name')=='Details' and n.get('role')=='group')
    for node in list(walk(details)):
        if 'children' in node:node['children']=[n for n in node['children'] if n.get('name')!='Select recipients']
    details['children'][:0]=[{'role':'heading','name':'Messages with 2 0 2 5 5 5 0 1 8 6'},
                            {'role':'button','name':'Call 2 0 2 5 5 5 0 1 8 6'}]
    return payload


def test_post_send_normal_header_verifies_number_without_editable_chips():
    result=parse_native_composer(normal_thread(),source_number='+15555550000',
        account_label='Google Account: Example Sender (sender@example.com), Google membership',url_prefix='https://voice.google.com/u/0/')
    assert result['recipient']=='+12025550186' and result['messages']


def test_normal_header_cannot_borrow_conflicting_call_number():
    payload=normal_thread()
    next(n for n in walk(payload['data']['tree']) if n.get('name')=='Call 2 0 2 5 5 5 0 1 8 6')['name']='Call 2 0 2 5 5 5 0 1 9 9'
    with pytest.raises(TextError,match='recipient'):
        parse_native_composer(payload,source_number='+15555550000',account_label='Google Account: Example Sender (sender@example.com), Google membership',url_prefix='https://voice.google.com/u/0/')

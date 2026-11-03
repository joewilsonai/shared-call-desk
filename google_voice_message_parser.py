"""Pure parsers for observed Google Voice messaging accessibility fixtures.

These parsers do not authenticate native process identity or the source line.
The adapter must independently verify both before wrapping parsed data in a
fresh observation. Native refs are transient and never conversation identity.
"""
import re
from datetime import datetime
from urllib.parse import urlparse,parse_qs
from messaging import TextError,digest,phone


def walk(node):
    if isinstance(node,dict):
        yield node
        for child in node.get('children',[]):yield from walk(child)


def recipient_number(value):
    if not isinstance(value,str) or not re.fullmatch(r'[+()0-9 .-]+',value.strip()):return None
    digits=re.sub(r'[^0-9]','',value)
    if len(digits)==10:return '+1'+digits
    if len(digits)==11 and digits.startswith('1'):return '+'+digits
    if value.strip().startswith('+') and 8<=len(digits)<=15:return '+'+digits
    return None


def local_conversation_id(source_number,recipient):
    return 'sms_'+digest([phone(source_number),phone(recipient)])[:32]


def full_timestamp(value):
    if not isinstance(value,str):return False
    try:parsed=datetime.strptime(value,'%A, %B %d %Y, %I:%M %p')
    except ValueError:return False
    return value.split(',',1)[0]==parsed.strftime('%A')


def native_message_page(payload,*,account_label,url_prefix,section='messages'):
    if not isinstance(payload,dict) or payload.get('ok') is not True:
        raise TextError('message_native_snapshot_failed')
    data=payload.get('data',{})
    if data.get('complete') is not True:raise TextError('message_native_snapshot_incomplete')
    nodes=list(walk(data.get('tree')))
    urls=[node.get('value') for node in nodes if node.get('name')=='Address and search bar']
    if len(urls)!=1 or not isinstance(urls[0],str):raise TextError('message_origin_unverified')
    actual=urlparse(urls[0] if '://' in urls[0] else 'https://'+urls[0]);expected=urlparse(url_prefix)
    slot=re.match(r'^(/u/[0-9]+/)',expected.path)
    if (actual.scheme!='https' or actual.netloc!='voice.google.com' or expected.netloc!=actual.netloc
            or not slot or actual.path!=slot[1]+section):
        raise TextError('message_origin_changed')
    pages=[node for node in nodes if node.get('role')=='webarea' and node.get('name')=='Voice - '+section.title()]
    if len(pages)!=1:raise TextError('message_page_ambiguous')
    web=list(walk(pages[0]))
    if sum(node.get('role')=='button' and node.get('name')==account_label for node in web)!=1:
        raise TextError('message_account_unverified')
    return pages[0],actual


def parse_native_source_line(payload,*,account_label,url_prefix):
    page,_=native_message_page(payload,account_label=account_label,url_prefix=url_prefix,section='settings')
    groups=[node for node in walk(page) if node.get('role')=='group' and node.get('name')=='Account']
    if len(groups)!=1:raise TextError('source_line_account_region_unverified')
    fields=[node for node in walk(groups[0]) if node.get('role') in ('statictext','button')]
    anchors=[i for i,node in enumerate(fields) if node.get('role')=='statictext' and node.get('name')=='Google Voice number']
    if len(anchors)!=1:raise TextError('source_line_heading_ambiguous')
    adjacent=fields[anchors[0]+1:anchors[0]+3]
    if len(adjacent)!=2 or adjacent[0].get('role')!='statictext' or adjacent[1].get('name')!='Copy phone number':
        raise TextError('source_line_region_unverified')
    number=recipient_number(adjacent[0].get('value',adjacent[0].get('name')))
    if number is None:raise TextError('source_line_number_unverified')
    closes=[node for node in walk(page) if node.get('role')=='button' and node.get('name')=='Close']
    if len(closes)!=1:raise TextError('settings_close_ambiguous')
    return {'voice_number':number,'close_ref':closes[0].get('ref_id')}


def parse_native_inbox(payload,*,source_number,account_label,url_prefix):
    page,_=native_message_page(payload,account_label=account_label,url_prefix=url_prefix)
    web=list(walk(page))
    detail_nodes={id(child) for node in web if node.get('role')=='group' and node.get('name')=='Details' for child in walk(node)}
    lists=[node for node in web if id(node) not in detail_nodes and node.get('role')=='list' and node.get('name')=='Latest messages']
    if len(lists)!=1:raise TextError('message_inbox_list_ambiguous')
    result=[];seen=set();unresolved=0
    for row in walk(lists[0]):
        if row.get('role')!='checkbox':continue
        # The observed recipient is its own direct text child. Never extract
        # a phone-shaped string from a preview, aggregate label, or history.
        labels=[node.get('value',node.get('name')) for node in row.get('children',[]) if node.get('role')=='statictext']
        recipient=recipient_number(labels[0]) if len(labels)==1 else None
        aggregate_recipient=recipient_number(str(row.get('name','')).split(' . ',1)[0])
        if aggregate_recipient!=recipient:recipient=None
        if recipient is not None and recipient in seen:raise TextError('message_recipient_row_ambiguous')
        groups=[node for node in row.get('children',[]) if node.get('role')=='group' and node.get('name')]
        stamps=[node.get('value',node.get('name')) for node in walk(row) if node.get('role')=='statictext'
                and re.match(r'^(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday), ',str(node.get('value',node.get('name',''))))]
        if recipient is None:unresolved+=1
        else:seen.add(recipient)
        display=recipient or (labels[0] if len(labels)==1 and isinstance(labels[0],str) else 'Unresolved conversation')
        identifier=(local_conversation_id(source_number,recipient) if recipient else
                    'sms_unresolved_'+digest([source_number,row.get('name'),len(result)])[:24])
        result.append({'conversation_id':identifier,'recipient':recipient,'can_reply':recipient is not None,
                       'display_name':display,'preview':groups[0]['name'] if len(groups)==1 else '',
                       'timestamp':stamps[0] if len(stamps)==1 and full_timestamp(stamps[0]) else None,
                       'unread':None,'conversation_id_source':'local_source_recipient_key' if recipient else 'local_unresolved_row_key'})
    controls={}
    for label,role,key in [('Calls','radiobutton','calls'),('Messages','radiobutton','messages'),('Send new message','button','new_message')]:
        matches=[node for node in web if node.get('name')==label and node.get('role')==role]
        if len(matches)==1 and isinstance(matches[0].get('ref_id'),str):controls[key]=matches[0]['ref_id']
    return {'view':'inbox','threads':result,'scope':'observed_threads_only',
            'unresolved_thread_count':unresolved,'controls':controls,'snapshot_id':payload['data'].get('snapshot_id')}


def parse_native_composer(payload,*,source_number,account_label,url_prefix):
    page,url=native_message_page(payload,account_label=account_label,url_prefix=url_prefix)
    details=[node for node in walk(page) if node.get('role')=='group' and node.get('name')=='Details']
    if len(details)!=1:raise TextError('message_details_ambiguous')
    nodes=list(walk(details[0]))
    selection=[node for node in nodes if node.get('role')=='group' and node.get('name')=='Select recipients']
    if len(selection)>1:raise TextError('message_recipients_unverified')
    rows=[node for node in walk(selection[0]) if node.get('role')=='row'] if selection else []
    if len(rows)>1:raise TextError('group_messaging_not_supported')
    recipient=None
    if rows:
        recipient=recipient_number(str(rows[0].get('name','')).split(' Remove ',1)[0])
        values={recipient_number(node.get('name')) for node in rows[0].get('children',[]) if node.get('role')=='cell' and node.get('name')}
        if recipient is None or values!={recipient}:raise TextError('message_recipient_chip_unverified')
    if not selection:
        headings=[node.get('name','')[len('Messages with '):] for node in nodes
                  if node.get('role')=='heading' and node.get('name','').startswith('Messages with ')]
        calls=[node.get('name','')[len('Call '):] for node in nodes
               if node.get('role')=='button' and node.get('name','').startswith('Call ')]
        if len(headings)!=1 or len(calls)!=1:raise TextError('message_recipients_unverified')
        recipient=recipient_number(headings[0])
        if recipient is None or recipient_number(calls[0])!=recipient:raise TextError('message_recipient_chip_unverified')
    fields=[node for node in nodes if node.get('role')=='textfield'  and node.get('name')=='Type a message']
    sends=[node for node in nodes if node.get('role')=='button' and node.get('name')=='Send message']
    if len(fields)!=1 or len(sends)!=1:raise TextError('message_composer_ambiguous')
    body=fields[0].get('value','')
    if not isinstance(body,str):raise TextError('message_composer_value_unavailable')
    lists=[node for node in nodes if node.get('role')=='list' and node.get('name')=='Latest messages']
    if len(lists)>1:raise TextError('message_history_ambiguous')
    messages=[];seen=set()
    pending_or_failed=any(str(node.get('value',node.get('name',''))).strip().lower() in {
        'sending','sending...','sending…','not sent','message not sent','failed to send','couldn’t send',"couldn't send",'retry'} for node in nodes)
    for node in walk(lists[0]) if lists else []:
        label=node.get('value',node.get('name',''))
        if node.get('role')!='statictext' or not isinstance(label,str) or not label.startswith('Message from '):continue
        match=re.fullmatch(r'Message from (you|[+()0-9 .-]+), (.*), ((?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday), .+)\.',label,re.DOTALL)
        if not match or recipient is None:raise TextError('message_history_label_unverified')
        sender,text_body,stamp=match.groups()
        if not full_timestamp(stamp):raise TextError('message_timestamp_unverified')
        if sender!='you' and recipient_number(sender)!=recipient:raise TextError('message_history_sender_mismatch')
        direction='outgoing' if sender=='you' else 'incoming'
        identifier='smsmsg_'+digest([source_number,recipient,direction,text_body,stamp])[:32]
        if identifier in seen:raise TextError('ambiguous_message_identity')
        seen.add(identifier)
        messages.append({'message_id':identifier,'direction':direction,'body':text_body,'timestamp':stamp,
                         'status':('outgoing_unconfirmed' if pending_or_failed else 'outgoing_observed') if direction=='outgoing' else 'received'})
    inputs=[node for node in nodes if node.get('role')=='textfield' and node.get('name')=='Type a name or phone number']
    suggestions=[node for node in walk(page) if node.get('role')=='button' and node.get('name','').startswith('Send to ')
                 and recipient_number(node['name'][len('Send to '):]) is not None]
    return {'view':'conversation','recipient':recipient,'recipient_committed':recipient is not None,
            'conversation_id':local_conversation_id(source_number,recipient) if recipient else None,
            'provider_thread_id':parse_qs(url.query).get('itemId',[None])[0],
            'composer_body':body,'send_enabled':'disabled' not in sends[0].get('states',[]),
            'messages':messages,'scope':'observed_messages_only',
            'controls':{'body':fields[0].get('ref_id'),'send':sends[0].get('ref_id'),
                        'recipient':inputs[0].get('ref_id') if len(inputs)==1 else None,
                        'recipient_value':inputs[0].get('value','') if len(inputs)==1 else None,
                        'suggestions':[{'recipient':recipient_number(node['name'][len('Send to '):]),'ref_id':node.get('ref_id')} for node in suggestions]}}

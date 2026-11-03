"""Durable, same-user exclusion for browser work that can leave the Calls page.

Lock order is incoming-control.lock, then registry.lock. Never hold registry.lock
across native UI. Expiry or a dead owner is not evidence that navigation finished.
"""
from __future__ import annotations
import contextlib
import copy
import fcntl
import hashlib
import json
import os
import re
import uuid
from service import CallError,read_json,now,text_value
from audio_policy import lease_released

IDENTITY_FIELDS=('app','window_id','pid','process_instance','url_prefix','account_label')


def require_browser_available(registry):
    operation=registry.get('browser_operation')
    if operation is not None:
        owner=operation.get('owner','unknown') if isinstance(operation,dict) else 'unknown'
        raise CallError('Browser is reserved by '+str(owner)+'; verify navigation cleanup before another operation')


def _watcher_running(home):
    fd=os.open(home/'incoming.lock',os.O_CREAT|os.O_RDWR,0o600)
    try:
        try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return True
        return False
    finally:os.close(fd)


def _identity(value):
    if not isinstance(value,dict):raise CallError('Exact enrolled browser identity is required')
    result={k:value.get(k) for k in IDENTITY_FIELDS}
    if (result['app']!='Google Chrome' or not isinstance(result['window_id'],str)
            or not re.fullmatch(r'w-[1-9][0-9]*',result['window_id'])
            or type(result['pid']) is not int or result['pid']<=0
            or any(not isinstance(result[k],str) or not result[k].strip()
                   for k in ('process_instance','url_prefix','account_label'))):
        raise CallError('Exact enrolled browser identity is invalid')
    return result


class BrowserOperation:
    def __init__(self,service):
        self.service=service
        self._session_held=False

    @contextlib.contextmanager
    def session_lock(self):
        if self._session_held:raise CallError('Browser session lock is already held')
        fd=os.open(self.service.home/'incoming-control.lock',os.O_CREAT|os.O_RDWR,0o600)
        try:
            try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:
                raise CallError('Browser setup, cleanup, or messaging is already in progress') from None
            self._session_held=True
            try:yield
            finally:self._session_held=False
        finally:os.close(fd)

    def _held(self):
        if not self._session_held:raise CallError('Hold session_lock across browser reservation and UI cleanup')

    def acquire(self,owner,request_key,*,kind='sms',identity):
        self._held()
        if kind!='sms':raise CallError('Unsupported browser operation kind')
        owner=text_value(owner,'owner',120);request_key=text_value(request_key,'request_key',200)
        identity=_identity(identity)
        key=hashlib.sha256((owner+'\0'+request_key).encode()).hexdigest()
        digest=hashlib.sha256(json.dumps({'owner':owner,'request_key':request_key,'kind':kind,'identity':identity},sort_keys=True).encode()).hexdigest()
        with self.service.locked() as registry:
            if registry.get('active'):raise CallError('Call desk is reserved; live messaging must wait')
            incoming=read_json(self.service.home/'incoming-settings.json',{})
            lease=read_json(self.service.home/'incoming-audio-lease.json',{})
            current=read_json(self.service.home/'incoming-state.json',{}).get('current')
            if incoming.get('enabled') or _watcher_running(self.service.home) or current or (lease and not lease_released(lease)):
                raise CallError('Incoming answering or audio cleanup owns the browser; live messaging must wait')
            if identity!=_identity(read_json(self.service.home/'google-voice-ui.json',{})):
                raise CallError('Browser enrollment changed before reservation')
            active=registry.get('browser_operation')
            if active is not None:
                if isinstance(active,dict) and active.get('key')==key and active.get('request_hash')==digest:
                    return copy.deepcopy(active)|{'resumed':True}
                require_browser_available(registry)
            history=registry.setdefault('browser_operations',{})
            if any(item.get('key')==key for item in history.values() if isinstance(item,dict)):
                raise CallError('Browser request key was already released; use a new key for new navigation')
            operation={'token':'browser_'+uuid.uuid4().hex,'owner':owner,'request_key':request_key,
                       'key':key,'request_hash':digest,'kind':kind,'identity':identity,
                       'status':'active','created_at':now()}
            registry['browser_operation']=operation
            history[operation['token']]=copy.deepcopy(operation)
            return copy.deepcopy(operation)|{'resumed':False}

    def release(self,token,owner,identity,restoration_evidence):
        """Record independently verified return to Calls/idle, never mere expiry.

        The UI adapter must establish page/account/identity and idle before this
        call. This method records that receipt; it does not inspect a browser.
        """
        self._held()
        identity=_identity(identity)
        evidence=text_value(restoration_evidence,'restoration_evidence',3000)
        with self.service.locked() as registry:
            operation=registry.get('browser_operation')
            if not isinstance(operation,dict) or operation.get('token')!=token or operation.get('owner')!=owner:
                raise CallError('Browser cleanup token or owner does not match the active reservation')
            if operation.get('identity')!=identity or identity!=_identity(read_json(self.service.home/'google-voice-ui.json',{})):
                raise CallError('Browser cleanup identity differs from the reserved enrollment')
            if registry.get('active'):
                raise CallError('A call is still reserved; inspect the conflicting browser ownership')
            released=copy.deepcopy(operation)|{'status':'released','released_at':now(),
                       'restoration_evidence':evidence,'restoration_source':'operator_verified_calls_idle'}
            registry.setdefault('browser_operations',{})[token]=released
            registry['browser_operation']=None
            return copy.deepcopy(released)

    def status(self):
        with self.service.locked() as registry:
            return copy.deepcopy(registry.get('browser_operation'))


@contextlib.contextmanager
def navigation_guard(service):
    """Exclude live Show/Diagnose from SMS; no effect on cached status or Stop."""
    browser=BrowserOperation(service)
    with browser.session_lock():
        with service.locked() as registry:require_browser_available(registry)
        yield

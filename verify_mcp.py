"""Harmless real stdio handshake + lifecycle in an isolated test state directory."""
import asyncio,json,os,tempfile
from pathlib import Path
from mcp.client import ClientSession
from mcp.client.stdio import StdioServerParameters,stdio_client

ROOT=Path(__file__).resolve().parent

def data(result):
    if result.is_error: raise RuntimeError(str(result.content))
    if result.structured_content: return result.structured_content
    return json.loads(next(item.text for item in result.content if item.type=='text'))

async def main():
    with tempfile.TemporaryDirectory(prefix='call-desk-smoke-') as state:
        # Synthetic source identity only for local draft/cache verification.
        # No live adapter is constructed and no message is sent.
        (Path(state)/'google-voice-ui.json').write_text(json.dumps({
            'app':'Google Chrome','window_id':'w-1','pid':123,'process_instance':'fixture',
            'url_prefix':'https://voice.google.com/u/0/','account_label':'Fixture account',
            'voice_number':'+15555550100'}))
        params=StdioServerParameters(command=str(ROOT/'.venv/bin/python'),args=[str(ROOT/'server.py')],env={**os.environ,'ELLE_CALL_STATE_DIR':state})
        async with stdio_client(params) as (read,write):
            async with ClientSession(read,write) as session:
                await session.initialize()
                names=[t.name for t in (await session.list_tools()).tools]
                preflight=data(await session.call_tool('call_preflight',{}))
                prepared=data(await session.call_tool('call_prepare',{'owner':'protocol-test','request_key':'offline-test','recipient':'+15555550123','brief':'Offline protocol test only; do not dial.','authorization':'Local test; no external action authorized.','other_spend_today_usd':0,'max_seconds':30}))
                c=prepared['call_id']
                await session.call_tool('call_stop',{'call_id':c})
                finished=data(await session.call_tool('call_finish',{'call_id':c,'owner':'protocol-test','hangup_evidence':'Test did not open or dial a browser.','restoration_evidence':'Audio devices never changed; no worker launched.','outcome':'Harmless MCP protocol test completed.'}))
                assert finished['phase']=='finished' and finished['actual_cost_usd']==0
                inbox=data(await session.call_tool('text_inbox',{}))
                assert inbox['source']=='cached' and inbox['threads']==[]
                draft=data(await session.call_tool('text_draft',{'owner':'protocol-test','request_key':'draft-local-only','recipient':'+15555550123','body':' Local draft only.\nDo not send. '}))
                receipt=data(await session.call_tool('text_status',{'draft_id':draft['draft_id']}))
                assert receipt['phase']=='draft' and receipt['body']==' Local draft only.\nDo not send. '
                # Synthetic unresolved send in isolated fixture state only.
                from messaging import TextService
                from service import CallService,write_json
                local=TextService(CallService(home=state))
                fixture=local.get(draft['draft_id'])
                fixture.update(phase='send_unknown',send_intent={'conversation_id':'fixture-thread','baseline_message_ids':['baseline']})
                write_json(local.path(draft['draft_id']),fixture)
                next_draft=data(await session.call_tool('text_draft',{'owner':'protocol-test','request_key':'blocked-local-only','recipient':'+15555550123','body':'Second synthetic draft; never send.'}))
                blocked=data(await session.call_tool('text_send',{'draft_id':next_draft['draft_id'],'owner':'protocol-test','expected_digest':next_draft['digest'],'authorization':'Offline fixture only; no external action.'}))
                assert blocked['ok'] is False and blocked['error']['code']=='prior_send_outcome_unresolved'
                assert blocked['error']['blocking_draft_id']==draft['draft_id']
                confirmed=data(await session.call_tool('text_confirm_sent',{'draft_id':draft['draft_id'],'owner':'protocol-test','expected_digest':draft['digest'],'confirmation_kind':'recipient_confirmation','evidence':'Synthetic offline fixture confirmation; no actual text sent or recipient contacted.'}))
                assert confirmed['phase']=='sent_confirmed' and confirmed['confirmation']['evidence_source']=='operator_reported'
                result={'tool_names':names,'tool_count':len(names),'stdio_handshake':'passed','lifecycle':'prepare-stop-finish passed in isolated temporary state','messaging_lifecycle':'cached draft/status, structured blocking error and audited confirmation passed in isolated temporary state','devices':preflight['devices'],'provider_sessions_started':0,'browser_calls_placed':0,'text_messages_sent':0,'cost_usd':0}
                (ROOT/'mcp-verification.json').write_text(json.dumps(result,indent=2)+'\n')
                print(json.dumps(result,indent=2))

if __name__=='__main__': asyncio.run(main())

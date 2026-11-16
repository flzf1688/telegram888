import os, asyncio, sqlite3, io, types
import pytest
from fastapi.testclient import TestClient
from app.core import query, normalize_rows, seal, unseal, queue_message, recover_pending
from app.main import app, runtime, failures
from app.runtime import Runtime

ORIGIN='https://104.168.91.152:8444'
@pytest.fixture
def client(monkeypatch):
    async def noop():pass
    monkeypatch.setattr(runtime,'start',noop);monkeypatch.setattr(runtime,'stop',noop)
    failures.clear()
    with TestClient(app,base_url=ORIGIN,headers={'Origin':ORIGIN,'X-Requested-With':'telegram888'}) as c:
        query('TRUNCATE admins,web_sessions,settings,groups,proxies,accounts,contacts,imports,conversations,messages,campaigns,outbox,ai_runs,audit RESTART IDENTITY CASCADE')
        query("INSERT INTO groups(name) VALUES ('默认分组')")
        b={'username':'testadmin','password':'isolated-test-password-2026','setup_code':os.environ['SETUP_CODE']}
        assert c.post('/api/setup',json=b).status_code==200
        assert c.post('/api/auth',json=b).status_code==200
        yield c

def fake_account():
    return query("INSERT INTO accounts(name,session,fingerprint,status,enabled,checked_at) VALUES ('test',%s,'test-key','online',true,now()) RETURNING id",(seal('not-a-real-session'),),one=True)['id']
def conversation(aid):
    return query("INSERT INTO conversations(account_id,peer_id,name,mode,ai_allowed,version) VALUES (%s,12345,'test','agent',true,1) RETURNING id",(aid,),one=True)['id']

def test_auth_csrf_and_single_setup(client):
    assert client.get('/api/dashboard').status_code==200
    assert client.post('/api/groups',json={'name':'x'},headers={'Origin':'https://evil.invalid'}).status_code==403
    assert client.post('/api/setup',json={'username':'another','password':'a-long-test-password','setup_code':os.environ['SETUP_CODE']}).status_code==409
    client.post('/api/logout')
    assert client.get('/api/contacts').status_code==401

def test_import_preview_commit_idempotency(client):
    data='姓名,电话号码,公司,用户名\nAlice,+14155552671,Example,alice_demo\nDuplicate,+14155552671,Example,\nBad,123,Example,\n'
    r=client.post('/api/import/preview',data={'group_id':'1'},files={'file':('contacts.csv',data.encode(),'text/csv')})
    assert r.status_code==200,r.text
    assert r.json()['valid']==1 and len(r.json()['errors'])==2
    token=r.json()['id']
    assert client.post('/api/import/'+token+'/confirm').json()['imported']==1
    assert client.post('/api/import/'+token+'/confirm').status_code==409
    d=client.get('/api/contacts').json();assert d['total']==1 and d['rows'][0]['consent'] is False
    r=client.post('/api/import/preview',data={'group_id':'1'},files={'file':('contacts.csv',data.encode(),'text/csv')})
    assert r.json()['valid']==0 and r.json()['duplicates']==1

def test_xlsx_and_groups(client):
    from openpyxl import Workbook
    wb=Workbook();ws=wb.active;ws.append(['姓名','电话号码']);ws.append(['测试','+442079460123']);b=io.BytesIO();wb.save(b)
    assert client.post('/api/groups',json={'name':'欧洲'}).status_code==200
    assert client.post('/api/groups',json={'name':'欧洲'}).status_code==409
    r=client.post('/api/import/preview',data={'group_id':'2'},files={'file':('x.xlsx',b.getvalue())})
    assert r.status_code==200 and r.json()['valid']==1

def test_settings_encrypt_and_gate(client):
    assert client.put('/api/settings',json={'agent_enabled':True}).status_code==422
    b={'openai_key':'fake-key-not-live','model':'test-model','knowledge':'own-product','main_username':'@support_demo','agent_enabled':False}
    assert client.put('/api/settings',json=b).status_code==200
    d=client.get('/api/settings').json();assert 'openai_key' not in d and d['openai_key_configured']
    raw=query("SELECT value FROM settings WHERE key='openai_key'",one=True)['value'];assert 'fake-key' not in raw and unseal(raw)=='fake-key-not-live'
    b['openai_key']='';assert client.put('/api/settings',json=b).json()['openai_key_configured']

def test_session_invalid_and_duplicate(client,tmp_path):
    r=client.post('/api/accounts/import',data={'name':'bad'},files={'file':('x.zip',b'no')});assert r.status_code==422
    p=tmp_path/'valid.session';c=sqlite3.connect(p);c.execute('CREATE TABLE sessions(dc_id integer,server_address text,port integer,auth_key blob)');c.execute('INSERT INTO sessions VALUES (2,?,443,?)',('149.154.167.51',bytes(range(256))));c.commit();c.close()
    data=p.read_bytes()
    r=client.post('/api/accounts/import',data={'name':'synthetic'},files={'file':('x.session',data)});assert r.status_code==200,r.text
    assert client.post('/api/accounts/import',data={'name':'duplicate'},files={'file':('x.session',data)}).status_code==409
    row=client.get('/api/accounts').json()[0];assert row['enabled'] is False and row['status']=='imported' and 'session' not in row
    assert client.post('/api/accounts/1/control',json={'enabled':True,'other_connections_stopped':True}).status_code==422

def test_proxy_private_address_rejected(client):
    for host in ['127.0.0.1','169.254.169.254','::1','10.0.0.1']:
        assert client.post('/api/proxies',json={'name':'test','host':host,'port':1080}).status_code==422

def test_campaign_consent_and_paused_creation(client):
    aid=fake_account();body={'name':'campaign','group_id':1,'account_id':aid,'body':'Requested catalog','interval_seconds':300}
    assert client.post('/api/campaigns',json=body).status_code==422
    contact={'name':'Alice','phone':'+14155552671','username':'alice_demo','group_id':1,'consent':False}
    client.post('/api/contacts',json=contact)
    assert client.post('/api/campaigns',json=body).status_code==422
    contact['consent']=True;assert client.patch('/api/contacts/1',json=contact).status_code==200
    r=client.post('/api/campaigns',json=body);assert r.json()['state']=='paused' and r.json()['recipients']==1
    contact['optout']=True;client.patch('/api/contacts/1',json=contact)
    assert query('SELECT status FROM outbox',one=True)['status']=='cancelled'

class FakeTelegram:
    def __init__(self,callback=None):self.sent=[];self.callback=callback
    async def get_input_entity(self,peer):
        if self.callback:self.callback()
        return peer
    async def __call__(self,request):self.sent.append(request);return types.SimpleNamespace(id=9001,updates=[])
class Owner:
    def execute(self,*a):return None

def test_delivery_ack_and_operation_dedup(client):
    aid=fake_account();cid=conversation(aid);r=Runtime();c=FakeTelegram();r.clients[aid]=c;r.owners[aid]=Owner()
    q=queue_message(aid,12345,'hello',cid,operation_id='operation-fixed')
    queue_message(aid,12345,'hello',cid,operation_id='operation-fixed')
    asyncio.run(r.deliver());asyncio.run(r.deliver())
    assert len(c.sent)==1
    assert query('SELECT status,tg_id FROM outbox WHERE id=%s',(q,),one=True)=={'status':'sent','tg_id':9001}

def test_inflight_takeover_cancels_agent(client):
    aid=fake_account();cid=conversation(aid);query("INSERT INTO settings VALUES ('agent_enabled','true')")
    r=Runtime();c=FakeTelegram(lambda:query("UPDATE conversations SET mode='human',version=version+1 WHERE id=%s",(cid,)));r.clients[aid]=c;r.owners[aid]=Owner()
    q=queue_message(aid,12345,'generated',cid,'agent',1)
    asyncio.run(r.deliver());assert not c.sent
    assert query('SELECT status FROM outbox WHERE id=%s',(q,),one=True)['status']=='cancelled'

def test_restart_unknown_does_not_retry(client):
    aid=fake_account();q=queue_message(aid,12345,'maybe delivered');query("UPDATE outbox SET status='sending' WHERE id=%s",(q,));recover_pending()
    r=Runtime();c=FakeTelegram();r.clients[aid]=c;r.owners[aid]=Owner();asyncio.run(r.deliver())
    assert not c.sent and query('SELECT status FROM outbox WHERE id=%s',(q,),one=True)['status']=='uncertain'

def test_stale_online_degrades(client):
    aid=fake_account();query("UPDATE accounts SET checked_at=now()-interval '10 minutes' WHERE id=%s",(aid,))
    assert client.get('/api/accounts').json()[0]['status']=='unverified'

def test_normalize_international_numbers():
    rows,errors=normalize_rows([{'phone':'00 44 20 7946 0123'},{'phone':'123'}]);assert rows[0]['phone']=='+442079460123' and len(errors)==1

def test_agent_handoff_uses_configured_target(client,monkeypatch):
    import app.runtime as mod
    aid=fake_account();cid=conversation(aid)
    for k,v in {'agent_enabled':'true','openai_key':seal('synthetic-key'),'model':'synthetic-model','knowledge':'Product catalog','main_username':'trusted_support'}.items():query('INSERT INTO settings VALUES (%s,%s)',(k,v))
    query("INSERT INTO messages(conversation_id,tg_id,outgoing,text) VALUES (%s,1,false,'Please quote 200 units')",(cid,))
    class FakeAI:
        def __init__(self,**kwargs):self.responses=self
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def create(self,**kwargs):
            assert kwargs['store'] is False and kwargs['tool_choice']['name']=='submit_reply'
            return types.SimpleNamespace(output=[types.SimpleNamespace(type='function_call',name='submit_reply',arguments='{"action":"handoff","text":"Our sales colleague can confirm the price."}')],usage=types.SimpleNamespace(input_tokens=30,output_tokens=20))
    monkeypatch.setattr(mod,'AsyncOpenAI',FakeAI)
    asyncio.run(Runtime().auto_reply(cid))
    row=query('SELECT body,status FROM outbox',one=True)
    assert row['body'].endswith('https://t.me/trusted_support') and row['status']=='queued'
    assert query('SELECT stage FROM conversations WHERE id=%s',(cid,),one=True)['stage']=='new'

def test_agent_stale_generation_is_discarded(client,monkeypatch):
    import app.runtime as mod
    aid=fake_account();cid=conversation(aid)
    for k,v in {'agent_enabled':'true','openai_key':seal('synthetic-key'),'model':'synthetic-model','knowledge':'Product catalog','main_username':'trusted_support'}.items():query('INSERT INTO settings VALUES (%s,%s)',(k,v))
    class FakeAI:
        def __init__(self,**kwargs):self.responses=self
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def create(self,**kwargs):
            query("UPDATE conversations SET mode='human',version=version+1 WHERE id=%s",(cid,))
            return types.SimpleNamespace(output=[types.SimpleNamespace(type='function_call',name='submit_reply',arguments='{"action":"reply","text":"Late reply"}')],usage=None)
    monkeypatch.setattr(mod,'AsyncOpenAI',FakeAI)
    asyncio.run(Runtime().auto_reply(cid))
    assert not query('SELECT id FROM outbox')
    assert query('SELECT status FROM ai_runs',one=True)['status']=='cancelled'

import asyncio, io, csv, json, os, re, secrets, tempfile, sqlite3, ipaddress, socket, zipfile, datetime
from pathlib import Path
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, HTTPException, UploadFile, File, Form
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from psycopg.types.json import Jsonb
from openpyxl import load_workbook
from telethon.sessions import StringSession
from telethon.crypto import AuthKey
from python_socks.async_.asyncio import Proxy
from python_socks import ProxyType
from openai import AsyncOpenAI
from .core import *
from .runtime import runtime
BASE=Path(__file__).parent
ORIGIN=os.environ.get('PUBLIC_ORIGIN','https://104.168.91.152:8444')
failures={}
@asynccontextmanager
async def lifespan(app):
    query((BASE/'schema.sql').read_text())
    await runtime.start()
    yield
    await runtime.stop()
app=FastAPI(lifespan=lifespan,docs_url=None,redoc_url=None,openapi_url=None)

@app.middleware('http')
async def security(request:Request,call_next):
    path=request.url.path
    if request.method not in ('GET','HEAD','OPTIONS'):
        if request.headers.get('origin')!=ORIGIN or request.headers.get('x-requested-with')!='telegram888':
            return JSONResponse({'detail':'请求来源验证失败'},403)
    if path.startswith('/api/') and path not in ('/api/auth','/api/setup','/api/status'):
        token=request.cookies.get('tg888','')
        session=query('SELECT admin_id FROM web_sessions WHERE token=%s AND expires>now()',(digest(token),),one=True) if token else None
        if not session:return JSONResponse({'detail':'请先登录'},401)
    try:response=await call_next(request)
    except Exception as exc:
        import logging;logging.getLogger('telegram888').error('request failure %s %s',path,type(exc).__name__)
        return JSONResponse({'detail':'请求未完成，请检查服务日志；未确认成功的操作请勿重复提交'},500)
    response.headers['Cache-Control']='no-store'
    response.headers['X-Content-Type-Options']='nosniff'
    response.headers['Referrer-Policy']='no-referrer'
    response.headers['Content-Security-Policy']="default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    return response

@app.get('/')
async def home():return FileResponse(BASE/'static/index.html')
@app.get('/api/status')
async def status():
    return {'setup_needed':not bool(query('SELECT id FROM admins LIMIT 1',one=True)),'version':'0.1.0-dev'}
class Credentials(BaseModel):
    username:str=Field(min_length=3,max_length=80)
    password:str=Field(min_length=12,max_length=200)
    setup_code:str=''
def throttle(request):
    key=request.headers.get('x-real-ip',request.client.host)
    t=now().timestamp();old=[x for x in failures.get(key,[]) if t-x<600];failures[key]=old
    if len(old)>=10:raise HTTPException(429,'尝试次数过多，请十分钟后再试')
    old.append(t)
@app.post('/api/setup')
async def setup(body:Credentials,request:Request):
    throttle(request)
    if not secrets.compare_digest(body.setup_code,os.environ.get('SETUP_CODE','!')):raise HTTPException(403,'初始化码不正确')
    with db() as c:
        c.execute('SELECT pg_advisory_xact_lock(88800002)')
        if c.execute('SELECT id FROM admins LIMIT 1').fetchone():raise HTTPException(409,'管理员已创建，请登录')
        c.execute('INSERT INTO admins(username,password) VALUES (%s,%s)',(body.username,password_hash(body.password)))
    audit('admin_created');return {'ok':True}
@app.post('/api/auth')
async def login(body:Credentials,request:Request):
    throttle(request)
    admin=query('SELECT * FROM admins WHERE username=%s',(body.username,),one=True)
    if not admin or not password_ok(body.password,admin['password']):raise HTTPException(401,'用户名或密码不正确')
    token=secrets.token_urlsafe(32)
    query("INSERT INTO web_sessions(token,admin_id,expires) VALUES (%s,%s,now()+interval '12 hours')",(digest(token),admin['id']))
    query('DELETE FROM web_sessions WHERE expires<now()')
    r=JSONResponse({'ok':True});r.set_cookie('tg888',token,secure=True,httponly=True,samesite='strict',max_age=43200)
    audit('login');return r
@app.post('/api/logout')
async def logout(request:Request):
    query('DELETE FROM web_sessions WHERE token=%s',(digest(request.cookies.get('tg888','')),))
    r=JSONResponse({'ok':True});r.delete_cookie('tg888');return r
@app.get('/api/dashboard')
async def dashboard():
    counts={}
    for table in ('contacts','accounts','conversations','proxies'):counts[table]=query(f'SELECT count(*) AS n FROM {table}',one=True)['n']
    counts['online']=query("SELECT count(*) AS n FROM accounts WHERE enabled AND status='online' AND checked_at>now()-interval '90 seconds'",one=True)['n']
    counts['sent']=query("SELECT count(*) AS n FROM outbox WHERE status='sent' AND created_at>=date_trunc('day',now())",one=True)['n']
    counts['pending']=query("SELECT count(*) AS n FROM outbox WHERE status IN ('queued','sending','uncertain')",one=True)['n']
    counts['agent_errors']=query("SELECT count(*) AS n FROM ai_runs WHERE status='error'",one=True)['n']
    return {'counts':counts,'runtime':bool(runtime.task and not runtime.task.done()),'last_tick':runtime.last_tick,'settings':public_config(),'recent':query('SELECT * FROM audit ORDER BY id DESC LIMIT 8')}
@app.get('/api/groups')
async def groups():return query('SELECT g.*,count(c.id) AS total FROM groups g LEFT JOIN contacts c ON c.group_id=g.id GROUP BY g.id ORDER BY g.id')
class Group(BaseModel):name:str=Field(min_length=1,max_length=80)
@app.post('/api/groups')
async def add_group(b:Group):
    r=query('INSERT INTO groups(name) VALUES (%s) ON CONFLICT(name) DO NOTHING RETURNING id',(b.name.strip(),),one=True)
    if not r:raise HTTPException(409,'分组已存在')
    audit('group_created',str(r['id']));return r
@app.get('/api/contacts')
async def contacts(q:str='',group_id:int=0,page:int=1):
    q=q[:150];page=max(1,page)
    params=('%'+q+'%',)*3+(group_id,group_id)
    where='WHERE (c.name ILIKE %s OR c.phone ILIKE %s OR c.company ILIKE %s) AND (%s=0 OR c.group_id=%s)'
    total=query('SELECT count(*) AS n FROM contacts c '+where,params,one=True)['n']
    rows=query('SELECT c.*,g.name AS group_name,a.name AS account_name FROM contacts c LEFT JOIN groups g ON g.id=c.group_id LEFT JOIN accounts a ON a.id=c.account_id '+where+' ORDER BY c.id DESC LIMIT 50 OFFSET %s',params+((page-1)*50,))
    return {'rows':rows,'total':total,'page':page}
class Contact(BaseModel):
    name:str=Field(default='',max_length=200)
    phone:str
    company:str=Field(default='',max_length=200)
    country:str=Field(default='',max_length=200)
    username:str=''
    group_id:int
    consent:bool=False
    ai_consent:bool=False
    optout:bool=False
@app.post('/api/contacts')
async def add_contact(b:Contact):
    try:p=phone(b.phone);u=username(b.username)
    except ValueError as e:raise HTTPException(422,str(e))
    if not query('SELECT id FROM groups WHERE id=%s',(b.group_id,),one=True):raise HTTPException(422,'分组不存在')
    r=query('INSERT INTO contacts(name,phone,company,country,username,group_id,consent,ai_consent,optout) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(phone) DO NOTHING RETURNING id',(b.name,p,b.company,b.country,u,b.group_id,b.consent,b.ai_consent,b.optout),one=True)
    if not r:raise HTTPException(409,'该号码已在通讯录中')
    audit('contact_created',str(r['id']));return r
@app.patch('/api/contacts/{cid}')
async def edit_contact(cid:int,b:Contact):
    try:p=phone(b.phone);u=username(b.username)
    except ValueError as e:raise HTTPException(422,str(e))
    row=query('UPDATE contacts SET name=%s,company=%s,country=%s,username=%s,group_id=%s,consent=%s,ai_consent=%s,optout=%s WHERE id=%s AND phone=%s RETURNING id',(b.name,b.company,b.country,u,b.group_id,b.consent,b.ai_consent,b.optout,cid,p),one=True)
    if not row:raise HTTPException(404,'联系人不存在或号码不匹配')
    if b.optout:
        query("UPDATE outbox SET status='cancelled',detail='客户已拒绝联系' WHERE contact_id=%s AND status='queued'",(cid,))
        if u:query("UPDATE conversations SET stage='optout',mode='human',version=version+1 WHERE lower(username)=lower(%s)",(u,))
    audit('contact_updated',str(cid));return row
@app.post('/api/import/preview')
async def import_preview(file:UploadFile=File(...),group_id:int=Form(...)):
    if not query('SELECT id FROM groups WHERE id=%s',(group_id,),one=True):raise HTTPException(422,'分组不存在')
    data=await file.read(2*1024*1024+1)
    if len(data)>2*1024*1024:raise HTTPException(413,'文件最大 2 MB，每批最多 5000 行')
    try:
        if file.filename.lower().endswith('.csv'):
            rows=list(csv.DictReader(io.StringIO(data.decode('utf-8-sig'))))
        elif file.filename.lower().endswith('.xlsx'):
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                if sum(x.file_size for x in z.infolist())>20*1024*1024 or len(z.infolist())>500:raise ValueError('工作簿过大')
            wb=load_workbook(io.BytesIO(data),read_only=True,data_only=True,keep_links=False)
            ws=wb.active;it=ws.iter_rows(values_only=True);headers=[str(x or '').strip() for x in next(it)]
            rows=[]
            for vals in it:
                rows.append(dict(zip(headers,vals)))
                if len(rows)>5000:break
            wb.close()
        else:raise ValueError('仅支持 UTF-8 CSV 和 XLSX')
        if len(rows)>5000:raise ValueError('每批最多 5000 行')
        valid,errors=normalize_rows(rows)
    except Exception as e:raise HTTPException(422,str(e)[:150] if isinstance(e,ValueError) else '文件格式无法读取')
    exists={x['phone'] for x in query('SELECT phone FROM contacts WHERE phone=ANY(%s)',([x['phone'] for x in valid],))}
    fresh=[r for r in valid if r['phone'] not in exists];token=secrets.token_urlsafe(20)
    query('INSERT INTO imports(id,rows,group_id) VALUES (%s,%s,%s)',(token,Jsonb(fresh),group_id))
    return {'id':token,'valid':len(fresh),'duplicates':len(exists),'errors':errors[:100],'sample':fresh[:10],'note':'导入默认未获得联系许可，不自动发送'}
@app.post('/api/import/{token}/confirm')
async def confirm_import(token:str):
    with db() as c:
        batch=c.execute('SELECT * FROM imports WHERE id=%s FOR UPDATE',(token,)).fetchone()
        if not batch:raise HTTPException(404,'导入批次不存在')
        if batch['confirmed']:raise HTTPException(409,'该批次已经导入，不能重复确认')
        if now()-batch['created_at']>datetime.timedelta(hours=1):raise HTTPException(410,'预览已过期，请重新上传')
        count=0
        for r in batch['rows']:
            result=c.execute('INSERT INTO contacts(phone,name,company,country,username,group_id) VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT(phone) DO NOTHING RETURNING id',(r['phone'],r['name'],r['company'],r['country'],r['username'],batch['group_id'])).fetchone()
            count+=bool(result)
        c.execute('UPDATE imports SET confirmed=true,rows=%s WHERE id=%s',(Jsonb([]),token))
    audit('contacts_imported',str(count));return {'imported':count}
@app.get('/api/settings')
async def settings():return public_config()
class Settings(BaseModel):
    api_id:str=Field(default='',max_length=20)
    api_hash:str=Field(default='',max_length=64)
    openai_key:str=Field(default='',max_length=1000)
    model:str=Field(default='',max_length=100)
    main_username:str=''
    knowledge:str=Field(default='',max_length=20000)
    agent_enabled:bool=False
    daily_calls:int=Field(default=100,ge=1,le=5000)
@app.put('/api/settings')
async def save_settings(b:Settings):
    old=config()
    if b.api_id and (not b.api_id.isdigit() or int(b.api_id)<=0):raise HTTPException(422,'API ID 必须为正整数')
    if b.api_hash and not re.fullmatch('[a-fA-F0-9]{32}',b.api_hash):raise HTTPException(422,'API Hash 应为 32 位十六进制字符串')
    try:b.main_username=username(b.main_username)
    except ValueError as e:raise HTTPException(422,str(e))
    if b.agent_enabled and not (b.openai_key or old.get('openai_key')):raise HTTPException(422,'尚未配置 OpenAI API 密钥')
    if b.agent_enabled and not (b.model and b.main_username and b.knowledge.strip()):raise HTTPException(422,'启用前请配置模型、客服主账号及产品知识')
    with db() as c:
        for k,v in b.model_dump().items():
            if k in ('api_hash','openai_key'):
                if not v:continue
                v=seal(v)
            else:v=str(v).lower() if isinstance(v,bool) else str(v)
            c.execute('INSERT INTO settings(key,value) VALUES (%s,%s) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value',(k,v))
    audit('settings_updated','密钥内容不记录');return public_config()
@app.post('/api/settings/openai-test')
async def test_openai():
    cfg=config()
    if not cfg.get('openai_key') or not cfg.get('model'):raise HTTPException(422,'先保存 API 密钥和模型')
    try:
        async with AsyncOpenAI(api_key=cfg['openai_key'],timeout=20,max_retries=0) as ai:
            await ai.models.retrieve(cfg['model'])
        audit('openai_access_verified');return {'detail':'API 鉴权与模型访问检查通过；尚未验证生成质量'}
    except Exception as e:raise HTTPException(502,'OpenAI 访问检查未通过：'+type(e).__name__)
class ProxyInput(BaseModel):
    name:str=Field(min_length=1,max_length=100)
    host:str=Field(min_length=1,max_length=253)
    port:int=Field(ge=1,le=65535)
    username:str=Field(default='',max_length=200)
    password:str=Field(default='',max_length=500)
async def resolve_public(host,port):
    try:infos=await asyncio.get_running_loop().getaddrinfo(host,port,type=socket.SOCK_STREAM)
    except Exception:raise HTTPException(422,'代理地址无法解析')
    addresses={x[4][0] for x in infos}
    if not addresses or any(not ipaddress.ip_address(x).is_global for x in addresses):raise HTTPException(422,'代理必须使用公网地址，禁止本地及内网地址')
    return sorted(addresses)[0]
@app.get('/api/proxies')
async def proxies():return query('SELECT id,name,host,port,username,status,detail,checked_at,(SELECT count(*) FROM accounts a WHERE a.proxy_id=p.id) AS accounts FROM proxies p ORDER BY id DESC')
@app.post('/api/proxies')
async def create_proxy(b:ProxyInput):
    host=await resolve_public(b.host,b.port)
    row=query('INSERT INTO proxies(name,host,port,username,password) VALUES (%s,%s,%s,%s,%s) RETURNING id',(b.name,host,b.port,b.username,seal(b.password)),one=True)
    audit('proxy_created',str(row['id']));return row
@app.post('/api/proxies/{pid}/test')
async def test_proxy(pid:int):
    p=query('SELECT * FROM proxies WHERE id=%s',(pid,),one=True)
    if not p:raise HTTPException(404,'网络不存在')
    host=await resolve_public(p['host'],p['port'])
    try:
        proxy=Proxy(ProxyType.SOCKS5,host,p['port'],username=p['username'] or None,password=unseal(p['password']) or None,rdns=True)
        sock=await proxy.connect(dest_host='149.154.167.51',dest_port=443,timeout=12);sock.close()
        state='reachable';detail='代理至 Telegram DC 的 TCP 连接通过；不代表账号授权有效'
    except Exception as e:state='error';detail='连接失败：'+type(e).__name__
    query('UPDATE proxies SET status=%s,detail=%s,checked_at=now() WHERE id=%s',(state,detail,pid));return {'status':state,'detail':detail}
@app.get('/api/accounts')
async def accounts():
    rows=query('SELECT a.id,a.name,a.phone,a.tg_user_id,a.proxy_id,a.enabled,a.status,a.detail,a.checked_at,a.cooldown_until,p.name AS proxy_name FROM accounts a LEFT JOIN proxies p ON p.id=a.proxy_id ORDER BY a.id DESC')
    for a in rows:
        if a['status']=='online' and (not a['checked_at'] or (now()-a['checked_at']).total_seconds()>90):a['status']='unverified';a['detail']='验证已过期，不能确认在线'
    return rows
@app.post('/api/accounts/import')
async def import_account(file:UploadFile=File(...),name:str=Form(...),proxy_id:int=Form(0)):
    if proxy_id and not query('SELECT id FROM proxies WHERE id=%s',(proxy_id,),one=True):raise HTTPException(422,'网络不存在')
    data=await file.read(4*1024*1024+1)
    if len(data)>4*1024*1024:raise HTTPException(413,'Session 文件不得超过 4 MB')
    if not file.filename.lower().endswith('.session') or not data.startswith(b'SQLite format 3\x00'):raise HTTPException(422,'首版只接受 Telethon SQLite .session；Tdata 和其他 Session 格式暂不支持')
    fd,path=tempfile.mkstemp(suffix='.session')
    try:
        with os.fdopen(fd,'wb') as f:f.write(data)
        con=sqlite3.connect(f'file:{path}?mode=ro&immutable=1',uri=True)
        con.execute('PRAGMA trusted_schema=OFF')
        con.set_progress_handler(lambda:1,100000)
        rows=con.execute('SELECT dc_id,server_address,port,auth_key FROM sessions LIMIT 2').fetchall();con.close()
        if len(rows)!=1:raise ValueError()
        dc,address,port,key=rows[0]
        if not isinstance(key,bytes) or len(key)!=256 or not isinstance(dc,int) or dc not in range(1,6) or not isinstance(port,int) or port!=443:raise ValueError()
        ip=ipaddress.ip_address(address)
        if not ip.is_global:raise ValueError()
        # Avoid arbitrary connection targets embedded in uploaded sessions.
        allowed=[ipaddress.ip_network(n) for n in ('149.154.160.0/20','91.108.0.0/16','2001:b28:f23d::/48','2001:b28:f23f::/48','2001:67c:4e8::/48')]
        if not any(ip in n for n in allowed):raise ValueError()
        session=StringSession();session.set_dc(dc,address,port);session.auth_key=AuthKey(key)
        fingerprint=hashlib.sha256(key).hexdigest();value=seal(session.save())
    except Exception:raise HTTPException(422,'Session 结构或授权材料不受支持；文件未执行，未建立 Telegram 连接')
    finally:os.unlink(path)
    row=query('INSERT INTO accounts(name,session,fingerprint,proxy_id) VALUES (%s,%s,%s,%s) ON CONFLICT(fingerprint) DO NOTHING RETURNING id',(name[:100],value,fingerprint,proxy_id or None),one=True)
    if not row:raise HTTPException(409,'同一份授权会话已导入')
    audit('account_imported',str(row['id']));return {'id':row['id'],'detail':'已保存，尚未连接。请确认同一 Session 已在其他系统停止后再启用。'}
class AccountControl(BaseModel):enabled:bool;other_connections_stopped:bool=False
@app.post('/api/accounts/{aid}/control')
async def control_account(aid:int,b:AccountControl):
    if b.enabled:
        if not b.other_connections_stopped:raise HTTPException(422,'必须确认同一 Session 已在其他系统停止连接')
        cfg=config()
        if not cfg.get('api_id') or not cfg.get('api_hash'):raise HTTPException(422,'先在设置中配置 Telegram API ID 和 API Hash')
    row=query("UPDATE accounts SET enabled=%s,status=%s,detail=%s,cooldown_until=NULL WHERE id=%s RETURNING id",(b.enabled,'pending' if b.enabled else 'offline','等待连接验证' if b.enabled else '用户要求停止',aid),one=True)
    if not row:raise HTTPException(404,'账号不存在')
    if not b.enabled:await runtime.stop_account(aid)
    audit('account_enabled' if b.enabled else 'account_disabled',str(aid))
    return {'detail':'已提交连接请求，不代表登录成功' if b.enabled else '已停止连接'}
@app.post('/api/accounts/{aid}/sync')
async def sync(aid:int):
    try:await asyncio.wait_for(runtime.sync(aid),45)
    except Exception as e:raise HTTPException(409,'同步未完成：'+type(e).__name__)
    return {'ok':True}
@app.get('/api/conversations')
async def conversations():return query('SELECT c.*,a.name AS account_name FROM conversations c JOIN accounts a ON a.id=c.account_id ORDER BY c.updated_at DESC LIMIT 200')
@app.get('/api/conversations/{cid}/messages')
async def messages(cid:int):return {'messages':query('SELECT * FROM (SELECT * FROM messages WHERE conversation_id=%s ORDER BY sent_at DESC,tg_id DESC LIMIT 100) m ORDER BY sent_at,tg_id',(cid,)),'outbox':query("SELECT id,body,status,detail,source FROM outbox WHERE conversation_id=%s AND status!='sent' ORDER BY created_at LIMIT 50",(cid,))}
class Mode(BaseModel):mode:str;ai_allowed:bool=False;received:bool=False
@app.post('/api/conversations/{cid}/mode')
async def conversation_mode(cid:int,b:Mode):
    if b.mode not in ('human','agent','optout'):raise HTTPException(422,'模式不正确')
    cfg=config()
    if b.mode=='agent' and (not b.ai_allowed or cfg.get('agent_enabled')!='true'):raise HTTPException(422,'先配置并启用 Agent，且确认本会话可交由 AI 处理')
    with db() as c:
        c.execute("UPDATE conversations SET mode=%s,ai_allowed=%s,version=version+1,stage=CASE WHEN %s THEN 'optout' WHEN %s THEN 'received' ELSE stage END WHERE id=%s",('human' if b.mode=='optout' else b.mode,b.ai_allowed,b.mode=='optout',b.received,cid))
        c.execute("UPDATE outbox SET status='cancelled',detail='接待模式已改变' WHERE conversation_id=%s AND source='agent' AND status='queued'",(cid,))
    if b.mode=='optout':
        conv=query('SELECT username FROM conversations WHERE id=%s',(cid,),one=True)
        if conv and conv['username']:
            query('UPDATE contacts SET optout=true WHERE lower(username)=lower(%s)',(conv['username'],))
            query("UPDATE conversations SET stage='optout',mode='human',version=version+1 WHERE lower(username)=lower(%s)",(conv['username'],))
    audit('conversation_mode',str(cid)+':'+b.mode);return {'ok':True}
class Send(BaseModel):body:str=Field(min_length=1,max_length=4000);operation_id:str=Field(min_length=16,max_length=80)
@app.post('/api/conversations/{cid}/send')
async def send(cid:int,b:Send):
    conv=query('SELECT * FROM conversations WHERE id=%s',(cid,),one=True)
    if not conv:raise HTTPException(404,'会话不存在')
    if conv['stage']=='optout':raise HTTPException(409,'客户已拒绝联系')
    if conv['account_id'] not in runtime.clients:raise HTTPException(409,'账号未连接，不能发送')
    query("UPDATE conversations SET mode='human',version=version+1 WHERE id=%s",(cid,))
    oid=queue_message(conv['account_id'],conv['peer_id'],b.body,cid,operation_id=b.operation_id)
    return {'id':oid,'status':'queued','detail':'已排队，需等待 Telegram 消息 ID 确认'}
@app.post('/api/conversations/{cid}/sync')
async def sync_history(cid:int):
    try:await asyncio.wait_for(runtime.history(cid),45)
    except Exception as e:raise HTTPException(409,'同步未完成：'+type(e).__name__)
    return {'ok':True}
@app.get('/api/campaigns')
async def campaigns():return {'campaigns':query('SELECT c.*,g.name AS group_name,a.name AS account_name FROM campaigns c JOIN groups g ON g.id=c.group_id JOIN accounts a ON a.id=c.account_id ORDER BY c.id DESC'),'outbox':query('SELECT id,account_id,campaign_id,status,detail,created_at,tg_id FROM outbox ORDER BY created_at DESC LIMIT 100')}
class Campaign(BaseModel):
    name:str=Field(min_length=1,max_length=100)
    group_id:int
    account_id:int
    body:str=Field(min_length=1,max_length=3500)
    interval_seconds:int=Field(default=300,ge=60,le=86400)
@app.post('/api/campaigns')
async def create_campaign(b:Campaign):
    with db() as c:
        if not c.execute('SELECT id FROM accounts WHERE id=%s',(b.account_id,)).fetchone():raise HTTPException(422,'账号不存在')
        contacts=c.execute('SELECT * FROM contacts WHERE group_id=%s AND consent=true AND optout=false AND username!=\'\' AND (account_id IS NULL OR account_id=%s) ORDER BY id LIMIT 500 FOR UPDATE',(b.group_id,b.account_id)).fetchall()
        if not contacts:raise HTTPException(422,'该分组没有已确认联系许可且具有 Telegram 用户名的客户，或客户已归属其他账号')
        row=c.execute('INSERT INTO campaigns(name,group_id,account_id,body,interval_seconds) VALUES (%s,%s,%s,%s,%s) RETURNING id',(b.name,b.group_id,b.account_id,b.body,b.interval_seconds)).fetchone()
        for i,contact in enumerate(contacts):
            c.execute('UPDATE contacts SET account_id=%s WHERE id=%s',(b.account_id,contact['id']))
            c.execute("INSERT INTO outbox(id,account_id,contact_id,campaign_id,peer,body,source,random_id,not_before) VALUES (%s,%s,%s,%s,%s,%s,'campaign',%s,now()+(%s * interval '1 second'))",(secrets.token_hex(16),b.account_id,contact['id'],row['id'],contact['username'],b.body,secrets.randbits(63),i*b.interval_seconds))
    audit('campaign_created',str(row['id']));return {'id':row['id'],'recipients':len(contacts),'state':'paused'}
class CampaignState(BaseModel):state:str
@app.post('/api/campaigns/{cid}/state')
async def campaign_state(cid:int,b:CampaignState):
    if b.state not in ('running','paused','cancelled'):raise HTTPException(422,'状态无效')
    query('UPDATE campaigns SET state=%s WHERE id=%s',(b.state,cid))
    if b.state=='cancelled':query("UPDATE outbox SET status='cancelled' WHERE campaign_id=%s AND status='queued'",(cid,))
    audit('campaign_state',str(cid)+':'+b.state);return {'ok':True,'detail':'已更新；已交给 Telegram 的请求无法撤回'}
@app.get('/api/audit')
async def logs():return {'audit':query('SELECT * FROM audit ORDER BY id DESC LIMIT 100'),'ai':query('SELECT * FROM ai_runs ORDER BY id DESC LIMIT 100')}
app.mount('/static',StaticFiles(directory=BASE/'static'),name='static')

import os, hashlib, hmac, secrets, re, datetime, json
from contextlib import contextmanager
import psycopg
from psycopg.rows import dict_row
from cryptography.fernet import Fernet

DSN=os.environ.get('DATABASE_URL','dbname=telegram888 user=telegram888 host=/var/run/postgresql')
VAULT=Fernet(os.environ['VAULT_KEY'].encode())

def now(): return datetime.datetime.now(datetime.timezone.utc)
@contextmanager
def db():
    with psycopg.connect(DSN,row_factory=dict_row) as c: yield c

def query(sql,args=(),one=False):
    with db() as c:
        cur=c.execute(sql,args)
        if cur.description: return cur.fetchone() if one else cur.fetchall()

def seal(x): return VAULT.encrypt(x.encode()).decode() if x else ''
def unseal(x): return VAULT.decrypt(x.encode()).decode() if x else ''
def digest(x): return hashlib.sha256(x.encode()).hexdigest()
def password_hash(x):
    salt=secrets.token_hex(16)
    return salt+':'+hashlib.scrypt(x.encode(),salt=salt.encode(),n=16384,r=8,p=1).hex()
def password_ok(x,stored):
    salt,expected=stored.split(':')
    return hmac.compare_digest(expected,hashlib.scrypt(x.encode(),salt=salt.encode(),n=16384,r=8,p=1).hex())
def config():
    d={x['key']:x['value'] for x in query('SELECT * FROM settings')}
    for k in ('api_hash','openai_key'): d[k]=unseal(d.get(k,''))
    return d

def public_config():
    d=config()
    for k in ('api_hash','openai_key'):
        d[k+'_configured']=bool(d.pop(k,''))
    return d

def audit(action,detail=''):query('INSERT INTO audit(action,detail) VALUES (%s,%s)',(action,detail[:500]))
def phone(s):
    s=re.sub(r'[\s()\-]','',str(s or ''))
    if s.startswith('00'): s='+'+s[2:]
    if not re.fullmatch(r'\+[1-9]\d{6,14}',s): raise ValueError('电话号码必须包含国家区号，例如 +14155552671')
    return s

def username(s):
    s=str(s or '').strip().removeprefix('https://t.me/').lstrip('@')
    if s and not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{4,31}',s): raise ValueError('Telegram 用户名格式不正确')
    return s

def normalize_rows(rows,start_line=2):
    valid=[];errors=[];seen=set()
    for i,r in enumerate(rows,start_line):
        if not any(str(v or '').strip() for v in r.values()):continue
        try:
            p=phone(r.get('phone') or r.get('电话号码') or r.get('手机号'))
            if p in seen: errors.append({'row':i,'reason':'文件内号码重复'});continue
            u=username(r.get('username') or r.get('用户名'))
            def val(a,b):return str(r.get(a) or r.get(b) or '').strip()[:200]
            valid.append({'phone':p,'name':val('name','姓名'),'company':val('company','公司'),'country':val('country','国家'),'username':u})
            seen.add(p)
        except ValueError as e:errors.append({'row':i,'reason':str(e)})
    return valid,errors

def queue_message(account_id,peer,body,conversation_id=None,source='human',version=None,operation_id=None):
    oid=operation_id or secrets.token_hex(16)
    row=query('INSERT INTO outbox(id,account_id,conversation_id,peer,body,source,version,random_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(id) DO NOTHING RETURNING id',
        (oid,account_id,conversation_id,str(peer),body,source,version,secrets.randbits(63)),one=True)
    return oid

def recover_pending():
    query("UPDATE outbox SET status='uncertain',detail='服务重启，发送结果待核对；不会自动重发' WHERE status='sending'")
    query("UPDATE ai_runs SET status='interrupted',detail='服务重启，未自动重新生成' WHERE status='running'")
    query("UPDATE accounts SET status='offline',detail='连接服务启动，等待验证',checked_at=NULL WHERE enabled=true")


def parse_txt(data,mode='',country_code=''):
    if data.startswith((b'\xff\xfe',b'\xfe\xff')):
        text=data.decode('utf-16')
    else:
        try:text=data.decode('utf-8-sig')
        except UnicodeDecodeError:text=data.decode('gb18030')
    if '\x00' in text:raise ValueError('TXT 包含不支持的字符，请另存为 UTF-8 文本')
    if mode not in ('','international','local'):raise ValueError('请选择正确的 TXT 号码格式')
    code=country_code.strip().lstrip('+')
    if mode=='local' and not re.fullmatch(r'[1-9]\d{0,2}',code):raise ValueError('本地号码需指定 1 至 3 位国家区号')
    rows=[]
    for line in text.splitlines():
        value=line.strip()
        clean=re.sub(r'[\s()\-]','',value)
        if clean and not clean.startswith(('+','00')):
            if mode=='international':value='+'+value
            elif mode=='local':
                # Never guess or strip a national trunk prefix.
                value='+'+code+value
            elif clean.isdigit():raise ValueError('TXT 有未带 + 的号码，请明确选择是否已含国家区号')
        rows.append({'phone':value})
    return rows

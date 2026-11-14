"""Local preview pairing only. No default actors, real credentials or server start."""
from contextlib import closing
from datetime import datetime,timezone
from hashlib import sha256
from hmac import compare_digest
from secrets import token_urlsafe
from types import MappingProxyType
from urllib.parse import urlsplit
from flask import request,g,jsonify,make_response
from .auth import Principal,ROLES

SCHEMA='''
CREATE TABLE IF NOT EXISTS preview_pair_codes (digest TEXT PRIMARY KEY,actor TEXT NOT NULL,expires REAL NOT NULL,used INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS preview_challenges (digest TEXT PRIMARY KEY,expires REAL NOT NULL,failures INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS preview_submission_failures (digest TEXT PRIMARY KEY,failures INTEGER NOT NULL,cooldown_until REAL NOT NULL);
CREATE TABLE IF NOT EXISTS preview_sessions (digest TEXT PRIMARY KEY,actor TEXT NOT NULL,csrf TEXT NOT NULL,issued REAL NOT NULL,last_seen REAL NOT NULL,revoked INTEGER NOT NULL DEFAULT 0);
'''
COOKIE='sports_preview_session'
CHALLENGE='sports_preview_challenge'


def digest(value):return sha256(value.encode()).hexdigest()


class LocalPairAuth:
    def __init__(self,store,*,origin,actors,clock=None,initialize=True):
        parsed=urlsplit(origin)
        if (parsed.scheme not in {'http','https'} or parsed.hostname not in {'127.0.0.1','localhost'}
                or parsed.port is None or parsed.path or parsed.query or parsed.fragment
                or parsed.username is not None or parsed.password is not None):
            raise ValueError('explicit loopback origin/port required')
        if not actors or any(not isinstance(key,str) or not key.startswith('preview-') or role not in ROLES for key,role in actors.items()):
            raise ValueError('explicit registered preview identities required')
        from .private_auth_store import PrivateAuthStore
        self.store=PrivateAuthStore(store);self.origin=origin;self.host=parsed.netloc
        self.secure=parsed.scheme=='https';self.actors=MappingProxyType(dict(actors))
        self.clock=clock or (lambda:datetime.now(timezone.utc).timestamp())
        if initialize:
            with closing(self.store.connect()) as db,db:db.executescript(SCHEMA)

    def issue(self,actor_id):
        """Local operator API ONLY; never exposed by HTTP or called at import."""
        if actor_id not in self.actors:raise ValueError('unregistered preview identity')
        code=token_urlsafe(32)
        with closing(self.store.connect()) as db,db:
            db.execute('INSERT INTO preview_pair_codes VALUES(?,?,?,0)',(digest(code),actor_id,self.clock()+120))
        return code

    def challenge(self):
        value=token_urlsafe(32)
        with closing(self.store.connect()) as db,db:
            db.execute('INSERT INTO preview_challenges VALUES(?,?,0)',(digest(value),self.clock()+300))
        return value

    def principal(self,req):
        value=req.cookies.get(COOKIE)
        if not value:return None
        now=self.clock()
        with closing(self.store.connect()) as db,db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT * FROM preview_sessions WHERE digest=?',(digest(value),)).fetchone()
            if (row is None or row['revoked'] or now>=row['issued']+1800 or now>=row['last_seen']+600
                    or row['actor'] not in self.actors):return None
            db.execute('UPDATE preview_sessions SET last_seen=? WHERE digest=?',(now,row['digest']))
        return Principal(row['actor'],self.actors[row['actor']],row['csrf'])

    def exchange(self,code,challenge,old_session=None):
        if not isinstance(code,str) or not isinstance(challenge,str):return None,401
        now=self.clock();result=None;status=401
        with closing(self.store.connect()) as db,db:
            db.execute('BEGIN IMMEDIATE')
            attempt=db.execute('SELECT * FROM preview_challenges WHERE digest=?',(digest(challenge),)).fetchone()
            if attempt is None or now>=attempt['expires']:return None,401
            if attempt['failures']>=5:return None,429
            failure=db.execute('SELECT * FROM preview_submission_failures WHERE digest=?',(digest(code),)).fetchone()
            if failure and failure['cooldown_until']>now:return None,429
            row=db.execute('SELECT * FROM preview_pair_codes WHERE digest=?',(digest(code),)).fetchone()
            if row is None or row['used'] or now>=row['expires'] or row['actor'] not in self.actors:
                db.execute('UPDATE preview_challenges SET failures=failures+1 WHERE digest=?',(digest(challenge),))
                count=(failure['failures'] if failure and failure['cooldown_until']==0 else 0)+1
                cooldown=now+300 if count>=5 else 0
                db.execute('INSERT INTO preview_submission_failures VALUES(?,?,?) '
                           'ON CONFLICT(digest) DO UPDATE SET failures=excluded.failures,cooldown_until=excluded.cooldown_until',
                           (digest(code),count,cooldown))
                status=429 if count>=5 or attempt['failures']+1>=5 else 401
            else:
                session=token_urlsafe(32);csrf=token_urlsafe(32)
                db.execute('UPDATE preview_pair_codes SET used=1 WHERE digest=?',(row['digest'],))
                db.execute('DELETE FROM preview_challenges WHERE digest=?',(digest(challenge),))
                if old_session:db.execute('UPDATE preview_sessions SET revoked=1 WHERE digest=?',(digest(old_session),))
                db.execute('INSERT INTO preview_sessions VALUES(?,?,?,?,?,0)',(digest(session),row['actor'],csrf,now,now))
                result=session;status=200
        return result,status

    def logout(self,req):
        principal=self.principal(req)
        header=req.headers.get('X-CSRF')
        if principal is None:return 401
        if not isinstance(header,str) or not compare_digest(header.encode(),principal.csrf_token.encode()):return 403
        with closing(self.store.connect()) as db,db:
            db.execute('UPDATE preview_sessions SET revoked=1 WHERE digest=?',(digest(req.cookies[COOKIE]),))
        return 200

    def guard(self,req):
        if req.host!=self.host or req.scheme!=urlsplit(self.origin).scheme:return 403
        if any(key.lower()=='forwarded' or key.lower().startswith('x-forwarded-') for key in req.headers.keys()):return 403
        if req.method not in {'GET','HEAD','OPTIONS'} and req.headers.get('Origin')!=self.origin:return 403
        return None

    def install(self,app):
        def safe_error(status):
            return jsonify(code='PAIR_AUTH_REJECTED',category='HARD_BLOCK',message='预览认证未通过'),status

        @app.get('/auth/login')
        def login():
            if request.args:return safe_error(400)
            challenge=self.challenge()
            # Single anonymous self-contained resource; never accepts a code in URL.
            response=make_response('''<!doctype html><html lang="zh"><meta charset="utf-8"><title>本机预览配对</title>
<p>仅用于隔离预览，并非业务账号登录。</p><form id="pair"><label>本机一次性配对码<input id="code" type="password" autocomplete="off" required></label><button>配对</button></form><p id="status"></p>
<script>document.querySelector('#pair').onsubmit=async(e)=>{e.preventDefault();const input=document.querySelector('#code');const code=input.value;input.value='';const r=await fetch('/auth/pair',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:JSON.stringify({code})});if(r.ok)location.replace('/');else document.querySelector('#status').textContent='配对未通过，请重新取得本机授权';};</script></html>''')
            response.set_cookie(CHALLENGE,challenge,httponly=True,samesite='Strict',secure=self.secure,path='/',max_age=300)
            return response

        @app.post('/auth/pair')
        def pair():
            if request.args:return safe_error(400)
            value=request.get_json(silent=True)
            if not isinstance(value,dict) or set(value)!={'code'} or not isinstance(value['code'],str) or len(value['code'])>256:return safe_error(400)
            session,status=self.exchange(value['code'],request.cookies.get(CHALLENGE),request.cookies.get(COOKIE))
            if session is None:return safe_error(status)
            response=jsonify(data={'status':'paired'})
            response.set_cookie(COOKIE,session,httponly=True,samesite='Strict',secure=self.secure,path='/',max_age=1800)
            response.delete_cookie(CHALLENGE,path='/')
            return response

        @app.post('/auth/logout')
        def logout():
            status=self.logout(request)
            if status!=200:return safe_error(status)
            response=jsonify(data={'status':'logged_out'});response.delete_cookie(COOKIE,path='/')
            return response

        @app.after_request
        def no_auth_cache(response):
            if request.path.startswith('/auth/'):
                response.headers['Cache-Control']='no-store'
                response.headers['X-Frame-Options']='DENY'
                response.headers['Referrer-Policy']='no-referrer'
            return response

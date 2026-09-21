"""Exercise pristine compiled services without persisting test accounts in artifacts."""
import json,os,pathlib,re,secrets,sqlite3,subprocess,sys,tempfile,time,urllib.request,urllib.error,urllib.parse
package=pathlib.Path(sys.argv[1]).resolve()
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args):return None
http=urllib.request.build_opener(NoRedirect)
def request(base,path,method='GET',body=None,headers=None):
    headers=dict(headers or {})
    if body is not None and not isinstance(body,bytes):
        body=json.dumps(body).encode();headers['Content-Type']='application/json'
    try:r=http.open(urllib.request.Request(base+path,data=body,method=method,headers=headers),timeout=10)
    except urllib.error.HTTPError as e:r=e
    with r:
        data=r.read();h=dict(r.headers)
        try:data=json.loads(data)
        except (ValueError,UnicodeDecodeError):pass
        return r.code,data,h
def wait(base,path):
    for _ in range(60):
        try:
            result=request(base,path)
            if result[0]==200:return result
        except (OSError,urllib.error.URLError):pass
        time.sleep(.5)
    raise AssertionError('service not ready: '+path)
with tempfile.TemporaryDirectory(prefix='aswired-smoke-') as temporary:
    root=pathlib.Path(temporary);data=root/'controller';data.mkdir();komari=root/'komari';komari.mkdir()
    control='http://127.0.0.1:24991';probe='http://127.0.0.1:24992';web='http://127.0.0.1:24993'
    bridge=secrets.token_hex(32);env=os.environ.copy()
    processes=[];logs=[]
    def start(command,cwd,extra):
        log=open(root/(str(len(logs))+'.log'),'wb');logs.append(log)
        processes.append(subprocess.Popen(command,cwd=cwd,env={**env,**extra},stdout=log,stderr=log))
    try:
        start([str(package/'bin/aswired-server'),'serve'],root,{
            'ASWIRED_DATA_DIR':str(data),'ASWIRED_LISTEN':'127.0.0.1:24991','ASWIRED_PUBLIC_URL':web,
            'ASWIRED_ALLOWED_ORIGINS':web,'ASWIRED_KOMARI_PUBLIC_URL':probe,'ASWIRED_KOMARI_BRIDGE_SECRET':bridge})
        status=wait(control,'/api/status')[1];assert status['initialized'] is False
        start([str(package/'bin/komari'),'server','--listen','127.0.0.1:24992'],komari,{
            'ASWIRED_IDENTITY_URL':control,'ASWIRED_LOGIN_URL':web,'KOMARI_PUBLIC_URL':probe,'ASWIRED_BRIDGE_SECRET':bridge})
        assert wait(probe,'/api/aswired/auth')[1]['enabled'] is True
        version=request(probe,'/api/version')[1]['data']['version']
        assert version=='1.2.5-fix2-aswired.'+(package/'VERSION').read_text().strip().removeprefix('v'),version
        start([str(package/'runtime/node'),str(package/'web/build/index.js')],package/'web',{'HOST':'127.0.0.1','PORT':'24993','ORIGIN':web,'NODE_ENV':'production'})
        # SvelteKit renders this application's text in the browser (ssr=false).
        # Verify the served shell and its actual executable entry assets here.
        html=wait(web,'/')[1]
        assert isinstance(html,bytes) and b'data-theme="glass"' in html
        entries=re.findall(rb'\./(_app/immutable/entry/[^"\s]+\.js)',html)
        assert len(set(entries))>=2,'Missing compiled SvelteKit entry assets'
        for entry in set(entries):
            code,payload,_=request(web,'/'+entry.decode())
            assert code==200 and isinstance(payload,bytes) and len(payload)>20,'Missing JavaScript entry'
        assert request(probe,'/api/login','POST',{'username':'admin','password':'admin'})[0]==403
        db=sqlite3.connect(data/'aswired.db');assert db.execute('select count(*) from users').fetchone()[0]==0
        password=secrets.token_urlsafe(24)
        setup={'username':'chosen-release-admin','password':password,'setupToken':'wrong'}
        assert request(control,'/api/setup','POST',setup)[0]==403
        setup['setupToken']=(data/'setup-token').read_text().strip()
        status,login,_=request(control,'/api/setup','POST',setup);assert status==200,status
        token=login['token'];auth={'MM-Authorization':token}
        assert request(control,'/api/setup','POST',setup)[0]==409
        assert request(control,'/api/state',headers=auth)[0]==200
        stored=db.execute('select password_hash from users where username=?',(setup['username'],)).fetchone()[0]
        assert stored!=password and stored.startswith('$2')
        probe_password=secrets.token_urlsafe(24)
        row={'id':'release-probe','username':'chosen-probe-admin','name':'Probe operator','password':probe_password,'application':'komari','role':'普通用户','status':'正常'}
        status,_,_=request(control,'/api/collections/members','POST',{'row':row},auth);assert status==200,status
        status,login,_=request(control,'/api/login','POST',{'username':row['username'],'password':probe_password});assert status==200 and login['kind']=='komari'
        form=urllib.parse.urlencode({'ticket':login['ticket']}).encode()
        headers={'Origin':web,'Content-Type':'application/x-www-form-urlencoded'}
        status,_,response_headers=request(probe,'/auth/aswired/session','POST',form,headers)
        cookie=response_headers.get('Set-Cookie','');assert status==303 and 'HttpOnly' in cookie and 'session_token=' in cookie
        cookie=cookie.split(';')[0]
        status,me,_=request(probe,'/api/me',headers={'Cookie':cookie});assert status==200 and me['logged_in'] is True
        _,_,replay=request(probe,'/auth/aswired/session','POST',form,headers);assert 'Set-Cookie' not in replay
        assert request(control,'/api/state',headers={'MM-Authorization':cookie.split('=',1)[1]})[0]==401
        db.close()
        print('PASS: compiled stack, pristine first-run setup, hashed chosen password, initialization lock, Komari integrated login, ticket replay and account isolation.')
    finally:
        for proc in reversed(processes):proc.terminate()
        for proc in processes:
            try:proc.wait(timeout=15)
            except subprocess.TimeoutExpired:proc.kill();proc.wait()
        for log in logs:log.close()

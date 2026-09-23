"""Real Caddy/TLS deployment, restart persistence and rollback on an isolated CI host."""
import importlib.util
import json
import os
import pathlib
import socket
import subprocess
import tempfile
import time
from unittest.mock import patch

assert os.geteuid()==0 and os.environ.get('GITHUB_ACTIONS')=='true', 'Disposable GitHub Actions runner only'
spec=importlib.util.spec_from_file_location('worker',pathlib.Path(__file__).resolve().parents[1]/'deploy/site-certificates.py')
worker=importlib.util.module_from_spec(spec);spec.loader.exec_module(worker)


def port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0));return sock.getsockname()[1]


def command(*args):
    return subprocess.check_output(list(args),stderr=subprocess.DEVNULL)


with tempfile.TemporaryDirectory(prefix='aswired-certificate-ci-') as temporary:
    root=pathlib.Path(temporary);root.chmod(0o755)
    state=root/'state';state.mkdir(mode=0o755)
    for name in ['material','caddy','backups']:(state/name).mkdir()
    data=root/'data';data.mkdir();env=root/'server.env';dropin=root/'caddy.service.d';dropin.mkdir()
    ca=root/'ca.pem';ca_key=root/'ca.key'
    command('openssl','req','-x509','-newkey','ec','-pkeyopt','ec_paramgen_curve:P-256','-nodes','-days','30',
            '-subj','/CN=ASWired isolated CI CA','-keyout',str(ca_key),'-out',str(ca),
            '-addext','basicConstraints=critical,CA:TRUE','-addext','keyUsage=critical,keyCertSign,cRLSign')
    def certificate(name,serial):
        key=root/(name+'.key');csr=root/(name+'.csr');cert=root/(name+'.pem');ext=root/(name+'.ext')
        ext.write_text('basicConstraints=CA:FALSE\nkeyUsage=digitalSignature\nextendedKeyUsage=serverAuth\nsubjectAltName=DNS:panel.example.test,DNS:probe.example.test\n')
        command('openssl','req','-new','-newkey','ec','-pkeyopt','ec_paramgen_curve:P-256','-nodes',
                '-subj','/CN=panel.example.test','-keyout',str(key),'-out',str(csr))
        command('openssl','x509','-req','-in',str(csr),'-CA',str(ca),'-CAkey',str(ca_key),'-set_serial',str(serial),
                '-days','10','-extfile',str(ext),'-out',str(cert))
        cert.write_bytes(cert.read_bytes()+ca.read_bytes())
        return cert,key
    old_cert,old_key=certificate('old',101);new_cert,new_key=certificate('new',102)
    https_port,admin_port=port(),port()
    targets={'panel':{'host':'panel.example.test','port':https_port},'komari':{'host':'probe.example.test','port':https_port}}
    env.write_text(f'ASWIRED_PUBLIC_URL=https://panel.example.test:{https_port}\nASWIRED_KOMARI_PUBLIC_URL=https://probe.example.test:{https_port}\n')
    config={'admin':{'listen':f'127.0.0.1:{admin_port}'},'apps':{'tls':{'certificates':{'load_files':[{'certificate':str(old_cert),'key':str(old_key),'tags':['original']}]}},
        'http':{'servers':{'test':{'listen':[f'127.0.0.1:{https_port}'],'automatic_https':{'disable_certificates':True,'disable_redirects':True},
            'routes':[{'match':[{'host':['panel.example.test','probe.example.test']}],'handle':[{'handler':'static_response','body':'original website'}]}]}}}}}
    initial=root/'original.json';initial.write_text(json.dumps(config))
    processes=[]
    log=(root/'caddy.log').open('wb')
    def start(path):
        process=subprocess.Popen(['/usr/bin/caddy','run','--config',str(path)],stdout=log,stderr=log,env={**os.environ,'SSL_CERT_FILE':str(ca)})
        processes.append(process)
        for _ in range(80):
            try:worker.caddy();return process
            except OSError:time.sleep(.1)
        raise AssertionError('Caddy did not start')
    original_run=worker.run
    def run(args,data=None):
        if args[:3]==['systemctl','show','caddy']:return b'caddy\n'
        if args==['systemctl','daemon-reload']:return b''
        return original_run(args,data)
    try:
        with patch.dict(os.environ,{'SSL_CERT_FILE':str(ca)}),patch.object(worker,'STATE',state),patch.object(worker,'DATA',data),\
             patch.object(worker,'ENV',env),patch.object(worker,'CONFIG',state/'caddy/config.json'),\
             patch.object(worker,'DROPIN',dropin/'90-aswired-certificates.conf'),\
             patch.object(worker,'CADDY',f'http://127.0.0.1:{admin_port}/config/'),patch.object(worker,'run',side_effect=run):
            process=start(initial)
            original_fingerprint=worker.served_fingerprint(targets['panel'])
            request=dict(id='ci-deployment-request-0001',operation='deploy',certificateId='wildcard-test',sites=['panel','komari'],
                         certificate=new_cert.read_text(),privateKey=new_key.read_text(),createdAt=worker.now().isoformat())
            # Real OpenSSL rejects a mismatched key before Caddy is changed.
            bad={**request,'id':'ci-deployment-request-bad1','privateKey':old_key.read_text()}
            try:worker.apply(bad);raise AssertionError('mismatched key accepted')
            except ValueError:pass
            assert worker.served_fingerprint(targets['panel'])==original_fingerprint
            serial=worker.apply(request);assert serial=='102'
            new_fingerprint=worker.served_fingerprint(targets['panel']);assert new_fingerprint!=original_fingerprint
            assert worker.served_fingerprint(targets['komari'])==new_fingerprint
            assert set(worker.read_json(state/'bindings.json',{}))=={'panel','komari'}
            assert 'certificate_selection' in (state/'caddy/config.json').read_text()
            # Reusing an active request ID cannot overwrite its live key material.
            before_key=(state/'material'/request['id']/'privkey.pem').read_bytes()
            try:worker.apply({**request,'privateKey':old_key.read_text()});raise AssertionError('reused request id accepted')
            except ValueError:pass
            assert (state/'material'/request['id']/'privkey.pem').read_bytes()==before_key
            assert worker.served_fingerprint(targets['panel'])==new_fingerprint
            # The file selected by the service drop-in survives a real process restart.
            process.terminate();process.wait(timeout=15)
            process=start(state/'caddy/config.json')
            assert worker.served_fingerprint(targets['panel'])==new_fingerprint
            assert subprocess.run(['runuser','-u','aswired','--','test','-r',str(state/'material'/request['id']/'privkey.pem')]).returncode!=0
            assert subprocess.run(['runuser','-u','aswired','--','test','-r',str(state/'bindings.json')]).returncode==0
            # Force a failed post-load TLS check after switching back to the old
            # leaf. The worker must actually reload the previous good config.
            bad={**request,'id':'ci-deployment-request-bad2','certificate':old_cert.read_text(),'privateKey':old_key.read_text()}
            with patch.object(worker,'served_fingerprint',side_effect=ValueError('simulated failed live verification')),patch.object(worker,'VERIFY_TIMEOUT',0):
                try:worker.apply(bad);raise AssertionError('verification failure ignored')
                except ValueError:pass
            assert worker.served_fingerprint(targets['panel'])==new_fingerprint
            assert worker.read_json(state/'bindings.json',{})['panel']['serial']=='102'
            external={**request,'id':'ci-deployment-request-return','operation':'external','certificateId':'','certificate':'','privateKey':''}
            worker.apply(external)
            assert worker.served_fingerprint(targets['panel'])==original_fingerprint
            assert worker.read_json(state/'bindings.json',{})=={}
            assert worker.caddy()['apps']['http']['servers']['test']['routes']==config['apps']['http']['servers']['test']['routes']
            assert not (state/'transaction.json').exists()
        print('PASS: real Caddy TLS switch for both sites, key mismatch rejection, restart persistence, private-key boundary, live rollback and return to external management')
    finally:
        for process in processes:
            if process.poll() is None:process.terminate();process.wait(timeout=15)
        log.close()

"""Narrow privileged worker: deploy validated website certificates to local Caddy.

Only panel/komari from root-owned server.env are valid targets. The controller
cannot supply paths, commands, proxy configuration, or an administrative URL.
"""
import copy
import datetime
import fcntl
import grp
import hashlib
import json
import os
import pathlib
import pwd
import re
import shlex
import socket
import ssl
import stat
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request

DATA = pathlib.Path('/var/lib/aswired')
STATE = pathlib.Path('/var/lib/aswired-certificates')
ENV = pathlib.Path('/etc/aswired/server.env')
DROPIN = pathlib.Path('/etc/systemd/system/caddy.service.d/90-aswired-certificates.conf')
CADDY = 'http://127.0.0.1:2019/config/'
CONFIG = STATE / 'caddy/config.json'
REQUEST = 'site-certificate-request.json'
LIMIT = 2 << 20
VERIFY_TIMEOUT = 30


def now():
    return datetime.datetime.now(datetime.timezone.utc)


def run(args, data=None):
    result = subprocess.run(args, input=data, capture_output=True, timeout=30)
    if result.returncode:
        raise ValueError('command failed: ' + pathlib.Path(args[0]).name)
    return result.stdout


def directory(path, mode=0o700, group=0):
    if path.exists() or path.is_symlink():
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError('unsafe worker directory')
    else:
        path.mkdir(mode=mode)
    os.chown(path, 0, group)
    os.chmod(path, mode)


def atomic(path, value, mode=0o600, group=0):
    fd, name = tempfile.mkstemp(prefix='.certificate-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as file:
            os.fchown(file.fileno(), 0, group)
            os.fchmod(file.fileno(), mode)
            file.write(value)
            file.flush()
            os.fsync(file.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def write_json(path, value, public=False):
    atomic(path, json.dumps(value).encode(), 0o640 if public else 0o600,
           grp.getgrnam('aswired').gr_gid if public else 0)


def read_json(path, default):
    try:
        return json.loads(path.read_bytes())
    except FileNotFoundError:
        return default


def status(phase, request=None, message='', serial=''):
    request = request or {}
    write_json(STATE/'status.json', dict(phase=phase, message=message, serial=serial,
        requestId=request.get('id', ''), certificateId=request.get('certificateId', ''),
        sites=request.get('sites', []), updatedAt=now().isoformat()), public=True)


def read_request():
    directory_fd = os.open(DATA, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        try:
            fd = os.open(REQUEST, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
        except FileNotFoundError:
            return None
        with os.fdopen(fd, 'rb') as file:
            info = os.fstat(file.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != pwd.getpwnam('aswired').pw_uid
                    or info.st_mode & 0o077 or info.st_size > LIMIT):
                raise ValueError('unsafe certificate request')
            row = json.loads(file.read(LIMIT+1))
        if set(row) != {'id', 'operation', 'certificateId', 'sites', 'certificate', 'privateKey', 'createdAt'}:
            raise ValueError('invalid request fields')
        if not isinstance(row['id'], str) or not re.fullmatch(r'[A-Za-z0-9_-]{16,128}', row['id']):
            raise ValueError('invalid request id')
        if row['operation'] not in ('deploy', 'external'):
            raise ValueError('invalid operation')
        if (not isinstance(row['sites'], list) or not 1 <= len(row['sites']) <= 2
                or any(site not in ('panel', 'komari') for site in row['sites'])
                or len(set(row['sites'])) != len(row['sites'])):
            raise ValueError('invalid sites')
        if not all(isinstance(row[key], str) for key in ('certificateId', 'certificate', 'privateKey', 'createdAt')):
            raise ValueError('invalid material')
        if row['operation'] == 'deploy' and not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', row['certificateId']):
            raise ValueError('invalid certificate id')
        if row['operation'] == 'external' and any(row[key] for key in ('certificateId', 'certificate', 'privateKey')):
            raise ValueError('unexpected certificate material')
        created = datetime.datetime.fromisoformat(row['createdAt'].replace('Z', '+00:00'))
        if not -5 <= (now()-created).total_seconds() <= 600:
            raise ValueError('expired request')
        return row
    finally:
        os.close(directory_fd)


def website_targets():
    info = ENV.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise ValueError('unsafe deployment environment')
    result = {}
    fields = {'ASWIRED_PUBLIC_URL':'panel', 'ASWIRED_KOMARI_PUBLIC_URL':'komari'}
    for line in ENV.read_text().splitlines():
        key, sep, value = line.partition('=')
        if key not in fields:
            continue
        parts = shlex.split(value, comments=True)
        if len(parts) != 1:
            raise ValueError('invalid website URL')
        url = urllib.parse.urlsplit(parts[0])
        if (url.scheme != 'https' or url.username or url.password or not url.hostname
                or not re.fullmatch(r'[A-Za-z0-9.-]+', url.hostname)):
            raise ValueError('website requires an HTTPS DNS name')
        result[fields[key]] = {'host':url.hostname.lower(), 'port':url.port or 443}
    return result


def caddy(value=None):
    request = urllib.request.Request(CADDY, data=None if value is None else json.dumps(value).encode(),
        method='GET' if value is None else 'POST', headers={'Content-Type':'application/json'})
    # POST /load replaces an entire config atomically. Redirects and proxies are
    # deliberately disabled; neither endpoint nor payload comes from the browser.
    if value is not None:
        request.full_url = CADDY.removesuffix('config/') + 'load'
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(request, timeout=30) as response:
        raw = response.read((8 << 20)+1)
        if len(raw) > 8 << 20:
            raise ValueError('Caddy configuration is too large')
        return json.loads(raw) if value is None else None


def validate_material(request, targets, directory_path):
    cert, key = request['certificate'].encode(), request['privateKey'].encode()
    if len(cert) > 1 << 20 or len(key) > 128 << 10:
        raise ValueError('material too large')
    group = grp.getgrnam('caddy').gr_gid
    # A reused request id must never overwrite files referenced by the active
    # proxy, even when the new certificate subsequently fails validation.
    if directory_path.exists() or directory_path.is_symlink():
        raise ValueError('certificate request id was already used')
    directory(directory_path, 0o750, group)
    chain_path, key_path = directory_path/'fullchain.pem', directory_path/'privkey.pem'
    atomic(chain_path, cert, 0o640, group)
    atomic(key_path, key, 0o640, group)
    pub_cert = run(['openssl','x509','-in',str(chain_path),'-pubkey','-noout'])
    pub_key = run(['openssl','pkey','-in',str(key_path),'-pubout'])
    if pub_cert != pub_key:
        raise ValueError('certificate and key do not match')
    run(['openssl','x509','-in',str(chain_path),'-checkend','0','-noout'])
    for site in request['sites']:
        run(['openssl','verify','-purpose','sslserver','-verify_hostname',targets[site]['host'],
             '-untrusted',str(chain_path),str(chain_path)])
    der = run(['openssl','x509','-in',str(chain_path),'-outform','DER'])
    serial = run(['openssl','x509','-in',str(chain_path),'-serial','-noout']).decode().strip().split('=',1)[1]
    return {'certificate':str(chain_path), 'key':str(key_path)}, hashlib.sha256(der).hexdigest(), str(int(serial,16))


def host_matches(pattern, host):
    return pattern.lower() == host or (pattern.startswith('*.') and host.endswith(pattern[1:])
                                     and pattern.count('.') == host.count('.'))


def route_host(routes, host):
    for route in routes:
        for matcher in route.get('match', []):
            if any(host_matches(pattern, host) for pattern in matcher.get('host', [])):
                return True
        for handler in route.get('handle', []):
            if route_host(handler.get('routes', []), host):
                return True
    return False


def compile_config(original, state, request, targets, material=None, serial=''):
    config, state = copy.deepcopy(original), copy.deepcopy(state)
    servers = config.get('apps', {}).get('http', {}).get('servers', {})
    tls = config.setdefault('apps', {}).setdefault('tls', {})
    loaders = tls.setdefault('certificates', {})
    for site in request['sites']:
        tag = 'aswired-site-' + site
        previous = state.pop(site, {})
        loaders['load_files'] = [row for row in loaders.get('load_files', []) if tag not in row.get('tags', [])]
        for name, server in servers.items():
            server['tls_connection_policies'] = [p for p in server.get('tls_connection_policies', [])
                if not (previous and p.get('match') == {'sni':[previous['host']]}
                    and p.get('certificate_selection') == {'serial_number':[previous['serial']]})]
            if not server['tls_connection_policies']:
                server.pop('tls_connection_policies', None)
            auto = server.get('automatic_https', {})
            if name in previous.get('addedSkips', []):
                auto['skip_certificates'] = [h for h in auto.get('skip_certificates', []) if h != previous.get('host')]
                if not auto['skip_certificates']:
                    auto.pop('skip_certificates', None)
        if request['operation'] == 'external':
            continue
        target = targets[site]
        if any(binding.get('host') == target['host'] for binding in state.values()):
            raise ValueError('website domains must be distinct')
        binding = {'host':target['host'], 'port':target['port'], 'certificateId':request['certificateId'],
                   'serial':serial, 'addedSkips':[]}
        found = False
        for name, server in servers.items():
            if not any(address.rsplit(':',1)[-1] == str(target['port']) for address in server.get('listen', [])):
                continue
            if not route_host(server.get('routes', []), target['host']):
                continue
            policies = server.get('tls_connection_policies', [{}])
            if any(p.get('match') and set(p['match']) != {'sni'} for p in policies):
                raise ValueError('custom TLS matcher requires manual review')
            if any(p.get('match') and any(host_matches(h, target['host']) for h in p['match'].get('sni', [])) for p in policies):
                raise ValueError('custom TLS SNI policy requires manual review')
            base = next((copy.deepcopy(p) for p in policies if not p.get('match')), None)
            if base is None:
                raise ValueError('no default TLS connection policy')
            # Preserve TLS versions, client authentication, ALPN and other policy
            # settings. Only this site's certificate selection is replaced.
            base['match'] = {'sni':[target['host']]}
            # CertMagic reuses certificates by DER hash and does not update tags
            # on a cached certificate. Select the validated leaf by serial so a
            # shared/wildcard certificate also works across sites and reloads.
            base['certificate_selection'] = {'serial_number':[serial]}
            server['tls_connection_policies'] = [base, *policies]
            skip = server.setdefault('automatic_https', {}).setdefault('skip_certificates', [])
            if target['host'] not in skip:
                skip.append(target['host'])
                binding['addedSkips'].append(name)
            found = True
        if not found:
            raise ValueError('website is not served by local Caddy')
        loaders['load_files'].append({**material, 'tags':[tag, 'aswired-serial-'+serial]})
        state[site] = binding
    if not loaders.get('load_files'):
        loaders.pop('load_files', None)
    return config, state


def served_fingerprint(target):
    with socket.create_connection(('127.0.0.1', target['port']), timeout=4) as sock:
        with ssl.create_default_context().wrap_socket(sock, server_hostname=target['host']) as conn:
            return hashlib.sha256(conn.getpeercert(binary_form=True)).hexdigest()


def restore():
    journal = read_json(STATE/'transaction.json', None)
    if journal is None:
        return
    # All journal paths/content were written by this root worker, never supplied
    # by the controller. Keep the journal when rollback cannot be completed.
    group = grp.getgrnam('caddy').gr_gid
    for path, key in [(CONFIG,'configFile'), (DROPIN,'dropin')]:
        if journal[key] is None:
            path.unlink(missing_ok=True)
        else:
            atomic(path, journal[key].encode(), 0o640 if path == CONFIG else 0o644, group if path == CONFIG else 0)
    run(['systemctl','daemon-reload'])
    caddy(journal['activeConfig'])
    write_json(STATE/'state.json', journal['state'])
    write_json(STATE/'bindings.json', {site:{key:row[key] for key in ('certificateId','serial')}
        for site,row in journal['state'].items()}, public=True)
    (STATE/'transaction.json').unlink()


def apply(request):
    if run(['systemctl','show','caddy','-p','User','--value']).decode().strip() != 'caddy':
        raise ValueError('unsupported Caddy service user')
    targets = website_targets()
    if any(site not in targets for site in request['sites']):
        raise ValueError('website target is not configured')
    original, state = caddy(), read_json(STATE/'state.json', {})
    material, expected, serial = None, '', ''
    if request['operation'] == 'deploy':
        material, expected, serial = validate_material(request, targets, STATE/'material'/request['id'])
    updated, new_state = compile_config(original, state, request, targets, material, serial)
    if updated == original and new_state == state:
        return serial
    journal = {'activeConfig':original, 'state':state,
               'configFile':CONFIG.read_text() if CONFIG.exists() else None,
               'dropin':DROPIN.read_text() if DROPIN.exists() else None}
    # Keep an operator-readable recovery backup even after a successful change.
    write_json(STATE/'backups'/(request['id']+'.json'), journal)
    write_json(STATE/'transaction.json', journal)
    try:
        atomic(CONFIG,json.dumps(updated).encode(),0o640,grp.getgrnam('caddy').gr_gid)
        run(['/usr/bin/caddy','validate','--config',str(CONFIG)])
        dropin = ('[Service]\nExecStart=\nExecStart=/usr/bin/caddy run --environ --config '+str(CONFIG)+
                  '\nExecReload=\nExecReload=/usr/bin/caddy reload --config '+str(CONFIG)+' --force\n')
        atomic(DROPIN,dropin.encode(),0o644)
        run(['systemctl','daemon-reload'])
        caddy(updated)
        for site in request['sites']:
            deadline = time.monotonic()+VERIFY_TIMEOUT
            while True:
                try:
                    actual = served_fingerprint(targets[site])
                    if expected and actual != expected:
                        raise ValueError('served certificate differs from requested certificate')
                    break
                except (OSError, ValueError):
                    if time.monotonic() >= deadline:
                        raise ValueError('website TLS verification failed') from None
                    time.sleep(1)
        write_json(STATE/'state.json',new_state)
        write_json(STATE/'bindings.json',{site:{key:row[key] for key in ('certificateId','serial')}
            for site,row in new_state.items()},public=True)
        (STATE/'transaction.json').unlink()
        return serial
    except Exception:
        restore()
        raise


def main():
    if os.geteuid() != 0:
        raise SystemExit('root required')
    directory(STATE,0o755)
    group = grp.getgrnam('caddy').gr_gid
    for child, mode, gid in [('material',0o750,group),('caddy',0o750,group),('backups',0o700,0)]:
        directory(STATE/child,mode,gid)
    directory(DROPIN.parent,0o755)
    with (STATE/'worker.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        if sys.argv[1:] == ['--recover']:
            previous = read_json(STATE/'status.json', {})
            if (STATE/'transaction.json').exists():
                restore()
                status('failed',message='证书部署进程中断，已恢复原反代配置')
                (DATA/REQUEST).unlink(missing_ok=True)
            elif previous.get('phase') == 'applying':
                status('failed',message='证书部署进程中断，尚未切换网站配置')
                (DATA/REQUEST).unlink(missing_ok=True)
            return
        if sys.argv[1:]:
            raise SystemExit('unexpected arguments')
        request = None
        try:
            restore()
            request = read_request()
            if request is None:
                return
            status('applying',request,'正在校验、部署并验证网站证书')
            serial = apply(request)
            status('completed',request,'网站证书已实际生效' if request['operation']=='deploy' else '网站已交回 Caddy 管理',serial)
        except Exception as error:
            detail = str(error) if type(error) is ValueError else type(error).__name__
            print('Website certificate operation failed:',detail,file=sys.stderr)
            status('failed',request,'操作失败，未完成网站证书切换；请检查 aswired-certificates 服务与恢复备份')
        finally:
            (DATA/REQUEST).unlink(missing_ok=True)


if __name__ == '__main__':
    main()

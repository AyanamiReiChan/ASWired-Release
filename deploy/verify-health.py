"""Read-only verification of the configured, currently running managed release."""
import argparse
import ipaddress
import json
import pathlib
import re
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request

CURRENT = pathlib.Path('/opt/aswired/current')
ENV = pathlib.Path('/etc/aswired')
UNITS = pathlib.Path('/etc/systemd/system')
PROC = pathlib.Path('/proc')


class VerificationError(ValueError):
    pass


def environment(path, wanted):
    # Parse only endpoint settings, without sourcing shell code or disclosing
    # unrelated service credentials. systemd EnvironmentFile does not expand $.
    result = {}
    for raw in path.read_text(encoding='utf-8').splitlines():
        line = raw.strip()
        if not line or line.startswith(('#', ';')):
            continue
        key, sep, value = line.partition('=')
        if sep and key.strip() in wanted:
            parts = shlex.split(value)
            if len(parts) > 1:
                raise VerificationError('监听设置格式无效：'+key.strip())
            result[key.strip()] = parts[0] if parts else ''
    return result


def address(host, port):
    if not re.fullmatch(r'[0-9]{1,5}', str(port)) or not 1 <= int(port) <= 65535:
        raise VerificationError('监听端口无效')
    if not host or host == 'localhost':
        host = '127.0.0.1'
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        raise VerificationError('健康检查需要配置 IP 监听地址') from None
    if ip.is_unspecified:
        ip = ipaddress.ip_address('::1' if ip.version == 6 else '127.0.0.1')
    authority = f'[{ip}]' if ip.version == 6 else str(ip)
    return f'http://{authority}:{int(port)}'


def listen(value):
    if value.startswith('['):
        match = re.fullmatch(r'\[([^]]+)\]:([0-9]+)', value)
        if not match:
            raise VerificationError('IPv6 监听地址格式无效')
        return address(*match.groups())
    host, sep, port = value.rpartition(':')
    if not sep or ':' in host:
        raise VerificationError('监听地址格式无效')
    return address(host, port)


def endpoints():
    server = environment(ENV/'server.env', {'ASWIRED_LISTEN'})
    komari = environment(ENV/'komari.env', {'KOMARI_LISTEN'})
    web = environment(ENV/'web.env', {'HOST', 'PORT'})
    return {
        'aswired-server': listen(server.get('ASWIRED_LISTEN') or '127.0.0.1:12889')+'/healthz',
        'komari': listen(komari.get('KOMARI_LISTEN') or '0.0.0.0:25774')+'/api/version',
        'aswired-web': address(web.get('HOST', '0.0.0.0'), web.get('PORT') or '3000')+'/',
    }


def service(name):
    result = subprocess.run(['systemctl', 'show', name, '-p', 'ActiveState', '-p', 'MainPID'],
                            capture_output=True, text=True, timeout=5)
    if result.returncode:
        raise VerificationError(name+' 无法读取服务状态')
    return dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)


def running_release(root):
    binaries = {'aswired-server':'bin/aswired-server', 'komari':'bin/komari', 'aswired-web':'runtime/node'}
    for name, binary in binaries.items():
        state = service(name)
        pid = state.get('MainPID', '')
        if state.get('ActiveState') != 'active' or not pid.isdigit() or int(pid) == 0:
            raise VerificationError(name+' 服务尚未运行')
        if (PROC/pid/'exe').resolve() != root/binary:
            raise VerificationError(name+' 仍未运行目标版本程序')
        if name == 'aswired-web' and (PROC/pid/'cwd').resolve() != root/'web':
            raise VerificationError('前端仍未使用目标版本网站目录')
        if (UNITS/(name+'.service')).read_bytes() != (root/'deploy/systemd'/(name+'.service')).read_bytes():
            raise VerificationError(name+' 服务定义与目标版本不一致')
    for name in ['aswired-update.path', 'aswired-certificates.path', 'aswired-upgrade-backups.path']:
        if service(name).get('ActiveState') != 'active':
            raise VerificationError(name+' 监听服务尚未运行')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def check_http(name, url, version):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(urllib.request.Request(url, headers={'User-Agent':'ASWired-health-check'}), timeout=3) as response:
            if response.status != 200:
                raise VerificationError('HTTP 状态异常')
            if name == 'aswired-web':
                if response.headers.get_content_type() != 'text/html':
                    raise VerificationError('网站未返回 HTML')
                return
            raw = response.read(8193)
            if len(raw) > 8192:
                raise VerificationError('健康响应过大')
            row = json.loads(raw)
            valid = (row.get('status') == 'ok' and row.get('version') == version) if name == 'aswired-server' else (
                row.get('status') == 'success' and row.get('data', {}).get('version') == '1.2.5-fix2-aswired.'+version.removeprefix('v'))
            if not valid:
                raise VerificationError('实际响应版本与目标版本不一致')
    except VerificationError as error:
        raise VerificationError(f'{name} ({url})：{error}') from None
    except urllib.error.HTTPError as error:
        code = error.code
        error.close()
        raise VerificationError(f'{name} ({url})：HTTP {code}') from None
    except Exception as error:
        # Never put response bodies or service credentials into status/logs.
        raise VerificationError(f'{name} ({url})：{type(error).__name__}') from None


def verify_once(version):
    root = CURRENT.resolve()
    if (root/'VERSION').read_text().strip() != version:
        raise VerificationError('当前版本目录与目标版本不一致')
    running_release(root)
    urls = endpoints()
    for name, url in urls.items():
        check_http(name, url, version)
    return urls


def verify(version, timeout):
    deadline = time.monotonic()+timeout
    while True:
        try:
            return verify_once(version)
        except (VerificationError, OSError, ValueError, subprocess.SubprocessError) as error:
            if time.monotonic() >= deadline:
                message = str(error) if isinstance(error, VerificationError) else type(error).__name__
                raise VerificationError(message) from None
            time.sleep(1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', required=True)
    parser.add_argument('--timeout', type=int, default=30)
    args = parser.parse_args()
    if not re.fullmatch(r'v[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?', args.version) or not 0 <= args.timeout <= 120:
        parser.error('invalid version or timeout')
    try:
        urls = verify(args.version, args.timeout)
    except VerificationError as error:
        print('升级后健康检查未通过：'+str(error), file=sys.stderr)
        return 1
    print('目标版本及三个服务健康检查通过：'+', '.join(urls.values()))
    return 0


if __name__ == '__main__':
    sys.exit(main())

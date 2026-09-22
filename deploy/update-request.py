"""Privileged, version-only update worker for the managed Linux installation."""
import datetime
import fcntl
import grp
import json
import os
import pathlib
import re
import stat
import subprocess
import sys
import tempfile

DATA = pathlib.Path('/var/lib/aswired')
STATE = pathlib.Path('/var/lib/aswired-updater')
CURRENT = pathlib.Path('/opt/aswired/current')


def version_key(value):
    if not isinstance(value, str) or len(value) > 100:
        raise ValueError('invalid version')
    match = re.fullmatch(r'v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-([0-9A-Za-z.-]+))?', value)
    if not match:
        raise ValueError('invalid version')
    pre = match[4]
    identifiers = []
    if pre:
        for part in pre.split('.'):
            if not part or (part.isdigit() and len(part) > 1 and part.startswith('0')):
                raise ValueError('invalid prerelease')
            identifiers.append((0, int(part)) if part.isdigit() else (1, part))
    return (*map(int, match.group(1, 2, 3)), 0 if pre else 1, tuple(identifiers))


def status(phase, version='', message='', backup=''):
    row = dict(phase=phase, version=version, message=message, backup=backup,
               updatedAt=datetime.datetime.now(datetime.timezone.utc).isoformat())
    fd, name = tempfile.mkstemp(prefix='.status-', dir=STATE)
    try:
        with os.fdopen(fd, 'w') as file:
            os.fchown(file.fileno(), 0, grp.getgrnam('aswired').gr_gid)
            os.fchmod(file.fileno(), 0o640)
            json.dump(row, file)
            file.flush()
            os.fsync(file.fileno())
        os.replace(name, STATE / 'status.json')
    finally:
        if os.path.exists(name):
            os.unlink(name)


def read_request():
    # Never follow a controller-writable symlink or block on a FIFO/device.
    directory = os.open(DATA, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        try:
            fd = os.open('update-request.json', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        except FileNotFoundError:
            return None
        with os.fdopen(fd, 'rb') as file:
            info = os.fstat(file.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > 4096:
                raise ValueError('invalid request file')
            row = json.loads(file.read(4097))
            if set(row) != {'version', 'createdAt'}:
                raise ValueError('invalid request fields')
            version_key(row['version'])
            created = datetime.datetime.fromisoformat(row['createdAt'].replace('Z', '+00:00'))
            age = (datetime.datetime.now(datetime.timezone.utc) - created).total_seconds()
            if not 0 <= age <= 600:
                raise ValueError('expired request')
            return row['version']
    finally:
        os.close(directory)


def main():
    if os.geteuid() != 0:
        raise SystemExit('root required')
    if not stat.S_ISDIR(DATA.lstat().st_mode):
        raise SystemExit('unsafe controller data directory')
    info = STATE.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise SystemExit('unsafe updater state directory')
    if sys.argv[1:] == ['--recover']:
        try:
            row = json.loads((STATE / 'status.json').read_text())
            if row.get('phase') == 'updating':
                status('failed', row.get('version', ''), '更新进程中断；请检查 aswired-update 服务日志和备份后处理')
        except FileNotFoundError:
            pass
        return
    if sys.argv[1:]:
        raise SystemExit('unexpected arguments')
    with (STATE / 'worker.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        version = ''
        try:
            version = read_request()
            if version is None:
                return
            if version_key(version) <= version_key((CURRENT / 'VERSION').read_text().strip()):
                raise ValueError('target must be newer')
            # Automatic PostgreSQL restoration is deliberately not inferred.
            if (DATA / 'database-active.enc').exists() or (DATA / 'database-pending.enc').exists():
                raise ValueError('managed database migration requires manual upgrade')
            status('updating', version, '正在下载校验、备份并升级；服务会短暂重启')
            result = subprocess.run(['/bin/bash', str(CURRENT / 'update.sh'), version], check=False)
            if result.returncode:
                status('failed', version, '升级未通过；请查看 aswired-update 服务日志和 /var/backups/aswired 备份')
                return
            actual = (CURRENT / 'VERSION').read_text().strip()
            if actual != version:
                raise ValueError('active version mismatch')
            status('completed', version, '升级完成，服务健康检查通过', '/var/backups/aswired')
        except Exception as error:
            # Do not publish raw request values or command output to browsers.
            print('Update rejected or failed:', type(error).__name__, file=sys.stderr)
            status('failed', version if isinstance(version, str) and re.fullmatch(r'v[0-9A-Za-z.-]{1,99}', version) else '',
                   '升级请求无效或执行失败；请检查更新服务日志，重新检查版本后再试')
        finally:
            # Keep the request present until completion to close the enqueue race.
            try:
                (DATA / 'update-request.json').unlink()
            except FileNotFoundError:
                pass


if __name__ == '__main__':
    main()

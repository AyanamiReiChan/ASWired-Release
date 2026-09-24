"""CI-only real systemd/path and unprivileged upgrade-backup request tests."""
import datetime
import fcntl
import json
import os
import pathlib
import stat
import subprocess
import time
import uuid

assert os.geteuid() == 0
assert os.environ.get('GITHUB_ACTIONS') == 'true', 'Disposable GitHub Actions runner only'
ROOT = pathlib.Path('/var/backups/aswired')
DATA = pathlib.Path('/var/lib/aswired')
STATE = pathlib.Path('/var/lib/aswired-updater/upgrade-backups.json')
version = pathlib.Path('/opt/aswired/current/VERSION').read_text().strip()


def run(args):
    return subprocess.check_output(args, text=True).strip()


def aswired(args, check=True):
    return subprocess.run(['runuser', '-u', 'aswired', '--', *args], check=check, capture_output=True, text=True)


def submit(operation='list', backup_id=''):
    request = {'id': uuid.uuid4().hex, 'operation': operation, 'backupId': backup_id, 'createdAt': datetime.datetime.now(datetime.timezone.utc).isoformat()}
    # Match the controller's atomic, no-overwrite request publication, executed
    # under its real service account. The root helper never receives a path.
    script = "import json,os,pathlib,sys; p=pathlib.Path('/var/lib/aswired'); row=json.loads(sys.argv[1]); tmp=p/('.backup-ci-'+row['id']); tmp.write_text(json.dumps(row)); os.link(tmp,p/'upgrade-backup-request.json'); tmp.unlink()"
    aswired(['python3', '-c', script, json.dumps(request)])
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if STATE.exists() and not (DATA/'upgrade-backup-request.json').exists():
            result = json.loads(STATE.read_text())
            if result.get('requestId') == request['id'] and result.get('phase') in ('completed', 'failed'):
                return result
        time.sleep(.1)
    raise AssertionError('backup request did not complete through installed path/service')


# v1.0.8's old update.sh installs all new units but does not enable new paths.
# Starting the new controller unit must pull this watcher in through Wants.
subprocess.run(['systemctl', 'stop', 'aswired-server', 'aswired-upgrade-backups.path'], check=True)
subprocess.run(['systemctl', 'disable', 'aswired-upgrade-backups.path'], check=True)
subprocess.run(['systemctl', 'start', 'aswired-server'], check=True)
assert run(['systemctl', 'is-active', 'aswired-upgrade-backups.path']) == 'active'
assert 'aswired-upgrade-backups.path' in run(['systemctl', 'show', 'aswired-server', '-p', 'Wants', '--value'])
pid = run(['systemctl', 'show', 'aswired-server', '-p', 'MainPID', '--value'])
assert pid != '0'
checker = ['/usr/bin/python3', '/opt/aswired/current/deploy/verify-health.py', '--version', version]
subprocess.run(checker, check=True)
subprocess.run(['systemctl', 'stop', 'aswired-upgrade-backups.path'], check=True)
assert subprocess.run([*checker, '--timeout', '0'], capture_output=True).returncode != 0
subprocess.run(['systemctl', 'start', 'aswired-upgrade-backups.path'], check=True)

ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
backup = ROOT/'20991231T235958Z'
assert not backup.exists()
backup.mkdir(mode=0o700)
for name, contents in [('data.tar.gz', b'CI-only fake private data'), ('config.tar.gz', b'CI-only fake private config'), ('previous-release', ('/opt/aswired/releases/'+version+'\n').encode()), ('postgres-backup', b'CI-only fake database dump')]:
    path = backup/name
    path.write_bytes(contents)
    path.chmod(0o600)
original_modes = {path.name: path.stat().st_mode for path in backup.iterdir()}

try:
    result = submit()
    assert result['phase'] == 'completed', result['message']
    row = next(item for item in result['items'] if item['id'] == backup.name)
    assert row['deletable'] and row['previousVersion'] == version
    assert {item['name'] for item in row['files']} == set(original_modes)
    assert row['sizeBytes'] == sum(path.stat().st_size for path in backup.iterdir())
    assert 'CI-only fake' not in json.dumps(result)
    assert {path.name: path.stat().st_mode for path in backup.iterdir()} == original_modes
    assert stat.S_IMODE(backup.stat().st_mode) == 0o700
    assert STATE.stat().st_uid == 0 and stat.S_IMODE(STATE.stat().st_mode) == 0o640
    aswired(['test', '-r', str(STATE)])
    assert aswired(['test', '-w', str(STATE)], check=False).returncode != 0
    assert aswired(['test', '-r', str(backup/'data.tar.gz')], check=False).returncode != 0

    # Holding the actual updater flock protects archives through the real
    # systemd worker, while status still remains readable by the controller.
    with open('/run/lock/aswired-update.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = submit('delete', backup.name)
        assert result['phase'] == 'failed' and backup.exists()
        assert all(not row['deletable'] for row in result['items'])
    result = submit('delete', backup.name)
    assert result['phase'] == 'completed' and not backup.exists(), result['message']
    assert not any(row['id'] == backup.name for row in result['items'])
    assert run(['systemctl', 'show', 'aswired-server', '-p', 'MainPID', '--value']) == pid
finally:
    # This is a disposable CI fixture; never recurse through the backup root.
    if backup.exists():
        for name in ('data.tar.gz', 'config.tar.gz', 'previous-release', 'postgres-backup'):
            path = backup/name
            if path.exists():
                path.unlink()
        backup.rmdir()

subprocess.run(checker, check=True)
print('PASS: legacy upgrade first-start Wants, installed root worker, private backup permissions, metadata-only status, updater flock exclusion and exact deletion without controller restart')

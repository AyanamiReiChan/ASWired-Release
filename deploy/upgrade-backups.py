"""Root-only metadata and deletion worker for fixed managed-upgrade backups.

Requests never contain filesystem paths. Archives are neither opened nor
unpacked, and deletion uses validated directory descriptors without recursion.
"""
import datetime
import fcntl
import grp
import json
import os
import pathlib
import re
import stat
import sys
import uuid

DATA = pathlib.Path('/var/lib/aswired')
STATE = pathlib.Path('/var/lib/aswired-updater')
BACKUPS = pathlib.Path('/var/backups/aswired')
UPDATE_LOCK = pathlib.Path('/run/lock/aswired-update.lock')
REQUEST = 'upgrade-backup-request.json'
STATUS = 'upgrade-backups.json'
KNOWN_FILES = ('data.tar.gz', 'config.tar.gz', 'previous-release', 'postgres-backup')
REQUIRED_FILES = frozenset(KNOWN_FILES[:3])
MAX_ITEMS = 1000
MAX_STATE = 2 * 1024 * 1024
ROOT_UID = 0


class Rejected(ValueError):
    pass


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def timestamp_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9]{8}T[0-9]{6}Z', value):
        return False
    try:
        datetime.datetime.strptime(value, '%Y%m%dT%H%M%SZ')
        return True
    except ValueError:
        return False


def no_duplicates(pairs):
    row = {}
    for key, value in pairs:
        if key in row:
            raise Rejected('请求字段重复')
        row[key] = value
    return row


def validate_request(row):
    if not isinstance(row, dict) or set(row) != {'id', 'operation', 'backupId', 'createdAt'}:
        raise Rejected('升级备份请求字段无效')
    if not isinstance(row['id'], str) or not re.fullmatch(r'[0-9a-f]{32}', row['id']):
        raise Rejected('升级备份请求标识无效')
    if row['operation'] not in ('list', 'delete') or not isinstance(row['backupId'], str):
        raise Rejected('升级备份操作无效')
    if row['operation'] == 'list' and row['backupId'] != '' or row['operation'] == 'delete' and not timestamp_id(row['backupId']):
        raise Rejected('升级备份标识无效')
    value = row['createdAt']
    if not isinstance(value, str) or not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,9})?(?:Z|[+-][0-9]{2}:[0-9]{2})', value):
        raise Rejected('升级备份请求时间无效')
    try:
        created = datetime.datetime.fromisoformat(value.replace('Z', '+00:00'))
        age = (datetime.datetime.now(datetime.timezone.utc) - created).total_seconds()
    except ValueError:
        raise Rejected('升级备份请求时间无效') from None
    if not 0 <= age <= 600:
        raise Rejected('升级备份请求已过期，请刷新后重试')
    return row


def identity(info):
    return info.st_dev, info.st_ino


def secure_directory(path, owned=False):
    """Walk every absolute path component with O_NOFOLLOW, including parents."""
    path = pathlib.Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise Rejected('目录路径无效')
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        if owned:
            safe_owner(os.fstat(fd), directory=True)
        return fd
    except BaseException:
        os.close(fd)
        raise


def safe_owner(info, directory=False):
    valid_type = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if not valid_type or info.st_uid != ROOT_UID or info.st_mode & 0o022 or (not directory and info.st_nlink != 1):
        raise Rejected('备份路径类型、所有权或权限不安全，需服务器管理员处理')


def lstat_at(directory, name):
    try:
        return os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return None


def read_json(directory, name, maximum, owned=False, publication_link=False):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    with os.fdopen(fd, 'rb') as file:
        before = os.fstat(file.fileno())
        # The controller atomically links its complete staging file to the
        # fixed request name, then unlinks staging. systemd may wake between
        # those calls. Only requests permit that transient second link; root
        # metadata, locks and archives retain the single-link requirement.
        valid_links = before.st_nlink in (1, 2) if publication_link else before.st_nlink == 1
        if not stat.S_ISREG(before.st_mode) or not valid_links or not 0 < before.st_size <= maximum:
            raise Rejected('请求或状态文件类型、大小无效')
        if owned:
            safe_owner(before)
        raw = file.read(maximum + 1)
        after = os.fstat(file.fileno())
        if len(raw) > maximum or (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise Rejected('请求或状态文件在读取时发生变化')
        return json.loads(raw, object_pairs_hook=no_duplicates)


def load_state(directory):
    try:
        row = read_json(directory, STATUS, MAX_STATE, owned=True)
        if not isinstance(row, dict) or not isinstance(row.get('items'), list) or len(row['items']) > MAX_ITEMS:
            raise Rejected('升级备份状态无效')
        return row
    except FileNotFoundError:
        return {'phase': 'idle', 'requestId': '', 'operation': 'list', 'backupId': '', 'items': [], 'totalSizeBytes': 0, 'truncated': False, 'remainingCount': 0}


def publish(directory, row):
    row = dict(row, updatedAt=now())
    raw = json.dumps(row, ensure_ascii=False, separators=(',', ':')).encode()
    if len(raw) > MAX_STATE:
        raise Rejected('升级备份元数据过大')
    name = '.upgrade-backups-' + uuid.uuid4().hex
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
    try:
        with os.fdopen(fd, 'wb') as file:
            os.fchown(file.fileno(), ROOT_UID, grp.getgrnam('aswired').gr_gid)
            os.fchmod(file.fileno(), 0o640)
            file.write(raw)
            file.flush()
            os.fsync(file.fileno())
        os.replace(name, STATUS, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    finally:
        if lstat_at(directory, name) is not None:
            os.unlink(name, dir_fd=directory)


def previous_version(directory, expected):
    fd = os.open('previous-release', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    with os.fdopen(fd, 'rb') as file:
        info = os.fstat(file.fileno())
        safe_owner(info)
        if identity(info) != identity(expected) or info.st_size > 4096:
            raise Rejected('旧版本记录无效')
        value = file.read(4097).decode('utf-8').strip()
    match = re.fullmatch(r'/opt/aswired/releases/(v[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?)', value)
    if not match:
        raise Rejected('旧版本记录无效')
    return match[1]


def inspect_backup(root, name):
    created = datetime.datetime.strptime(name, '%Y%m%dT%H%M%SZ').replace(tzinfo=datetime.timezone.utc).isoformat()
    row = {'id': name, 'createdAt': created, 'previousVersion': '', 'sizeBytes': 0, 'files': [], 'deletable': False}
    directory = None
    snapshots = {}
    try:
        directory = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
        safe_owner(os.fstat(directory), directory=True)
        names = set(os.listdir(directory))
        if names - set(KNOWN_FILES):
            raise Rejected('备份包含额外文件或目录，需服务器管理员处理')
        for filename in KNOWN_FILES:
            if filename not in names:
                continue
            info = os.stat(filename, dir_fd=directory, follow_symlinks=False)
            safe_owner(info)
            snapshots[filename] = info
            row['files'].append({'name': filename, 'sizeBytes': info.st_size})
            row['sizeBytes'] += info.st_size
        if not REQUIRED_FILES.issubset(names) or any(snapshots[filename].st_size == 0 for filename in REQUIRED_FILES):
            raise Rejected('备份文件不完整，需服务器管理员处理')
        row['previousVersion'] = previous_version(directory, snapshots['previous-release'])
        row['deletable'] = True
        return row, snapshots, os.fstat(directory)
    except (OSError, UnicodeError, ValueError) as error:
        row['reason'] = str(error) if isinstance(error, Rejected) else '备份路径无法安全读取，需服务器管理员处理'
        return row, {}, None
    finally:
        if directory is not None:
            os.close(directory)


def scan():
    try:
        root = secure_directory(BACKUPS, owned=True)
    except FileNotFoundError:
        return {'items': [], 'totalSizeBytes': 0, 'truncated': False, 'remainingCount': 0}
    try:
        names = sorted(name for name in os.listdir(root) if timestamp_id(name))
        items = [inspect_backup(root, name)[0] for name in names[:MAX_ITEMS]]
        return {'items': items, 'totalSizeBytes': sum(row['sizeBytes'] for row in items), 'truncated': len(names) > MAX_ITEMS, 'remainingCount': max(0, len(names)-MAX_ITEMS)}
    finally:
        os.close(root)


def assert_update_idle(data, state):
    if lstat_at(data, 'update-request.json') is not None:
        raise Rejected('主控升级已排队或正在进行，请升级完成后重试')
    try:
        row = read_json(state, 'status.json', 16384, owned=True)
        if not isinstance(row, dict) or row.get('phase') not in ('idle', 'completed', 'failed', 'unavailable'):
            raise Rejected('主控升级尚未结束，请升级完成后重试')
    except FileNotFoundError:
        pass


def update_lock():
    directory = secure_directory(UPDATE_LOCK.parent)
    try:
        fd = os.open(UPDATE_LOCK.name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=directory)
    finally:
        os.close(directory)
    try:
        safe_owner(os.fstat(fd))
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except BaseException:
        os.close(fd)
        raise


def delete_backup(name):
    if not timestamp_id(name):
        raise Rejected('升级备份标识无效')
    root = secure_directory(BACKUPS, owned=True)
    directory = None
    try:
        row, snapshots, expected = inspect_backup(root, name)
        if not row['deletable']:
            raise Rejected(row['reason'])
        directory = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
        safe_owner(os.fstat(directory), directory=True)
        if identity(os.fstat(directory)) != identity(expected) or set(os.listdir(directory)) != set(snapshots):
            raise Rejected('备份目录发生变化，请刷新后重试')
        for filename, expected_file in snapshots.items():
            actual = os.stat(filename, dir_fd=directory, follow_symlinks=False)
            safe_owner(actual)
            if (actual.st_dev, actual.st_ino, actual.st_size, actual.st_mtime_ns) != (expected_file.st_dev, expected_file.st_ino, expected_file.st_size, expected_file.st_mtime_ns):
                raise Rejected('备份文件发生变化，请刷新后重试')
        # No recursion and no path concatenation from request fields. Unlink
        # removes only entries of the already opened, verified directory.
        for filename in KNOWN_FILES:
            if filename in snapshots:
                os.unlink(filename, dir_fd=directory)
        os.fsync(directory)
        if identity(os.stat(name, dir_fd=root, follow_symlinks=False)) != identity(expected):
            raise Rejected('备份目录发生变化，已停止删除')
        os.rmdir(name, dir_fd=root)
        os.fsync(root)
    finally:
        if directory is not None:
            os.close(directory)
        os.close(root)


def clean_request(data, before):
    current = lstat_at(data, REQUEST)
    if before is not None and current is not None and identity(before) == identity(current):
        os.unlink(REQUEST, dir_fd=data)
        os.fsync(data)


def locked_items(row, reason):
    return dict(row, items=[dict(item, deletable=False, reason=reason) for item in row.get('items', [])])


def run(data, state, recover=False):
    before = lstat_at(data, REQUEST)
    previous = load_state(state)
    if recover:
        if previous.get('phase') in ('scanning', 'deleting'):
            previous = locked_items(previous, '上次操作中断，请刷新列表后重试')
            publish(state, dict(previous, phase='failed', message='升级备份操作中断，请刷新列表后重试'))
        # The worker, not recovery, owns cleanup. On a crash the next run
        # rejects the same request ID instead of automatically resuming delete.
        return
    if before is None:
        return
    row = dict(previous, requestId='', operation='list', backupId='')
    lock = None
    try:
        request = validate_request(read_json(data, REQUEST, 4096, publication_link=True))
        row.update(requestId=request['id'], operation=request['operation'], backupId=request['backupId'])
        if previous.get('requestId') == request['id']:
            if previous.get('phase') in ('completed', 'failed'):
                return
            raise Rejected('重复或中断的升级备份请求，请刷新后重试')
        try:
            lock = update_lock()
        except BlockingIOError:
            raise Rejected('主控升级正在进行，请升级完成后重试') from None
        assert_update_idle(data, state)
        phase = 'deleting' if request['operation'] == 'delete' else 'scanning'
        publish(state, dict(locked_items(row, '操作进行中，请稍候'), phase=phase, message='正在删除升级备份' if phase == 'deleting' else '正在读取升级备份'))
        if request['operation'] == 'delete':
            assert_update_idle(data, state)
            delete_backup(request['backupId'])
        row.update(scan())
        message = '升级备份已删除' if request['operation'] == 'delete' else '升级备份列表已刷新'
        if row['truncated']:
            message += f'；仅显示最早 {MAX_ITEMS} 份，另有 {row["remainingCount"]} 份未列出，合计大小仅含当前列表'
        publish(state, dict(row, phase='completed', message=message))
    except Exception as error:
        reason = str(error) if isinstance(error, Rejected) else '升级备份操作失败，请检查服务日志后重试'
        print('Upgrade backup operation failed:', type(error).__name__, file=sys.stderr)
        publish(state, dict(locked_items(row, reason), phase='failed', message=reason))
    finally:
        if lock is not None:
            os.close(lock)
        clean_request(data, before)


def main():
    if os.geteuid() != 0:
        raise SystemExit('root required')
    if sys.argv[1:] not in ([], ['--recover']):
        raise SystemExit('unexpected arguments')
    data = secure_directory(DATA)
    state = secure_directory(STATE, owned=True)
    worker = None
    try:
        worker = os.open('upgrade-backups.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=state)
        safe_owner(os.fstat(worker))
        fcntl.flock(worker, fcntl.LOCK_EX)
        run(data, state, sys.argv[1:] == ['--recover'])
    finally:
        if worker is not None:
            os.close(worker)
        os.close(state)
        os.close(data)


if __name__ == '__main__':
    main()

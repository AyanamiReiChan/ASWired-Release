"""Metadata/request tests everywhere; real descriptor and lock tests on Linux."""
import contextlib
import datetime
import importlib.util
import json
import os
import pathlib
import stat
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

if os.name != 'posix':
    for name in ('fcntl', 'grp'):
        sys.modules[name] = types.ModuleType(name)

root = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('upgrade_backups', root/'deploy/upgrade-backups.py')
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)


def request(operation='list', backup_id='', request_id='a'*32):
    return {'id': request_id, 'operation': operation, 'backupId': backup_id, 'createdAt': worker.now()}


class ValidationTests(unittest.TestCase):
    def test_fixed_request_and_timestamp_allowlist(self):
        self.assertEqual(worker.validate_request(request())['operation'], 'list')
        self.assertEqual(worker.validate_request(request('delete', '20260924T012345Z'))['backupId'], '20260924T012345Z')
        for invalid in ['../20260924T012345Z', 'latest', '20261324T012345Z', '20260924T250000Z', '20260924T012345Z/x', '20260924T012345Z\n']:
            self.assertFalse(worker.timestamp_id(invalid))
        valid = request()
        for row in [[], None, {**valid, 'path': '/etc'}, {**valid, 'id': '../x'}, {**valid, 'id': 'A'*32}, {**valid, 'id': 1}, {**valid, 'operation': 'shell'}, {**valid, 'backupId': '20260924T012345Z'}, {**valid, 'createdAt': '2000-01-01T00:00:00Z'}, {**valid, 'createdAt': '2026-09-24'}, {**valid, 'createdAt': (datetime.datetime.now(datetime.timezone.utc)+datetime.timedelta(hours=1)).isoformat()}]:
            with self.subTest(row=row), self.assertRaises((ValueError, TypeError)):
                worker.validate_request(row)
        with self.assertRaises(ValueError):
            json.loads('{"id":"a","id":"b"}', object_pairs_hook=worker.no_duplicates)

    def test_managed_installation_watches_new_requests(self):
        self.assertIn('aswired-upgrade-backups.path', (root/'deploy/systemd/aswired-server.service').read_text())
        self.assertIn('PathExists=/var/lib/aswired/upgrade-backup-request.json', (root/'deploy/systemd/aswired-upgrade-backups.path').read_text())
        for name in ('install.sh', 'update.sh', 'deploy/enable-updater.sh'):
            content = (root/name).read_text()
            self.assertTrue(any('systemctl enable --now' in line and 'aswired-upgrade-backups.path' in line for line in content.splitlines()), name)
        self.assertIn('--exclude=aswired/upgrade-backup-request.json', (root/'update.sh').read_text())


@unittest.skipUnless(os.name == 'posix', 'POSIX descriptor and flock boundary')
class FilesystemTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='aswired-backup-test-')
        self.root = pathlib.Path(self.temporary.name)
        self.data = self.root/'data'
        self.state = self.root/'state'
        self.backups = self.root/'backups'
        for path in (self.data, self.state, self.backups):
            path.mkdir(mode=0o700)
        self.stack = contextlib.ExitStack()
        for name, value in [('DATA', self.data), ('STATE', self.state), ('BACKUPS', self.backups), ('UPDATE_LOCK', self.root/'update.lock'), ('ROOT_UID', os.geteuid())]:
            self.stack.enter_context(patch.object(worker, name, value))
        self.stack.enter_context(patch.object(worker.grp, 'getgrnam', return_value=types.SimpleNamespace(gr_gid=os.getegid())))
        self.data_fd = worker.secure_directory(self.data)
        self.state_fd = worker.secure_directory(self.state, owned=True)

    def tearDown(self):
        os.close(self.state_fd)
        os.close(self.data_fd)
        self.stack.close()
        self.temporary.cleanup()

    def make_backup(self, name='20260924T012345Z', postgres=False):
        path = self.backups/name
        path.mkdir(mode=0o700)
        for filename, value in [('data.tar.gz', b'private-data'), ('config.tar.gz', b'private-configuration'), ('previous-release', b'/opt/aswired/releases/v1.0.8\n')]:
            (path/filename).write_bytes(value)
            (path/filename).chmod(0o600)
        if postgres:
            (path/'postgres-backup').write_bytes(b'private-postgres')
            (path/'postgres-backup').chmod(0o600)
        return path

    def run_request(self, row=None):
        (self.data/worker.REQUEST).write_text(json.dumps(row or request()))
        worker.run(self.data_fd, self.state_fd)
        self.assertFalse((self.data/worker.REQUEST).exists())
        return json.loads((self.state/worker.STATUS).read_text())

    def test_legacy_metadata_and_exact_delete(self):
        backup = self.make_backup(postgres=True)
        unrelated = self.backups/'manual'
        unrelated.mkdir()
        (unrelated/'secret').write_text('keep')
        before = {p.name: (p.stat().st_mode, p.read_bytes()) for p in backup.iterdir()}
        result = self.run_request()
        self.assertEqual(result['phase'], 'completed')
        self.assertEqual(len(result['items']), 1)
        item = result['items'][0]
        self.assertTrue(item['deletable'])
        self.assertEqual(item['previousVersion'], 'v1.0.8')
        self.assertEqual(item['sizeBytes'], sum(len(v[1]) for v in before.values()))
        self.assertEqual(result['totalSizeBytes'], item['sizeBytes'])
        self.assertEqual({p.name: (p.stat().st_mode, p.read_bytes()) for p in backup.iterdir()}, before)
        self.assertNotIn('private-', json.dumps(result))
        self.assertEqual(stat.S_IMODE((self.state/worker.STATUS).stat().st_mode), 0o640)
        result = self.run_request(request('delete', backup.name, 'b'*32))
        self.assertEqual(result['phase'], 'completed')
        self.assertEqual(result['items'], [])
        self.assertFalse(backup.exists())
        self.assertEqual((unrelated/'secret').read_text(), 'keep')

    def test_symlink_fifo_nested_unknown_hardlink_and_incomplete_rejected(self):
        secret = self.root/'outside-secret'
        secret.write_text('never read or remove')
        cases = ['directory_link', 'archive_link', 'fifo', 'nested', 'extra', 'hardlink', 'incomplete', 'writable', 'bad-version']
        for index, case in enumerate(cases):
            name = f'20260924T0123{index:02}Z'
            if case == 'directory_link':
                (self.backups/name).symlink_to(self.root, target_is_directory=True)
            else:
                backup = self.make_backup(name)
                if case == 'archive_link':
                    (backup/'data.tar.gz').unlink()
                    (backup/'data.tar.gz').symlink_to(secret)
                elif case == 'fifo':
                    (backup/'data.tar.gz').unlink()
                    os.mkfifo(backup/'data.tar.gz')
                elif case == 'nested':
                    (backup/'nested').mkdir()
                elif case == 'extra':
                    (backup/'unknown-secret-name').write_text('private')
                elif case == 'hardlink':
                    (backup/'data.tar.gz').unlink()
                    os.link(secret, backup/'data.tar.gz')
                elif case == 'incomplete':
                    (backup/'config.tar.gz').unlink()
                elif case == 'writable':
                    backup.chmod(0o777)
                elif case == 'bad-version':
                    (backup/'previous-release').write_text('/etc/shadow')
        result = self.run_request()
        self.assertEqual(len(result['items']), len(cases))
        self.assertTrue(all(not row['deletable'] for row in result['items']))
        self.assertNotIn('unknown-secret-name', json.dumps(result))
        self.assertNotIn('/etc/shadow', json.dumps(result))
        for row in result['items']:
            with self.assertRaises((ValueError, OSError)):
                worker.delete_backup(row['id'])
        self.assertEqual(secret.read_text(), 'never read or remove')

    def test_no_parent_symlink_or_unsafe_root(self):
        self.make_backup()
        linked = self.root/'parent-link'
        linked.symlink_to(self.root, target_is_directory=True)
        with patch.object(worker, 'BACKUPS', linked/'backups'), self.assertRaises(OSError):
            worker.scan()
        self.backups.chmod(0o777)
        with self.assertRaises(ValueError):
            worker.scan()
        self.backups.chmod(0o700)
        self.assertEqual(worker.scan()['items'][0]['previousVersion'], 'v1.0.8')

    def test_missing_legacy_root_is_empty(self):
        self.backups.rmdir()
        self.assertEqual(self.run_request()['items'], [])

    def test_request_link_fifo_size_extra_fields_and_cleanup(self):
        secret = self.root/'outside-secret'
        secret.write_text('keep')
        request_path = self.data/worker.REQUEST
        for make in [lambda: request_path.symlink_to(secret), lambda: os.mkfifo(request_path), lambda: request_path.write_text('x'*5000), lambda: request_path.write_text(json.dumps({**request(), 'command': 'id'}))]:
            make()
            worker.run(self.data_fd, self.state_fd)
            self.assertFalse(request_path.exists())
            self.assertEqual(json.loads((self.state/worker.STATUS).read_text())['phase'], 'failed')
            self.assertEqual(secret.read_text(), 'keep')

    def test_atomic_link_publication_can_race_worker_wakeup(self):
        self.make_backup()
        staged = self.data/'complete-staging-request'
        staged.write_text(json.dumps(request()))
        os.link(staged, self.data/worker.REQUEST)
        self.assertEqual(staged.stat().st_nlink, 2)
        # Deliberately leave staging linked for the entire worker execution,
        # reproducing systemd waking before the client's deferred unlink.
        worker.run(self.data_fd, self.state_fd)
        result = json.loads((self.state/worker.STATUS).read_text())
        self.assertEqual(result['phase'], 'completed')
        self.assertEqual(len(result['items']), 1)
        self.assertFalse((self.data/worker.REQUEST).exists())
        self.assertTrue(staged.exists())
        self.assertEqual(staged.stat().st_nlink, 1)
        # The relaxation is request-specific, never applied to root status.
        linked_state = self.state/'linked-state'
        os.link(self.state/worker.STATUS, linked_state)
        with self.assertRaises(ValueError):
            worker.load_state(self.state_fd)

    def test_update_flock_and_queued_or_running_update_prevent_delete(self):
        backup = self.make_backup()
        self.run_request()
        locked = worker.update_lock()
        try:
            result = self.run_request(request('delete', backup.name, 'b'*32))
            self.assertEqual(result['phase'], 'failed')
            self.assertTrue(backup.exists())
            self.assertTrue(all(not item['deletable'] for item in result['items']))
        finally:
            os.close(locked)
        (self.data/'update-request.json').write_text('{}')
        result = self.run_request(request('delete', backup.name, 'c'*32))
        self.assertEqual(result['phase'], 'failed')
        (self.data/'update-request.json').unlink()
        (self.state/'status.json').write_text(json.dumps({'phase': 'updating'}))
        result = self.run_request(request('delete', backup.name, 'd'*32))
        self.assertEqual(result['phase'], 'failed')
        self.assertTrue(backup.exists())
        (self.state/'status.json').write_text(json.dumps({'phase': 'completed'}))
        self.assertEqual(self.run_request(request('delete', backup.name, 'e'*32))['phase'], 'completed')

    def test_lock_symlink_and_status_symlink_do_not_touch_target(self):
        secret = self.root/'outside-secret'
        secret.write_text('keep')
        worker.UPDATE_LOCK.symlink_to(secret)
        result = self.run_request()
        self.assertEqual(result['phase'], 'failed')
        self.assertEqual(secret.read_text(), 'keep')
        worker.UPDATE_LOCK.unlink()
        (self.state/worker.STATUS).unlink()
        (self.state/worker.STATUS).symlink_to(secret)
        (self.data/worker.REQUEST).write_text(json.dumps(request()))
        with self.assertRaises(OSError):
            worker.run(self.data_fd, self.state_fd)
        self.assertEqual(secret.read_text(), 'keep')

    def test_replay_interruption_and_replaced_request(self):
        backup = self.make_backup()
        first = request()
        self.run_request(first)
        # Replaying the completed ID as a delete must not execute it.
        result = self.run_request({**first, 'operation': 'delete', 'backupId': backup.name})
        self.assertEqual(result['phase'], 'completed')
        self.assertTrue(backup.exists())
        interrupted = dict(result, phase='deleting', requestId='b'*32, operation='delete', backupId=backup.name)
        worker.publish(self.state_fd, interrupted)
        (self.data/worker.REQUEST).write_text(json.dumps(request('delete', backup.name, 'b'*32)))
        worker.run(self.data_fd, self.state_fd, recover=True)
        worker.run(self.data_fd, self.state_fd)
        self.assertTrue(backup.exists())
        self.assertEqual(json.loads((self.state/worker.STATUS).read_text())['phase'], 'failed')
        # An independently replaced request is not removed by old cleanup.
        path = self.data/worker.REQUEST
        path.write_text(json.dumps(request(request_id='c'*32)))
        before = path.stat()
        staged = self.data/'next'
        staged.write_text(json.dumps(request(request_id='d'*32)))
        staged.replace(path)
        worker.clean_request(self.data_fd, before)
        self.assertTrue(path.exists())

    def test_directory_exchange_stops_delete_before_unlink(self):
        backup = self.make_backup()
        original = worker.inspect_backup
        moved = self.backups/'kept-original'
        def replace_after_inspection(root_fd, name):
            result = original(root_fd, name)
            backup.rename(moved)
            self.make_backup(name)
            return result
        with patch.object(worker, 'inspect_backup', side_effect=replace_after_inspection), self.assertRaises(ValueError):
            worker.delete_backup(backup.name)
        self.assertTrue((moved/'data.tar.gz').exists())
        self.assertTrue((backup/'data.tar.gz').exists())

    def test_bounded_oldest_first_list_has_explicit_remaining_size_scope(self):
        for name in ['20260923T000000Z', '20260924T000000Z', '20260922T000000Z']:
            self.make_backup(name)
        with patch.object(worker, 'MAX_ITEMS', 2):
            result = self.run_request()
        self.assertEqual([row['id'] for row in result['items']], ['20260922T000000Z', '20260923T000000Z'])
        self.assertTrue(result['truncated'])
        self.assertEqual(result['remainingCount'], 1)
        self.assertEqual(result['totalSizeBytes'], sum(row['sizeBytes'] for row in result['items']))
        self.assertIn('合计大小仅含当前列表', result['message'])


if __name__ == '__main__':
    unittest.main()

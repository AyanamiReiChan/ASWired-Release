import importlib.util
import io
import os
import pathlib
import tarfile
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('extractor', pathlib.Path(__file__).resolve().parents[1] / 'deploy/extract-release.py')
extractor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(extractor)


def entry(name, kind=tarfile.REGTYPE, link='', mode=0o644):
    item = tarfile.TarInfo(name)
    item.type, item.linkname, item.mode = kind, link, mode
    return item


class ExtractionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = pathlib.Path(self.temporary.name)

    def archive(self, items):
        path = self.root / 'package.tar.gz'
        with tarfile.open(path, 'w:gz') as archive:
            archive.addfile(entry('aswired', tarfile.DIRTYPE, mode=0o755))
            for item in items:
                data = b'fixture-content' if item.isreg() else b''
                item.size = len(data)
                archive.addfile(item, io.BytesIO(data))
        return path

    def reject(self, items):
        source = self.archive(items)
        destination = self.root / 'rejected'
        with self.assertRaises(ValueError):
            extractor.extract_release(source, destination)
        self.assertFalse(destination.exists(), 'validation wrote files')

    def test_plain_files_and_hardlinks_without_filter_api(self):
        source = self.archive([entry('aswired/bin/server', mode=0o6755), entry('aswired/copy', tarfile.LNKTYPE, 'aswired/bin/server')])
        destination = self.root / 'output'
        # Deliberately prohibit extract/extractall, even on newer Python.
        with patch.object(tarfile.TarFile, 'extractall', side_effect=AssertionError), patch.object(tarfile.TarFile, 'extract', side_effect=AssertionError):
            extractor.extract_release(source, destination)
        self.assertEqual((destination / 'aswired/copy').read_bytes(), b'fixture-content')
        if os.name != 'nt':
            self.assertEqual((destination / 'aswired/bin/server').stat().st_mode & 0o7777, 0o755)

    @unittest.skipIf(os.name == 'nt', 'real POSIX symlinks run in Linux CI')
    def test_pnpm_links_and_forward_hardlinks(self):
        source = self.archive([
            entry('aswired/web/node_modules/pkg', tarfile.SYMTYPE, '.pnpm/pkg/node_modules/pkg'),
            entry('aswired/web/node_modules/.pnpm/pkg/node_modules/dep', tarfile.SYMTYPE, '../../../../lib/dep'),
            entry('aswired/web/node_modules/.pnpm/pkg/node_modules/pkg/index.js'),
            entry('aswired/web/lib/dep/index.js'),
            entry('aswired/first', tarfile.LNKTYPE, 'aswired/second'),
            entry('aswired/second', tarfile.LNKTYPE, 'aswired/final'),
            entry('aswired/final'),
        ])
        destination = self.root / 'output'
        extractor.extract_release(source, destination)
        self.assertEqual((destination / 'aswired/web/node_modules/pkg/index.js').read_bytes(), b'fixture-content')
        self.assertEqual((destination / 'aswired/web/node_modules/.pnpm/pkg/node_modules/dep/index.js').read_bytes(), b'fixture-content')
        self.assertEqual((destination / 'aswired/first').read_bytes(), b'fixture-content')

    def test_reject_paths_and_special_files(self):
        for name in ['/tmp/escaped', '../escaped', 'aswired/../../escaped', 'other/file', 'C:/escaped', 'aswired\\escaped']:
            with self.subTest(name=name):
                self.reject([entry(name)])
        for kind in [tarfile.FIFOTYPE, tarfile.CHRTYPE, tarfile.BLKTYPE]:
            with self.subTest(kind=kind):
                self.reject([entry('aswired/device', kind)])

    def test_reject_link_escape_and_cycles(self):
        for link in ['/tmp/escaped', '../../escaped', '../aswired/../../escaped', 'C:/escaped']:
            with self.subTest(link=link):
                self.reject([entry('aswired/link', tarfile.SYMTYPE, link)])
        self.reject([entry('aswired/a', tarfile.SYMTYPE, 'b'), entry('aswired/b', tarfile.SYMTYPE, 'a')])
        self.reject([entry('aswired/a', tarfile.LNKTYPE, 'aswired/b'), entry('aswired/b', tarfile.LNKTYPE, 'aswired/a')])
        self.reject([entry('aswired/link', tarfile.LNKTYPE, '../escaped')])
        self.reject([entry('aswired/link', tarfile.LNKTYPE, 'aswired/missing')])
        # Normalize '..' after resolving an earlier symlink, not before.
        self.reject([entry('aswired/sub/up', tarfile.SYMTYPE, '..'), entry('aswired/out', tarfile.SYMTYPE, 'sub/up/../escaped')])

    def test_reject_duplicate_and_writes_through_links(self):
        self.reject([entry('aswired/file'), entry('aswired/./file')])
        self.reject([entry('aswired/dir', tarfile.SYMTYPE, 'target'), entry('aswired/dir/file')])
        self.reject([entry('aswired/file'), entry('aswired/file/child')])
        self.reject([entry('aswired/link', tarfile.SYMTYPE, 'target'), entry('aswired/hard', tarfile.LNKTYPE, 'aswired/link')])

    def test_existing_destination_is_untouched(self):
        source = self.archive([entry('aswired/file')])
        destination = self.root / 'existing'
        destination.mkdir()
        sentinel = destination / 'sentinel'
        sentinel.write_text('keep')
        with self.assertRaises(FileExistsError):
            extractor.extract_release(source, destination)
        self.assertEqual(sentinel.read_text(), 'keep')
        self.assertFalse((destination / 'aswired').exists())


if __name__ == '__main__':
    unittest.main()

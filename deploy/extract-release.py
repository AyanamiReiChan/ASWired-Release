"""Extract an ASWired release without depending on tarfile extraction filters.

Accept only directories, regular files and links confined to the aswired tree.
Validate the entire manifest before writing, never extract through a link, and
create links last. Do not preserve archive ownership or privileged mode bits.
Compatible with Debian 12's Python 3.11.2.
"""
import collections
import pathlib
import shutil
import sys
import tarfile


def member_name(value):
    if not value or value.startswith('/') or '\\' in value or ':' in value:
        raise ValueError('invalid archive path')
    parts = [part for part in value.split('/') if part not in ('', '.')]
    if not parts or parts[0] != 'aswired' or '..' in parts:
        raise ValueError('archive path escapes package')
    return '/'.join(parts)


def manifest(archive):
    entries = {}
    for item in archive.getmembers():
        name = member_name(item.name)
        if name in entries:
            raise ValueError('duplicate archive path')
        if not (item.isdir() or item.isreg() or item.issym() or item.islnk()):
            raise ValueError('unsupported archive entry')
        entries[name] = item
    if 'aswired' not in entries or not entries['aswired'].isdir():
        raise ValueError('missing package root')
    for name, item in entries.items():
        parts = name.split('/')
        for length in range(1, len(parts)):
            parent = entries.get('/'.join(parts[:length]))
            if parent is not None and not parent.isdir():
                raise ValueError('archive entry has a non-directory parent')
        if item.issym():
            if not item.linkname or item.linkname.startswith('/') or '\\' in item.linkname or ':' in item.linkname:
                raise ValueError('invalid symbolic link')
        elif item.islnk():
            member_name(item.linkname)

    # Resolve using archive metadata, including symlink-before-.. semantics.
    # No host filesystem is consulted, and cycles cannot consume unbounded work.
    for name, item in entries.items():
        if not item.issym():
            continue
        pending = collections.deque(name.split('/'))
        resolved = []
        hops = 0
        while pending:
            part = pending.popleft()
            if part in ('', '.'):
                continue
            if part == '..':
                if len(resolved) <= 1:
                    raise ValueError('symbolic link escapes package')
                resolved.pop()
                continue
            resolved.append(part)
            target = entries.get('/'.join(resolved))
            if target is not None and target.issym():
                hops += 1
                if hops > 40:
                    raise ValueError('cyclic or excessive symbolic links')
                resolved.pop()
                pending.extendleft(reversed(target.linkname.split('/')))

    hardlinks = {}
    for name, item in entries.items():
        if not item.islnk():
            continue
        seen = {name}
        target = member_name(item.linkname)
        while target in entries and entries[target].islnk():
            if target in seen:
                raise ValueError('cyclic hard links')
            seen.add(target)
            target = member_name(entries[target].linkname)
        if target not in entries or not entries[target].isreg():
            raise ValueError('hard link must target a regular package file')
        hardlinks[name] = target
    return entries, hardlinks


def extract_release(source, destination):
    destination = pathlib.Path(destination)
    with tarfile.open(source, 'r:gz') as archive:
        entries, hardlinks = manifest(archive)
        # A fresh private destination excludes pre-existing symlink races.
        destination.mkdir(mode=0o700)
        for name, item in entries.items():
            path = destination / name
            path.parent.mkdir(parents=True, exist_ok=True)
            if item.isdir():
                path.mkdir(exist_ok=True)
                path.chmod(0o755)
            elif item.isreg():
                with archive.extractfile(item) as reader, path.open('xb') as writer:
                    shutil.copyfileobj(reader, writer)
                path.chmod(0o644 | (item.mode & 0o111))
        for name, target in hardlinks.items():
            (destination / name).hardlink_to(destination / target)
        for name, item in entries.items():
            if item.issym():
                (destination / name).symlink_to(item.linkname)


if __name__ == '__main__':
    if len(sys.argv) != 3:
        raise SystemExit('Usage: extract-release.py ARCHIVE NEW_DESTINATION')
    extract_release(sys.argv[1], sys.argv[2])

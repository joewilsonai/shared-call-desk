#!/usr/bin/env python3
"""Install Call Desk with inert ZIP backups; never arm calls or edit TCC."""
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
import argparse, base64, hashlib, json, os, plistlib, posixpath, shutil, stat
import subprocess, tempfile, uuid, zipfile
import fcntl

ROOT = Path(__file__).resolve().parent
BUNDLE_ID = 'local.joewilson.call-desk'
LSREGISTER = Path('/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister')
ARCHIVE_METADATA = '_CALL_DESK_BACKUP_METADATA.json'


def bundle_id(app):
    return plistlib.loads((Path(app) / 'Contents/Info.plist').read_bytes()).get('CFBundleIdentifier')


def bundle_manifest(app):
    """Capture bytes, links, modes and extended attributes without following links."""
    app = Path(app)
    result = {}
    for path in [app, *sorted(app.rglob('*'))]:
        info = path.lstat()
        kind = 'link' if stat.S_ISLNK(info.st_mode) else 'dir' if stat.S_ISDIR(info.st_mode) else 'file'
        if kind == 'file' and not stat.S_ISREG(info.st_mode):
            raise ValueError('Unsupported bundle node')
        data = os.readlink(path).encode() if kind == 'link' else path.read_bytes() if kind == 'file' else b''
        attrs = {name: base64.b64encode(os.getxattr(path, name, follow_symlinks=False)).decode()
                 for name in os.listxattr(path, follow_symlinks=False)} if hasattr(os, 'listxattr') else {}
        result[str(path.relative_to(app))] = {'kind': kind, 'mode': stat.S_IMODE(info.st_mode),
            'size': len(data), 'sha256': hashlib.sha256(data).hexdigest(), 'xattrs': attrs,
            'mtime_ns': info.st_mtime_ns}
    return result


def archive_app(app, archive_dir, label='backup'):
    """Verify a unique inert backup before any caller can retire its source."""
    app, archive_dir = Path(app), Path(archive_dir)
    if app.is_symlink() or bundle_id(app) != BUNDLE_ID:
        raise ValueError('Not a regular Call Desk app bundle')
    before = bundle_manifest(app)
    archive_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    label = ''.join(c if c.isalnum() or c in '-_' else '_' for c in label)
    archive = archive_dir / f'{label}-{stamp}-{uuid.uuid4().hex}.zip'
    temporary = archive.with_suffix('.zip.partial')
    with zipfile.ZipFile(temporary, 'w', zipfile.ZIP_DEFLATED) as zipped:
        for relative, item in before.items():
            path = app if relative == '.' else app / relative
            name = app.name if relative == '.' else f'{app.name}/{relative}'
            entry = zipfile.ZipInfo(name + ('/' if item['kind'] == 'dir' else ''))
            entry.create_system = 3
            node_type = {'dir': stat.S_IFDIR, 'file': stat.S_IFREG, 'link': stat.S_IFLNK}[item['kind']]
            entry.external_attr = ((node_type | item['mode']) << 16) | (0x10 if item['kind'] == 'dir' else 0)
            data = os.readlink(path).encode() if item['kind'] == 'link' else path.read_bytes() if item['kind'] == 'file' else b''
            zipped.writestr(entry, data, compress_type=zipfile.ZIP_DEFLATED)
        zipped.writestr(ARCHIVE_METADATA, json.dumps({'bundle': app.name, 'manifest': before}, sort_keys=True))
    os.chmod(temporary, 0o600)
    with zipfile.ZipFile(temporary) as zipped:
        if zipped.testzip() is not None:
            raise ValueError('Backup CRC verification failed')
        for relative, item in before.items():
            name = app.name if relative == '.' else f'{app.name}/{relative}'
            entry = zipped.getinfo(name + ('/' if item['kind'] == 'dir' else ''))
            node_type = {'dir': stat.S_IFDIR, 'file': stat.S_IFREG, 'link': stat.S_IFLNK}[item['kind']]
            data = zipped.read(entry)
            if (stat.S_IFMT(entry.external_attr >> 16) != node_type
                    or stat.S_IMODE(entry.external_attr >> 16) != item['mode']
                    or len(data) != item['size']
                    or hashlib.sha256(data).hexdigest() != item['sha256']):
                raise ValueError('Backup content verification failed')
        if json.loads(zipped.read(ARCHIVE_METADATA)) != {'bundle': app.name, 'manifest': before}:
            raise ValueError('Backup metadata verification failed')
    if bundle_manifest(app) != before:
        raise ValueError('Bundle changed during backup; source retained')
    os.replace(temporary, archive)
    return archive


def is_generated_app(app, root, temp_root, protected):
    app, root, temp_root, protected = map(Path, (app, root, temp_root, protected))
    if not app.is_dir() or app.is_symlink(): return False
    if app.resolve() == protected.resolve() or (protected.exists() and app.samefile(protected)): return False
    project_copy = (app.parent == root / 'mac-app' and app.name.startswith('installed-backup-') and app.suffix == '.app') or app == root / 'mac-app/build/Call Desk.app'
    stage_copy = app.name == 'Call Desk.app' and app.parent.parent == temp_root and app.parent.name.startswith(('call-desk-build.', 'call-desk-sign.'))
    if not (project_copy or stage_copy) or any(p.is_symlink() for p in (app.parent, app.parent.parent)): return False
    try: return bundle_id(app) == BUNDLE_ID
    except (OSError, ValueError): return False


def discover_generated_apps(root, temp_root, protected):
    root, temp_root = Path(root), Path(temp_root)
    candidates = [*root.joinpath('mac-app').glob('installed-backup-*.app'), root / 'mac-app/build/Call Desk.app']
    for pattern in ('call-desk-build.*', 'call-desk-sign.*'):
        candidates.extend(p / 'Call Desk.app' for p in temp_root.glob(pattern))
    return sorted(p for p in set(candidates) if is_generated_app(p, root, temp_root, protected))


def retire_generated_app(app, root, temp_root, protected):
    app = Path(app)
    if not is_generated_app(app, root, temp_root, protected):
        raise ValueError('Refusing to retire a protected or unrecognized app')
    before = bundle_manifest(app)
    archive = archive_app(app, Path(root) / 'mac-app/archives', label=app.stem)
    result = subprocess.run([str(LSREGISTER), '-u', str(app)], capture_output=True, text=True, timeout=20)
    detail = (result.stdout or '') + (result.stderr or '')
    not_found = result.returncode != 0 and '-10814' in detail  # Apple's kLSApplicationNotFoundErr.
    if result.returncode and not not_found:
        raise RuntimeError('Unregister failed; verified ZIP and source both retained')
    if bundle_manifest(app) != before or not is_generated_app(app, root, temp_root, protected):
        raise RuntimeError('Bundle identity/content changed; source retained')
    shutil.rmtree(app)
    return {'source': str(app), 'archive': str(archive), 'archive_sha256': hashlib.sha256(archive.read_bytes()).hexdigest(),
        'unregister': 'already absent (-10814)' if not_found else 'completed', 'source_retired': True}


def cleanup_generated_apps(root, temp_root, protected):
    candidates = discover_generated_apps(root, temp_root, protected)
    if not candidates:
        return []
    processes = subprocess.run(['/bin/ps', '-axo', 'args'], check=True, capture_output=True, text=True, timeout=10).stdout
    for app in candidates:
        if str(app) + '/Contents/MacOS/' in processes:
            raise RuntimeError('Generated app is still running; preserved: ' + str(app))
    return [retire_generated_app(p, root, temp_root, protected) for p in candidates]


def verify_signature(app):
    subprocess.run(['/usr/bin/codesign', '--verify', '--deep', '--strict', str(app)], check=True, capture_output=True, timeout=20)


@contextmanager
def prepared_source(source):
    source = Path(source)
    if source.is_dir():
        yield source
        return
    if not source.is_file() or source.suffix != '.zip':
        raise ValueError('Source must be a Call Desk app or ZIP')
    with tempfile.TemporaryDirectory(prefix='call-desk-install-', dir='/private/tmp') as temporary:
        target = Path(temporary)
        with zipfile.ZipFile(source) as zipped:
            entries = [e for e in zipped.infolist() if e.filename != ARCHIVE_METADATA and not e.filename.startswith('__MACOSX/')]
            roots = {PurePosixPath(e.filename).parts[0] for e in entries if PurePosixPath(e.filename).parts}
            if len(roots) != 1 or not next(iter(roots)).endswith('.app'):
                raise ValueError('ZIP must contain exactly one app bundle')
            app_name = next(iter(roots))
            names = [e.filename.rstrip('/') for e in entries]
            if len(set(names)) != len(names):
                raise ValueError('Duplicate archive member')
            links = {PurePosixPath(e.filename) for e in entries if stat.S_ISLNK(e.external_attr >> 16)}
            for entry in entries:
                if entry.filename.startswith('/') or '..' in PurePosixPath(entry.filename).parts:
                    raise ValueError('Unsafe archive member')
                if any(parent in links for parent in PurePosixPath(entry.filename).parents):
                    raise ValueError('Archive member descends through a symlink')
            # Links are written last; their normalized targets must stay inside this app.
            for entry in sorted(entries, key=lambda e: stat.S_ISLNK(e.external_attr >> 16)):
                output, mode = target / entry.filename, entry.external_attr >> 16
                if entry.is_dir(): output.mkdir(parents=True, exist_ok=True)
                elif stat.S_ISLNK(mode):
                    link = zipped.read(entry).decode()
                    resolved = posixpath.normpath(str(PurePosixPath(entry.filename).parent / link))
                    if link.startswith('/') or not resolved.startswith(app_name + '/'):
                        raise ValueError('Unsafe archive symlink')
                    output.parent.mkdir(parents=True, exist_ok=True); output.symlink_to(link)
                else:
                    output.parent.mkdir(parents=True, exist_ok=True); output.write_bytes(zipped.read(entry))
                    output.chmod(stat.S_IMODE(mode) or 0o644)
        yield target / app_name


def _install_app(source, root=ROOT, destination=None, agent=None, temp_root=Path('/private/tmp')):
    root = Path(root)
    destination = Path(destination) if destination else Path.home() / 'Applications/Call Desk.app'
    agent = Path(agent) if agent else Path.home() / 'Library/LaunchAgents/com.joewilson.calldesk.plist'
    if Path(source).resolve() == destination.resolve():
        raise ValueError('Installation source and destination must differ')
    with prepared_source(source) as app:
        if app.is_symlink() or bundle_id(app) != BUNDLE_ID: raise ValueError('Unexpected source bundle')
        verify_signature(app)
        destination.parent.mkdir(parents=True, exist_ok=True)
        backup = archive_app(destination, root / 'mac-app/archives', label='installed-backup') if destination.exists() else None
        with tempfile.TemporaryDirectory(prefix='.call-desk-replace-', dir=destination.parent) as work:
            staged, rollback = Path(work) / 'new', Path(work) / 'old'
            subprocess.run(['/usr/bin/ditto', '--norsrc', '--noextattr', str(app), str(staged)], check=True)
            verify_signature(staged)
            had_previous = destination.exists()
            if had_previous: os.replace(destination, rollback)
            try:
                os.replace(staged, destination); verify_signature(destination)
            except Exception:
                if destination.exists(): shutil.rmtree(destination)
                if had_previous: os.replace(rollback, destination)
                raise
        cleanup = cleanup_generated_apps(root, temp_root, destination)
    agent.parent.mkdir(parents=True, exist_ok=True)
    if agent.exists():
        subprocess.run(['launchctl', 'bootout', 'gui/' + str(os.getuid()), str(agent)], capture_output=True)
        shutil.copy2(agent, root / 'mac-app' / f'login-backup-{uuid.uuid4().hex}.plist')
    agent.write_bytes(plistlib.dumps({'Label': 'com.joewilson.calldesk', 'RunAtLoad': True,
        'ProgramArguments': ['/usr/bin/open', str(destination)], 'ProcessType': 'Interactive'}))
    agent.chmod(0o600)
    result = subprocess.run(['launchctl', 'bootstrap', 'gui/' + str(os.getuid()), str(agent)], capture_output=True, text=True)
    receipt = {'app': str(destination), 'login_agent': str(agent), 'registered': result.returncode == 0,
        'register_exit_code': result.returncode, 'automatic_answering_enabled_by_install': False,
        'backup': str(backup) if backup else None, 'retired_generated_apps': cleanup,
        'note': 'Only the installed app remains launchable in generated locations. Login registration is not a future restart or permission test.'}
    (root / 'mac-app/installation.json').write_text(json.dumps(receipt, indent=2) + '\n')
    return receipt


def install_app(source, root=ROOT, destination=None, agent=None, temp_root=Path('/private/tmp')):
    root = Path(root)
    (root / 'mac-app').mkdir(parents=True, exist_ok=True)
    with (root / 'mac-app/.installer.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _install_app(source, root, destination, agent, temp_root)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=ROOT / 'mac-app/build/Call Desk.zip')
    args = parser.parse_args(argv)
    receipt = install_app(args.source)
    print(json.dumps(receipt))
    return receipt['register_exit_code']


if __name__ == '__main__':
    raise SystemExit(main())

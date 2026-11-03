"""Offline installer/archive tests; never launch or register a real app."""
import argparse
import importlib.util
import os
from pathlib import Path
import plistlib
import shutil
import stat
import subprocess
import sys
import zipfile

import pytest


MODULE = Path(__file__).with_name('install_mac_app.py')


def load_installer(monkeypatch):
    name='_call_desk_installer_offline_test'
    spec=importlib.util.spec_from_file_location(name,MODULE)
    module=importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules,name,module)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def installer(monkeypatch):
    # Old import-time installation cannot reach real user paths if reintroduced.
    monkeypatch.setattr(argparse.ArgumentParser,'parse_args',lambda *a,**k:pytest.fail('Installer parsed arguments during import'))
    return load_installer(monkeypatch)


def make_app(path,bundle_id='local.joewilson.call-desk'):
    contents=path/'Contents';(contents/'MacOS').mkdir(parents=True)
    (contents/'Resources').mkdir()
    (contents/'Info.plist').write_bytes(plistlib.dumps({
        'CFBundleIdentifier':bundle_id,'CFBundleExecutable':'CallDesk',
        'CFBundlePackageType':'APPL','CFBundleName':'Call Desk'}))
    executable=contents/'MacOS'/'CallDesk'
    executable.write_bytes(b'offline fixture executable\n');executable.chmod(0o755)
    (contents/'Resources'/'executable-link').symlink_to('../MacOS/CallDesk')
    return path


def paths(tmp_path):
    root=tmp_path/'service';root.mkdir()
    temp=tmp_path/'temporary';temp.mkdir()
    protected=make_app(tmp_path/'Applications'/'Call Desk.app')
    return root,temp,protected


def forbid_native(monkeypatch,installer):
    calls=[]
    def run(args,*a,**k):
        calls.append([str(value) for value in args])
        assert Path(str(args[0])).name=='codesign','Unexpected native command in archive-only test'
        return subprocess.CompletedProcess(args,0,stdout='',stderr='')
    monkeypatch.setattr(installer.subprocess,'run',run)
    return calls


def test_import_has_no_installation_or_native_side_effects(monkeypatch):
    def forbidden(*args,**kwargs):pytest.fail('Import performed an installation side effect')
    monkeypatch.setattr(subprocess,'run',forbidden)
    monkeypatch.setattr(shutil,'copytree',forbidden)
    monkeypatch.setattr(shutil,'rmtree',forbidden)
    monkeypatch.setattr(argparse.ArgumentParser,'parse_args',forbidden)
    module=load_installer(monkeypatch)
    assert callable(module.archive_app) and callable(module.cleanup_generated_apps)


def test_manifest_tracks_contents_modes_and_symlink_targets(installer,tmp_path):
    app=make_app(tmp_path/'fixture.app')
    original=installer.bundle_manifest(app)
    assert isinstance(original,dict) and original==installer.bundle_manifest(app)
    executable=app/'Contents/MacOS/CallDesk'
    executable.write_bytes(b'changed executable\n')
    changed=installer.bundle_manifest(app)
    assert changed!=original
    executable.chmod(0o644)
    mode_changed=installer.bundle_manifest(app)
    assert mode_changed!=changed
    link=app/'Contents/Resources/executable-link';link.unlink();link.symlink_to('../Info.plist')
    assert installer.bundle_manifest(app)!=mode_changed


def test_archive_retains_file_bytes_execute_mode_and_symlink(installer,tmp_path,monkeypatch):
    forbid_native(monkeypatch,installer)
    app=make_app(tmp_path/'fixture.app')
    archive=installer.archive_app(app,tmp_path/'archives',label='backup')
    assert archive.is_file() and archive.suffix=='.zip' and app.is_dir()
    with zipfile.ZipFile(archive) as zipped:
        assert zipped.testzip() is None
        executable=next(entry for entry in zipped.infolist() if entry.filename.endswith('Contents/MacOS/CallDesk'))
        assert zipped.read(executable)==b'offline fixture executable\n'
        assert stat.S_IMODE(executable.external_attr>>16)==0o755
        link=next(entry for entry in zipped.infolist() if entry.filename.endswith('Contents/Resources/executable-link'))
        assert stat.S_ISLNK(link.external_attr>>16)
        assert zipped.read(link)==b'../MacOS/CallDesk'


def test_two_archives_do_not_overwrite_each_other(installer,tmp_path,monkeypatch):
    forbid_native(monkeypatch,installer)
    app=make_app(tmp_path/'fixture.app');directory=tmp_path/'archives'
    first=installer.archive_app(app,directory,label='backup')
    original=first.read_bytes()
    second=installer.archive_app(app,directory,label='backup')
    assert first!=second and first.read_bytes()==original and second.is_file()


def test_generated_candidates_are_exact_and_protected_is_never_included(installer,tmp_path):
    root,temp,protected=paths(tmp_path)
    candidates=[make_app(root/'mac-app/installed-backup-123.app'),
                make_app(root/'mac-app/build/Call Desk.app'),
                make_app(temp/'call-desk-build.ABC123/Call Desk.app'),
                make_app(temp/'call-desk-sign.XYZ987/Call Desk.app')]
    unrelated=make_app(root/'mac-app/another-copy.app')
    unrelated_temp=make_app(temp/'some-other-build/Call Desk.app')
    wrong_id=make_app(root/'mac-app/installed-backup-wrong.app',bundle_id='another.app')
    alias=root/'mac-app/installed-backup-protected.app';alias.symlink_to(protected,target_is_directory=True)
    for app in candidates:assert installer.is_generated_app(app,root,temp,protected)
    for app in (protected,alias,unrelated,unrelated_temp,wrong_id):
        assert not installer.is_generated_app(app,root,temp,protected)
    assert set(installer.discover_generated_apps(root,temp,protected))==set(candidates)


def test_retire_archives_then_unregisters_exact_path_then_deletes(installer,tmp_path,monkeypatch):
    root,temp,protected=paths(tmp_path)
    app=make_app(root/'mac-app/installed-backup-123.app');events=[]
    archive=root/'retained.zip'
    def archive_app(source,*args,**kwargs):
        assert source==app and app.is_dir();events.append('archive');archive.write_bytes(b'preverified zip fixture');return archive
    def run(args,*a,**k):
        assert archive.is_file() and app.is_dir();events.append('unregister')
        assert list(map(str,args))==[str(installer.LSREGISTER),'-u',str(app)]
        return subprocess.CompletedProcess(args,0,stdout='',stderr='')
    original_rmtree=shutil.rmtree
    def remove(path,*a,**k):
        assert Path(path)==app and events==['archive','unregister'];events.append('delete')
        return original_rmtree(path,*a,**k)
    monkeypatch.setattr(installer,'archive_app',archive_app)
    monkeypatch.setattr(installer.subprocess,'run',run)
    monkeypatch.setattr(installer.shutil,'rmtree',remove)
    installer.retire_generated_app(app,root,temp,protected)
    assert events==['archive','unregister','delete'] and not app.exists()
    assert archive.exists() and protected.is_dir()


def test_archive_failure_keeps_bundle_and_never_unregisters(installer,tmp_path,monkeypatch):
    root,temp,protected=paths(tmp_path);app=make_app(root/'mac-app/installed-backup-123.app')
    def failed(*a,**k):raise RuntimeError('archive verification failed')
    monkeypatch.setattr(installer,'archive_app',failed)
    monkeypatch.setattr(installer.subprocess,'run',lambda *a,**k:pytest.fail('Must not unregister before verified archive'))
    try:installer.retire_generated_app(app,root,temp,protected)
    except Exception:pass
    assert app.is_dir() and protected.is_dir()


def test_unregister_failure_keeps_verified_archive_and_bundle(installer,tmp_path,monkeypatch):
    root,temp,protected=paths(tmp_path);app=make_app(root/'mac-app/installed-backup-123.app')
    archive=root/'retained.zip';archive.write_bytes(b'preverified zip fixture')
    monkeypatch.setattr(installer,'archive_app',lambda *a,**k:archive)
    def failed(args,*a,**k):
        if k.get('check'):raise subprocess.CalledProcessError(1,args,stderr='offline failure')
        return subprocess.CompletedProcess(args,1,stdout='',stderr='offline failure')
    monkeypatch.setattr(installer.subprocess,'run',failed)
    try:installer.retire_generated_app(app,root,temp,protected)
    except Exception:pass
    assert app.is_dir() and protected.is_dir() and archive.is_file()


@pytest.mark.parametrize('alias',[False,True])
def test_retire_refuses_installed_bundle_or_generated_alias(installer,tmp_path,monkeypatch,alias):
    root,temp,protected=paths(tmp_path);candidate=protected
    if alias:
        candidate=root/'mac-app/installed-backup-alias.app';candidate.parent.mkdir(parents=True)
        candidate.symlink_to(protected,target_is_directory=True)
    monkeypatch.setattr(installer,'archive_app',lambda *a,**k:pytest.fail('Protected app must not be archived for deletion'))
    monkeypatch.setattr(installer.subprocess,'run',lambda *a,**k:pytest.fail('Protected app must not be unregistered'))
    try:installer.retire_generated_app(candidate,root,temp,protected)
    except Exception:pass
    assert protected.is_dir() and candidate.exists()


def test_empty_cleanup_is_idempotent_and_has_no_native_calls(installer,tmp_path,monkeypatch):
    root,temp,protected=paths(tmp_path)
    monkeypatch.setattr(installer.subprocess,'run',lambda *a,**k:pytest.fail('Nothing generated should be unregistered'))
    assert installer.cleanup_generated_apps(root,temp,protected)==[]
    assert installer.cleanup_generated_apps(root,temp,protected)==[]
    assert protected.is_dir()


@pytest.mark.parametrize('alias',[False,True])
def test_install_source_cannot_be_destination_or_alias(installer,tmp_path,monkeypatch,alias):
    root,temp,protected=paths(tmp_path);source=protected
    if alias:
        source=tmp_path/'source-alias.app';source.symlink_to(protected,target_is_directory=True)
    before=installer.bundle_manifest(protected)
    monkeypatch.setattr(installer.subprocess,'run',lambda *a,**k:pytest.fail('Must reject before any native command'))
    with pytest.raises(ValueError,match='must differ'):
        installer.install_app(source,root=root,destination=protected,agent=tmp_path/'agent.plist',temp_root=temp)
    assert installer.bundle_manifest(protected)==before


def local_extraction(monkeypatch,installer,tmp_path):
    real=installer.tempfile.TemporaryDirectory
    def temporary(*args,**kwargs):
        kwargs['dir']=tmp_path
        return real(*args,**kwargs)
    monkeypatch.setattr(installer.tempfile,'TemporaryDirectory',temporary)


def test_prepared_zip_restores_executable_and_safe_symlink(installer,tmp_path,monkeypatch):
    local_extraction(monkeypatch,installer,tmp_path)
    forbid_native(monkeypatch,installer)
    app=make_app(tmp_path/'source.app')
    archive=installer.archive_app(app,tmp_path/'archives')
    with installer.prepared_source(archive) as extracted:
        assert (extracted/'Contents/MacOS/CallDesk').read_bytes()==b'offline fixture executable\n'
        assert stat.S_IMODE((extracted/'Contents/MacOS/CallDesk').stat().st_mode)==0o755
        assert (extracted/'Contents/Resources/executable-link').is_symlink()
        assert os.readlink(extracted/'Contents/Resources/executable-link')=='../MacOS/CallDesk'
    assert not extracted.exists()


@pytest.mark.parametrize('member',['fixture.app/../escaped.txt','/absolute/fixture.app/file'])
def test_prepared_zip_rejects_traversal_members(installer,tmp_path,monkeypatch,member):
    local_extraction(monkeypatch,installer,tmp_path)
    archive=tmp_path/'unsafe.zip'
    with zipfile.ZipFile(archive,'w') as zipped:zipped.writestr(member,b'unsafe fixture')
    with pytest.raises(ValueError):
        with installer.prepared_source(archive):pytest.fail('Unsafe ZIP was yielded')
    assert not (tmp_path/'escaped.txt').exists()


@pytest.mark.parametrize('target',['../../../../outside','/outside'])
def test_prepared_zip_rejects_escaping_symlinks(installer,tmp_path,monkeypatch,target):
    local_extraction(monkeypatch,installer,tmp_path)
    archive=tmp_path/'unsafe-link.zip'
    with zipfile.ZipFile(archive,'w') as zipped:
        entry=zipfile.ZipInfo('fixture.app/Contents/Resources/link');entry.create_system=3
        entry.external_attr=(stat.S_IFLNK|0o777)<<16
        zipped.writestr(entry,target)
    with pytest.raises(ValueError,match='symlink'):
        with installer.prepared_source(archive):pytest.fail('Escaping symlink was yielded')


def test_absent_registration_can_retire_after_verified_archive(installer,tmp_path,monkeypatch):
    root,temp,protected=paths(tmp_path);app=make_app(root/'mac-app/installed-backup-123.app')
    archive=root/'retained.zip';archive.write_bytes(b'preverified archive')
    monkeypatch.setattr(installer,'archive_app',lambda *a,**k:archive)
    monkeypatch.setattr(installer.subprocess,'run',lambda args,**k:subprocess.CompletedProcess(
        args,1,stdout='',stderr='kLSApplicationNotFoundErr: -10814'))
    receipt=installer.retire_generated_app(app,root,temp,protected)
    assert receipt['source_retired'] is True and not app.exists()
    assert archive.exists() and protected.exists()


def install_runner(monkeypatch,installer,*,reject_destination=None):
    """Emulate only filesystem copying; never execute native tools."""
    calls=[]
    def run(args,**kwargs):
        args=list(map(str,args));calls.append(args)
        if args[0]=='/usr/bin/ditto':
            shutil.copytree(args[-2],args[-1],symlinks=True)
        elif args[0]=='/usr/bin/codesign':
            if reject_destination is not None and Path(args[-1])==reject_destination:
                raise subprocess.CalledProcessError(1,args,stderr='fixture final-signature failure')
        elif args[0]=='launchctl':
            assert args[1] in ('bootstrap','bootout')
        else:pytest.fail('Unexpected subprocess: '+repr(args))
        return subprocess.CompletedProcess(args,0,stdout='',stderr='')
    monkeypatch.setattr(installer.subprocess,'run',run)
    return calls


def test_install_replaces_instead_of_merging_and_archives_original(installer,tmp_path,monkeypatch):
    root,temp,destination=paths(tmp_path)
    (destination/'Contents/Resources/obsolete.txt').write_bytes(b'old-only resource')
    source=make_app(tmp_path/'input'/'Call Desk.app')
    (source/'Contents/MacOS/CallDesk').write_bytes(b'new fixture executable')
    agent=tmp_path/'LaunchAgents'/'com.joewilson.calldesk.plist'
    calls=install_runner(monkeypatch,installer)
    receipt=installer.install_app(source,root=root,destination=destination,agent=agent,temp_root=temp)
    assert (destination/'Contents/MacOS/CallDesk').read_bytes()==b'new fixture executable'
    assert not (destination/'Contents/Resources/obsolete.txt').exists()
    archive=Path(receipt['backup'])
    assert archive.is_file() and archive.suffix=='.zip'
    with zipfile.ZipFile(archive) as zipped:
        assert zipped.read('Call Desk.app/Contents/Resources/obsolete.txt')==b'old-only resource'
        assert zipped.read('Call Desk.app/Contents/MacOS/CallDesk')==b'offline fixture executable\n'
    assert list((root/'mac-app').rglob('*.app'))==[]
    assert list((root/'mac-app/archives').glob('*.zip'))==[archive]
    assert plistlib.loads(agent.read_bytes())['ProgramArguments']==['/usr/bin/open',str(destination)]
    assert receipt['registered'] is True and receipt['automatic_answering_enabled_by_install'] is False
    assert any(command[0]=='launchctl' and command[1]=='bootstrap' for command in calls)
    assert not any(command[0]==str(installer.LSREGISTER) for command in calls)


def test_failed_final_signature_restores_original_destination(installer,tmp_path,monkeypatch):
    root,temp,destination=paths(tmp_path)
    (destination/'Contents/Resources/original.txt').write_bytes(b'preserve me')
    original=installer.bundle_manifest(destination)
    source=make_app(tmp_path/'input'/'Call Desk.app')
    (source/'Contents/MacOS/CallDesk').write_bytes(b'invalid replacement fixture')
    agent=tmp_path/'LaunchAgents'/'com.joewilson.calldesk.plist'
    calls=install_runner(monkeypatch,installer,reject_destination=destination)
    with pytest.raises(subprocess.CalledProcessError):
        installer.install_app(source,root=root,destination=destination,agent=agent,temp_root=temp)
    assert installer.bundle_manifest(destination)==original
    assert not agent.exists()
    assert len(list((root/'mac-app/archives').glob('*.zip')))==1
    assert list(destination.parent.glob('.call-desk-replace-*'))==[]
    assert not any(command[0]=='launchctl' for command in calls)

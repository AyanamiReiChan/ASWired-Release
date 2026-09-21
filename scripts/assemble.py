import hashlib,json,os,pathlib,shutil,tarfile
root=pathlib.Path.cwd();version=os.environ['VERSION'];out=root/'dist';out.mkdir()
for arch in ['amd64','arm64']:
    package=root/('package-'+arch)/'aswired';package.mkdir(parents=True)
    with tarfile.open(root/'artifacts'/('components_linux_'+arch+'.tar.gz')) as t:t.extractall(package,filter='data')
    with tarfile.open(root/'websites/web.tar.gz') as t:t.extractall(package,filter='data')
    for name in ['install.sh','update.sh','VERSION','README.md','LICENSE','THIRD-PARTY-NOTICES.md','CHANGELOG.md','SOURCES.json']:
        shutil.copy2(root/name,package/name)
    for name in ['deploy','docs']:shutil.copytree(root/name,package/name)
    for cpu in ['amd64','arm64']:
        target=package/'agent-releases'/('linux-'+cpu)/'aswired-agent';target.parent.mkdir(parents=True)
        shutil.copy2(root/'artifacts'/f'aswired-agent_{version}_linux_{cpu}',target);target.chmod(0o755)
    for binary in [*list((package/'bin').iterdir()),package/'runtime/node',package/'install.sh',package/'update.sh']:binary.chmod(0o755)
    # Package only compiled application code and production dependencies.
    assert not (package/'web/src').exists()
    for path in package.rglob('*'):
        assert path.name not in ('.git','.env','setup-token','aswired.db','komari.db'),path
    with tarfile.open(out/f'aswired_{version}_linux_{arch}.tar.gz','w:gz') as t:t.add(package,arcname='aswired')
for path in (root/'artifacts').iterdir():
    if not path.name.startswith('components_'):shutil.copy2(path,out/path.name)
shutil.copy2(root/'SOURCES.json',out/'SOURCES.json')
lines=[hashlib.sha256(p.read_bytes()).hexdigest()+'  '+p.name for p in sorted(out.iterdir()) if p.is_file()]
(out/'SHA256SUMS').write_text('\n'.join(lines)+'\n',encoding='utf-8')
print('Assembled',len(lines),'verified release assets')

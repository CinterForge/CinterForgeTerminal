"""Setup-only acquisition of the pinned prebuilt Bedrock display providers."""
from __future__ import annotations
import hashlib
import json
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path

ENDSTONE = '0.11.16'
MEDIAPLAYER = '2.3.1'
BASE = f'https://github.com/ReallocAll/endstone-mediaplayer/releases/download/v{MEDIAPLAYER}/'
PINS = {
    'linux': ('manylinux_2_31_x86_64', '534a0e06025f63eafd2f282003cd54f4aec6d35dc75c8bdc1f7bd3d702bacbcd',
              'endstone_mediaplayer-linux-x64.tar.gz', 'cee92d3a1d011f42c1f602cdb5a46d2dc55529c17215acb4d18db68bb2cac9ef', 'endstone_mediaplayer.so'),
    'win': ('win_amd64', '80ff92763a2cc28e331c9b4f305bbfaed02fc15ceed5277f8fc7b4976f50b4ce',
            'endstone_mediaplayer-windows-x64.zip', 'ba7f88bdef01215077e9bbb67c8e52c481c4e6b9ef6282af5518cd9cedae4f1d', 'endstone_mediaplayer.dll'),
}
SDK = ('endstone_mediaplayer-sdk.zip', 'e17b2d6e513a67b20d26ccf3a8ac79123ce409ae24156f26000dbdf596ef2ebc')


def digest(path):
    with path.open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def download(root, filename, sha):
    path = root / filename
    if path.is_file() and digest(path) == sha:
        return path
    tmp = path.with_suffix(path.suffix + '.partial')
    print('[BEDROCK DISPLAY] Downloading', filename, flush=True)
    try:
        with urllib.request.urlopen(urllib.request.Request(BASE + filename, headers={'User-Agent':'CinterForgeTerminal'}), timeout=120) as src, tmp.open('wb') as out:
            shutil.copyfileobj(src, out)
        if digest(tmp) != sha:
            raise RuntimeError('Bedrock display release SHA-256 mismatch: ' + filename)
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)
    return path


def extract_member(archive, name, output):
    if archive.suffix == '.zip':
        with zipfile.ZipFile(archive) as z:
            members = [n for n in z.namelist() if Path(n).name == name and not n.endswith('/')]
            if len(members) != 1:
                raise RuntimeError(f'Expected exactly one {name} in {archive}')
            with z.open(members[0]) as src, output.open('wb') as dst:
                shutil.copyfileobj(src, dst)
    else:
        with tarfile.open(archive) as t:
            members = [m for m in t.getmembers() if m.isfile() and Path(m.name).name == name]
            if len(members) != 1:
                raise RuntimeError(f'Expected exactly one {name} in {archive}')
            with t.extractfile(members[0]) as src, output.open('wb') as dst:
                shutil.copyfileobj(src, dst)


def provision(builds: Path, runtimes: dict[str, Path]):
    root = builds / 'CinterForgeTerminal/Dependencies/BedrockDisplay'
    downloads = builds / 'Intermediates/CinterForgeTerminal/BedrockDisplayDownloads'
    downloads.mkdir(parents=True, exist_ok=True)
    root.mkdir(parents=True, exist_ok=True)
    sdk = download(downloads, *SDK)
    for target in sorted(set(runtimes) & set(PINS)):
        platform, wheel_sha, native, native_sha, plugin = PINS[target]
        receipt = {'schema':'cinterforge.terminal.bedrock-display.v1', 'endstone':ENDSTONE,
                   'mediaplayer':MEDIAPLAYER, 'target':target, 'wheel_sha256':wheel_sha,
                   'provider_sha256':native_sha, 'sdk_sha256':SDK[1]}
        destination = root / target
        saved = destination / 'receipt.json'
        if saved.is_file():
            old = json.loads(saved.read_text())
            if all(old.get(k) == v for k,v in receipt.items()) and all((destination/p).is_file() and digest(destination/p)==sha for p,sha in old.get('files',{}).items()) and old.get('files'):
                print('[BEDROCK DISPLAY] Verified', target, flush=True)
                continue
        with tempfile.TemporaryDirectory(prefix='Display-', dir=root) as temp:
            stage = Path(temp) / 'runtime'; stage.mkdir()
            wheels = Path(temp) / 'wheels'; wheels.mkdir()
            flags = ['--only-binary=:all:', '--implementation','cp','--python-version','3.11','--abi','cp311','--platform',platform]
            if target == 'linux':
                for p in ('manylinux_2_28_x86_64','manylinux_2_27_x86_64','manylinux_2_24_x86_64','manylinux_2_17_x86_64','manylinux2014_x86_64','manylinux2010_x86_64','manylinux1_x86_64'):
                    flags += ['--platform',p]
            requirements = [f'endstone=={ENDSTONE}', 'Pillow==11.3.0', 'colorama==0.4.6']
            subprocess.run([sys.executable,'-m','pip','download',*flags,'--dest',str(wheels),*requirements],check=True)
            provider_wheels = list(wheels.glob(f'endstone-{ENDSTONE}-cp311-*.whl'))
            if len(provider_wheels)!=1 or digest(provider_wheels[0])!=wheel_sha:
                raise RuntimeError('Pinned Endstone wheel SHA-256 mismatch for '+target)
            sdk_wheel = wheels / 'endstone_mediaplayer-0.1.0-py3-none-any.whl'
            extract_member(sdk,sdk_wheel.name,sdk_wheel)
            subprocess.run([sys.executable,'-m','pip','install',*flags,'--no-index','--no-deps','--no-compile','--target',str(stage/'site'),*[str(p) for p in sorted(wheels.glob('*.whl'))]],check=True)
            shutil.copytree(runtimes[target],stage/'python',symlinks=False,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
            native_archive=download(downloads,native,native_sha)
            extract_member(native_archive,plugin,stage/plugin)
            notices=stage/'upstream'; notices.mkdir()
            for name in ('LICENSE','LICENSE.txt','COPYING','README.md'):
                try: extract_member(native_archive,name,notices/name)
                except RuntimeError: pass
            (notices/'Sources.txt').write_text('Endstone '+ENDSTONE+'\nhttps://github.com/EndstoneMC/endstone/tree/v'+ENDSTONE+'\nMediaPlayer '+MEDIAPLAYER+'\nhttps://github.com/ReallocAll/endstone-mediaplayer/tree/v'+MEDIAPLAYER+'\n',encoding='utf-8')
            # Include upstream notices alongside the binary where provided by release.
            receipt['files'] = {p.relative_to(stage).as_posix():digest(p) for p in sorted(stage.rglob('*')) if p.is_file()}
            receipt['wheels'] = {p.name:digest(p) for p in sorted(wheels.glob('*.whl'))}
            (stage/'receipt.json').write_text(json.dumps(receipt,indent=2)+'\n')
            if destination.exists(): shutil.rmtree(destination)
            stage.rename(destination)
        print('[BEDROCK DISPLAY] Provisioned prebuilt Endstone and MediaPlayer:',target,flush=True)

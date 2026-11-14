"""Setup-only HFS Standard disk assembler running in the existing Debian AMD64 guest.

The macOS producer encodes Finder metadata and resource forks into MacBinary II
files before crossing into Linux. This worker never reads original Mac metadata,
changes the authored source, or requires any extra VM or Docker image.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile

TOOLS = ('hformat', 'hmount', 'hcopy', 'hmkdir', 'hls', 'humount')


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for data in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(data)
    return digest.hexdigest()


def _under(root: Path, rel: str) -> Path:
    path = Path(rel)
    if not rel or path.is_absolute() or any(p in ('..', '.') for p in path.parts):
        raise RuntimeError(f'Unsafe Linux HFS job path: {rel!r}')
    result = root.joinpath(*path.parts)
    if result.resolve().is_relative_to(root.resolve()) is False:
        raise RuntimeError(f'Escaping Linux HFS job path: {rel!r}')
    return result


def _run(*args: str, home: Path, stdout: bool = True) -> subprocess.CompletedProcess:
    env = dict(os.environ, HOME=str(home))
    print('[LAGOMORPH LINUX] ' + ' '.join(args), flush=True)
    return subprocess.run(list(args), check=True, env=env, capture_output=not stdout, text=True)


def assemble(root: Path, job_file: Path) -> None:
    root = root.resolve(strict=True)
    if not job_file.resolve(strict=True).is_relative_to(root):
        raise RuntimeError('Linux HFS job must be inside the mounted Builds tree')
    job = json.loads(job_file.read_text(encoding='utf-8'))
    if job.get('schema') != 'cinterforge.lagomorph.hfs-linux.v1' or not job.get('images'):
        raise RuntimeError('Unrecognized or empty Linux HFS job')
    package = _under(root, job['package'])
    if not package.is_file() or _sha(package) != job['package_sha256']:
        raise RuntimeError('Pinned Debian hfsutils package missing or checksum mismatch')
    # dpkg-deb -x restores root-owned archive metadata. Lima's macOS bind mount
    # does not permit chown, even for the container root user. Extract into the
    # container's own filesystem, not the shared Builds mount.
    with tempfile.TemporaryDirectory(prefix='cinter-hfsutils-', dir='/tmp') as local_dir:
        unpacked = Path(local_dir)
        _run('dpkg-deb', '-x', str(package), str(unpacked), home=unpacked)
        tools = {name: str(unpacked / 'usr/bin' / name) for name in TOOLS}
        for name, binary in tools.items():
            if not Path(binary).is_file() or not os.access(binary, os.X_OK):
                raise RuntimeError(f'Pinned Debian hfsutils lacks {name}')
        _assemble_images(root, job, tools)


def _assemble_images(root: Path, job: dict, tools: dict[str, str]) -> None:
    for index, spec in enumerate(job['images']):
        image = _under(root, spec['image'])
        if not image.parent.is_dir():
            raise RuntimeError('HFS output parent missing')
        capacity = spec['bytes']
        if not isinstance(capacity, int) or not 1024 * 1024 <= capacity <= 1024 ** 3:
            raise RuntimeError('Invalid HFS image capacity')
        home = _under(root, job['working']) / ('hfs-state-%d' % index)
        home.mkdir(parents=True, exist_ok=True)
        with image.open('wb') as output:
            output.truncate(capacity)
        _run(tools['hformat'], '-l', spec['volume_name'], str(image), home=home)
        with image.open('rb') as volume:
            volume.seek(1024)
            if volume.read(2) != b'BD':
                raise RuntimeError('Linux hformat did not create HFS Standard')
        _run(tools['hmount'], str(image), home=home)
        try:
            for directory in spec['directories']:
                _run(tools['hmkdir'], ':' + ':'.join(Path(directory).parts), home=home)
            for entry in spec['files']:
                encoded = _under(root, entry['macbinary'])
                if _sha(encoded) != entry['sha256']:
                    raise RuntimeError('MacBinary input modified after Mac fork encoding')
                _run(tools['hcopy'], '-m', str(encoded), ':' + ':'.join(Path(entry['target']).parts), home=home)
            application = spec.get('application')
            if application:
                verification = home / 'roundtrip.macbinary'
                _run(tools['hcopy'], '-m', ':' + ':'.join(Path(application['target']).parts),
                     str(verification), home=home)
                result = verification.read_bytes()
                if len(result) < 128:
                    raise RuntimeError('HFS application roundtrip is truncated')
                length = int.from_bytes(result[83:87], 'big')
                fork_len = int.from_bytes(result[87:91], 'big')
                start = 128 + ((length + 127) // 128) * 128
                if (result[65:73].hex() != application['finder8']
                        or fork_len != application['fork_bytes']
                        or _sha_bytes(result[start:start + fork_len]) != application['fork_sha256']):
                    raise RuntimeError('HFS application Finder type/creator or resource fork changed')
                print(f'[LAGOMORPH LINUX] PASS APPL fork roundtrip: {fork_len} bytes', flush=True)
            listing = _run(tools['hls'], home=home, stdout=False)
            if not listing.stdout.strip():
                raise RuntimeError('HFS volume listing is empty')
        finally:
            _run(tools['humount'], home=home)
        with image.open('rb') as volume:
            volume.seek(1024)
            if volume.read(2) != b'BD':
                raise RuntimeError('HFS Standard signature damaged during assembly')
        print('[LAGOMORPH LINUX] PASS ' + image.name, flush=True)


def _sha_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--builds', type=Path, required=True)
    parser.add_argument('--job', type=Path, required=True)
    args = parser.parse_args(argv)
    assemble(args.builds, args.job)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

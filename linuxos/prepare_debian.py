#!/usr/bin/env python3
"""Prepare a persistent Debian base userspace under Builds/Linux/Debian.

Requires Docker or Podman on the build Mac. This does not install Creative Suite
applications and does not modify the existing LinuxKit/CraftMaster image.
"""
from __future__ import annotations

import argparse
import json
import inspect
import os
import posixpath
import shutil
import os
import signal
import stat
import time
import subprocess
import tarfile
import tempfile
from pathlib import Path

import sys

def cfos_builds_root(repo_root: Path) -> Path:
    """Resolve the sole sibling Builds location using the CFOS source identity."""
    repo = repo_root.resolve()
    if not (repo / 'CFOS').is_file():
        raise RuntimeError(f'Expected CinterForgeOS checkout: {repo}')
    return repo.parent / 'Builds'


IMAGE = "docker.io/library/debian:bookworm-slim"
DEBIAN_DISK_GIB = 32


def release_orphaned_debian_disk_holders(disk: Path) -> None:
    """Reap abandoned Apple VM services, never an active VM or unknown owner.

    Apple's Virtualization XPC service can outlive CinterBDSVM. Both read-only
    and writable descriptors prevent an offline filesystem resize.
    """
    launcher = str(disk.parent / "CinterBDSVM")
    process_list = subprocess.run(
        ["ps", "-axo", "pid=,command="], capture_output=True, text=True, check=True,
    ).stdout
    for line in process_list.splitlines():
        fields = line.strip().split(maxsplit=1)
        if len(fields) == 2 and fields[1].split()[0] == launcher:
            raise RuntimeError(
                f"Cannot resize {disk}: CinterForge VM launcher PID {fields[0]} is still running"
            )

    def holders() -> set[int]:
        result = subprocess.run(
            ["lsof", "-t", "--", str(disk)],
            capture_output=True, text=True, check=False,
        )
        if result.returncode not in (0, 1):
            raise RuntimeError(f"Unable to inspect Debian disk users: {result.stderr.strip()}")
        return {int(value) for value in result.stdout.split() if value.isdigit()}

    remaining = holders()
    if not remaining:
        return

    xpc_prefix = (
        "/System/Library/Frameworks/Virtualization.framework/Versions/A/"
        "XPCServices/com.apple.Virtualization."
    )
    for pid in sorted(remaining):
        details = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True, text=True, check=False,
        )
        command = details.stdout.strip()
        if not command.startswith(xpc_prefix):
            raise RuntimeError(
                f"Cannot resize {disk}: disk holder PID {pid} is not a "
                f"CinterForge Virtualization XPC service: {command!r}"
            )

    for sig, timeout in ((signal.SIGTERM, 5.0), (signal.SIGKILL, 3.0)):
        for pid in sorted(remaining):
            print(f"[linux] releasing orphaned Debian VM disk holder PID {pid} "
                  f"with {sig.name}", flush=True)
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                pass
            except PermissionError as error:
                raise RuntimeError(f"Cannot terminate Debian disk holder PID {pid}: {error}") from error
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            remaining = holders()
            if not remaining:
                return
            time.sleep(0.2)
        remaining = holders()
        if not remaining:
            return
        # Do not escalate against a newly attached or unknown process.
        for pid in remaining:
            details = subprocess.run(
                ["ps", "-p", str(pid), "-o", "command="],
                capture_output=True, text=True, check=False,
            )
            if not details.stdout.strip().startswith(xpc_prefix):
                raise RuntimeError(f"Debian disk acquired by unexpected PID {pid}; refusing to kill")

    raise RuntimeError(f"Debian disk still open by PID(s) {sorted(remaining)}: {disk}")


def grow_ext4_disk(system: Path, engine: str) -> None:
    """Expand an existing Debian disk without replacing its installed contents.

    Filesystem repair and resize run offline in a Linux container; resizing an
    ext4 filesystem while the VM has it mounted would risk data loss.
    """
    disk = system / "DebianAMD64.ext4"
    target_bytes = DEBIAN_DISK_GIB * 1024 ** 3
    if disk.stat().st_size >= target_bytes:
        return

    release_orphaned_debian_disk_holders(disk)

    print(f"[linux] expanding existing Debian ext4 disk to {DEBIAN_DISK_GIB} GiB: {disk}", flush=True)
    # The original disk is preserved; if resizing fails, do not start the VM.
    # Keep all operations inside Linux, where e2fsck and resize2fs are available.
    subprocess.run([
        engine, "run", "--rm", "--platform", "linux/amd64",
        "-v", f"{system}:/cinter", "alpine:3.20", "/bin/sh", "-ec",
        "apk add --no-cache e2fsprogs >/dev/null; "
        "e2fsck -f -y /cinter/DebianAMD64.ext4 || test $? -eq 1; "
        f"truncate -s {DEBIAN_DISK_GIB}G /cinter/DebianAMD64.ext4; "
        "resize2fs /cinter/DebianAMD64.ext4; "
        "e2fsck -f -n /cinter/DebianAMD64.ext4",
    ], check=True)
    print("[linux] Debian ext4 filesystem expanded and checked", flush=True)


def ensure_ext4_disk(system: Path, engine: str, image: str) -> None:
    """Build the writable Linux-owned Debian filesystem once, from the base image."""
    disk = system / "DebianAMD64.ext4"
    if disk.is_file():
        if disk.stat().st_size < 1024 * 1024 * 1024:
            raise RuntimeError(f"Debian disk image is unexpectedly small: {disk}")
        grow_ext4_disk(system, engine)
        print(f"[linux] persistent Debian ext4 disk ready: {disk}", flush=True)
        return
    rootfs = system / "DebianAMD64"
    if not (rootfs / "usr/bin/apt-get").is_file():
        raise RuntimeError(f"Debian base rootfs is missing: {rootfs}")
    temporary = system / "DebianAMD64.ext4.creating"
    temporary.unlink(missing_ok=True)
    # The previously prepared host-side Debian tree may contain partial dpkg
    # writes from failed VirtioFS installations. It is not a safe mkfs input:
    # files can disappear during traversal and host ownership differs from Linux.
    # Export a fresh official Debian image and extract it INSIDE a Linux
    # container, then populate ext4 from that stable Linux-local directory.
    print(f"[linux] creating persistent {DEBIAN_DISK_GIB} GiB Debian ext4 disk from clean Debian image", flush=True)
    archive = system / "Debian-base-export.tar"
    container_id = subprocess.check_output(
        [engine, "create", "--platform", "linux/amd64", image], text=True
    ).strip()
    try:
        with archive.open("wb") as output:
            subprocess.run([engine, "export", container_id], stdout=output, check=True)
    finally:
        subprocess.run(
            [engine, "rm", "-f", container_id], check=False,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    try:
        subprocess.run([
            engine, "run", "--rm", "--platform", "linux/amd64",
            "-v", f"{system}:/cinter", "alpine:3.20", "/bin/sh", "-ec",
            "set -eu; "
            "apk add --no-cache e2fsprogs >/dev/null; "
            "mkdir -p /tmp/cinter-debian-rootfs; "
            "tar -xf /cinter/Debian-base-export.tar -C /tmp/cinter-debian-rootfs; "
            "test -f /tmp/cinter-debian-rootfs/usr/bin/apt-get; "
            f"truncate -s {DEBIAN_DISK_GIB}G /cinter/DebianAMD64.ext4.creating; "
            "mkfs.ext4 -F -q -d /tmp/cinter-debian-rootfs /cinter/DebianAMD64.ext4.creating; "
            "e2fsck -fn /cinter/DebianAMD64.ext4.creating >/dev/null",
        ], check=True)
        temporary.replace(disk)
    finally:
        temporary.unlink(missing_ok=True)
        archive.unlink(missing_ok=True)
    print(f"[linux] Debian ext4 disk created: {disk}", flush=True)


def remove_host_extracted_rootfs(destination: Path) -> None:
    """Remove only the generated rootfs, including inaccessible Linux directories.

    Docker's exported tar preserves directory modes that can deny traversal on
    macOS. Make the directory tree traversable and writable before calling
    shutil.rmtree; its onerror callback cannot safely retry os.open because
    shutil uses relative names and directory file descriptors internally.
    """
    if destination.is_symlink() or not destination.is_dir():
        raise RuntimeError(f"Expected generated Debian directory: {destination}")

    def allow_owner_access(directory: Path) -> None:
        if directory.is_symlink():
            return
        current_mode = stat.S_IMODE(directory.stat(follow_symlinks=False).st_mode)
        os.chmod(directory, current_mode | stat.S_IRWXU, follow_symlinks=False)

    allow_owner_access(destination)
    for current_root, directories, _files in os.walk(destination, topdown=True, followlinks=False):
        for name in directories:
            allow_owner_access(Path(current_root) / name)

    shutil.rmtree(destination)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", required=True, type=Path)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--shared-distro", action="store_true",
                        help="Use the canonical CinterForge Linux OCI image for a NEW LinuxKit Debian disk")
    args = parser.parse_args()
    # Build the shared distribution beside the active Ardour LinuxKit disk.
    # Never overwrite or resize the existing DebianAMD64.ext4 while Ardour builds.
    linux_builds = cfos_builds_root(args.repo_root) / "Linux"
    system = linux_builds / "SharedDistro" if args.shared_distro else linux_builds
    destination = system / "DebianAMD64"
    marker = destination / "CinterDebian.json"
    image = "cinterforge-linux:amd64" if args.shared_distro else IMAGE
    disk_path = system / "DebianAMD64.ext4"
    if args.shared_distro and disk_path.exists() and args.force:
        raise RuntimeError(
            f"Refusing --force on existing shared LinuxKit disk {disk_path}. "
            "Delete or relocate the inactive shared disk explicitly before rebuilding."
        )
    if marker.is_file() and not args.force:
        metadata = json.loads(marker.read_text())
        if metadata.get("image") == image and metadata.get("architecture") == "amd64" and (destination / "usr/bin/apt-get").exists():
            print(f"[linux] Debian userspace already prepared: {destination}")
            engine = shutil.which("docker") or shutil.which("podman")
            if not engine:
                raise RuntimeError("Creating the Debian ext4 disk requires Docker or Podman")
            ensure_ext4_disk(system, engine, image)
            return 0
    engine = shutil.which("docker") or shutil.which("podman")
    if not engine:
        raise RuntimeError("Debian preparation requires Docker or Podman on the build Mac")
    system.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="cinter-debian-", dir=system) as temporary:
        staging = Path(temporary) / "rootfs"
        staging.mkdir()
        if args.shared_distro:
            # Build from the same Dockerfile used by cloud and standalone runtime.
            # Do not create a second independently managed Linux dependency set.
            subprocess.run([engine, "build", "--platform", "linux/amd64", "-f",
                            str(Path(__file__).with_name("Dockerfile.distro")), "-t", image,
                            str(Path(__file__).parent)], check=True)
        else:
            subprocess.run([engine, "pull", "--platform", "linux/amd64", image], check=True)
        container = subprocess.check_output(
            [engine, "create", "--platform", "linux/amd64", image], text=True
        ).strip()
        try:
            archive = Path(temporary) / "rootfs.tar"
            with archive.open("wb") as output:
                subprocess.run([engine, "export", container], stdout=output, check=True)
            # The archive comes from the explicitly selected official image.
            # Refuse escaping paths before extraction.
            with tarfile.open(archive) as package:
                for item in package:
                    name = Path(item.name)
                    if name.is_absolute() or ".." in name.parts:
                        raise RuntimeError(f"Unsafe rootfs archive path: {item.name}")
                # Python 3.11.0 does not support extractall(filter=...).
                # Validate archive members and links before using the older API.
                # The container export is trusted, but must not escape staging.
                members = package.getmembers()
                for item in members:
                    if item.isdev() or item.isfifo():
                        raise RuntimeError(f"Unsupported Debian rootfs entry: {item.name}")
                    if not (item.issym() or item.islnk()):
                        continue

                    # Container root filesystems routinely contain absolute
                    # links (for example /etc/alternatives/awk -> /usr/bin/mawk).
                    # Those links are guest-root-relative, not host-root-relative.
                    # Convert them to relative links within the extracted tree.
                    if item.linkname.startswith("/"):
                        guest_target = posixpath.normpath(item.linkname.lstrip("/"))
                        if guest_target in ("", ".", "..") or guest_target.startswith("../"):
                            raise RuntimeError(
                                f"Unsafe Debian rootfs link: {item.name} -> {item.linkname}"
                            )
                        if item.issym():
                            item.linkname = posixpath.relpath(
                                guest_target, posixpath.dirname(item.name) or "."
                            )
                        else:
                            item.linkname = guest_target

                    base = posixpath.dirname(item.name) if item.issym() else ""
                    target = posixpath.normpath(posixpath.join(base, item.linkname))
                    if target == ".." or target.startswith("../") or target.startswith("/"):
                        raise RuntimeError(
                            f"Escaping Debian rootfs link: {item.name} -> {item.linkname}"
                        )

                # Reject entries nested under archive symlinks. Otherwise an
                # extraction could follow a link outside the staging tree.
                symlink_names = {posixpath.normpath(item.name) for item in members if item.issym()}
                for item in members:
                    parent = posixpath.dirname(posixpath.normpath(item.name))
                    while parent not in ("", ".", "/"):
                        if parent in symlink_names:
                            raise RuntimeError(
                                f"Debian rootfs entry traverses archive symlink: {item.name}"
                            )
                        parent = posixpath.dirname(parent)
                # Python 3.11.0 lacks the `filter` keyword, even though newer
                # 3.11 patch releases and Python 3.12+ provide it. Test the
                # extractall signature rather than assuming a Python version.
                supports_filter = "filter" in inspect.signature(package.extractall).parameters
                if supports_filter:
                    print("[linux] extracting Debian rootfs with tar data filter", flush=True)
                    package.extractall(path=staging, filter="data")
                else:
                    print("[linux] extracting Debian rootfs with validated members (Python 3.11.0)", flush=True)
                    package.extractall(path=staging)
        finally:
            subprocess.run([engine, "rm", "-f", container], check=False,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if not (staging / "usr/bin/apt-get").is_file():
            raise RuntimeError("Exported Debian image has no apt-get")
        (staging / "CinterDebian.json").write_text(
            json.dumps({"image": image, "architecture": "amd64", "status": "base-only"}, indent=2) + "\n"
        )
        if destination.exists():
            remove_host_extracted_rootfs(destination)
        staging.rename(destination)
    ensure_ext4_disk(system, engine, image)
    print(f"[linux] Debian base prepared: {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

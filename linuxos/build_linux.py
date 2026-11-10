#!/usr/bin/env python3
"""Prepare the shared Linux runtime from checked-in Linux source.

All LinuxKit outputs are under Builds/CreativeSuite/CFOS/LinuxKit. Application packages are not
part of this shared runtime and remain owned by their respective consumers.
"""
from __future__ import annotations

import argparse
import hashlib
import shutil
import platform
import os
import subprocess
from pathlib import Path
def cfos_builds_root(repo_root: Path) -> Path:
    """Resolve the sole sibling Builds location using the CFOS source identity."""
    repo = repo_root.resolve()
    if not (repo / 'Cinter').is_file():
        raise RuntimeError(f'Expected CinterForgeTerminal checkout: {repo}')
    return repo.parent / 'Builds'


_LINUXKIT_ARTIFACT_NAMES = frozenset({
    "linuxkit.yml", "CinterBDSVM.swift", "CinterBDSVM",
    "bootstrap.json",
    "cinter-bds-kernel", "cinter-bds-initrd.img", "cinter-bds-cmdline",
    "built-source-fingerprint.txt", "vm-launcher-source-fingerprint.txt",
})


def linuxkit_bootstrap_root(repo_root: Path, *, migrate: bool = True) -> Path:
    """Resolve CFOS-owned LinuxKit artifacts, safely relocating old Builds/Linux.

    Move only CFOS-owned LinuxKit outputs. A legacy Builds/Linux directory can
    contain unrelated applications and disk state: leave those files intact.
    Reuse existing signed launchers, images and fingerprints without rebuilding.
    """
    builds = cfos_builds_root(repo_root)
    target = builds / "CreativeSuite" / "CFOS" / "LinuxKit"
    legacy = builds / "Linux"
    if migrate and legacy.exists():
        if legacy.is_symlink() or not legacy.is_dir():
            raise RuntimeError(f"Legacy LinuxKit location is not a regular directory: {legacy}")
        selected = [p for p in legacy.iterdir() if p.name in _LINUXKIT_ARTIFACT_NAMES]
        if selected:
            if target.exists() and (not target.is_dir() or target.is_symlink()):
                raise RuntimeError(f"Invalid LinuxKit destination: {target}")
            # Check for conflicts and symlinks before moving any file.
            for artifact in selected:
                destination = target / artifact.name
                if artifact.is_symlink() or not artifact.is_file():
                    raise RuntimeError(f"Invalid legacy LinuxKit artifact: {artifact}")
                if destination.exists() or destination.is_symlink():
                    if destination.is_symlink() or not destination.is_file() or \
                       artifact.stat().st_size != destination.stat().st_size or \
                       artifact.read_bytes() != destination.read_bytes():
                        raise RuntimeError(
                            f"Conflicting LinuxKit artifacts; preserve both and reconcile: "
                            f"{artifact} and {destination}")
            target.mkdir(parents=True, exist_ok=True)
            for artifact in selected:
                destination = target / artifact.name
                if destination.is_file():
                    artifact.unlink()  # Byte-for-byte identical verified duplicate
                else:
                    artifact.rename(destination)
            if not any(legacy.iterdir()):
                legacy.rmdir()
            else:
                print(f"[CFOS LinuxKit] leaving unrelated legacy files in {legacy}", flush=True)
            print(f"[CFOS LinuxKit] migrated {len(selected)} artifacts: {legacy} -> {target}", flush=True)
    return target


from launcher_signing import has_virtualization_entitlement, sign_launcher

HERE = Path(__file__).resolve().parent
SOURCES = ("linuxkit.yml", "CinterBDSVM.swift")



def ensure_arm64_boot_kernel(kernel: Path) -> None:
    """Ensure the VZLinuxBootLoader receives a raw ARM64 Image, not Image.gz."""
    import gzip
    import os

    if not kernel.is_file():
        raise FileNotFoundError(f"LinuxKit ARM64 kernel is missing: {kernel}")
    with kernel.open("rb") as stream:
        signature = stream.read(2)
    if signature == b"\x1f\x8b":
        temporary = kernel.with_name(kernel.name + ".uncompressed.tmp")
        try:
            with gzip.open(kernel, "rb") as source, temporary.open("wb") as output:
                shutil.copyfileobj(source, output)
            with temporary.open("rb") as stream:
                stream.seek(0x38)
                magic = stream.read(4)
            if magic != b"ARM\x64":
                raise RuntimeError(
                    f"Decompressed LinuxKit kernel is not an ARM64 Linux Image: {kernel} "
                    f"(header magic={magic.hex()})"
                )
            os.replace(temporary, kernel)
            print(f"[linux] decompressed ARM64 Linux Image for Virtualization.framework: {kernel}", flush=True)
        finally:
            temporary.unlink(missing_ok=True)
    else:
        with kernel.open("rb") as stream:
            stream.seek(0x38)
            magic = stream.read(4)
        if magic != b"ARM\x64":
            raise RuntimeError(
                f"LinuxKit kernel is not a raw ARM64 Linux Image: {kernel} "
                f"(header magic={magic.hex()}, first bytes={signature.hex()}). "
                "Virtualization.framework cannot boot this kernel format."
            )
    print(f"[linux] verified ARM64 Linux Image: {kernel} ({kernel.stat().st_size} bytes)", flush=True)


def validate_linuxkit_images(source: Path) -> None:
    """Do not send unexpanded local package placeholders to linuxkit build.

    The CFOS appliance uses explicitly selected LinuxKit ARM64 OCI images;
    linuxkit-source/pkg is neither checked in nor staged beside this YAML.
    """
    configuration = source.read_text()
    if any(token in configuration for token in ('@pkg:', 'dummy://', 'linuxkit-source/pkg/')):
        raise RuntimeError(
            f"Unresolved LinuxKit package reference in {source}. "
            "The LinuxKit base images must have explicit ARM64 image references "
            "in the checked-in linuxkit.yml; no local linuxkit-source tree is staged."
        )


# The original VirtioFS kernel producer lived in CraftMaster's BUILD_SH before
# the repository split. Keep its pinned kernel source, config fragment and
# linuxkit pkg commands; Setup (via linuxos/lifecycle.py) owns invoking this.
KERNEL_SOURCE_TAG = "v1.8.2"
KERNEL_SOURCE_COMMIT = "43200ea"
KERNEL_VERSION = "6.6.71"
KERNEL_EXTRA = "-cinter-virtiofs"
KERNEL_IMAGE_PATTERN = KERNEL_VERSION + "-cinter-virtiofs-{{.Hash}}"
KERNEL_FRAGMENT_CONTENT = "CONFIG_VIRTIO_FS=y\n"


def _render_kernel_image(configuration: str, image: str) -> str:
    lines = configuration.splitlines()
    try:
        position = next(i for i, line in enumerate(lines) if line == "kernel:")
    except StopIteration as error:
        raise RuntimeError("Missing LinuxKit kernel stanza") from error
    if position + 1 == len(lines) or not lines[position + 1].startswith("  image: "):
        raise RuntimeError("Unexpected LinuxKit kernel image stanza")
    lines[position + 1] = "  image: " + image
    return "\n".join(lines) + "\n"


def _staged_kernel_image(destination: Path) -> str | None:
    marker = destination / "custom-kernel-image.txt"
    if not marker.is_file():
        return None
    image = marker.read_text().strip()
    if not image.startswith("linuxkit/kernel:" + KERNEL_VERSION + "-cinter-virtiofs-"):
        return None
    return image


def _clone_kernel_source(builds: Path) -> Path:
    """Provision the original v1.8.2 LinuxKit kernel sources solely in Builds."""
    root = builds / "CinterForgeTerminal/Toolchains/mac/linuxkit/kernel-source"
    git = root / ".git"
    if git.is_dir():
        result = subprocess.run(["git", "-C", str(root), "rev-parse", "--short=7", "HEAD"],
                                check=True, capture_output=True, text=True)
        if result.stdout.strip() == KERNEL_SOURCE_COMMIT:
            return root
        # A different checkout must not silently generate an unrelated kernel.
        raise RuntimeError(f"Pinned LinuxKit kernel source changed at {root}; "
                           f"expected {KERNEL_SOURCE_COMMIT}, found {result.stdout.strip()}")
    if root.exists():
        if root.is_symlink() or not root.is_dir():
            raise RuntimeError(f"Unsafe LinuxKit source staging path: {root}")
        print(f"[linux] repairing incomplete generated LinuxKit source clone: {root}", flush=True)
        shutil.rmtree(root)
    root.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "clone", "--depth", "1", "--branch", KERNEL_SOURCE_TAG,
                    "--single-branch", "https://github.com/linuxkit/linuxkit.git", str(root)],
                   check=True)
    return _clone_kernel_source(builds)


def _linuxkit_build_environment(repo_root: Path) -> dict[str, str]:
    """Use only the Setup-managed Docker backend and Builds-scoped LinuxKit cache."""
    docker = shutil.which("docker")
    if not docker:
        raise RuntimeError("Kernel provisioning requires the Setup-managed Docker CLI")
    context = subprocess.run([docker, "context", "show"], check=True,
                             capture_output=True, text=True).stdout.strip()
    if not context:
        raise RuntimeError("Managed Docker context is empty; cannot select ARM64 builder")
    cache = cfos_builds_root(repo_root) / "CinterForgeTerminal/Toolchains/mac/linuxkit/cache"
    cache.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["LINUXKIT_BUILDERS"] = "linux/arm64=" + context
    env["LINUXKIT_CACHE"] = str(cache)
    return env


def ensure_custom_kernel_image(repo_root: Path, destination: Path) -> str:
    """Restore the original LinuxKit pkg producer; rebuild absent/dirty OCI images.

    `linuxkit build` cannot fetch the private -dirty- VirtioFS kernel from Docker
    Hub. Setup must materialize it in its managed Colima Docker engine first.
    """
    builds = cfos_builds_root(repo_root)
    source = _clone_kernel_source(builds)
    kernel_dir = source / "kernel"
    base_args = kernel_dir / "6.6.x/build-args"
    build_yml = kernel_dir / "build-kernel.yml"
    if not base_args.is_file() or not build_yml.is_file():
        raise RuntimeError(f"Pinned LinuxKit kernel build inputs missing from {kernel_dir}")
    fragment = kernel_dir / ("6.6.x-" + KERNEL_EXTRA) / "config-aarch64"
    fragment.parent.mkdir(parents=True, exist_ok=True)
    if not fragment.is_file() or fragment.read_text() != KERNEL_FRAGMENT_CONTENT:
        fragment.write_text(KERNEL_FRAGMENT_CONTENT)
    args_file = destination / "cinter-kernel-build-args"
    args = base_args.read_text().rstrip("\n") + "\nEXTRA=" + KERNEL_EXTRA + "\n"
    if not args_file.is_file() or args_file.read_text() != args:
        args_file.write_text(args)
    # Preserve the original build and show-tag arguments byte-for-byte in
    # substance, including the custom ARM64 config fragment and EXTRA flag.
    tag = subprocess.run(["linuxkit", "pkg", "show-tag", ".", "--build-yml",
                          "./build-kernel.yml", "--tag", KERNEL_IMAGE_PATTERN],
                         cwd=kernel_dir, check=True, text=True, capture_output=True).stdout.strip()
    if not tag.startswith("linuxkit/kernel:" + KERNEL_VERSION + "-cinter-virtiofs-") or "\n" in tag:
        raise RuntimeError(f"Unexpected custom VirtioFS kernel package tag: {tag!r}")
    previous = _staged_kernel_image(destination)
    inputs_digest = hashlib.sha256((KERNEL_SOURCE_COMMIT + "\n" + KERNEL_SOURCE_TAG + "\n"
                                     + args + KERNEL_FRAGMENT_CONTENT + tag).encode()).hexdigest() + "\n"
    inputs_marker = destination / "custom-kernel-inputs.sha256"
    inputs_dirty = inputs_marker.is_file() and inputs_marker.read_text() != inputs_digest
    # Check the active managed Docker engine, NOT a marker from another VM.
    docker = shutil.which("docker")
    env = _linuxkit_build_environment(repo_root)
    inspection = subprocess.run([docker, "image", "inspect", tag],
                                capture_output=True, text=True, check=False, env=env)
    if (inspection.returncode != 0 or not inputs_marker.is_file() or
            inputs_dirty or (previous is not None and previous != tag)):
        print(f"[linux] building missing/dirty ARM64 VirtioFS kernel: {tag}", flush=True)
        subprocess.run(["linuxkit", "pkg", "build", ".", "--force", "--docker", "--platforms", "linux/arm64",
                        "--build-yml", "./build-kernel.yml", "--tag", KERNEL_IMAGE_PATTERN,
                        "--build-arg-file", str(args_file)],
                       cwd=kernel_dir, env=env, check=True)
        # The original producer resolves the hash again AFTER pkg build.
        resolved = subprocess.run(["linuxkit", "pkg", "show-tag", ".", "--build-yml",
                                   "./build-kernel.yml", "--tag", KERNEL_IMAGE_PATTERN],
                                  cwd=kernel_dir, check=True, capture_output=True,
                                  text=True, env=env).stdout.strip()
        if resolved != tag:
            raise RuntimeError(f"LinuxKit kernel package changed during build: {tag} -> {resolved}")
        # Do not publish this private -dirty- image. It must exist locally.
        subprocess.run([docker, "image", "inspect", tag], check=True,
                       capture_output=True, text=True, env=env)
    else:
        print(f"[linux] custom VirtioFS kernel present and current: {tag}", flush=True)
    (destination / "custom-kernel-image.txt").write_text(tag + "\n")
    inputs_marker.write_text(inputs_digest)
    return tag


def stage_sources(repo_root: Path) -> Path:
    destination = linuxkit_bootstrap_root(repo_root)
    destination.mkdir(parents=True, exist_ok=True)
    for filename in SOURCES:
        source = HERE / filename
        if not source.is_file():
            raise FileNotFoundError(f"Missing checked-in Linux source: {source}")
        if filename == "linuxkit.yml":
            validate_linuxkit_images(source)
        target = destination / filename
        # The rendered kernel tag is a derived local image ID, not an authored
        # source change. Keep it when all other YAML bytes still match.
        if filename == "linuxkit.yml" and target.is_file():
            image = _staged_kernel_image(destination)
            if image and target.read_text() == _render_kernel_image(source.read_text(), image):
                continue
        if not target.is_file() or target.read_bytes() != source.read_bytes():
            shutil.copy2(source, target)
    return destination


def shared_source_fingerprint(destination: Path) -> str:
    """Identify the authored LinuxKit build inputs, not runtime build timestamps."""
    digest = hashlib.sha256()
    for filename in SOURCES:
        digest.update(filename.encode())
        digest.update((HERE / filename).read_bytes())
    rendered = destination / "linuxkit.yml"
    if not rendered.is_file():
        raise RuntimeError(f"CFOS shared LinuxKit configuration missing: {rendered}")
    digest.update(rendered.read_bytes())
    digest.update(b"linuxkit-bedrock-rosetta-appliance-v1")
    return digest.hexdigest() + "\n"


def verify_shared_bootstrap(repo_root: Path, *, require_launcher: bool = False) -> Path:
    """Read-only consumer prerequisite; only CFOS Linux Build may construct it.

    The VM host launcher is macOS-only (Virtualization.framework), whereas the
    ARM64 LinuxKit kernel/initrd can be built on a Linux build host. When run
    on macOS the launcher must already be compiled and signed by CFOS.
    """
    destination = linuxkit_bootstrap_root(repo_root)
    kernel = destination / "cinter-bds-kernel"
    required = (
        destination / "cinter-bds-kernel",
        destination / "cinter-bds-initrd.img",
        destination / "cinter-bds-cmdline",
        destination / "built-source-fingerprint.txt",
    )
    if require_launcher:
        required += (
            destination / "CinterBDSVM",
            destination / "vm-launcher-source-fingerprint.txt",
        )
    missing = [str(path) for path in required if not path.is_file() or path.stat().st_size == 0]
    if missing:
        raise RuntimeError(
            "CFOS LinuxKit must be built and provisioned before any consumer build; "
            "run ./CFOS build cinterforge-os linuxkit Release first. Missing: " + ", ".join(missing)
        )
    actual = (destination / "built-source-fingerprint.txt").read_text()
    if actual != shared_source_fingerprint(destination):
        # Old LinuxKit receipts fingerprinted the host-only CinterBDSVM.swift
        # along with linuxkit.yml. A host networking/bridging edit must not
        # invalidate the already built guest kernel and initrd. Accept the
        # original receipt only when the guest YAML is still identical to the
        # source and the saved legacy source snapshot matches its receipt.
        staged_yaml = destination / 'linuxkit.yml'
        staged_swift = destination / 'CinterBDSVM.swift'
        guest_unchanged = (staged_yaml.is_file() and
                           staged_yaml.read_bytes() == (HERE / 'linuxkit.yml').read_bytes())
        legacy_fingerprint = hashlib.sha256()
        if staged_swift.is_file():
            for name in SOURCES:
                legacy_fingerprint.update(name.encode())
                legacy_fingerprint.update((destination / name).read_bytes())
            legacy_fingerprint.update(staged_yaml.read_bytes())
            legacy_fingerprint.update(b'linuxkit-bedrock-rosetta-appliance-v1')
        legacy_matches = (staged_swift.is_file() and
                          actual == legacy_fingerprint.hexdigest() + '\n')
        if not (guest_unchanged and legacy_matches):
            raise RuntimeError(
                'CFOS LinuxKit guest is stale: LinuxKit image inputs changed. '
                'Rebuild the guest image before consuming it.'
            )
        print('[CFOS LinuxKit] Host launcher source changed; existing guest image remains valid', flush=True)
    with kernel.open("rb") as stream:
        stream.seek(0x38)
        magic = stream.read(4)
    if magic != b"ARM\x64":
        raise RuntimeError(f"CFOS LinuxKit kernel is not an uncompressed ARM64 Image: {kernel}")
    if require_launcher:
        expected = hashlib.sha256((HERE / "CinterBDSVM.swift").read_bytes() + (HERE / "CinterBDSVM.entitlements").read_bytes()).hexdigest() + "\n"
        if (destination / "vm-launcher-source-fingerprint.txt").read_text() != expected:
            raise RuntimeError("CFOS macOS LinuxKit host launcher is stale; rebuild it through CFOS")
        if not has_virtualization_entitlement(destination / "CinterBDSVM"):
            raise RuntimeError("CFOS LinuxKit launcher has no valid virtualization entitlement")
    print(f"[CFOS LinuxKit] VERIFIED prebuilt shared bootstrap: {destination}", flush=True)
    return destination


def shared_bootstrap_digest(repo_root: Path) -> str:
    """Fingerprint the tested LinuxKit image consumed by the CFOS Ubuntu lock."""
    destination = verify_shared_bootstrap(repo_root)
    digest = hashlib.sha256()
    for name in ("built-source-fingerprint.txt", "cinter-bds-kernel", "cinter-bds-initrd.img", "cinter-bds-cmdline"):
        digest.update(name.encode())
        with (destination / name).open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--verify-only", action="store_true",
                        help="Validate the already built shared LinuxKit artifact without modifying it")
    args = parser.parse_args()
    root = args.repo_root.resolve()
    if args.verify_only:
        verify_shared_bootstrap(root, require_launcher=False)
        return 0
    destination = stage_sources(root)
    required = ("cinter-bds-kernel", "cinter-bds-initrd.img", "cinter-bds-cmdline")
    # LinuxKit produces the kernel/initrd/cmdline only. The macOS Swift launcher
    # is compiled below, by CFOS, after its guest image is ready. It is never
    # an expected output of `linuxkit build`.
    fingerprint = shared_source_fingerprint(destination)
    built_marker = destination / "built-source-fingerprint.txt"
    current = all((destination / name).is_file() for name in required) and built_marker.is_file() and built_marker.read_text() == fingerprint
    if current:
        print("[linux] shared LinuxKit bootstrap current; skipping", flush=True)
    else:
        if shutil.which("linuxkit") is None:
            raise RuntimeError("Shared Linux runtime build requires the Setup-managed linuxkit executable on PATH")
        # The previous Setup produced this package via linuxkit pkg build.
        # A new Colima VM has no local custom kernel image; reconstruct it
        # before linuxkit build rather than trying to pull a private dirty tag.
        image = ensure_custom_kernel_image(root, destination)
        rendered = _render_kernel_image((HERE / "linuxkit.yml").read_text(), image)
        if (destination / "linuxkit.yml").read_text() != rendered:
            (destination / "linuxkit.yml").write_text(rendered)
        fingerprint = shared_source_fingerprint(destination)
        print("[linux] building shared LinuxKit bootstrap from staged YAML", flush=True)
        subprocess.run([
            "linuxkit", "build", "--docker", "--arch", "arm64", "--format", "kernel+initrd",
            "--name", "cinter-bds", "linuxkit.yml",
        ], cwd=destination, check=True, env=_linuxkit_build_environment(root))
        missing = [name for name in required if not (destination / name).is_file()]
        if missing:
            raise RuntimeError(f"Linux bootstrap did not produce: {missing}")
        # A failed kernel+initrd build must not look current on retry.
        built_marker.write_text(fingerprint)
    ensure_arm64_boot_kernel(destination / "cinter-bds-kernel")
    # The LinuxKit builder may reuse an older compiled VM launcher even when
    # CinterBDSVM.swift changes. Compile the shared host launcher independently:
    # its VirtioFS tags must match those mounted by linuxkit.yml.
    swift_source = HERE / "CinterBDSVM.swift"
    swift_fingerprint = hashlib.sha256(swift_source.read_bytes() + (HERE / "CinterBDSVM.entitlements").read_bytes()).hexdigest() + "\n"
    swift_marker = destination / "vm-launcher-source-fingerprint.txt"
    swift_binary = destination / "CinterBDSVM"
    if platform.system() == "Darwin":
        launcher_current = (
            swift_binary.is_file()
            and swift_marker.is_file()
            and swift_marker.read_text() == swift_fingerprint
        )
        if launcher_current:
            print("[linux] shared VM launcher current; skipping", flush=True)
            if not has_virtualization_entitlement(swift_binary):
                print("[linux] signing VM launcher with virtualization entitlement", flush=True)
                sign_launcher(swift_binary)
        else:
            print("[linux] compiling shared VM launcher (VirtioFS shares)", flush=True)
            subprocess.run([
                "xcrun", "swiftc", "-framework", "Virtualization",
                "-o", str(swift_binary), str(swift_source),
            ], check=True)
            sign_launcher(swift_binary)
            swift_marker.write_text(swift_fingerprint)
    verify_shared_bootstrap(root, require_launcher=platform.system() == "Darwin")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        import sys
        print(f'[CFOS LinuxKit] ERROR: {error}', file=sys.stderr, flush=True)
        raise SystemExit(1)

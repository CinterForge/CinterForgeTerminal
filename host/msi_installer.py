"""Native Debian wixl Windows Installer producer for the existing CinterForge lifecycle.

Setup ONLY: use the Setup-provisioned, version-checked wixl/msitools toolchain
and compile, inspect, and extract a real tiny x64 MSI. Publish ONLY: compile
verified unpacked Windows Build files using the prevalidated OCI image.
No WiX 3 executable, PE32 loader, Wine Mono, provisioning, or extraction is
needed by MSI packaging. Other Windows guest testing still uses patched Wine.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import shutil
import uuid
import xml.etree.ElementTree as ET

from host.setup_progress import progress_step

WIXL_PACKAGE_VERSION = '0.106+repack-1'  # Debian Trixie package provided by msitools source

UPGRADE_CODE = '506BDCC3-BB2B-5035-BC0D-FD99F49F3CB3'
GUID_NAMESPACE = uuid.UUID('e67096da-e7c1-4c25-9a96-68b6eea50ff1')
NS = 'http://schemas.microsoft.com/wix/2006/wi'
SCHEMA = 'cinterforge.terminal.windows-msi-preflight.v5'


def _builds(builds: Path) -> Path:
    builds = Path(builds).resolve(strict=True)
    if builds.name != 'Builds' or not builds.is_dir() or builds.is_symlink():
        raise RuntimeError('Windows MSI requires the canonical sibling Builds directory')
    return builds


def _stage(builds: Path) -> Path:
    return builds / 'CinterForgeTerminal/Toolchains/WindowsMSI'


def _sha(path: Path) -> str:
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def _check_tools(builds: Path) -> None:
    """Publish validates Setup's completed acceptance, never installs tools."""
    stage = _stage(builds)
    proof = stage / 'msi-preflight.json'
    if proof.is_symlink():
        raise RuntimeError('Symlinked Windows MSI Setup receipt')
    try:
        record = json.loads(proof.read_text('utf-8'))
    except (OSError, ValueError):
        record = {}
    smoke = builds / 'Intermediates/CinterForgeTerminal/WindowsMSI/Smoke/InstallerSmoke.msi'
    if (record.get('schema') != SCHEMA or record.get('status') != 'passed' or
            record.get('wixl_package_version') != WIXL_PACKAGE_VERSION or
            not record.get('wixl_version') or
            not smoke.is_file() or smoke.is_symlink() or
            record.get('smoke_sha256') != _sha(smoke) or
            not (builds / 'Intermediates/CinterForgeTerminal/WindowsMSI/TemplateSmoke/CinterForgeTerminal-win.msi').is_file() or
            (builds / 'Intermediates/CinterForgeTerminal/WindowsMSI/TemplateSmoke/CinterForgeTerminal-win.msi').is_symlink() or
            record.get('template_sha256') != _sha(builds / 'Intermediates/CinterForgeTerminal/WindowsMSI/TemplateSmoke/CinterForgeTerminal-win.msi')):
        raise RuntimeError('Setup MSI tools missing or acceptance receipt invalid; run ./Cinter setup all')
    # Confirm the OCI image whose native wixl/msitools produced the Setup receipt
    # is still the staged, pinned toolchain identity. No daemon/start/download.
    from host.windows_environment import WindowsCrossEnvironment
    _, identity = WindowsCrossEnvironment.validate_staged(builds)
    if record.get('image') != identity.get('image'):
        raise RuntimeError('Windows MSI Setup image changed; rerun Setup')
    # Wine is not a reliable implementation of the native Windows ARP UI.
    # Require the actual compiled MSI table evidence; native Windows installation
    # and removal are user acceptance, not a simulated Setup gate.
    proof_registration = record.get('template_checks', {}).get('registration', {})
    if (proof_registration.get('arp_install_location') != '[INSTALLDIR]' or
            proof_registration.get('arp_action_type') != 51 or
            proof_registration.get('allusers') != '1' or
            not proof_registration.get('uninstall_supported') or
            not proof_registration.get('start_menu_managed')):
        raise RuntimeError('Setup MSI compiled registration acceptance missing; rerun Setup')


def _cross(builds: Path, *, setup: bool):
    from host.windows_environment import WindowsCrossEnvironment
    cross = WindowsCrossEnvironment(builds)
    if setup:
        return cross.ensure()
    # Publish must not create/load/download an OCI image or repair dependencies.
    archive_root, identity = WindowsCrossEnvironment.validate_staged(builds)
    cross.image = identity['image']
    cross.provider = 'docker-linux-amd64-wine'
    cross.docker = cross._docker_available()   # may START the existing managed VM
    if not cross._has_image(cross.image):
        raise RuntimeError('Setup Windows cross image is not loaded; run Setup (Publish cannot load it)')
    return cross


def _run(cross, name: str, command: list[str], *, cwd: Path, timeout: int = 600) -> str:
    # Native wixl/msitools capture diagnostics until each command exits.
    # Emit elapsed-time heartbeats meanwhile, without changing their execution.
    with progress_step(name, prefix='[WINDOWS MSI]'):
        result = cross.execute(command, cwd=cwd, timeout=timeout)
        if result.returncode:
            raise RuntimeError(f'Windows MSI {name} failed ({result.returncode}): {(result.stdout or "")[-4000:]}')
    return result.stdout or ''


def _verify_native_msi_tools(cross, *, cwd: Path) -> str:
    """Require the Setup-pinned native Debian wixl, not the Wine PE32 path."""
    result = cross.execute(['dpkg-query', '-W', '-f=${Version}', 'wixl'],
                           cwd=cwd, timeout=60)
    version = (result.stdout or '').strip()
    if result.returncode or version != WIXL_PACKAGE_VERSION:
        raise RuntimeError('Missing/incompatible native wixl package ' + WIXL_PACKAGE_VERSION +
                           ' in Setup WindowsCross image: ' + version)
    wixl = cross.execute(['wixl', '--version'], cwd=cwd, timeout=60)
    if wixl.returncode or not (wixl.stdout or '').strip():
        raise RuntimeError('Windows MSI Setup cannot execute wixl: ' + str(wixl.stdout or '')[-600:])
    print('[WINDOWS MSI SETUP] Native wixl ' + version +
          ' ready; building real x64 MSI with Debian msitools', flush=True)
    return (wixl.stdout or '').strip()


def _wix(tag: str, **attrs):
    return ET.Element('{' + NS + '}' + tag, {k: str(v) for k, v in attrs.items()})


def _child(parent, tag, **attrs):
    node = _wix(tag, **attrs)
    parent.append(node)
    return node


def _msi_version(release: str) -> str:
    # Windows Installer only accepts a major version <=255; source uses YYYY.
    match = re.fullmatch(r'(20\d{2})\.(\d{1,2})\.(\d{1,5})', release)
    if not match:
        raise ValueError('Release cannot be mapped to a three-field MSI product version: ' + release)
    year, minor, build = (int(part) for part in match.groups())
    if not (2020 <= year < 2100 and 1 <= minor <= 12 and 0 <= build <= 65535):
        raise ValueError('Release is outside Windows Installer version bounds: ' + release)
    return f'{year - 2000}.{minor}.{build}'


def _product_code(release: str, *, smoke: bool = False) -> str:
    # Stable when the same release is built again; different releases use new
    # ProductCodes while keeping UPGRADE_CODE stable for major upgrades.
    namespace_key = ('smoke/' if smoke else 'win/') + _msi_version(release)
    return str(uuid.uuid5(GUID_NAMESPACE, namespace_key)).upper()


def _document(source_files: dict[str, Path], *, release: str, smoke: bool = False) -> bytes:
    """Build a minimal wixl-compatible MSI with stable component/upgrade IDs."""
    ET.register_namespace('', NS)
    root = _wix('Wix')
    product = _child(root, 'Product', Id=_product_code(release, smoke=smoke),
                     Name=('CinterForge MSI Preflight' if smoke else 'CinterForge Terminal'),
                     Language='1033', Version=('1.0.0' if smoke else _msi_version(release)),
                     Manufacturer='CinterForge', UpgradeCode=(str(uuid.uuid5(GUID_NAMESPACE, 'smoke'))
                                                       if smoke else UPGRADE_CODE))
    # The tiny README MSI succeeded with this wixl subset on the BUILDER.
    # 64-bit is selected by `wixl --arch x64` and Win64 components below,
    # not the unsupported WiX `Package Platform` attribute.
    _child(product, 'Package', InstallerVersion='500', Compressed='yes',
           InstallScope='perMachine', Description='CinterForge Terminal installer')
    _child(product, 'MajorUpgrade', DowngradeErrorMessage='A newer version is already installed.')
    _child(product, 'MediaTemplate', EmbedCab='yes')
    if not smoke:
        # Let Windows Installer create its own Add/Remove Programs registration.
        # ARPNOREMOVE and ARPSYSTEMCOMPONENT must never be authored.
        _child(product, 'Property', Id='ARPNOMODIFY', Value='1')
        _child(product, 'Property', Id='ARPURLINFOABOUT', Value='https://www.cinterforge.com/')
        # wixl 0.106 supports the underlying MSI Type 51 CustomAction. Its
        # <SetProperty> WiX sugar is not consistently implemented by wixl.
        # Resolve INSTALLDIR only after CostFinalize, never at authoring time.
        _child(product, 'CustomAction', Id='SetARPINSTALLLOCATION',
               Property='ARPINSTALLLOCATION', Value='[INSTALLDIR]')
        sequence = _child(product, 'InstallExecuteSequence')
        _child(sequence, 'Custom', Action='SetARPINSTALLLOCATION',
               After='CostFinalize')
    target = _child(product, 'Directory', Id='TARGETDIR', Name='SourceDir')
    program = _child(target, 'Directory', Id='ProgramFiles64Folder')
    if not smoke:
        # Standard per-machine Start-menu folder; Wixl generates both create
        # and remove actions for the shortcut tied to the EXE component.
        _child(target, 'Directory', Id='ProgramMenuFolder')
    maker = _child(program, 'Directory', Id='CinterForgeDir', Name='CinterForge')
    install = _child(maker, 'Directory', Id='INSTALLDIR', Name=('MSI Preflight' if smoke else 'CinterForge Terminal'))
    feature = _child(product, 'Feature', Id='MainFeature', Title='CinterForge Terminal', Level='1')
    directories = {(): install}
    for rel, source in sorted(source_files.items()):
        parts = Path(rel).parts
        if (not parts or Path(rel).is_absolute() or '..' in parts or '\\' in rel or
                any(part in ('', '.') for part in parts)):
            raise RuntimeError('Invalid MSI installation member: ' + rel)
        for depth in range(1, len(parts)):
            key = parts[:depth]
            if key not in directories:
                ident = 'D' + hashlib.sha1('/'.join(key).lower().encode()).hexdigest()[:28]
                directories[key] = _child(directories[key[:-1]], 'Directory', Id=ident, Name=parts[depth - 1])
        digest = hashlib.sha1(rel.lower().encode()).hexdigest()[:28]
        identifier = 'C' + digest
        component = _child(directories[parts[:-1]], 'Component', Id=identifier,
                           Guid=str(uuid.uuid5(GUID_NAMESPACE, ('smoke/' if smoke else 'win/') + rel.lower())),
                           Win64='yes')
        file_node = _child(component, 'File', Id='F' + digest, Name=parts[-1],
                           Source=str(source), KeyPath='yes')
        if not smoke and rel == 'CinterForge Terminal.exe':
            # The shortcut is owned by this MSI component. RemoveShortcuts is
            # automatically scheduled on MSI uninstall: no free-standing .lnk.
            _child(file_node, 'Shortcut', Id='CinterForgeTerminalStartMenu',
                   Directory='ProgramMenuFolder', Name='CinterForge Terminal',
                   Description='CinterForge Terminal', Advertise='yes',
                   WorkingDirectory='INSTALLDIR')
        _child(feature, 'ComponentRef', Id=identifier)
    return ET.tostring(root, encoding='utf-8', xml_declaration=True)


def _compile(builds: Path, cross, *, work: Path, sources: dict[str, Path],
             release: str, smoke: bool) -> Path:
    work.mkdir(parents=True, exist_ok=True)
    # wixl is a native Linux executable: SourceFile attributes use guest POSIX
    # paths. Wine Z: paths and 32-bit Windows utilities are never involved.
    guest_sources = {name: Path(cross.path(source)) for name, source in sources.items()}
    xml = _document(guest_sources, release=release, smoke=smoke)
    wxs = work / 'Installer.wxs'
    output = work / ('InstallerSmoke.msi' if smoke else 'CinterForgeTerminal-win.msi')
    wxs.write_bytes(xml)
    output.unlink(missing_ok=True)
    _run(cross, 'wixl native x64 MSI compiler',
         ['wixl', '--arch', 'x64', '--output', cross.path(output), cross.path(wxs)],
         cwd=work, timeout=1200)
    if not output.is_file() or output.is_symlink() or output.stat().st_size < 1024:
        raise RuntimeError('wixl completed without writing a nonempty MSI: ' + str(output))
    return output


def _msi_table(cross, msi: Path, *, cwd: Path, name: str) -> list[dict[str, str]]:
    """Read the compiled MSI, not the WiX source. msiinfo exports IDT rows."""
    raw = _run(cross, 'read ' + name, ['msiinfo', 'export', cross.path(msi), name], cwd=cwd)
    lines = [line.rstrip('\r') for line in raw.splitlines() if line.strip()]
    if len(lines) < 3:
        raise RuntimeError('MSI ' + name + ' table has no IDT header')
    fields = lines[0].split('\t')
    if not fields or len(set(fields)) != len(fields):
        raise RuntimeError('MSI ' + name + ' has invalid column names')
    rows = []
    for line in lines[3:]:
        parts = line.split('\t')
        if len(parts) != len(fields):
            raise RuntimeError('MSI ' + name + ' has malformed IDT row: ' + line[:160])
        rows.append(dict(zip(fields, parts)))
    return rows


def _verify_registration(cross, msi: Path, *, cwd: Path, smoke: bool) -> dict:
    """Fail before distribution if the MSI cannot register or uninstall."""
    property_rows = _msi_table(cross, msi, cwd=cwd, name='Property')
    props = {row['Property']: row['Value'] for row in property_rows}
    for key in ('ProductName', 'UpgradeCode', 'ProductCode', 'ProductVersion'):
        if not props.get(key):
            raise RuntimeError('MSI missing product identity: ' + key)
    try:
        uuid.UUID(props['ProductCode'].strip('{}'))
    except (ValueError, AttributeError) as exc:
        raise RuntimeError('MSI ProductCode is not a GUID') from exc
    if props.get('ALLUSERS') != '1':
        raise RuntimeError('MSI does not install per machine (ALLUSERS=1)')
    for forbidden in ('ARPSYSTEMCOMPONENT', 'ARPNOREMOVE', 'MSIINSTALLPERUSER'):
        if props.get(forbidden) not in (None, '', '0'):
            raise RuntimeError('MSI suppresses registration or uninstall: ' + forbidden)
    directories = {row['Directory']: row['Directory_Parent']
                   for row in _msi_table(cross, msi, cwd=cwd, name='Directory')}
    if (directories.get('ProgramFiles64Folder') != 'TARGETDIR' or
            directories.get('CinterForgeDir') != 'ProgramFiles64Folder' or
            directories.get('INSTALLDIR') != 'CinterForgeDir'):
        raise RuntimeError('MSI INSTALLDIR does not default beneath ProgramFiles64Folder')
    sequence_rows = _msi_table(cross, msi, cwd=cwd, name='InstallExecuteSequence')
    actions = {row['Action'] for row in sequence_rows}
    mandatory = {'InstallFiles', 'RemoveFiles', 'RegisterUser', 'RegisterProduct',
                 'PublishFeatures', 'PublishProduct', 'ProcessComponents',
                 'InstallFinalize', 'FindRelatedProducts', 'RemoveExistingProducts'}
    missing = mandatory - actions
    if missing:
        raise RuntimeError('MSI cannot register/uninstall: missing actions ' + str(sorted(missing)))
    arp_type = None
    arp_value = None
    if not smoke:
        if props.get('Manufacturer') != 'CinterForge' or props.get('ProductName') != 'CinterForge Terminal':
            raise RuntimeError('MSI missing Add/Remove Programs publisher or display name')
        if props.get('ARPNOMODIFY') != '1' or not props.get('ARPURLINFOABOUT'):
            raise RuntimeError('MSI missing explicit Add/Remove Programs metadata')
        custom_actions = _msi_table(cross, msi, cwd=cwd, name='CustomAction')
        location_actions = [row for row in custom_actions
                            if row.get('Action') == 'SetARPINSTALLLOCATION']
        if len(location_actions) != 1:
            raise RuntimeError('MSI missing ARPINSTALLLOCATION assignment')
        action = location_actions[0]
        try:
            arp_type = int(action.get('Type', '-1'))
        except ValueError as exc:
            raise RuntimeError('MSI ARPINSTALLLOCATION action has invalid type') from exc
        arp_value = action.get('Target')
        if (arp_type != 51 or action.get('Source') != 'ARPINSTALLLOCATION' or
                arp_value != '[INSTALLDIR]'):
            raise RuntimeError('MSI ARPINSTALLLOCATION must be Type 51 from [INSTALLDIR]')
        custom_sequence = [row for row in sequence_rows
                           if row.get('Action') == 'SetARPINSTALLLOCATION']
        cost_sequence = [row for row in sequence_rows if row.get('Action') == 'CostFinalize']
        if len(custom_sequence) != 1 or len(cost_sequence) != 1:
            raise RuntimeError('MSI ARPINSTALLLOCATION action not scheduled after CostFinalize')
        try:
            ordered = (int(cost_sequence[0]['Sequence']), int(custom_sequence[0]['Sequence']))
        except (KeyError, ValueError) as exc:
            raise RuntimeError('MSI ARPINSTALLLOCATION action sequence is invalid') from exc
        if not (0 < ordered[0] < ordered[1] < 6000):
            raise RuntimeError('MSI ARPINSTALLLOCATION action must follow CostFinalize before product registration')
        shortcuts = _msi_table(cross, msi, cwd=cwd, name='Shortcut')
        if (len(shortcuts) != 1 or
                shortcuts[0].get('Directory_') != 'ProgramMenuFolder'):
            raise RuntimeError('MSI Start-menu shortcut is not MSI-owned')
        if {'CreateShortcuts', 'RemoveShortcuts'} - actions:
            raise RuntimeError('MSI has no Start-menu shortcut removal action')
    return {'product_code': props['ProductCode'], 'allusers': props['ALLUSERS'],
            'default_directory': 'ProgramFiles64Folder/CinterForge/CinterForge Terminal',
            'registration_actions': sorted(mandatory),
            'uninstall_supported': True,
            'start_menu_managed': not smoke,
            'arp_install_location': arp_value,
            'arp_action_type': arp_type,
            'native_windows_install_test': 'required-on-Windows'}


def _verify_msi(cross, msi: Path, *, cwd: Path, smoke_file: str | None = None,
                smoke: bool = False) -> dict:
    # Real MSI table + cabinet inspection; not an in-memory authoring assertion.
    # This does NOT certify native Windows Add/Remove Programs registration.
    tables = _run(cross, 'inspect tables', ['msiinfo', 'tables', cross.path(msi)], cwd=cwd)
    required = {'Property', 'File', 'Component', 'Feature', 'Upgrade', 'Media', 'Directory',
                'InstallExecuteSequence'}
    if not smoke:
        required.update({'Shortcut', 'CustomAction'})
    missing = required - set(tables.split())
    if missing:
        raise RuntimeError('MSI missing Windows Installer tables: ' + str(sorted(missing)))
    registration = _verify_registration(cross, msi, cwd=cwd, smoke=smoke)
    if smoke_file:
        extracted = cwd / 'Extracted'
        if extracted.exists():
            shutil.rmtree(extracted)
        extracted.mkdir()
        _run(cross, 'extract preflight MSI', ['msiextract', '-C', cross.path(extracted), cross.path(msi)],
             cwd=cwd, timeout=180)
        actual = list(extracted.rglob(smoke_file))
        if len(actual) != 1 or 'CinterForge MSI preflight' not in actual[0].read_text('utf-8'):
            raise RuntimeError('MSI preflight cabinet cannot extract its README payload')
    return {'bytes': msi.stat().st_size, 'sha256': _sha(msi),
            'table_count': len(tables.split()), 'upgrade_code_present': True,
            'registration': registration}


def setup(builds: Path) -> dict:
    """Setup-only native compiler + real MSI table/cabinet acceptance."""
    builds = _builds(builds)
    stage = _stage(builds)
    stage.mkdir(parents=True, exist_ok=True)
    log = builds / 'Logs/CinterForgeTerminal/windows-msi-preflight.json'
    log.parent.mkdir(parents=True, exist_ok=True)
    receipt = stage / 'msi-preflight.json'
    report = {'schema': SCHEMA, 'status': 'failed',
              'compiler': 'debian-wixl', 'wixl_package_version': WIXL_PACKAGE_VERSION}
    try:
        cross = _cross(builds, setup=True)
        work = builds / 'Intermediates/CinterForgeTerminal/WindowsMSI/Smoke'
        work.mkdir(parents=True, exist_ok=True)
        report['image'] = str(getattr(cross, 'image', ''))
        report['wixl_version'] = _verify_native_msi_tools(cross, cwd=work)
        readme = work / 'README.txt'
        readme.write_text('CinterForge MSI preflight: actual native wixl MSI cabinet verification.\n',
                          encoding='utf-8')
        output = _compile(builds, cross, work=work,
                          sources={'README.txt': readme}, release='2026.11.0', smoke=True)
        checks = _verify_msi(cross, output, cwd=work, smoke_file='README.txt', smoke=True)
        # Exercise the exact same minimal MSI schema with actual launcher filenames
        # during Setup so Publish cannot fail on an untested template variant.
        template_work = builds / 'Intermediates/CinterForgeTerminal/WindowsMSI/TemplateSmoke'
        template_work.mkdir(parents=True, exist_ok=True)
        sample_cmd = template_work / 'Terminal.cmd'
        sample_exe = template_work / 'CinterForge Terminal.exe'
        sample_cmd.write_text('@echo off\r\necho CinterForge MSI template preflight\r\n',
                              encoding='utf-8')
        sample_exe.write_bytes(b'CinterForge MSI template fixture (not executable)')
        template = _compile(builds, cross, work=template_work,
                            sources={'Terminal.cmd': sample_cmd,
                                     'CinterForge Terminal.exe': sample_exe},
                            release='2026.11.0', smoke=False)
        template_checks = _verify_msi(cross, template, cwd=template_work)
        # Native Windows ARP visibility/uninstall cannot be inferred from Wine.
        # The compiled MSI's property, action, sequence and shortcut tables are
        # the Setup gate. Test the final package on Windows before release.
        report.update(status='passed', smoke_sha256=checks['sha256'],
                      smoke_file=str(output), checks=checks,
                      template_sha256=template_checks['sha256'],
                      template_file=str(template), template_checks=template_checks,
                      execution='native-linux-wixl-x64')
        print('[WINDOWS MSI SETUP] PASS: native wixl x64 MSI, ARPINSTALLLOCATION, '
              'per-machine registration/uninstall action tables and managed shortcut verified; '
              'native Windows install/uninstall remains external acceptance', flush=True)
    except Exception as error:
        report['error'] = repr(error)
        raise
    finally:
        content = json.dumps(report, indent=2, sort_keys=True) + '\n'
        log.write_text(content, encoding='utf-8')
        receipt.write_text(content, encoding='utf-8')
    return report


def publish(builds: Path, *, release: str, entries: dict[str, dict]) -> dict:
    """Compile MSI in the existing provisioned AMD64 guest, owned by Publish."""
    from host.amd64_backend import managed_amd64_backend
    with managed_amd64_backend(builds, setup=False):
        return _publish_with_backend(builds, release=release, entries=entries)


def _publish_with_backend(builds: Path, *, release: str,
                          entries: dict[str, dict]) -> dict:
    """Publish-only: produce MSI from immutable Windows Build inputs."""
    builds = _builds(builds)
    _check_tools(builds)  # validate Setup receipts, do not provision
    from host.unpacked_release import _source
    from host.universal_package import APP_ROOTS
    prefix = APP_ROOTS['win'] + '/'
    sources = {}
    for name, detail in sorted(entries.items()):
        if not name.startswith(prefix):
            raise RuntimeError('Unexpected Windows Build member: ' + name)
        source = _source(builds, name, detail, 'win', verify=False)
        sources[name[len(prefix):]] = source
    required = ('Terminal.cmd', 'CinterForge Terminal.exe')
    if any(item not in sources for item in required):
        raise RuntimeError('Windows Build receipt lacks launchers required for MSI')
    # No optional DevTools content belongs in the lightweight Windows installer.
    if any(name.startswith('DevTools/') for name in sources):
        raise RuntimeError('Windows MSI accidentally includes the optional DevTools bundle')
    cross = _cross(builds, setup=False)
    work = builds / 'Intermediates/CinterForgeTerminal/WindowsMSI/Publish'
    built = _compile(builds, cross, work=work, sources=sources,
                     release=release, smoke=False)
    evidence = _verify_msi(cross, built, cwd=work)
    destination = builds / 'CinterForgeTerminal/Distribution/CinterForgeTerminal-win.msi'
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.with_suffix('.msi.partial')
    try:
        shutil.copyfile(built, temp)
        temp.replace(destination)
    finally:
        temp.unlink(missing_ok=True)
    receipt = {'schema': 'cinterforge.terminal.windows-msi.v1', 'release': release,
               'filename': destination.name, 'sha256': evidence['sha256'],
               'bytes': evidence['bytes'], 'format': 'msi',
               'msi_version': _msi_version(release), 'upgrade_code': UPGRADE_CODE,
               'file_count': len(sources), 'execution': 'native-linux-wixl-x64',
               'registration': evidence['registration']}
    (destination.parent / 'windows-msi.json').write_text(json.dumps(receipt, indent=2, sort_keys=True) + '\n',
                                                        encoding='utf-8')
    print('[TERMINAL PUBLISH] Windows MSI package verified: ' + str(destination), flush=True)
    return receipt


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--builds', type=Path, required=True)
    args = parser.parse_args()
    setup(args.builds)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

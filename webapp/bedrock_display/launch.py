"""Launch the already-installed official BDS with its pinned Endstone runtime.
Never call the upstream downloader from an installed app or acceptance test.
"""
import os
import sys
import sysconfig
from pathlib import Path

root = Path.cwd()
site = Path(__file__).resolve().parent / 'site'
sys.path.insert(0, str(site))
os.environ['PYTHONPATH'] = os.pathsep.join([str(site), *[p for p in sys.path if p]])
from terminal_mount import seed_slots
seed_slots(root)
os.environ['PYTHONNOUSERSITE'] = '1'
os.environ['PYTHONHOME'] = str(Path(__file__).resolve().parent/'python')
if sys.platform == 'win32':
    from endstone.cli.windows import WindowsBootstrap
    bootstrap = WindowsBootstrap(str(root), False, '', False)
    # Only the injection/launch step of the pinned upstream bootstrap. The
    # CinterForge manager already verifies and installs the BDS package.
    sys.exit(bootstrap._run())
else:
    from endstone.cli.linux import LinuxBootstrap
    bootstrap = LinuxBootstrap(str(root), False, '', False)
    env = bootstrap._endstone_runtime_env
    # Preserve the existing LinuxKit x86-64 glibc closure and BDS library path.
    env['LD_LIBRARY_PATH'] = ':'.join(filter(None, [os.environ.get('LD_LIBRARY_PATH'), str(Path(sys.executable).resolve().parent.parent/'lib'), str(root)]))
    executable = str(root/'bedrock_server')
    print('CINTERFORGE_DISPLAY_ENDSTONE_EXEC', flush=True)
    os.execve(executable, [executable], env)

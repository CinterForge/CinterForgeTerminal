"""Application-neutral JSON-lines RPC worker.

An application supplies ``handle(method, params)`` or
``handle(method, params, emit)``. In the latter form, ``emit({...})`` sends
structured notifications (progress, logs, status) before the final response.
Only RPC records go to stdout; diagnostics must go to stderr.
"""
# CINTERFORGE SOURCE HEADER — host/worker.py
# PURPOSE: Implements the generic JSON-lines RPC child process used to import and call the Python service.
# OWNERSHIP: This authored source is part of one canonical Terminal runtime.
# STARTUP: Called or loaded by the existing lifecycle/entry point; no
#          hidden dependency provisioning or alternate VM is allowed.
# SHUTDOWN: Release only resources explicitly owned by this component.
# CALLERS: Preserve existing APIs, filenames, CLI flags and data formats.
# KEY CLASSES / METHODS:
#   send: Send send for this module.
#   main: Start the current executable and return its status.
# MAINTENANCE: Keep generated outputs in sibling Builds or the actual
# Steam library; do not change Lagomorph timings, user assets or guest
# readiness without tracing their real callers and failure boundaries.
from __future__ import annotations
import importlib.util
import inspect
import json
from pathlib import Path
import sys


def send(message: dict) -> None:
    print(json.dumps(message, sort_keys=True), flush=True)


def main(argv=None) -> int:
    args = sys.argv[1:] if argv is None else argv
    handler = None
    accepts_events = False
    if args:
        location = args[0]
        spec = importlib.util.spec_from_file_location('cinterforge_host_service', location)
        if spec is None or spec.loader is None:
            raise ValueError(f'Invalid service module: {location}')
        # A service loaded by absolute filename is not a Python script launch:
        # its directory is NOT automatically placed on sys.path. Keep the
        # service's own package tree importable for the lifetime of this worker
        # (e.g. staged webapp/runtime/CinterForge*.py beside the entry file).
        # This stays within the staged service closure; no repository lookup.
        service_dir = str(Path(location).resolve().parent)
        if service_dir not in sys.path:
            sys.path.insert(0, service_dir)
        send({'event': 'startup', 'phase': 'import-service'})
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        send({'event': 'startup', 'phase': 'service-imported'})
        handler = getattr(module, 'handle', None)
        if not callable(handler):
            raise ValueError(f'Service {location} must implement handle(method, params)')
        signature = inspect.signature(handler)
        accepts_events = len(signature.parameters) >= 3
    send({'event': 'ready', 'ready': True, 'protocol': 'cinterforge.python-host.jsonl.v1'})
    for line in sys.stdin:
        request = None
        try:
            request = json.loads(line)
            if not isinstance(request, dict) or not isinstance(request.get('method'), str):
                raise ValueError('RPC request must be an object with method')
            identifier, method = request['id'], request['method']
            if method == 'ping':
                result = {'pong': True}
            elif method == 'shutdown':
                send({'id': identifier, 'result': {'stopped': True}})
                return 0
            elif handler:
                params = request.get('params', {})
                result = handler(method, params, send) if accepts_events else handler(method, params)
            else:
                raise ValueError(f'Unknown method: {method}')
            send({'id': identifier, 'result': result})
        except Exception as error:
            send({'id': request.get('id') if isinstance(request, dict) else None,
                  'error': {'message': str(error), 'type': type(error).__name__}})
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

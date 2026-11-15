"""CinterForge Code-OSS producer ownership.

PURPOSE
    Setup-only pinned Code-OSS acquisition, compilation, staging and authored
    CinterForge extension bundling. These modules are Host lifecycle code,
    never imported by the installed app runtime or its HTTP service.

ENTRY POINTS
    setup.py: setup() / main()  - Setup CLI stage and reuse validation.
    producer.py: ensure_codeoss() / validate_codeoss_web() / _stage_cinterforge_extension().
    adapters.py: codeoss_root() / run_process() - paths and real-time process output.
    extension_bundle.py: render_extension_bundle() - deterministic browser worker source.

ASSETS
    The authored extension remains in webapp/codeoss/extension, unchanged.
"""

#!/usr/bin/env python3
"""Generate the shared Builds/Assets/AssetGallery.html (Terminal-owned).

The IF migration compatibility command delegates to this same source. Read
canonical source manifests only; never copy or repackage authored assets.
The output lives beside the manifests so Babylon preview URLs remain relative.
"""
from __future__ import annotations

import argparse
from html import escape
import json
import os
import re
from pathlib import Path, PurePosixPath
import sys
from urllib.parse import quote

REPO = Path(__file__).resolve().parents[1]
def _asset_url(asset_dir: Path, asset_root: Path, filename: object) -> str:
    """Translate a manifest-relative resource path to a safe relative URL."""
    if not isinstance(filename, str) or not filename.strip():
        return ""
    posix = PurePosixPath(filename)
    if posix.is_absolute() or any(part in ("..", ".") for part in posix.parts) or "\\" in filename:
        raise ValueError(f"Unsafe relative gallery resource in {asset_dir / 'manifest.json'}: {filename!r}")
    source = (asset_dir / filename).resolve()
    if not source.is_relative_to(asset_root) or not source.is_relative_to(asset_dir.resolve()):
        raise ValueError(f"Gallery resource escapes asset folder: {filename!r}")
    return quote(source.relative_to(asset_root).as_posix(), safe="/")


def _card(manifest: dict, folder: Path, library: Path) -> str:
    reps = manifest.get("representations") or {}
    if not isinstance(reps, dict):
        raise ValueError(f"Invalid representations object: {folder / 'manifest.json'}")
    solid = reps.get("solid") or {}
    blocks = [(key, value) for key, value in reps.items() if str(key).startswith("block_") and isinstance(value, dict)]
    block = blocks[0][1] if blocks else {}
    if not isinstance(solid, dict):
        raise ValueError(f"Invalid solid representation: {folder / 'manifest.json'}")
    # The established IFEditor library uses manifest["versions"] rather than
    # newer Blockify representations. List and preview BOTH formats without
    # rewriting the user's existing UCI manifests or source files.
    if not reps and isinstance(manifest.get("versions"), list):
        versions = [v for v in manifest["versions"] if isinstance(v, dict)]
        preferred = str(manifest.get("default_version") or manifest.get("last_viewed") or "")
        versions.sort(key=lambda v: (v.get("file") != preferred))
        for version in versions:
            source_file = version.get("file")
            if isinstance(source_file, str) and Path(source_file).suffix.casefold() in (".glb", ".gltf"):
                if (folder / source_file).is_file():
                    solid = {"model": source_file, "thumbnail": version.get("thumbnail_file")}
                    break
    solid_thumb = _asset_url(folder, library, solid.get("thumbnail"))
    block_thumb = _asset_url(folder, library, block.get("thumbnail"))
    thumbnail = block_thumb or solid_thumb
    name = str(manifest.get("name") or folder.name)
    uci = str(manifest.get("uci") or folder.name)
    shown = escape(name)
    attr = lambda s: escape(s, quote=True)
    image_html = (f'<img loading="lazy" src="{attr(thumbnail)}" alt="{attr(name)}">'
                  if thumbnail else "No preview")
    return (
        f'<article class="asset" data-name="{attr(name)}">'
        f'<button type="button" class="thumb" data-uci="{attr(uci)}" '
        f'aria-label="Open Asset View for {attr(name)}">'
        f'{image_html}'
        '</button><div class="meta">'
        f'<strong>{shown}</strong><small>{escape(uci)}</small>'
        '</div></article>'
    )


HTML = '''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>CinterForge Asset Gallery</title>
<style>
body{margin:0;background:#101113;color:#eee;font:14px system-ui,-apple-system,sans-serif}
header{position:sticky;top:0;z-index:2;background:#191b1e;padding:12px 16px;border-bottom:1px solid #333;display:flex;gap:16px;align-items:center;flex-wrap:wrap}
h1{font-size:18px;margin:0}#filter{flex:1;min-width:160px;max-width:320px;padding:9px;background:#242629;color:#fff;border:1px solid #444;border-radius:6px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:12px;padding:12px}
.asset{background:#1b1d20;border:1px solid #353535;border-radius:8px;overflow:hidden}
.thumb{display:block;width:100%;height:180px;padding:0;border:0;background:#262626;cursor:pointer;color:#aaa}
.thumb:hover,.thumb:focus-visible{outline:2px solid #8ccaff;outline-offset:-2px}
.thumb img{width:100%;height:100%;object-fit:contain}.meta{padding:9px}
.meta strong,.meta small{display:block;overflow:hidden;text-overflow:ellipsis}.meta small{color:#999;font-size:10px;margin-top:3px}
#gallery-status{padding:8px 16px;color:#f4bd77}#gallery-status:empty{display:none}
</style></head>
<body><header><h1>CinterForge Asset Gallery · __COUNT__ assets</h1><input id="filter" type="search" placeholder="Find assets" aria-label="Find assets"></header>
<div id="gallery-status" role="status"></div><main class="grid">__CARDS__</main>
<script>
const status=document.getElementById('gallery-status');
document.addEventListener('click',async event=>{
  const button=event.target.closest('button[data-uci]');
  if(!button||button.disabled)return;
  button.disabled=true;
  status.textContent='';
  try{
    // Both gallery clicks and `assets view <uci>` use the single Terminal
    // Asset View tab, with the original editor/Blockify/Export/Remove buttons.
    const response=await fetch('/api/assets/view',{
      method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({uci:button.dataset.uci}),credentials:'same-origin'
    });
    const reply=await response.json();
    if(!response.ok)throw new Error(reply.error||'Unable to open Asset View');
  }catch(error){status.textContent='Asset View failed: '+String(error.message||error);}
  finally{button.disabled=false;}
});
document.getElementById('filter').addEventListener('input',event=>{
  const search=event.target.value.toLocaleLowerCase();
  for(const card of document.querySelectorAll('.asset'))
    card.hidden=!card.dataset.name.toLocaleLowerCase().includes(search);
});
</script></body></html>
'''


def generate(builds: Path) -> dict:
    builds = Path(builds).expanduser().resolve()
    library = builds / "Assets"
    if not library.is_dir():
        raise FileNotFoundError(f"Canonical shared assets library missing: {library}")
    cards = []
    # The historical Assets library also has nested categories such as
    # Animated/Mixamo/<UCI> and Animated/Fixed/<UCI>.
    # Match the original Server's _uci_asset_folders contract: a saved asset
    # exists only when its manifest explicitly owns the matching UCI folder.
    # Raw provider downloads and pre-UCI manifests are not report entries.
    manifests = sorted(library.glob("**/CINTERASST*/manifest.json"),
                       key=lambda path: path.as_posix().casefold())
    for source in manifests:
        if source.is_symlink():
            raise ValueError(f"Symlinked asset manifest forbidden: {source}")
        try:
            manifest = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if (not isinstance(manifest, dict) or
                str(manifest.get("uci") or "").strip() != source.parent.name):
            continue
        cards.append(_card(manifest, source.parent, library))
    page = (HTML.replace("__COUNT__", str(len(cards)))
                .replace("__CARDS__", "\n".join(cards)))
    output = library / "AssetGallery.html"
    temp = library / "AssetGallery.html.partial"
    try:
        temp.write_text(page, encoding="utf-8")
        os.replace(temp, output)
    finally:
        temp.unlink(missing_ok=True)
    return {"path": str(output), "assets": len(cards), "viewer": "terminal-asset-view"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Rebuild the canonical IF/Converter AssetGallery.html")
    parser.add_argument("--builds", type=Path, default=REPO.parent / "Builds")
    args = parser.parse_args(argv)
    print(json.dumps(generate(args.builds), sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"[Terminal Asset Gallery] {exc}", file=sys.stderr)
        raise SystemExit(1)

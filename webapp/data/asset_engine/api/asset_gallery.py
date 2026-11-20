#!/usr/bin/env python3
"""Generate the shared Builds/Assets/AssetGallery.html (Terminal-owned).

The IF migration compatibility command delegates to this same source. Read
canonical source manifests only; never copy or repackage authored assets.
The output lives beside the manifests so Babylon preview URLs remain relative.
"""
from __future__ import annotations

import argparse
import hashlib
from html import escape
import json
import os
import re
from pathlib import Path, PurePosixPath
import shutil
import sys
from urllib.parse import quote

REPO = Path(__file__).resolve().parents[1]
VIEWER_FILES = ("babylon.js", "babylonjs.loaders.min.js")


def _viewer_scripts(builds: Path, library: Path) -> tuple[str, str]:
    """Use verified pinned browser dependencies, without fetching at gallery time."""
    pins = json.loads((REPO / "build" / "browser-dependencies.json").read_text(encoding="utf-8"))
    candidates = (
        builds / "Dependencies" / "Babylon" / pins["version"],
        REPO / "web" / "vendor",            # native .app resource bundle
        REPO / "frontend" / "vendor",       # staged web package
        REPO / "vendor",                    # explicitly staged frontend
    )
    staged = next((folder for folder in candidates if all(
        (folder / name).is_file() for name in VIEWER_FILES)), None)
    if staged is not None:
        for name in VIEWER_FILES:
            src = staged / name
            actual = hashlib.sha256(src.read_bytes()).hexdigest()
            expected = pins["assets"][name]["sha256"]
            if actual != expected:
                raise RuntimeError(f"Pinned Babylon runtime digest mismatch: {src}")
        viewer = library / "_viewer"
        viewer.mkdir(parents=True, exist_ok=True)
        for name in VIEWER_FILES:
            source, dest = staged / name, viewer / name
            expected = pins["assets"][name]["sha256"]
            if not dest.is_file() or hashlib.sha256(dest.read_bytes()).hexdigest() != expected:
                tmp = viewer / (name + ".partial")
                try:
                    shutil.copyfile(source, tmp)
                    os.replace(tmp, dest)
                finally:
                    tmp.unlink(missing_ok=True)
        return "_viewer/babylon.js", "_viewer/babylonjs.loaders.min.js"
    raise RuntimeError("Pinned Terminal Babylon engine unavailable; run ./Cinter setup, build, stage")


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
    solid_model = _asset_url(folder, library, solid.get("model"))
    block_model = _asset_url(folder, library, block.get("model"))
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
        f'<button class="thumb" data-model="{attr(block_model or solid_model)}" data-title="{attr(name)}" '
        f'{"disabled" if not (block_model or solid_model) else ""}>'
        f'{image_html}'
        '</button><div class="meta">'
        f'<strong>{shown}</strong><small>{escape(uci)}</small>'
        '<div class="actions">'
        f'<button data-model="{attr(solid_model)}" data-title="{attr(name + " · Solid")}" {"" if solid_model else "disabled"}>Solid</button>'
        f'<button data-model="{attr(block_model)}" data-title="{attr(name + " · Block")}" {"" if block_model else "disabled"}>Block</button>'
        '</div></div></article>'
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
.thumb{display:block;width:100%;height:180px;padding:0;border:0;background:#262626;cursor:pointer;color:#aaa}.thumb:disabled{cursor:default}
.thumb img{width:100%;height:100%;object-fit:contain}.meta{padding:9px}
.meta strong,.meta small{display:block;overflow:hidden;text-overflow:ellipsis}.meta small{color:#999;font-size:10px;margin-top:3px}
.actions{display:flex;gap:6px;margin-top:8px}.actions button{flex:1;padding:6px;cursor:pointer}
.viewer{position:fixed;inset:0;z-index:10;background:#000e;display:none}.viewer.open{display:block}
.viewer canvas{width:100%;height:100%;touch-action:none}.viewer .bar{position:absolute;z-index:11;top:10px;left:10px;right:10px;display:flex;justify-content:space-between;align-items:center}.viewer .bar button{font-size:24px}
</style>
<script src="__BABYLON__"></script><script src="__LOADERS__"></script></head>
<body><header><h1>CinterForge Asset Gallery · __COUNT__ assets</h1><input id="filter" type="search" placeholder="Find assets" aria-label="Find assets"></header>
<main class="grid">__CARDS__</main>
<div id="viewer" class="viewer"><div class="bar"><strong id="viewerTitle"></strong><button id="close" aria-label="Close viewer">×</button></div><canvas id="canvas"></canvas></div>
<script>
const viewer=document.getElementById('viewer'),canvas=document.getElementById('canvas'),title=document.getElementById('viewerTitle');
let engine=null,scene=null;
async function show(model,label){
  if(!model)return;
  viewer.classList.add('open');title.textContent=label;
  if(!window.BABYLON){title.textContent='Babylon viewer unavailable';return;}
  if(!engine){engine=new BABYLON.Engine(canvas,true);engine.runRenderLoop(()=>{if(scene)scene.render()});}
  if(scene)scene.dispose();scene=new BABYLON.Scene(engine);scene.clearColor=new BABYLON.Color4(.08,.08,.08,1);
  const camera=new BABYLON.ArcRotateCamera('camera',Math.PI/2,Math.PI/2.4,4,BABYLON.Vector3.Zero(),scene);
  camera.attachControl(canvas,true);new BABYLON.HemisphericLight('fill',new BABYLON.Vector3(0,1,0),scene).intensity=1.2;
  const slash=model.lastIndexOf('/');const result=await BABYLON.SceneLoader.ImportMeshAsync('',model.substring(0,slash+1),model.substring(slash+1),scene);
  const meshes=result.meshes.filter(m=>m.getBoundingInfo&&m.getTotalVertices&&m.getTotalVertices()>0);
  if(meshes.length){let min=meshes[0].getBoundingInfo().boundingBox.minimumWorld.clone(),max=meshes[0].getBoundingInfo().boundingBox.maximumWorld.clone();
    for(const m of meshes.slice(1)){m.computeWorldMatrix(true);const b=m.getBoundingInfo().boundingBox;min=BABYLON.Vector3.Minimize(min,b.minimumWorld);max=BABYLON.Vector3.Maximize(max,b.maximumWorld)}
    const center=min.add(max).scale(.5),size=Math.max(...max.subtract(min).asArray(),.01);camera.target=center;camera.radius=size*1.8}
  if(scene.animationGroups.length)scene.animationGroups[0].start(true);engine.resize();
}
document.addEventListener('click',e=>{const b=e.target.closest('[data-model]');if(b&&!b.disabled)show(b.dataset.model,b.dataset.title).catch(err=>{title.textContent='FAILED: '+err.message;console.error(err)})});
document.getElementById('close').onclick=()=>{viewer.classList.remove('open');if(scene){scene.dispose();scene=null}};
window.addEventListener('resize',()=>{if(engine)engine.resize()});
document.getElementById('filter').addEventListener('input',e=>{const s=e.target.value.toLocaleLowerCase();for(const card of document.querySelectorAll('.asset'))card.hidden=!card.dataset.name.toLocaleLowerCase().includes(s)});
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
    manifests = sorted((path for path in library.rglob("manifest.json")
                        if path.parent.name.startswith("CINTERASST")),
                       key=lambda path: path.as_posix().casefold())
    for source in manifests:
        if source.is_symlink():
            raise ValueError(f"Symlinked asset manifest forbidden: {source}")
        manifest = json.loads(source.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict):
            raise ValueError(f"Invalid gallery asset manifest: {source}")
        cards.append(_card(manifest, source.parent, library))
    babylon, loaders = _viewer_scripts(builds, library)
    page = (HTML.replace("__COUNT__", str(len(cards)))
                .replace("__CARDS__", "\n".join(cards))
                .replace("__BABYLON__", escape(babylon, quote=True))
                .replace("__LOADERS__", escape(loaders, quote=True)))
    output = library / "AssetGallery.html"
    temp = library / "AssetGallery.html.partial"
    try:
        temp.write_text(page, encoding="utf-8")
        os.replace(temp, output)
    finally:
        temp.unlink(missing_ok=True)
    return {"path": str(output), "assets": len(cards), "viewer": "pinned-local" if babylon.startswith("_viewer/") else "online-cdn"}


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

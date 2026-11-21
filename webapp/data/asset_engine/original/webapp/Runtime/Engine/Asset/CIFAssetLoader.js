/*
 * CINTERFORGE MODULE PURPOSE
 * CIF Asset subsystem: model loader
 *
 * Purpose: Centralizes asset URL, model-loading, or sound behavior used by the retained OWB runtime.
 * Ownership boundary: Asset resolution must remain compatible with the CinterForge /Assets runtime path and filesystem-authoritative build pipeline.
 *
 * Maintenance rule: this is production CinterForge/IFPlayer code. Preserve existing
 * runtime behavior while refactoring; prefer explicit, descriptive classes/functions
 * and keep compatibility exports when older OWB callers depend on them.
 */

import { assetUrl } from "./CIFAssetRuntime.js";
export async function loadModels(scene, urls) {
  const loadModelPromises = urls.map((url) => loadModel(scene, url)); // Create an array of promises
  const modelsArray = await Promise.all(loadModelPromises); // Await all promises in parallel
  const modelsDict = modelsArray.reduce((acc, result, index) => {
    let selector = urls[index]
      .split("/")
      .pop()
      .replace(/\.(glb|gltf)$/, "");
    acc[selector] = result.meshes[0]; // Store the first mesh in the dictionary with the URL as key
    return acc;
  }, {});
  return modelsDict; // Return the dictionary containing all the models
}

async function loadModel(scene, url) {
  const resolved = assetUrl(url);
  const extension = /\.gltf(?:$|[?#])/i.test(String(url)) ? ".gltf" : ".glb";
  const missingSet = new Set(Array.isArray(globalThis.CINTERFORGE_IF_KNOWN_MISSING_ASSETS) ? globalThis.CINTERFORGE_IF_KNOWN_MISSING_ASSETS.map(String) : []);
  if (missingSet.has(String(resolved))) {
    console.warn('[CinterForge Assets] canonical asset is known unavailable; using Missing Asset placeholder without HTTP request', {url, resolved});
    return missingAssetResult(scene, url, resolved);
  }

  // Canonical Cloudflare is the only persistent model authority. Browser
  // loads never perform local recovery, reacquisition, or republishing.
  const loadUrl = resolved;

  try {
    const result = await BABYLON.SceneLoader.ImportMeshAsync('', '', loadUrl, scene, undefined, extension);
    console.log(`Loaded ${url}`);
    return result;
  } catch (error) {
    // Standalone published games cannot repair Cloudflare. A load failure is a
    // gameplay-visible Missing Asset, never a fatal game error. Project-open
    // finalization and build-time known-missing metadata prevent expected 404s.
    console.error('[CinterForge Assets] model load failed; using Missing Asset placeholder', {url, loadUrl, error});
    try { globalThis.CINTERFORGE_DIAGNOSE_ASSET_FAILURE?.(loadUrl, error); } catch (_) {}
    return missingAssetResult(scene, url, loadUrl);
  }
}

function missingAssetResult(scene, url, resolved) {
  const root = new BABYLON.TransformNode(`missing-asset:${url}`, scene);
  const mesh = BABYLON.MeshBuilder.CreateBox(`Missing Asset: ${url}`, {size:1}, scene);
  mesh.parent = root;
  mesh.metadata = {...(mesh.metadata||{}), cinterforgeMissingAsset:true, requestedAsset:String(url||''), resolvedAsset:String(resolved||'')};
  const material = new BABYLON.StandardMaterial(`missing-asset-material:${url}`, scene);
  material.diffuseColor = new BABYLON.Color3(1, 0, 1);
  material.emissiveColor = new BABYLON.Color3(.25, 0, .25);
  mesh.material = material;
  return {meshes:[mesh], transformNodes:[root], skeletons:[], animationGroups:[], particleSystems:[], geometries:[mesh.geometry].filter(Boolean), lights:[]};
}

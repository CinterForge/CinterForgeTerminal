/*
 * glTF 2.1 External Assets compatibility for Babylon.js 9.26.1.
 *
 * Testing Grounds / replacement-asset loader for Babylon.js 9.26.1.
 *
 * The master scene is an ordinary, self-contained glTF 2.0 document. It owns
 * every transform and contains visible colored placeholder geometry. Replacement
 * asset URIs are inert metadata only, so the master can be opened/rendered with
 * zero external dependencies. Runtime GLBs are instantiated at local identity
 * beneath their authored placement nodes after the master has loaded.
 */
(function (global) {
  "use strict";

  const B = global.BABYLON;
  if (!B || !B.SceneLoader || typeof B.LoadAssetContainerAsync !== "function") {
    throw new Error("[glTF 2.1 External Assets] Babylon glTF loader is not available");
  }

  const templatePromises = new Map();

  const sanitizedObjectURLs = new Map();

  function removePunctualLightsFromDocument(document, sourceURL) {
    const extensionName = "KHR_lights_punctual";
    const embeddedLightCount = Array.isArray(document?.extensions?.[extensionName]?.lights)
      ? document.extensions[extensionName].lights.length
      : 0;

    if (!embeddedLightCount) return 0;

    delete document.extensions[extensionName];
    for (const node of document.nodes || []) {
      if (!node?.extensions) continue;
      delete node.extensions[extensionName];
      if (!Object.keys(node.extensions).length) delete node.extensions;
    }
    if (Array.isArray(document.extensionsUsed)) {
      document.extensionsUsed = document.extensionsUsed.filter((name) => name !== extensionName);
      if (!document.extensionsUsed.length) delete document.extensionsUsed;
    }
    if (Array.isArray(document.extensionsRequired)) {
      document.extensionsRequired = document.extensionsRequired.filter((name) => name !== extensionName);
      if (!document.extensionsRequired.length) delete document.extensionsRequired;
    }
    if (document.extensions && !Object.keys(document.extensions).length) delete document.extensions;

    console.info("[Testing Grounds Assets] removed KHR_lights_punctual before Babylon import", {
      url: String(sourceURL || ""),
      embeddedLights: embeddedLightCount
    });
    return embeddedLightCount;
  }

  async function embedExternalGltfResources(document, sourceURL) {
    // Babylon resolves every non-data glTF resource URI against the URL of the
    // glTF document. Once the sanitized document lives at blob:, even an HTTP
    // URI can be incorrectly joined to that blob root by the loader. Embed the
    // existing external resources as data URIs so the sanitized document is
    // completely self-contained and resource resolution is unambiguous.
    const dataUriCache = new Map();

    async function asDataURI(uri) {
      const value = String(uri || "");
      if (!value || /^(?:data:|blob:)/i.test(value)) return value;
      const resourceURL = new URL(value, sourceURL).toString();
      let promise = dataUriCache.get(resourceURL);
      if (!promise) {
        promise = (async () => {
          const response = await fetch(resourceURL, { cache: "no-store" });
          if (!response.ok) {
            throw new Error(`[Testing Grounds Assets] failed to fetch glTF resource '${resourceURL}': HTTP ${response.status}`);
          }
          const bytes = new Uint8Array(await response.arrayBuffer());
          let binary = "";
          const chunkSize = 0x8000;
          for (let offset = 0; offset < bytes.length; offset += chunkSize) {
            binary += String.fromCharCode(...bytes.subarray(offset, offset + chunkSize));
          }
          const mimeType = response.headers.get("content-type") || "application/octet-stream";
          return `data:${mimeType};base64,${btoa(binary)}`;
        })();
        dataUriCache.set(resourceURL, promise);
      }
      return promise;
    }

    for (const buffer of document.buffers || []) {
      if (buffer?.uri) buffer.uri = await asDataURI(buffer.uri);
    }
    for (const image of document.images || []) {
      if (image?.uri) image.uri = await asDataURI(image.uri);
    }
  }

  async function sourceWithoutPunctualLights(url) {
    const sourceURL = String(url || "");
    const isGLB = /\.glb(?:[?#]|$)/i.test(sourceURL);
    const isGLTF = /\.gltf(?:[?#]|$)/i.test(sourceURL);
    if (!isGLB && !isGLTF) return sourceURL;

    let promise = sanitizedObjectURLs.get(sourceURL);
    if (promise) return promise;

    promise = (async () => {
      const response = await fetch(sourceURL, { cache: "no-store" });
      if (!response.ok) {
        throw new Error(`[Testing Grounds Assets] failed to fetch replacement '${sourceURL}' for light sanitization: HTTP ${response.status}`);
      }

      if (isGLTF) {
        const document = await response.json();
        const embeddedLightCount = removePunctualLightsFromDocument(document, sourceURL);
        if (!embeddedLightCount) return sourceURL;
        await embedExternalGltfResources(document, sourceURL);
        return URL.createObjectURL(new Blob(
          [JSON.stringify(document)],
          { type: "model/gltf+json" }
        ));
      }

      const bytes = new Uint8Array(await response.arrayBuffer());
      const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
      if (bytes.byteLength < 20 || view.getUint32(0, true) !== 0x46546c67 || view.getUint32(4, true) !== 2) {
        throw new Error(`[Testing Grounds Assets] replacement '${sourceURL}' is not a valid GLB 2.0 file`);
      }

      const declaredLength = view.getUint32(8, true);
      if (declaredLength !== bytes.byteLength) {
        throw new Error(`[Testing Grounds Assets] replacement '${sourceURL}' has invalid GLB length ${declaredLength}/${bytes.byteLength}`);
      }

      const jsonLength = view.getUint32(12, true);
      const jsonType = view.getUint32(16, true);
      if (jsonType !== 0x4e4f534a || 20 + jsonLength > bytes.byteLength) {
        throw new Error(`[Testing Grounds Assets] replacement '${sourceURL}' has no valid GLB JSON chunk`);
      }

      const decoder = new TextDecoder("utf-8");
      const document = JSON.parse(decoder.decode(bytes.subarray(20, 20 + jsonLength)).replace(/\u0000+$/g, "").trimEnd());
      const embeddedLightCount = removePunctualLightsFromDocument(document, sourceURL);
      if (!embeddedLightCount) return sourceURL;

      const encoder = new TextEncoder();
      const encodedJSON = encoder.encode(JSON.stringify(document));
      const paddedJSONLength = (encodedJSON.length + 3) & ~3;
      const tailOffset = 20 + jsonLength;
      const tail = bytes.subarray(tailOffset);
      const output = new Uint8Array(20 + paddedJSONLength + tail.length);
      const outputView = new DataView(output.buffer);
      outputView.setUint32(0, 0x46546c67, true);
      outputView.setUint32(4, 2, true);
      outputView.setUint32(8, output.length, true);
      outputView.setUint32(12, paddedJSONLength, true);
      outputView.setUint32(16, 0x4e4f534a, true);
      output.set(encodedJSON, 20);
      output.fill(0x20, 20 + encodedJSON.length, 20 + paddedJSONLength);
      output.set(tail, 20 + paddedJSONLength);

      return URL.createObjectURL(new Blob([output], { type: "model/gltf-binary" }));
    })().catch((error) => {
      sanitizedObjectURLs.delete(sourceURL);
      throw error;
    });

    sanitizedObjectURLs.set(sourceURL, promise);
    return promise;
  }

  function absoluteURL(base, relative) {
    return new URL(String(relative || ""), base).toString();
  }

  function replacementCatalog(document, url) {
    if (String(document?.asset?.version || "") !== "2.0") {
      throw new Error(`[Testing Grounds Assets] '${url}' must be a self-contained glTF 2.0 master`);
    }
    const catalog = document?.extras?.cinterforge?.assetList;
    if (!Array.isArray(catalog) || !Array.isArray(document.nodes)) {
      throw new Error(`[Testing Grounds Assets] '${url}' is missing extras.cinterforge.assetList or nodes[]`);
    }
    document.nodes.forEach((node, index) => {
      const replacement = node?.extras?.cinterforge?.replacementAsset;
      if (replacement !== undefined && (!Number.isInteger(replacement) || !catalog[replacement]?.uci)) {
        throw new Error(`[Testing Grounds Assets] nodes[${index}] has invalid replacementAsset UCI`);
      }
    });
    return catalog;
  }

  async function loadTemplate(url, scene) {
    let promise = templatePromises.get(url);
    if (!promise) {
      promise = loadTemplateWithoutPresentationRig(url, scene)
        .catch((error) => {
          templatePromises.delete(url);
          throw new Error(`[glTF 2.1 External Assets] failed to load '${url}': ${error?.message || error}`);
        });
      templatePromises.set(url, promise);
    }
    return promise;
  }

  async function runWithoutImportedPresentationRig(scene, url, importer) {
    // GLTF authoring/thumbnail rigs are never CIFWorld lighting. Guard the live
    // scene for the *entire* import interval so a loader cannot briefly attach a
    // source light/camera and dirty live shaders before post-load stripping runs.
    // The final sweep is intentional as well: it closes any loader path that
    // mutates scene.lights/scene.cameras without firing Babylon's add observable.
    const residentLights = new Set(scene.lights || []);
    const residentCameras = new Set(scene.cameras || []);
    const rejectedLights = new Set();
    const rejectedCameras = new Set();

    function rejectLight(light) {
      if (!light || residentLights.has(light)) return;
      rejectedLights.add(String(light.name || light.id || ''));
      light.dispose?.();
    }

    function rejectCamera(camera) {
      if (!camera || residentCameras.has(camera)) return;
      rejectedCameras.add(String(camera.name || camera.id || ''));
      camera.dispose?.();
    }

    const lightObserver = scene.onNewLightAddedObservable?.add?.(rejectLight);
    const cameraObserver = scene.onNewCameraAddedObservable?.add?.(rejectCamera);

    try {
      const result = await importer();

      for (const light of [...(scene.lights || [])]) rejectLight(light);
      for (const camera of [...(scene.cameras || [])]) rejectCamera(camera);

      // Disposal is not enough here. During a glTF import Babylon can compile a
      // material in the same turn in which an embedded preview light is attached.
      // Re-establish the exact pre-import scene collections before any imported
      // material is allowed to become live, then dirty the imported materials so
      // their next effect is built only against CIFWorld lighting.
      scene.lights = [...residentLights].filter((light) => light && !light.isDisposed?.());
      scene.cameras = [...residentCameras].filter((camera) => camera && !camera.isDisposed?.());

      const importedMaterials = result?.materials || result?.meshes
        ?.map((mesh) => mesh?.material)
        .filter(Boolean) || [];
      for (const material of new Set(importedMaterials)) {
        if ('maxSimultaneousLights' in material) {
          material.maxSimultaneousLights = Math.min(4, Math.max(0, scene.lights.length));
        }
        material.markAsDirty?.(B.Material?.LightDirtyFlag ?? 2);
      }

      if (scene.lights.length !== residentLights.size) {
        throw new Error(`[Testing Grounds Assets] imported presentation rig changed CIFWorld light ownership for '${url}': expected ${residentLights.size}, found ${scene.lights.length}`);
      }

      if (rejectedLights.size || rejectedCameras.size) {
        console.info('[Testing Grounds Assets] blocked imported presentation rig', {
          url: String(url || ''),
          lights: [...rejectedLights],
          cameras: [...rejectedCameras],
          residentLightCount: residentLights.size,
          finalLightCount: scene.lights?.length || 0
        });
      }
      return result;
    } finally {
      if (lightObserver) scene.onNewLightAddedObservable?.remove?.(lightObserver);
      if (cameraObserver) scene.onNewCameraAddedObservable?.remove?.(cameraObserver);
    }
  }

  async function loadTemplateWithoutPresentationRig(url, scene) {
    // Strip KHR_lights_punctual from the GLB bytes before Babylon parses the
    // document. Post-import disposal is too late: Babylon may compile PBR effects
    // while the embedded lights are temporarily resident in the scene.
    const importURL = await sourceWithoutPunctualLights(url);
    const container = await runWithoutImportedPresentationRig(
      scene,
      url,
      () => B.LoadAssetContainerAsync(importURL, scene)
    );
    stripEmbeddedPresentationRig(container, url);
    return container;
  }

  function stripEmbeddedPresentationRig(container, url) {
    // Replacement GLBs are geometry/material presentations inside an already-lit
    // CIFWorld. Embedded authoring lights/cameras belong to the source asset's
    // preview rig, not to the live game world. If they survive into repeated
    // instances, every placement contributes more scene lights and Babylon builds
    // progressively larger lighting shaders (hundreds of DIRLIGHT defines).
    // Remove those non-geometry resources once from the cached template before any
    // placement is instantiated. Geometry, materials, skeletons and animations are
    // deliberately left untouched.
    const embeddedLights = [...(container?.lights || [])];
    const embeddedCameras = [...(container?.cameras || [])];

    for (const light of embeddedLights) {
      light?.dispose?.();
    }
    for (const camera of embeddedCameras) {
      camera?.dispose?.();
    }

    if (container) {
      container.lights = [];
      container.cameras = [];
    }

    // Some generated replacement assets were authored while a large preview scene
    // was active. Babylon serializes PBRMaterial.maxSimultaneousLights with the
    // material, so those GLTFs can ask the shader compiler for hundreds of light
    // slots even after their embedded Light objects have been removed. CIFWorld
    // owns live lighting; replacement materials must compile against a bounded
    // world-light budget rather than the source preview scene's historical count.
    // Four leaves room for the current world lighting contract (three resident
    // lights in the shared scene) without changing which scene lights actually
    // illuminate the material.
    const normalizedMaterials = [];
    for (const material of container?.materials || []) {
      if (!("maxSimultaneousLights" in material)) continue;
      const previousMaximum = Number(material.maxSimultaneousLights);
      const boundedMaximum = 4;
      material.maxSimultaneousLights = boundedMaximum;
      material.markAsDirty?.(B.Material?.LightDirtyFlag ?? 2);
      if (!Number.isFinite(previousMaximum) || previousMaximum !== boundedMaximum) {
        normalizedMaterials.push({
          name: String(material.name || material.id || ""),
          from: previousMaximum,
          to: boundedMaximum
        });
      }
    }

    if (embeddedLights.length || embeddedCameras.length || normalizedMaterials.length) {
      console.info("[Testing Grounds Assets] stripped embedded presentation rig", {
        url: String(url || ""),
        lights: embeddedLights.length,
        cameras: embeddedCameras.length,
        normalizedMaterialLightBudgets: normalizedMaterials
      });
    }
  }

  async function LoadSceneAsync(url, scene, options = {}) {
    if (!scene) throw new Error("[glTF 2.1 External Assets] active Babylon scene is required");
    const response = await fetch(url, { cache: "no-store" });
    if (!response.ok) throw new Error(`[glTF 2.1 External Assets] failed to fetch '${url}': HTTP ${response.status}`);
    const document = await response.json();
    const catalog = replacementCatalog(document, url);

    // Load the exact authored master directly. The master is geometry/metadata,
    // never a lighting/camera authority. Apply the same import-time presentation
    // guard used for replacement GLBs so Map bootstrap cannot leak the master's
    // authoring rig into the persistent CIFWorld before Test is entered.
    const sanitizedMasterURL = await sourceWithoutPunctualLights(url);
    const hierarchyResult = await runWithoutImportedPresentationRig(
      scene,
      url,
      () => B.SceneLoader.ImportMeshAsync("", "", sanitizedMasterURL, scene, undefined, ".gltf")
    );

    const placementByName = new Map();
    for (const node of [...(hierarchyResult.transformNodes || []), ...(hierarchyResult.meshes || [])]) {
      const name = String(node?.name || "");
      if (name && name !== "__root__" && !placementByName.has(name)) placementByName.set(name, node);
    }

    const baseURL = new URL(".", url).toString();
    const replacementSession = `${Date.now()}-${Math.random().toString(36).slice(2)}`;
    const instantiatedRoots = [];
    const resolver = typeof options?.resolveReplacementURL === "function"
      ? options.resolveReplacementURL
      : null;

    // Resolve and load each unique asset identity once. A Testing Grounds scene can
    // contain dozens of walls/props that all bind the same UCI. The old loop paid
    // the resolver/server/staging cost once per placement and loaded unique assets
    // serially. UCI is identity, so it is also the correct cache key here.
    const replacementRecords = [];
    const spatialDiagnostics = [];
    const proxyVisibilityDiagnostics = [];
    const resolvedAssetPromises = new Map();
    // This option governs the entire replacement pass, not an individual node.
    // Keep it in LoadSceneAsync scope so the post-scan load/instantiate phases use
    // the same explicit contract as the node scan. In the shared editor scene this
    // is false: Testing Grounds contributes only its authored master/proxies.
    const loadExternalReplacements = options?.loadExternalReplacements !== false;

    for (let index = 0; index < document.nodes.length; index++) {
      const node = document.nodes[index];
      const replacementIndex = node?.extras?.cinterforge?.replacementAsset;
      if (replacementIndex === undefined) continue;

      const replacement = catalog[replacementIndex];
      const placement = placementByName.get(String(node.name || ""));
      if (!placement) {
        throw new Error(`[Testing Grounds Assets] placement node '${node.name || index}' was not created by Babylon`);
      }

      placement.metadata = {
        ...(placement.metadata || {}),
        cinterforgeAssetObject: true,
        cinterforgeAssetName: String(replacement.name || node.name || replacement.uci || "Asset"),
        cinterforgeAssetUCI: String(replacement.uci || ""),
        cinterforgeAssetPath: String(replacement.uri || ""),
        cinterforgeMasterDocument: document,
        cinterforgeMasterNodeIndex: index,
        cinterforgeMasterPath: "TestingGrounds/testing_grounds.gltf"
      };

      const identityKey = String(replacement.uci || replacement.uri || replacementIndex);
      if (loadExternalReplacements && !resolvedAssetPromises.has(identityKey)) {
        const assetPromise = (async () => {
          const assetURL = resolver
            ? await resolver({ replacement, node, nodeIndex: index, masterURL: url })
            : absoluteURL(baseURL, replacement.uri);
          if (!assetURL) {
            throw new Error(`[Testing Grounds Assets] replacement '${replacement.name || node.name || index}' resolved no model URL`);
          }

          const sessionAssetURL = new URL(assetURL);
          sessionAssetURL.searchParams.set("cinterforge_session", replacementSession);
          const template = await loadTemplate(sessionAssetURL.toString(), scene);
          return { template, url: sessionAssetURL.toString() };
        })();
        resolvedAssetPromises.set(identityKey, assetPromise);
      }

      replacementRecords.push({
        index,
        node,
        replacement,
        placement,
        identityKey
      });
    }

    // The live editor world may intentionally use the dependency-free authored
    // master presentation. In that mode the master placements and collision proxies
    // are the geometry authority and external replacement assets are not imported.
    // This is not an error fallback: callers opt into this contract explicitly.
    if (loadExternalReplacements) {
      // Start every unique UCI load together. This keeps standalone Build/Play startup
      // bounded by the slowest unique representation rather than the sum of every
      // representation. Promise.allSettled is intentional there because the authored
      // master remains structurally complete even if an optional presentation asset fails.
      await Promise.allSettled([...resolvedAssetPromises.values()]);
    }

    for (const record of replacementRecords) {
      if (!loadExternalReplacements) {
        continue;
      }
      const { index, node, replacement, placement, identityKey } = record;
      let loadedAsset;
      try {
        loadedAsset = await resolvedAssetPromises.get(identityKey);
      } catch (error) {
        console.error("[Testing Grounds Assets] replacement failed; keeping authored placeholder", {
          placement: String(node.name || index),
          replacement: String(replacement.name || identityKey),
          uci: String(replacement.uci || ""),
          error: String(error?.stack || error?.message || error)
        });
        continue;
      }

      // The cached AssetContainer is deliberately detached from the live scene. Do
      // not create InstancedMesh render nodes whose source meshes still belong to
      // that detached template: Babylon can report healthy bounds/material/visibility
      // for those instances while the source presentation never participates in the
      // live render path. Testing Grounds currently contains only a small authored
      // fixture set, so clone the mesh nodes into the live scene instead. Geometry
      // and materials remain shared by Babylon where legal; transforms become live
      // scene nodes owned by this placement.
      const instance = loadedAsset.template.instantiateModelsToScene(
        (sourceName) => `${node.name || replacement.name || "replacementAsset"}::${sourceName}`,
        false,
        { doNotInstantiate: true }
      );

      for (const root of instance.rootNodes || []) {
        const basis = Array.isArray(replacement.basisCorrectionEulerDegrees)
          ? replacement.basisCorrectionEulerDegrees
          : [0, 0, 0];
        let rx = B.Tools.ToRadians(Number(basis[0]) || 0);
        const ry = B.Tools.ToRadians(Number(basis[1]) || 0);
        const rz = B.Tools.ToRadians(Number(basis[2]) || 0);

        const isRegressionGround = String(node?.name || '') === 'regression-ground';
        const isWallReplacement = /wall/i.test(String(replacement?.name || node?.name || ''));

        // Wall placeholders in the authored Testing Grounds use the Solid asset's
        // X-up presentation basis. The generated Block representation is Y-up, so
        // wall replacements alone need the missing -90 degree local X basis
        // correction before the authored placement TRS is inherited. Do not fold
        // this into the placement: edited wall translation/rotation/signed scale
        // remain exactly as authored in testing_grounds.gltf.
        if (isWallReplacement && !isRegressionGround) {
          rx -= Math.PI / 2;
        }
        let representationParent = placement;
        let groundOrientationCorrection = null;

        // regression-ground is the one replacement whose source plane uses the
        // asset's XY basis while Testing Grounds is Y-up/XZ.  Rotate only the
        // representation, then fit its three resulting WORLD dimensions to the
        // authored external-asset-bounds proxy.  This is intentionally a
        // ground-only non-uniform fit: a floor slab has independent footprint
        // and thickness dimensions.  Ordinary replacement assets must never be
        // distorted this way.
        if (isRegressionGround) {
          groundOrientationCorrection = new B.TransformNode(
            `${node.name || 'regression-ground'}::orientation-correction`,
            scene
          );
          groundOrientationCorrection.parent = placement;
          groundOrientationCorrection.position.set(0, 0, 0);
          groundOrientationCorrection.scaling.set(1, 1, 1);
          groundOrientationCorrection.rotationQuaternion = B.Quaternion.FromEulerAngles(-Math.PI / 2, 0, 0);
          representationParent = groundOrientationCorrection;
          instantiatedRoots.push(groundOrientationCorrection);
        }

        root.rotationQuaternion = B.Quaternion.FromEulerAngles(rx, ry, rz);
        root.rotation.set(0, 0, 0);
        root.parent = representationParent;

        // AssetContainer templates are detached presentation sources. Depending on
        // the imported GLTF hierarchy, Babylon can preserve a disabled template
        // root on the instantiated hierarchy. That makes resolution/instancing
        // succeed while every descendant remains culled. A replacement instance
        // is explicit Test presentation, so normalize only its enabled hierarchy
        // here before CIFMapRuntime captures Test-only ownership. Map/Test remains
        // responsible for subsequently gating the complete Testing Grounds root.
        root.setEnabled?.(true);
        for (const descendant of root.getDescendants?.(false) || []) {
          descendant.setEnabled?.(true);
        }
        root.computeWorldMatrix?.(true);

        // The replacement hierarchy is now live. Do not rewrite visibility, alpha,
        // depth, transparency, or winding here: those are authored render state.
        // The only normalization owned by this loader is the CIFWorld light budget.
        // This second boundary is deliberate because instantiateModelsToScene may
        // clone a material after the detached template was normalized.
        const replacementRenderHierarchy = [root, ...(root.getDescendants?.(false) || [])]
          .filter((candidate) => Number(candidate?.getTotalVertices?.() || 0) > 0);
        const liveMaterials = new Set();
        for (const renderMesh of replacementRenderHierarchy) {
          const material = renderMesh.material;
          if (!material || liveMaterials.has(material)) continue;
          liveMaterials.add(material);
          if ('maxSimultaneousLights' in material) {
            material.maxSimultaneousLights = 4;
          }
          material.markAsDirty?.(B.Material?.LightDirtyFlag ?? 2);
        }

        // Babylon's StandardMaterial light budget is per material, not a global
        // scene-light limit. The shared CIFWorld can legitimately contain more
        // than four lights (for example the Map environment plus RPG world-time
        // lighting). Keep each replacement material capped at four participating
        // lights above, but never reject an otherwise valid Blockified replacement
        // merely because other CIFWorld-owned lights exist in the shared scene.

        if (isRegressionGround && groundOrientationCorrection) {
          const boundsProxy = (placement.getChildren?.() || []).find((child) =>
            child?.metadata?.gltf?.extras?.cinterforge?.role === "external-asset-bounds" ||
            String(child?.name || "").startsWith("__external_asset_bounds__")
          );
          if (!boundsProxy) {
            throw new Error('[Testing Grounds Assets] regression-ground has no authored bounds proxy');
          }

          placement.computeWorldMatrix?.(true);
          boundsProxy.computeWorldMatrix?.(true);
          groundOrientationCorrection.computeWorldMatrix?.(true);
          root.computeWorldMatrix?.(true);

          const targetBounds = boundsProxy.getHierarchyBoundingVectors?.(true);
          const visibleBounds = root.getHierarchyBoundingVectors?.(true);
          if (!targetBounds || !visibleBounds) {
            throw new Error('[Testing Grounds Assets] regression-ground bounds are unavailable');
          }

          const targetSize = targetBounds.max.subtract(targetBounds.min);
          const visibleSize = visibleBounds.max.subtract(visibleBounds.min);
          const epsilon = 1e-8;
          if (visibleSize.x <= epsilon || visibleSize.y <= epsilon || visibleSize.z <= epsilon) {
            throw new Error('[Testing Grounds Assets] regression-ground replacement has degenerate bounds');
          }

          // After -90 degrees about X, correction-local Y controls world Z and
          // correction-local Z controls world Y. X remains X.
          const worldScaleX = targetSize.x / visibleSize.x;
          const worldScaleY = targetSize.y / visibleSize.y;
          const worldScaleZ = targetSize.z / visibleSize.z;
          groundOrientationCorrection.scaling.set(worldScaleX, worldScaleZ, worldScaleY);
          groundOrientationCorrection.computeWorldMatrix?.(true);
          root.computeWorldMatrix?.(true);

          const fittedBounds = root.getHierarchyBoundingVectors?.(true);
          const fittedSize = fittedBounds?.max.subtract(fittedBounds.min);
          console.info('[Testing Grounds Assets] regression-ground fitted to authored floor dimensions', {
            targetSize: [targetSize.x, targetSize.y, targetSize.z],
            sourceAfterRotation: [visibleSize.x, visibleSize.y, visibleSize.z],
            correctionScale: [worldScaleX, worldScaleZ, worldScaleY],
            fittedSize: fittedSize ? [fittedSize.x, fittedSize.y, fittedSize.z] : null,
            rotationXDegrees: -90
          });
        }

        // Diagnostic only: compare the authored placement/collision proxy against
        // the ACTUAL renderable descendant geometry. This catches replacements that
        // successfully load but are tiny, huge, offset, underground, or buried in
        // unrelated structure. Do not mutate placement, scale, visibility, or bounds.
        try {
          placement.computeWorldMatrix?.(true);
          root.computeWorldMatrix?.(true);

          const boundsProxy = (placement.getChildren?.() || []).find((child) =>
            child?.metadata?.gltf?.extras?.cinterforge?.role === "external-asset-bounds" ||
            String(child?.name || "").startsWith("placeholder_box_") ||
            String(child?.name || "").startsWith("__external_asset_bounds__")
          );
          boundsProxy?.computeWorldMatrix?.(true);
          const proxyBounds = boundsProxy?.getHierarchyBoundingVectors?.(true) || null;

          const renderMeshes = [root, ...(root.getDescendants?.(false) || [])].filter((candidate) => {
            if (!candidate?.getBoundingInfo) return false;
            if (candidate?.metadata?.gltf?.extras?.cinterforge?.role === "external-asset-bounds") return false;
            const name = String(candidate?.name || "");
            if (name.startsWith("placeholder_box_") || name.startsWith("__external_asset_bounds__")) return false;
            const vertexCount = Number(candidate.getTotalVertices?.() || 0);
            return vertexCount > 0;
          });

          let renderMin = null;
          let renderMax = null;
          let renderVertexCount = 0;
          for (const mesh of renderMeshes) {
            mesh.computeWorldMatrix?.(true);
            const boundingBox = mesh.getBoundingInfo?.()?.boundingBox;
            if (!boundingBox) continue;
            renderVertexCount += Number(mesh.getTotalVertices?.() || 0);
            if (!renderMin) {
              renderMin = boundingBox.minimumWorld.clone();
              renderMax = boundingBox.maximumWorld.clone();
            } else {
              renderMin.minimizeInPlace(boundingBox.minimumWorld);
              renderMax.maximizeInPlace(boundingBox.maximumWorld);
            }
          }

          const vector = (value) => value ? [value.x, value.y, value.z] : null;
          const sizeFor = (minimum, maximum) => minimum && maximum ? maximum.subtract(minimum) : null;
          const centerFor = (minimum, maximum) => minimum && maximum ? minimum.add(maximum).scale(0.5) : null;
          const proxyMin = proxyBounds?.min || null;
          const proxyMax = proxyBounds?.max || null;
          const proxySize = sizeFor(proxyMin, proxyMax);
          const proxyCenter = centerFor(proxyMin, proxyMax);
          const renderSize = sizeFor(renderMin, renderMax);
          const renderCenter = centerFor(renderMin, renderMax);
          const centerDelta = proxyCenter && renderCenter ? renderCenter.subtract(proxyCenter) : null;
          const epsilon = 1e-8;
          const scaleRatio = proxySize && renderSize ? [
            proxySize.x > epsilon ? renderSize.x / proxySize.x : null,
            proxySize.y > epsilon ? renderSize.y / proxySize.y : null,
            proxySize.z > epsilon ? renderSize.z / proxySize.z : null
          ] : null;
          const cameraPosition = scene.activeCamera?.globalPosition || scene.activeCamera?.position || null;
          const cameraDistance = cameraPosition && renderCenter
            ? B.Vector3.Distance(cameraPosition, renderCenter)
            : null;
          const maximumCenterDelta = centerDelta
            ? Math.max(Math.abs(centerDelta.x), Math.abs(centerDelta.y), Math.abs(centerDelta.z))
            : null;
          const finiteRatios = (scaleRatio || []).filter((value) => Number.isFinite(value) && value > 0);
          const minimumScaleRatio = finiteRatios.length ? Math.min(...finiteRatios) : null;
          const maximumScaleRatio = finiteRatios.length ? Math.max(...finiteRatios) : null;

          spatialDiagnostics.push({
            placement: String(node?.name || index),
            replacement: String(replacement?.name || identityKey),
            uci: String(replacement?.uci || ""),
            root: String(root?.name || ""),
            renderMeshCount: renderMeshes.length,
            renderVertexCount,
            proxyBounds: proxyBounds ? { min: vector(proxyMin), max: vector(proxyMax), size: vector(proxySize), center: vector(proxyCenter) } : null,
            renderBounds: renderMin && renderMax ? { min: vector(renderMin), max: vector(renderMax), size: vector(renderSize), center: vector(renderCenter) } : null,
            centerDelta: vector(centerDelta),
            maximumCenterDelta,
            scaleRatio,
            minimumScaleRatio,
            maximumScaleRatio,
            cameraDistance,
            flags: {
              missingProxyBounds: !proxyBounds,
              missingRenderableGeometry: !renderMin || !renderMax,
              tinyRelativeToProxy: minimumScaleRatio !== null && minimumScaleRatio < 0.1,
              hugeRelativeToProxy: maximumScaleRatio !== null && maximumScaleRatio > 10,
              displacedFromProxy: maximumCenterDelta !== null && proxySize
                ? maximumCenterDelta > Math.max(proxySize.x, proxySize.y, proxySize.z)
                : false,
              belowProxy: !!(renderMax && proxyMin && renderMax.y < proxyMin.y)
            }
          });
        } catch (error) {
          spatialDiagnostics.push({
            placement: String(node?.name || index),
            replacement: String(replacement?.name || identityKey),
            uci: String(replacement?.uci || ""),
            root: String(root?.name || ""),
            diagnosticError: String(error?.stack || error?.message || error)
          });
        }

        instantiatedRoots.push(root);
      }

      for (const child of placement.getChildren?.() || []) {
        if (child?.metadata?.gltf?.extras?.cinterforge?.role === "external-asset-bounds" ||
            String(child?.name || "").startsWith("placeholder_box_") ||
            String(child?.name || "").startsWith("__external_asset_bounds__")) {
          // This is the one intentional proxy-hiding authority in the replacement
          // loader. Record both the proxy and the replacement render state before
          // hiding it so a runtime log can distinguish an intentionally invisible
          // collision proxy from missing/disabled replacement geometry.
          const replacementRenderMeshes = instantiatedRoots
            .filter((candidateRoot) => candidateRoot?.parent === placement || candidateRoot?.parent?.parent === placement)
            .flatMap((candidateRoot) => [candidateRoot, ...(candidateRoot.getDescendants?.(false) || [])])
            .filter((candidate) => Number(candidate?.getTotalVertices?.() || 0) > 0);
          proxyVisibilityDiagnostics.push({
            placement: String(node?.name || index),
            replacement: String(replacement?.name || identityKey),
            uci: String(replacement?.uci || ""),
            proxy: String(child?.name || ""),
            proxyBeforeHide: {
              enabledLocal: child?.isEnabled?.(false),
              enabledEffective: child?.isEnabled?.(),
              isVisible: child?.isVisible !== false,
              visibility: Number(child?.visibility ?? 1),
              vertices: Number(child?.getTotalVertices?.() || 0)
            },
            hiddenBy: "CIFGltf21ExternalAssets.LoadSceneAsync: successful external replacement owns visible presentation",
            replacementRenderMeshCount: replacementRenderMeshes.length,
            replacementRenderMeshes: replacementRenderMeshes.slice(0, 8).map((mesh) => ({
              name: String(mesh?.name || ""),
              enabledLocal: mesh?.isEnabled?.(false),
              enabledEffective: mesh?.isEnabled?.(),
              isVisible: mesh?.isVisible !== false,
              visibility: Number(mesh?.visibility ?? 1),
              vertices: Number(mesh?.getTotalVertices?.() || 0),
              material: String(mesh?.material?.name || ""),
              materialAlpha: Number(mesh?.material?.alpha ?? 1),
              layerMask: Number(mesh?.layerMask ?? 0)
            }))
          });
          child.metadata = {
            ...(child.metadata || {}),
            testingGroundsCollisionProxy: true,
            testingGroundsProxyHiddenBy: "successful-external-replacement"
          };
          child.visibility = 0;
          child.isPickable = true;
        }
      }
    }

    console.info("[Testing Grounds Assets] instanced replacements ready", {
      placements: replacementRecords.length,
      uniqueAssets: resolvedAssetPromises.size,
      roots: instantiatedRoots.length
    });

    const suspiciousSpatialDiagnostics = spatialDiagnostics.filter((sample) =>
      sample?.diagnosticError ||
      sample?.flags?.missingProxyBounds ||
      sample?.flags?.missingRenderableGeometry ||
      sample?.flags?.tinyRelativeToProxy ||
      sample?.flags?.hugeRelativeToProxy ||
      sample?.flags?.displacedFromProxy ||
      sample?.flags?.belowProxy
    );
    console.info("[Testing Grounds Assets] replacement spatial diagnostics", {
      count: spatialDiagnostics.length,
      suspiciousCount: suspiciousSpatialDiagnostics.length,
      suspicious: suspiciousSpatialDiagnostics.slice(0, 40),
      samples: spatialDiagnostics.slice(0, 12)
    });

    console.info("[Testing Grounds Assets] proxy visibility ownership diagnostic", {
      proxyCount: proxyVisibilityDiagnostics.length,
      proxiesHiddenByReplacementLoader: proxyVisibilityDiagnostics.filter((sample) =>
        sample?.hiddenBy?.startsWith("CIFGltf21ExternalAssets.LoadSceneAsync")
      ).length,
      proxiesWithNoReplacementRenderMeshes: proxyVisibilityDiagnostics.filter((sample) =>
        Number(sample?.replacementRenderMeshCount || 0) === 0
      ).length,
      samples: proxyVisibilityDiagnostics.slice(0, 20)
    });

    // Diagnostic only: capture representative replacement presentation state at
    // the exact point where instancing has completed, before CIFMapRuntime takes
    // Test-only ownership. Do not normalize or mutate any state here.
    const diagnosticRoots = instantiatedRoots.slice(0, 8).map((root) => {
      root.computeWorldMatrix?.(true);
      const bounds = root.getHierarchyBoundingVectors?.(true);
      const worldPosition = root.getAbsolutePosition?.() || root.absolutePosition || root.position;
      return {
        name: String(root?.name || ''),
        enabledLocal: root?.isEnabled?.(false),
        enabledEffective: root?.isEnabled?.(),
        visibility: Number.isFinite(root?.visibility) ? root.visibility : null,
        parent: String(root?.parent?.name || ''),
        parentEnabledEffective: root?.parent?.isEnabled?.(),
        worldPosition: worldPosition ? [worldPosition.x, worldPosition.y, worldPosition.z] : null,
        bounds: bounds ? {
          min: [bounds.min.x, bounds.min.y, bounds.min.z],
          max: [bounds.max.x, bounds.max.y, bounds.max.z]
        } : null
      };
    });
    console.info("[Testing Grounds Assets] replacement presentation diagnostic after instancing", {
      samples: diagnosticRoots
    });

    const result = {
      ...hierarchyResult,
      externalAssetRoots: instantiatedRoots,
      document,
      url
    };
    console.info("[Testing Grounds Assets] scene loaded", { url, placements: instantiatedRoots.length });
    return result;
  }

  global.CIFGltf21ExternalAssets = Object.freeze({
    LoadSceneAsync,
    SourceWithoutPunctualLights: sourceWithoutPunctualLights,
    RunWithoutImportedPresentationRig: runWithoutImportedPresentationRig
  });
  console.info("[glTF 2.1 External Assets] compatibility loader ready");
})(typeof globalThis !== "undefined" ? globalThis : window);

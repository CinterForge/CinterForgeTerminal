/**
 * CIFAssetPreview ownership extracted from the legacy Asset Library controller.
 *
 * Methods intentionally execute with the controller as `this` while the refactor
 * proceeds, preserving current state without duplicating provider/preview state.
 */
class CIFAssetPreview {
    static VERSION = 'refactor-v3';

    static SuspendRendering() {
        for (const engine of this.engines.values()) {
            try { engine?.stopRenderLoop?.();
             } catch (_) {
             }
        }
    }

    static DisposePreviewKey(key) {
        const k=String(key||''),engine=this.engines.get(k),scene=this.scenes.get(k),controller=this.embeddedAnimationControllers.get(k);
        if(controller){try{controller.dispose?.();
        }catch(_){ }this.embeddedAnimationControllers.delete(k);
        }
        const canvas=engine?.getRenderingCanvas?.();
        try{engine?.stopRenderLoop?.();
        } catch (_) {
        }
        try{scene?.dispose?.();
        } catch (_) {
        }
        try{engine?.dispose?.();
        } catch (_) {
        }
        const card=canvas?.closest?.('.assignment-current-card,.assignment-result,.saved-asset-card');
        if(card){card.classList.remove('viewer-active','blockified-preview-active');
        }
        this.scenes.delete(k);
        this.engines.delete(k);
        if (this.activePreviewKey===k) {
            this.activePreviewKey=null;
        }
    }

    static DisposeDetachedPreviews() {
        for(const [key,engine] of Array.from(this.engines.entries())){
            const canvas=engine?.getRenderingCanvas?.();
            if (!canvas||!canvas.isConnected) {
                this.DisposePreviewKey(key);
            }
        }
    }

    static ActivatePreviewEngine(key) {
        const requested=String(key||'');
        this.DisposeDetachedPreviews();
        // IFPlayer shows one interactive Babylon preview at a time. Retaining every
        // old scene/engine merely paused render loops while meshes, textures,
        // animation groups and GPU buffers stayed alive. Recreate previews on demand.
        for(const candidate of Array.from(this.engines.keys())){
            if (String(candidate)!==requested) {
                this.DisposePreviewKey(candidate);
            }
        }
        this.activePreviewKey=requested;
        const engine=this.engines.get(requested),scene=this.scenes.get(requested);
        if (!engine||!scene) {
            return;
        }
        try{engine.stopRenderLoop?.();
        } catch (_) {
        }
        engine.runRenderLoop(()=>scene.render());
        try{engine.resize?.();
        } catch (_) {
        }
    }

    static ResumeRendering() {
        if (this.activePreviewKey && this.engines.has(this.activePreviewKey))
            this.ActivatePreviewEngine(this.activePreviewKey);
    }

    static async LoadPreview(card) {
        const uid = String(card.dataset.uid || '');
        const canvas = card.querySelector('.objaverse-canvas');
        if (!canvas) {
            throw new Error('3D preview canvas is unavailable.');
        }
        // A search can rebuild the result card while leaving this UID cached. Reuse
        // an Objaverse engine only when it still renders into this exact live canvas.
        // This is intentionally local to Preview; do not dispose unrelated F4/Mixamo
        // engines while rebuilding search results.
        const existingEngine = this.engines.get(uid);
        if (existingEngine) {
            let renderingCanvas = null;
            try { renderingCanvas = existingEngine.getRenderingCanvas?.() || null;
             } catch (_) {
             }
            if (renderingCanvas === canvas && canvas.isConnected) {
                this.ActivatePreviewEngine(uid);
                card.classList.add('viewer-active');
                return;
            }
            try { existingEngine.stopRenderLoop?.();
             } catch (_) {
             }
            try { this.scenes.get(uid)?.dispose?.();
             } catch (_) {
             }
            try { existingEngine.dispose?.();
             } catch (_) {
             }
            this.engines.delete(uid);
            this.scenes.delete(uid);
        }
        await this.StartTask(card, 'preview');
        await ensureBabylon();
        if (!window.BABYLON) {
            throw new Error('Babylon.js is unavailable.');
        }
        // Keep the provider/generated thumbnail visible while the Objaverse GLB is
        // downloading, staging, and importing. Large models can take several seconds
        // to parse; switching to the Babylon canvas before the first rendered frame
        // made that normal load interval look like a broken black Preview.
        const engine = new BABYLON.Engine(canvas, true, {preserveDrawingBuffer:true, stencil:true, adaptToDeviceRatio:true});
        const scene = new BABYLON.Scene(engine);
        this.engines.set(uid, engine);
         this.scenes.set(uid, scene);
        scene.clearColor = new BABYLON.Color4(.035,.04,.05,1);
        const camera = new BABYLON.ArcRotateCamera(`objaverse-camera-${uid}`, Math.PI/4, Math.PI/3, 3, BABYLON.Vector3.Zero(), scene);
        camera.lowerRadiusLimit=.05;
            camera.upperRadiusLimit=100000;
            camera.wheelPrecision=40;
            camera.panningSensibility=80;
            camera.attachControl(canvas,false);
        canvas.addEventListener('wheel', event => event.preventDefault(), {passive:false});
        const hemi = new BABYLON.HemisphericLight(`objaverse-hemi-${uid}`, new BABYLON.Vector3(0,1,0), scene);
            hemi.intensity=1.2;
        const dir = new BABYLON.DirectionalLight(`objaverse-dir-${uid}`, new BABYLON.Vector3(-.5,-1,-.5), scene);
            dir.intensity=.9;
        const modelUrl = await CIFRuntime.stagedAssetUrl('objaverse-model',{id:uid});
        if (!modelUrl) {
            throw new Error(`Objaverse model ${uid} was downloaded for Preview but no staged GLB URL is available.`);
        }
        const result = await BABYLON.SceneLoader.ImportMeshAsync('', '', modelUrl, scene, undefined, '.glb');
        if((result.animationGroups||[]).length) this.SetupEmbeddedAnimationCycle(card,scene,result,`objaverse:${uid}`);
        const meshes = result.meshes.filter(mesh => { try { return mesh.getTotalVertices() > 0;
            } catch (_) { return false;
            } });
        if (!meshes.length) {
            throw new Error(`Objaverse model ${uid} loaded but contains no renderable meshes.`);
        }

        // Imported GLBs commonly put geometry below transform roots.  BoundingInfo's
        // minimumWorld/maximumWorld values are not reliable until those transforms
        // have been evaluated at least once.  The old Preview code framed the camera
        // immediately after ImportMeshAsync, which could leave the camera looking at
        // stale/origin bounds and produce a perfectly valid but completely black
        // canvas.  Force the imported hierarchy current before deriving the frame.
        for (const mesh of result.meshes || []) {
            try { mesh.computeWorldMatrix?.(true); } catch (_) {}
        }
        let min = new BABYLON.Vector3(Infinity,Infinity,Infinity);
        let max = new BABYLON.Vector3(-Infinity,-Infinity,-Infinity);
        meshes.forEach(mesh => {
            const box=mesh.getBoundingInfo().boundingBox;
            min=BABYLON.Vector3.Minimize(min,box.minimumWorld);
            max=BABYLON.Vector3.Maximize(max,box.maximumWorld);
        });
        const center=min.add(max).scale(.5);
        const diagonal=max.subtract(min).length();
        if (!Number.isFinite(diagonal) || diagonal <= 0) {
            throw new Error(`Objaverse model ${uid} has invalid preview bounds.`);
        }
        camera.setTarget(center);
        camera.radius=Math.max(diagonal*1.25,.5);
        camera.lowerRadiusLimit=Math.max(diagonal*.03,.01);
        camera.upperRadiusLimit=Math.max(diagonal*12,10);
        camera.minZ=Math.max(diagonal/10000,.001);
        camera.maxZ=Math.max(diagonal*100,1000);

        // Paint one known-good frame before handing the canvas to the continuous
        // preview loop.  This also makes Preview deterministic if the browser delays
        // the first requestAnimationFrame after the button click.
        engine.resize();
        scene.render();
        // Reveal the interactive canvas only after Babylon has successfully imported,
        // framed, and painted the model. Until this point the existing thumbnail is
        // deliberately left visible; failed imports therefore cannot leave a black tile.
        card.classList.add('viewer-active');
        console.info('[IFPlayer Assets] objaverse-preview-ready',{
            uid,
            modelUrl,
            meshes:meshes.length,
            center:{x:center.x,y:center.y,z:center.z},
            diagonal,
            cameraRadius:camera.radius
        });
        this.ActivatePreviewEngine(uid);
        if (window.ResizeObserver) {
            new ResizeObserver(() => engine.resize()).observe(canvas);
        }
        this.SetProgress(card, this.text('objaverse_preview_ready','Interactive 3D preview ready'));
    }

    static async LoadCharacterPreview(card,requestedAnimationId='',playAnimation=false,{forceTPose=false}={}) {
        const characterId=String(card.dataset.assetId||'');
            const canvas=card?.querySelector('.asset-character-canvas');
            if (!characterId||!canvas) {
                return;
            }
        const animationId=forceTPose?'':String(requestedAnimationId||card.querySelector('.mixamo-character-animation-picker')?.value||this.selectedDefaultAnimationId||this.elements().animationPicker?.value||'');
        const key=`character:${characterId}:${animationId||'tpose'}`;
            let engine=this.engines.get(key),scene=this.scenes.get(key);
        if(engine){
            let renderingCanvas=null;
            try{renderingCanvas=engine.getRenderingCanvas?.()||null;
            } catch (_) {
            }
            if(renderingCanvas!==canvas || !canvas.isConnected){
                try{engine.stopRenderLoop?.();
                } catch (_) {
                }
                try{scene?.dispose?.();
                } catch (_) {
                }
                try{engine.dispose?.();
                } catch (_) {
                }
                this.engines.delete(key);
                this.scenes.delete(key);
                engine=null;
                scene=null;
            }
        }
        if(!engine){
            await ensureBabylon();
                engine=new BABYLON.Engine(canvas,true,{preserveDrawingBuffer:true,stencil:false});
                scene=new BABYLON.Scene(engine);
                scene.clearColor=new BABYLON.Color4(.04,.045,.055,1);
            const camera=new BABYLON.ArcRotateCamera('camera',Math.PI/2,Math.PI/2.25,2.6,BABYLON.Vector3.Zero(),scene);
                camera.attachControl(canvas,true);
                new BABYLON.HemisphericLight('light',new BABYLON.Vector3(.4,1,.2),scene);
            this.engines.set(key,engine);
                this.scenes.set(key,scene);
                const before=new Set(scene.getNodes());
                const characterUrl=await CIFRuntime.stagedAssetUrl('preview-model',{kind:'3d',id:characterId});
                const character=await BABYLON.SceneLoader.ImportMeshAsync('','',characterUrl,scene,undefined,'.glb');
            const characterNodes=scene.getNodes().filter(n=>!before.has(n));
                (character.skeletons||[]).forEach(s=>{try{s.returnToRest();
                }catch(_){}});
                this.FrameScene(scene,camera);
            if(animationId){
                const groupsBefore=new Set(scene.animationGroups),sourceBefore=new Set(scene.getNodes());
                const animationUrl=await CIFRuntime.stagedAssetUrl('preview-model',{kind:'animation',id:animationId});
                    await BABYLON.SceneLoader.ImportMeshAsync('','',animationUrl,scene,undefined,'.glb');
                const sourceNodes=scene.getNodes().filter(n=>!sourceBefore.has(n)),sourceGroups=scene.animationGroups.filter(g=>!groupsBefore.has(g));
                const retarget=this.RetargetAnimation(scene,character,characterNodes,sourceNodes,sourceGroups,`character-${characterId}`);
                    const playing=retarget.playing;
                if (playAnimation) {
                    playing.start(true,1,playing.from,playing.to,false);
                }
                else {playing.start(false,1,playing.from,playing.to,false);
                playing.goToFrame(0);
                playing.pause();
                }
            }
        }
        card.classList.add('viewer-active');
        this.ActivatePreviewEngine(key);
        try{scene.render();
        } catch (_) {
        }
        if (card.dataset.thumbnailGenerating!=='1') {
            await this.CacheCharacterThumbnail(characterId,canvas);
        }
        this.SetProgress(card,animationId?(playAnimation?'Playing '+String((this.allMixamoAnimations.find(row=>String(row.id||'')===animationId)||{}).title||'animation'):this.text('asset_character_preview_frame0','Default animation \u00b7 frame 0')):this.text('asset_character_preview_tpose','T-pose preview'));
    }

}

window.CIFAssetPreview = CIFAssetPreview;
console.info('[CIF Asset] CIFAssetPreview loaded', CIFAssetPreview.VERSION);

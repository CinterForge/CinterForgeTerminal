/**
 * Asset thumbnail loading, generation, staging, and hydration ownership.
 *
 * Extracted from the legacy Asset Library controller. Methods intentionally
 * execute with the active Asset Library controller as `this` while the larger
 * refactor continues, so ownership moves without duplicating runtime state.
 */
class CIFAssetThumbnail {
    static VERSION = 'refactor-v4';
    static CHARACTER_THUMBNAIL_RECIPE = Object.freeze({
        schema: 'cinterforge.mixamo-character-thumbnail.v4',
        posePolicy: 'animation-frame-zero',
        poseFraction: 0,
        background: [.39, .68, .93, 1],
        camera: { alpha: -Math.PI / 2, beta: Math.PI / 2.25, radius: 2.6 },
        hemisphericLight: { direction: [.4, 1, .2], intensity: 1.8 },
        jpegQuality: .82,
    });

    static CharacterThumbnailRecipeFingerprint() {
        const encoded = JSON.stringify(this.CHARACTER_THUMBNAIL_RECIPE);
        let hash = 2166136261;
        for (let index = 0; index < encoded.length; index += 1) {
            hash ^= encoded.charCodeAt(index);
            hash = Math.imul(hash, 16777619);
        }
        return `mixamo-thumb-${(hash >>> 0).toString(16).padStart(8, '0')}`;
    }

    static StableCharacterAnimationRows(characterId) {
        const rows = (this.allMixamoAnimations.length ? this.allMixamoAnimations : this.mixamoAnimations)
            .filter(row => String(row?.id || '').trim());
        if (!rows.length) {
            return [];
        }
        const seed = String(characterId || '');
        let hash = 2166136261;
        for (let index = 0; index < seed.length; index += 1) {
            hash ^= seed.charCodeAt(index);
            hash = Math.imul(hash, 16777619);
        }
        const start = (hash >>> 0) % rows.length;
        return rows.slice(start).concat(rows.slice(0, start));
    }

    static DisposeObjaverseThumbnailRenderer() {
        if(this.objaverseThumbnailRendererDisposeTimer!==null){
            clearTimeout(this.objaverseThumbnailRendererDisposeTimer);
            this.objaverseThumbnailRendererDisposeTimer=null;
        }
        const renderer=this.objaverseThumbnailRenderer;
        this.objaverseThumbnailRenderer=null;
        if (!renderer) {
            return;
        }
        try{renderer.scene?.dispose?.();
        } catch (_) {
        }
        try{renderer.engine?.stopRenderLoop?.();
        } catch (_) {
        }
        try{renderer.engine?.dispose?.();
        } catch (_) {
        }
        try{renderer.canvas?.remove?.();
        } catch (_) {
        }
        console.info('[IFPlayer Assets] objaverse-thumbnail-renderer-disposed');
    }

    static async ObjaverseThumbnailRenderer() {
        if(this.objaverseThumbnailRendererDisposeTimer!==null){
            clearTimeout(this.objaverseThumbnailRendererDisposeTimer);
            this.objaverseThumbnailRendererDisposeTimer=null;
        }
        if (this.objaverseThumbnailRenderer) {
            return this.objaverseThumbnailRenderer;
        }
        await ensureBabylon();
        const canvas=document.createElement('canvas');
        canvas.width=320;
        canvas.height=320;
        canvas.style.cssText='position:fixed;left:-10000px;top:-10000px;width:320px;height:320px;pointer-events:none;opacity:0;';
        document.body.appendChild(canvas);
        const engine=new BABYLON.Engine(canvas,true,{preserveDrawingBuffer:true,stencil:false});
        this.objaverseThumbnailRenderer={canvas,engine,scene:null};
        console.info('[IFPlayer Assets] objaverse-thumbnail-renderer-created');
        return this.objaverseThumbnailRenderer;
    }

    static QueueObjaverseThumbnailGeneration(image,uid){
        const id=String(uid||'');
        if (!id) {
            return Promise.resolve('');
        }
        const existing=this.objaverseThumbnailPending.get(id);
        if (existing) {
            return existing;
        }
        if(this.objaverseThumbnailRendererDisposeTimer!==null){clearTimeout(this.objaverseThumbnailRendererDisposeTimer);
            this.objaverseThumbnailRendererDisposeTimer=null;
            }
        // Objaverse thumbnail production is intentionally one serial GPU worker.
        // Search cards never allocate Babylon engines just to manufacture thumbnails.
        const job=this.objaverseThumbnailSerial.catch(()=>{}).then(()=>this.GenerateObjaverseSearchThumbnail(image,id));
        this.objaverseThumbnailPending.set(id,job);
        this.objaverseThumbnailSerial=job.finally(()=>{
            if (this.objaverseThumbnailPending.get(id)===job) {
                this.objaverseThumbnailPending.delete(id);
            }
            if(!this.objaverseThumbnailPending.size){
                this.objaverseThumbnailRendererDisposeTimer=setTimeout(()=>this.DisposeObjaverseThumbnailRenderer(),1000);
            }
        });
        return job;
    }

    static async GenerateObjaverseSearchThumbnail(image,uid){
        const card=image?.closest?.('.objaverse-card');
        if (!card||!uid) {
            return '';
        }
        const renderer=await this.ObjaverseThumbnailRenderer();
        const {canvas,engine}=renderer;
        let scene=null;
        try{
            if(renderer.scene){try{renderer.scene.dispose();
            }catch(_){} renderer.scene=null;
            }
            scene=new BABYLON.Scene(engine);
             renderer.scene=scene;
            scene.clearColor=new BABYLON.Color4(.035,.04,.05,1);
            const camera=new BABYLON.ArcRotateCamera(`objaverse-thumb-camera-${uid}`,Math.PI/4,Math.PI/3,3,BABYLON.Vector3.Zero(),scene);
            const hemi=new BABYLON.HemisphericLight(`objaverse-thumb-hemi-${uid}`,new BABYLON.Vector3(0,1,0),scene);
                hemi.intensity=1.2;
            const dir=new BABYLON.DirectionalLight(`objaverse-thumb-dir-${uid}`,new BABYLON.Vector3(-.5,-1,-.5),scene);
                dir.intensity=.9;
            const modelUrl=await CIFRuntime.stagedAssetUrl('objaverse-model',{id:uid});
            // Provider thumbnails can legitimately be unavailable (expired/403), while
            // the corresponding Objaverse GLB has never been downloaded locally.  An
            // empty staged URL is therefore a cache miss, not a Babylon-load request.
            // Passing '' to ImportMeshAsync makes Babylon fetch the current HTML page
            // and report an opaque "Unexpected magic" GLB error.  Only render a
            // replacement thumbnail when the actual model bytes are already present.
            if (!modelUrl) {
                console.info('[IFPlayer Assets] objaverse-thumbnail-model-unavailable',{uid});
                return '';
            }
            const result=await BABYLON.SceneLoader.ImportMeshAsync('','',modelUrl,scene,undefined,'.glb');
            const meshes=(result.meshes||[]).filter(mesh=>{try{return mesh.getTotalVertices()>0;
                }catch(_){return false;
                }});
            if(meshes.length){
                let min=new BABYLON.Vector3(Infinity,Infinity,Infinity),max=new BABYLON.Vector3(-Infinity,-Infinity,-Infinity);
                meshes.forEach(mesh=>{const box=mesh.getBoundingInfo().boundingBox;
                    min=BABYLON.Vector3.Minimize(min,box.minimumWorld);
                    max=BABYLON.Vector3.Maximize(max,box.maximumWorld);
                    });
                const center=min.add(max).scale(.5),diagonal=max.subtract(min).length();
                camera.setTarget(center);
                    camera.radius=Math.max(diagonal*1.25,.5);
                    camera.minZ=Math.max(diagonal/10000,.001);
                    camera.maxZ=Math.max(diagonal*100,1000);
            }
            try{await scene.whenReadyAsync?.();
            } catch (_) {
            }
            engine.resize();
            for(let frame=0;frame<3;frame++){scene.render();
            await new Promise(resolve=>requestAnimationFrame(resolve));
            }
            const dataUrl=canvas.toDataURL('image/jpeg',0.82);
            if (!String(dataUrl||'').startsWith('data:image/')) {
                return '';
            }
            await CIFRuntime.assetCommand({operation:'save-objaverse-thumbnail',id:uid,data_url:dataUrl});
            const payload=await CIFRuntime.assetCommand({operation:'stage',resource:'objaverse-thumb',id:uid});
            const relative=String(payload?.url||'').trim();
            return relative?CIFRuntime.presentationAssetUrl(`SVG/${relative}`):'';
        }catch(error){
            console.error('[IFPlayer Assets] objaverse-thumbnail-generate-failed',{uid,error:String(error?.message||error)});
                return '';
        }finally{
            if (renderer.scene===scene) {
                renderer.scene=null;
            }
            try{scene?.dispose?.();
            } catch (_) {
            }
        }
    }

    static QueueSavedThumbnailGeneration(image,modelPath){
        const rel=String(modelPath||'').replace(/^\/+/, '');
        if (!rel) {
            return Promise.resolve('');
        }
        // A failed thumbnail is optional presentation data. One failure per
        // asset per page is enough; repeated hydration cannot flood Safari's
        // console or starve the original 3D Blockify engine.
        if (this.savedThumbnailFailures?.has(rel)) return Promise.resolve('');
        const existing=this.savedThumbnailPending.get(rel);
        if (existing) {
            return existing;
        }
        if(this.objaverseThumbnailRendererDisposeTimer!==null){clearTimeout(this.objaverseThumbnailRendererDisposeTimer);
            this.objaverseThumbnailRendererDisposeTimer=null;
            }
        // Canonical saved-asset thumbnails share the same single reusable GPU
        // worker as Objaverse. Never create one WebGL engine per asset card.
        const job=this.objaverseThumbnailSerial.catch(()=>{}).then(()=>this.GenerateSavedThumbnail(image,rel));
        this.savedThumbnailPending.set(rel,job);
        this.objaverseThumbnailSerial=job.finally(()=>{
            if (this.savedThumbnailPending.get(rel)===job) {
                this.savedThumbnailPending.delete(rel);
            }
            if(!this.savedThumbnailPending.size&&!this.objaverseThumbnailPending.size){
                this.objaverseThumbnailRendererDisposeTimer=setTimeout(()=>this.DisposeObjaverseThumbnailRenderer(),1000);
            }
        });
        return job;
    }

    static async GenerateSavedThumbnail(image, modelPath) {
        const rel=String(modelPath||'').replace(/^\/+/, '');
        if (!rel) {
            return '';
        }
        const renderer=await this.ObjaverseThumbnailRenderer();
        const {canvas,engine}=renderer;
        let scene=null;
        try{
            if(renderer.scene){try{renderer.scene.dispose();
            }catch(_){} renderer.scene=null;
            }
            scene=new BABYLON.Scene(engine);
            renderer.scene=scene;
            scene.clearColor=new BABYLON.Color4(.035,.04,.05,1);
            const camera=new BABYLON.ArcRotateCamera(`saved-thumb-camera-${Date.now()}`,Math.PI/4,Math.PI/3,3,BABYLON.Vector3.Zero(),scene);
            const hemi=new BABYLON.HemisphericLight(`saved-thumb-hemi-${Date.now()}`,new BABYLON.Vector3(0,1,0),scene);
                hemi.intensity=1.2;
            const dir=new BABYLON.DirectionalLight(`saved-thumb-dir-${Date.now()}`,new BABYLON.Vector3(-.5,-1,-.5),scene);
                dir.intensity=.9;
            // Use the same model identity/path as GLB Preview. The model itself
            // remains canonical; only the generated JPEG is persisted as a derivative.
            const modelUrl=await CIFRuntime.stagedAssetUrl('saved-file',{path:rel});
            if (!modelUrl) {
                throw new Error(`Saved GLB unavailable: ${rel}`);
            }
            const extension=new URL(modelUrl,window.location.href).pathname.toLowerCase().endsWith('.gltf')?'.gltf':'.glb';
            const name=modelUrl.slice(modelUrl.lastIndexOf('/')+1),rootUrl=modelUrl.slice(0,modelUrl.lastIndexOf('/')+1);
            const result=await BABYLON.SceneLoader.ImportMeshAsync('',rootUrl,name,scene,undefined,extension);
            const recipe=String(image?.dataset.thumbnailRecipe||'');
            let poseGroup=null;
            if (recipe === 'animation-frame-zero-v1') {
                const groups=(result.animationGroups||[]).filter(group=>group?.targetedAnimations?.length);
                let selected=groups[0]||null;
                if (!selected) {
                    const folderRel=rel.slice(0,rel.lastIndexOf('/'));
                    const saved=this.SavedRows().find(row=>row.folder===folderRel)||this.AssignmentSavedRows().map(row=>row.folder).find(row=>row?.folder===folderRel);
                    const refs=(saved?.assigned_animations||[]).filter(row=>row?.folder&&row?.file);
                    if(!refs.length&&saved?.default_animation?.folder&&saved?.default_animation?.file) refs.push(saved.default_animation);
                    if(refs.length) {
                        const ref=refs[0],originalNodes=scene.getNodes().slice(),beforeGroups=new Set(scene.animationGroups);
                        const url=await CIFRuntime.stagedAssetUrl('saved-runtime-animation',{path:`${ref.folder}/${ref.file}`});
                        if (!url) throw new Error(`Saved animation unavailable: ${ref.folder}/${ref.file}`);
                        await BABYLON.SceneLoader.ImportMeshAsync('','',url,scene,undefined,'.glb');
                        const sources=scene.getNodes().filter(node=>!originalNodes.includes(node));
                        const sourceGroups=scene.animationGroups.filter(group=>!beforeGroups.has(group));
                        selected=this.RetargetAnimation(scene,result,originalNodes,sources,sourceGroups,'saved-thumbnail').playing;
                    }
                }
                // A clipless rig can be classified Animated while its import
                // migration is still pending or failed. Do not publish a
                // rest-pose thumbnail as an animation frame or flood the console
                // with a second error; retry after a real clip is installed.
                if (!selected) {
                    this.savedThumbnailFailures=this.savedThumbnailFailures||new Set();
                    this.savedThumbnailFailures.add(rel);
                    console.info('[IFPlayer Assets] animated-thumbnail-awaiting-clip: '+rel);
                    return '';
                }
                poseGroup=selected;
            }
            const meshes=(result.meshes||[]).filter(mesh=>{try{return mesh.getTotalVertices()>0;
                }catch(_){return false;
                }});
            if(meshes.length){
                let min=new BABYLON.Vector3(Infinity,Infinity,Infinity),max=new BABYLON.Vector3(-Infinity,-Infinity,-Infinity);
                meshes.forEach(mesh=>{const box=mesh.getBoundingInfo().boundingBox;
                    min=BABYLON.Vector3.Minimize(min,box.minimumWorld);
                    max=BABYLON.Vector3.Maximize(max,box.maximumWorld);
                    });
                const center=min.add(max).scale(.5),diagonal=max.subtract(min).length();
                camera.setTarget(center);
                    camera.radius=Math.max(diagonal*1.25,.5);
                    camera.minZ=Math.max(diagonal/10000,.001);
                    camera.maxZ=Math.max(diagonal*100,1000);
            }
            try{await scene.whenReadyAsync?.();
            } catch (_) {
            }
            engine.resize();
            if(poseGroup){
                // Babylon evaluates the imported rig only after a render. Seek
                // after that evaluation, then freeze the first animation frame.
                poseGroup.start(false,1,poseGroup.from,poseGroup.to,false);
                scene.render();
                await new Promise(resolve=>requestAnimationFrame(resolve));
                poseGroup.goToFrame(Number(poseGroup.from||0));
                scene.render();
                await new Promise(resolve=>requestAnimationFrame(resolve));
                poseGroup.pause();
            }
            for(let frame=0;frame<4;frame++){scene.render();
            await new Promise(resolve=>requestAnimationFrame(resolve));
            }
            const dataUrl=canvas.toDataURL('image/jpeg',0.82);
            if (!String(dataUrl||'').startsWith('data:image/')) {
                return '';
            }
            const saved=await CIFRuntime.assetCommand({operation:'save-saved-thumbnail',path:rel,data_url:dataUrl,recipe:String(image?.dataset.thumbnailRecipe||'')});
            if (!String(saved?.thumbnail_path||'').trim()) {
                return '';
            }
            if(image){image.dataset.stagedResource='saved-thumb';
            image.dataset.stagedPath=rel;
            }
            console.info('[IFPlayer Assets] saved-thumbnail-generated',{path:rel,thumbnail:saved.thumbnail_path});
            return await CIFRuntime.stagedAssetUrl('saved-thumb',{path:rel});
        }catch(error){
            this.savedThumbnailFailures=this.savedThumbnailFailures||new Set();
            this.savedThumbnailFailures.add(rel);
            console.error('[IFPlayer Assets] saved-thumbnail-generate-failed: '+rel+' | '+String(error?.stack||error?.message||error));
            return '';
        }finally{
            if (renderer.scene===scene) {
                renderer.scene=null;
            }
            try{scene?.dispose?.();
            } catch (_) {
            }
        }
    }

    static HydrateStagedImages(root=null, options={}) {
        const container=root||this.elements().results;
         if (!container) {
             return;
         }
        try{this.stagedImageObserver?.disconnect?.();
        } catch (_) {
        }
        this.stagedImageObserver=null;
        const generation=++this.stagedImageGeneration;
        this.stagedImageQueue=[];
        this.stagedImageActive=0;
        const images=Array.from(container.querySelectorAll('img[data-staged-resource]'));
        if (!images.length) {
            return;
        }
        const eager=!!options.eager;

        const pump=()=>{
            if (generation!==this.stagedImageGeneration) {
                return;
            }
            while(this.stagedImageActive<this.stagedImageConcurrency&&this.stagedImageQueue.length){
                const image=this.stagedImageQueue.shift();
                if (!image||!image.isConnected||image.dataset.stagePending==='1'||image.dataset.stageReady==='1') {
                    continue;
                }
                image.dataset.stagePending='1';
                this.stagedImageActive+=1;
                const resource=String(image.dataset.stagedResource||'');
                const params={};
                if (image.dataset.stagedId) {
                    params.id=String(image.dataset.stagedId);
                }
                if (image.dataset.stagedPath) {
                    params.path=String(image.dataset.stagedPath);
                }
                if (resource === 'character-thumb') {
                    params.recipe = window.CIFAssetThumbnail.CharacterThumbnailRecipeFingerprint();
                }
                if (resource === 'saved-thumb' && image.dataset.thumbnailRecipe) {
                    params.recipe = image.dataset.thumbnailRecipe;
                }
                const staged=(resource==='objaverse-thumb')
                    ? CIFRuntime.assetCommand({operation:'stage',resource,...params}).then(payload=>{
                        const relative=String(payload?.url||'').trim();
                        return relative?CIFRuntime.presentationAssetUrl(`SVG/${relative}`):'';
                    })
                    : CIFRuntime.stagedAssetUrl(resource,params);
                staged.then(async url=>{
                    if (generation!==this.stagedImageGeneration||!image.isConnected) {
                        return;
                    }
                    if(!url && resource==='saved-thumb' && params.path){
                        url=await this.QueueSavedThumbnailGeneration(image,params.path);
                    } else if(!url && resource==='objaverse-thumb' && params.id){
                        url=await this.QueueObjaverseThumbnailGeneration(image,params.id);
                    } else if(!url && resource==='character-thumb' && params.id){
                        await this.QueueCharacterThumbnailGeneration(image.closest('.objaverse-card'));
                        url=await CIFRuntime.stagedAssetUrl('character-thumb',{id:params.id,recipe:window.CIFAssetThumbnail.CharacterThumbnailRecipeFingerprint()});
                    }
                    if (generation!==this.stagedImageGeneration||!image.isConnected) {
                        return;
                    }
                    if(url){image.src=url;
                     image.dataset.stageReady='1';
                     delete image.dataset.stageFailed;
                    }
                    else {image.dataset.stageFailed='1';
                     image.removeAttribute('src');
                    }
                }).catch(async error=>{
                    if (generation!==this.stagedImageGeneration) {
                        return;
                    }
                    let recovered='';
                    if (resource==='saved-thumb'&&params.path) {
                        recovered=await this.QueueSavedThumbnailGeneration(image,params.path);
                    }
                    else if(resource==='objaverse-thumb'&&params.id) recovered=await this.QueueObjaverseThumbnailGeneration(image,params.id);
                    else if(resource==='character-thumb'&&params.id){
                        try{
                            await this.QueueCharacterThumbnailGeneration(image.closest('.objaverse-card'));
                            recovered=await CIFRuntime.stagedAssetUrl('character-thumb',{id:params.id,recipe:window.CIFAssetThumbnail.CharacterThumbnailRecipeFingerprint()});
                        }catch(thumbnailError){
                            console.error('[IFPlayer Assets] character-thumbnail-recovery-failed',{id:params.id,error:String(thumbnailError?.message||thumbnailError)});
                        }
                    }
                    if(recovered && generation===this.stagedImageGeneration && image.isConnected){
                        image.src=recovered;
                         image.dataset.stageReady='1';
                         delete image.dataset.stageFailed;
                         return;
                    }
                    image.dataset.stageFailed='1';
                    if(resource!=='objaverse-thumb'&&resource!=='saved-thumb') console.error('[IFPlayer Assets] staged-image-failed',{resource,...params,error:String(error?.message||error)});
                }).finally(()=>{
                    delete image.dataset.stagePending;
                    if(generation===this.stagedImageGeneration){
                        this.stagedImageActive=Math.max(0,this.stagedImageActive-1);
                        pump();
                    }
                });
            }
        };
        const enqueue=image=>{
            if (generation!==this.stagedImageGeneration||!image||image.dataset.stageQueued==='1'||image.dataset.stageReady==='1') {
                return;
            }
            image.dataset.stageQueued='1';
            this.stagedImageQueue.push(image);
            pump();
        };

        // Search-result thumbnails are provider images, not rendered models.
        // Queue every returned result, but only allow a very small number of
        // staging/download requests to be active at once.  This prevents a
        // large Objaverse result set from hammering the provider/runtime while
        // still progressively filling the result grid.
        if(eager){images.forEach(enqueue);
        return;
        }
        // Non-search views remain viewport-staged to avoid unnecessary local IO.
        if(typeof IntersectionObserver!=='function'){images.slice(0,12).forEach(enqueue);
        return;
        }
        this.stagedImageObserver=new IntersectionObserver(entries=>entries.forEach(entry=>{
            if (!entry.isIntersecting) {
                return;
            }
            this.stagedImageObserver?.unobserve?.(entry.target);
            enqueue(entry.target);
        }),{root:container,rootMargin:'240px'});
        images.forEach(image=>this.stagedImageObserver.observe(image));
    }

    static SetupCharacterThumbnailGeneration() {
        try{this.characterThumbnailObserver?.disconnect?.();
        } catch (_) {
        }
        this.characterThumbnailObserver=null;
        if(this.characterThumbnailKickoff!==null){
            try{ if(typeof cancelIdleCallback==='function') cancelIdleCallback(this.characterThumbnailKickoff);
                else clearTimeout(this.characterThumbnailKickoff);
                } catch (_) {
                }
        }
        this.characterThumbnailKickoff=null;
        this.characterThumbnailQueue=[];
        this.characterThumbnailActive=0;
        const root=this.elements().results;
        const cards=Array.from(root?.querySelectorAll?.('.asset-character-card')||[]).filter(card=>!card.querySelector('img[data-staged-resource="character-thumb"]'));
        if (!cards.length) {
            return;
        }
        // Character and animation catalogs load concurrently. No selected Idle
        // here means the catalog has not arrived yet, not that every character
        // lacks Idle. LoadAnimationOptions restarts this queue after selecting it.
        if (!String(this.selectedDefaultAnimationId||'')) {
            return;
        }
        const queued=new Set();
        const pump=()=>{
            while(this.characterThumbnailActive<this.characterThumbnailConcurrency&&this.characterThumbnailQueue.length){
                const card=this.characterThumbnailQueue.shift();
                if (!card?.isConnected) {
                    continue;
                }
                const id=String(card.dataset.assetId||'');
                if (!id||card.dataset.thumbnailGenerating==='1') {
                    continue;
                }
                card.dataset.thumbnailGenerating='1';
                this.characterThumbnailActive+=1;
                this.QueueCharacterThumbnailGeneration(card).catch(error=>{
                    console.error('[IFPlayer Assets] character-thumbnail-generate-failed',{id,error:String(error?.message||error)});
                }).finally(()=>{
                    delete card.dataset.thumbnailGenerating;
                    this.characterThumbnailActive=Math.max(0,this.characterThumbnailActive-1);
                    pump();
                });
            }
        };
        const enqueue=card=>{
            const id=String(card?.dataset?.assetId||'');
            if (!id||queued.has(id)) {
                return;
            }
            queued.add(id);
            this.characterThumbnailQueue.push(card);
        };
        cards.forEach(enqueue);
        // Fill missing Mixamo thumbnails automatically, but deliberately use one
        // background worker so browsing never bulk-loads the entire character
        // catalog onto the GPU at once. Cached thumbnails are reused thereafter.
        const kickoff=()=>{this.characterThumbnailKickoff=null;
        pump();
        };
        if(typeof requestIdleCallback==='function') this.characterThumbnailKickoff=requestIdleCallback(kickoff,{timeout:750});
        else this.characterThumbnailKickoff=setTimeout(kickoff,180);
    }

    static DisposeCharacterThumbnailRenderer() {
        if(this.characterThumbnailRendererDisposeTimer!==null){
            clearTimeout(this.characterThumbnailRendererDisposeTimer);
            this.characterThumbnailRendererDisposeTimer=null;
        }
        const renderer=this.characterThumbnailRenderer;
        this.characterThumbnailRenderer=null;
        if (!renderer) {
            return;
        }
        try{renderer.scene?.dispose?.();
        } catch (_) {
        }
        try{renderer.engine?.stopRenderLoop?.();
        } catch (_) {
        }
        try{renderer.engine?.dispose?.();
        } catch (_) {
        }
        try{renderer.canvas?.remove?.();
        } catch (_) {
        }
        console.info('[IFPlayer Assets] character-thumbnail-renderer-disposed');
    }

    static async CharacterThumbnailRenderer() {
        if(this.characterThumbnailRendererDisposeTimer!==null){
            clearTimeout(this.characterThumbnailRendererDisposeTimer);
            this.characterThumbnailRendererDisposeTimer=null;
        }
        if (this.characterThumbnailRenderer) {
            return this.characterThumbnailRenderer;
        }
        await ensureBabylon();
        const canvas=document.createElement('canvas');
        canvas.width=320;
        canvas.height=320;
        canvas.style.cssText='position:fixed;left:-10000px;top:-10000px;width:320px;height:320px;pointer-events:none;opacity:0;';
        document.body.appendChild(canvas);
        const engine=new BABYLON.Engine(canvas,true,{preserveDrawingBuffer:true,stencil:false});
        this.characterThumbnailRenderer={canvas,engine,scene:null};
        console.info('[IFPlayer Assets] character-thumbnail-renderer-created');
        return this.characterThumbnailRenderer;
    }

    static QueueCharacterThumbnailGeneration(card) {
        const characterId=String(card?.dataset?.assetId||'');
        if (!characterId) {
            return Promise.resolve('');
        }
        const existing=this.characterThumbnailPending.get(characterId);
        if (existing) {
            return existing;
        }
        if(this.characterThumbnailRendererDisposeTimer!==null){clearTimeout(this.characterThumbnailRendererDisposeTimer);
            this.characterThumbnailRendererDisposeTimer=null;
            }
        // All callers share one serial producer and one reusable WebGL context.
        // Cards never create their own thumbnail Engine.
        const job=this.characterThumbnailSerial.catch(()=>{}).then(()=>this.GenerateCharacterSearchThumbnail(card));
        this.characterThumbnailPending.set(characterId,job);
        this.characterThumbnailSerial=job.finally(()=>{
            if (this.characterThumbnailPending.get(characterId)===job) {
                this.characterThumbnailPending.delete(characterId);
            }
            if(!this.characterThumbnailPending.size){
                this.characterThumbnailRendererDisposeTimer=setTimeout(()=>this.DisposeCharacterThumbnailRenderer(),1000);
            }
        });
        return job;
    }

    static async EnsureThumbnailSourceAvailable(kind, id, query, card=null) {
        await CIFRuntime.assetCommand({operation:'download',provider:'library',kind,id,q:query});
        return await new Promise((resolve,reject)=>{
            const check=async()=>{
                try {
                    const state=await CIFRuntime.assetCommand({operation:'status',provider:'library',kind,id});
                    if (card) {
                        this.SetProgress(card,state.message||state.stage||'Working…');
                    }
                    if (!state.complete) {
                        return;
                    }
                    clearInterval(timer);
                    if (state.error) {
                        reject(new Error(state.error));
                        return;
                    }
                    resolve(state);
                }
                catch(error) {
                    clearInterval(timer);
                    reject(error);
                }
            };
            const timer=setInterval(check,650);
            check();
        });
    }

    static async GenerateCharacterSearchThumbnail(card) {
        const characterId = String(card?.dataset?.assetId || '');
        if (!characterId) {
            return;
        }
        const row = this.mixamoCharacters.find(candidate => String(candidate.id || '') === characterId);
        const recipe = window.CIFAssetThumbnail.CHARACTER_THUMBNAIL_RECIPE;
        const recipeFingerprint = window.CIFAssetThumbnail.CharacterThumbnailRecipeFingerprint();
        const animationRows = window.CIFAssetThumbnail.StableCharacterAnimationRows.call(this, characterId);
        if (!animationRows.length) {
            throw new Error(`No local Mixamo animations are available for thumbnail: ${characterId}`);
        }

        const characterQuery = String(row?.name || row?.title || row?.file || characterId);
        await window.CIFAssetThumbnail.EnsureThumbnailSourceAvailable.call(this, '3d', characterId, characterQuery, card);

        let selectedAnimation = null;
        let selectedAnimationUrl = '';
        let lastAnimationError = null;
        for (const animationRow of animationRows) {
            const animationId = String(animationRow?.id || '');
            const animationQuery = String(animationRow?.title || animationRow?.file || animationId);
            try {
                await window.CIFAssetThumbnail.EnsureThumbnailSourceAvailable.call(this, 'animation', animationId, animationQuery, card);
                selectedAnimationUrl = await CIFRuntime.stagedAssetUrl('preview-model', { kind: 'animation', id: animationId });
                if (selectedAnimationUrl) {
                    selectedAnimation = animationRow;
                    break;
                }
            } catch (error) {
                lastAnimationError = error;
            }
        }
        if (!selectedAnimation || !selectedAnimationUrl) {
            throw lastAnimationError || new Error(`No local Mixamo animation could be staged for thumbnail: ${characterId}`);
        }

        console.info('[IFPlayer Assets] character-thumbnail-producer-start', {
            id: characterId,
            animation_id: String(selectedAnimation.id || ''),
            recipe: recipeFingerprint,
        });

        const renderer = await this.CharacterThumbnailRenderer();
        try {
            renderer.scene?.dispose?.();
        } catch (_) {
        }
        const scene = new BABYLON.Scene(renderer.engine);
        renderer.scene = scene;
        scene.clearColor = new BABYLON.Color4(...recipe.background);
        const camera = new BABYLON.ArcRotateCamera(
            'thumbnail-camera',
            recipe.camera.alpha,
            recipe.camera.beta,
            recipe.camera.radius,
            BABYLON.Vector3.Zero(),
            scene,
        );
        const light = new BABYLON.HemisphericLight(
            'thumbnail-light',
            new BABYLON.Vector3(...recipe.hemisphericLight.direction),
            scene,
        );
        light.intensity = recipe.hemisphericLight.intensity;
        light.groundColor = new BABYLON.Color3(.6, .6, .6);
        const keyLight = new BABYLON.DirectionalLight(
            'thumbnail-key-light',
            new BABYLON.Vector3(-.45, -1, .65),
            scene,
        );
        keyLight.intensity = 1.45;
        const fillLight = new BABYLON.PointLight(
            'thumbnail-fill-light',
            new BABYLON.Vector3(2.5, 3, -2.5),
            scene,
        );
        fillLight.intensity = .95;
        scene.ambientColor = new BABYLON.Color3(.42, .42, .42);
        scene.imageProcessingConfiguration.exposure = 1.3;

        const before = new Set(scene.getNodes());
        const characterUrl = await CIFRuntime.stagedAssetUrl('preview-model', { kind: '3d', id: characterId });
        const character = await BABYLON.SceneLoader.ImportMeshAsync('', '', characterUrl, scene, undefined, '.glb');
        const characterNodes = scene.getNodes().filter(node => !before.has(node));
        (character.skeletons || []).forEach(skeleton => {
            try {
                skeleton.returnToRest();
            } catch (_) {
            }
        });

        const groupsBefore = new Set(scene.animationGroups);
        const sourceBefore = new Set(scene.getNodes());
        await BABYLON.SceneLoader.ImportMeshAsync('', '', selectedAnimationUrl, scene, undefined, '.glb');
        const sourceNodes = scene.getNodes().filter(node => !sourceBefore.has(node));
        const sourceGroups = scene.animationGroups.filter(group => !groupsBefore.has(group));
        const retarget = this.RetargetAnimation(
            scene,
            character,
            characterNodes,
            sourceNodes,
            sourceGroups,
            `thumbnail-${characterId}`,
        );
        const playing = retarget.playing;
        playing.start(false, 1, playing.from, playing.to, false);
        const poseFrame = playing.from + ((playing.to - playing.from) * recipe.poseFraction);

        // Babylon does not necessarily evaluate a newly-started AnimationGroup until
        // the scene has rendered.  Render once while it is live, seek to the recipe
        // frame, then render that seek before pausing.  Capturing after pause without
        // this evaluation leaves the avatar in its bind/T-pose.
        renderer.engine.resize();
        scene.render();
        await new Promise(resolve => requestAnimationFrame(resolve));
        playing.goToFrame(poseFrame);
        scene.render();
        await new Promise(resolve => requestAnimationFrame(resolve));
        scene.render();
        playing.pause();

        this.FrameScene(scene, camera);
        try {
            await scene.whenReadyAsync?.();
        } catch (_) {
        }
        for (let frame = 0; frame < 2; frame += 1) {
            await new Promise(resolve => requestAnimationFrame(resolve));
            renderer.engine.resize();
            scene.render();
        }
        const dataUrl = renderer.canvas.toDataURL('image/jpeg', recipe.jpegQuality);
        if (!String(dataUrl || '').startsWith('data:image/')) {
            throw new Error(`Mixamo thumbnail capture failed: ${characterId}`);
        }
        console.info('[IFPlayer Assets] character-thumbnail-captured', { id: characterId, recipe: recipeFingerprint });
        const saved = await CIFRuntime.assetCommand({
            operation: 'save-character-thumbnail',
            id: characterId,
            data_url: dataUrl,
            recipe: recipeFingerprint,
            animation_id: String(selectedAnimation.id || ''),
        });
        const managedPath = String(saved?.managed_path || '').trim();
        if (!managedPath) {
            throw new Error(`Mixamo thumbnail save returned no managed path: ${characterId}`);
        }
        if (row) {
            row.has_thumbnail = true;
        }
        let image = card?.querySelector?.('img[data-staged-resource="character-thumb"]');
        if (!image && card) {
            image = document.createElement('img');
            image.className = 'objaverse-thumb';
            image.loading = 'lazy';
            image.dataset.stagedResource = 'character-thumb';
            image.dataset.stagedId = characterId;
            image.alt = String(row?.name || row?.title || 'Mixamo character');
            card.querySelector('.objaverse-visual')?.prepend(image);
        }
        if (image) {
            const localThumbnailUrl = await CIFRuntime.stagedAssetUrl('character-thumb', { id: characterId, recipe: recipeFingerprint });
            if (!localThumbnailUrl) {
                throw new Error(`Mixamo thumbnail staging returned no local URL: ${characterId}`);
            }
            image.src = localThumbnailUrl;
            image.dataset.stageReady = '1';
            delete image.dataset.stageFailed;
        }
        console.info('[IFPlayer Assets] character-thumbnail-saved-local', {
            id: characterId,
            managed_path: managedPath,
            recipe: recipeFingerprint,
        });
        try {
            scene.dispose();
        } catch (_) {
        }
        renderer.scene = null;
        return managedPath;
    }

    static async CacheCharacterThumbnail(characterId, canvas) {
        const row=this.mixamoCharacters.find(candidate=>String(candidate.id||'')===String(characterId||''));
        if (row?.has_thumbnail || !canvas?.toDataURL) {
            return;
        }
        try {
            await new Promise(resolve=>requestAnimationFrame(()=>resolve()));
            const dataUrl=canvas.toDataURL('image/jpeg',0.82);
            if (!String(dataUrl||'').startsWith('data:image/')) {
                return;
            }
            await CIFRuntime.assetCommand({operation:'save-character-thumbnail',id:characterId,data_url:dataUrl});
            if (row) {
                row.has_thumbnail=true;
            }
        } catch(error) {
            console.error('[IFPlayer Assets] character-thumbnail-save-failed',{id:characterId,error:String(error?.message||error)});
        }
    }

}

window.CIFAssetThumbnail = CIFAssetThumbnail;
console.info('[CIF Asset] CIFAssetThumbnail loaded', CIFAssetThumbnail.VERSION);

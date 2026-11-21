'use strict';

// Shared Blockify conversion, local artifact, rig, voxelization, and source-preparation authority.
class CIFBlockify {
    static BlockifyArtifactFile(version) {
        const direct = String(version?.blockify?.file || '').trim();
        if (direct) {
            return direct;
        }
        return String(version?.artifacts?.blockify?.file || '').trim();
    }

    static HasBlockifyArtifact(version) {
        return Boolean(this.BlockifyArtifactFile(version));
    }

    static AssetLoaderExtension(fileName) {
        const normalized = String(fileName || '').trim().toLowerCase();
        if (normalized.endsWith('.gltf')) {
            return '.gltf';
        }
        if (normalized.endsWith('.glb')) {
            return '.glb';
        }
        throw new Error(`Unsupported 3D asset format for local authoring: ${fileName || '(missing file)'}`);
    }

    static async PrepareCloudflareBlockify(card){
        const rel=String(card?.dataset.directCloudflarePath||'').replace(/^\/+/, '');
        const canvas=card?.querySelector('.saved-asset-canvas');
            if (!rel||!canvas) {
                throw new Error('Cloudflare 3D asset is unavailable.');
            }
        const dimensions=this.BlockifyDimensions(card);
            this.SetProgress(card,`Preparing Minecraft model at ${dimensions.width}×${dimensions.height}×${dimensions.depth} blocks…`);
        await ensureBabylon();
            const engine=new BABYLON.Engine(canvas,true,{preserveDrawingBuffer:true,stencil:false}),scene=new BABYLON.Scene(engine);
        try{
            const before=new Set(scene.getNodes()), imported=await BABYLON.SceneLoader.ImportMeshAsync('','',CIFRuntime.requestAsset(rel),scene,undefined,'.glb');
                await scene.whenReadyAsync?.();
            const nodes=scene.getNodes().filter(n=>!before.has(n));
             this.NormalizeStaticPreviewUp(scene,nodes);
            const result=await this.VoxelizeBlockifyScene(scene,dimensions,'frame0');
            return {path:rel,...dimensions,resolution:16,voxels:result.voxels||[],product_name:String(card.querySelector('.objaverse-title')?.textContent||rel.split('/').pop()||'Asset').trim()};
        }finally{try{scene.dispose();
        }catch(_){}try{engine.dispose();
        } catch (_) {
            }
        }
    }

    static AnimationMotionKind(name){
        const text=String(name||'').toLowerCase().replace(/[^a-z0-9]+/g,' ');
        if (/\b(run|running|sprint|sprinting|jog|jogging)\b/.test(text)) {
            return 'run';
        }
        if (/\b(walk|walking)\b/.test(text)) {
            return 'walk';
        }
        return 'stationary';
    }

    static AnimationGroupSeconds(group){
        const targeted=(group?.targetedAnimations||[]).find(row=>row?.animation);
        const fps=Math.max(1,Number(targeted?.animation?.framePerSecond||30));
        return Math.max(.033,(Number(group?.to||0)-Number(group?.from||0))/fps||1);
    }

    static AnimationBehaviorEntry(group,name=''){
        const label=String(name||group?.name||'Animation');
        const kind=this.AnimationMotionKind(label),clipLength=this.AnimationGroupSeconds(group);
        return {group,name:label,kind,clipLength,cycles:(kind==='walk'||kind==='run')?2:1,holdSeconds:clipLength*((kind==='walk'||kind==='run')?2:1),speed:kind==='run'?1.65:(kind==='walk' ? .72 : 0)};
    }

    static ApplyAnimationBehaviorProfile(entries,profile){
        const rows=Array.isArray(profile)?profile:[];
        if (!rows.length) {
            return entries||[];
        }
        const normalize=value=>String(value||'').trim().toLowerCase();
        const byName=new Map(rows.map(row=>[normalize(row?.name),row]));
        return (entries||[]).map((entry,index)=>{
            const row=byName.get(normalize(entry?.name))||rows[index]||null;
            if (!row) {
                return entry;
            }
            const kind=['walk','run','stationary'].includes(String(row.kind||'').toLowerCase())?String(row.kind).toLowerCase():entry.kind;
            const cycles=Math.max(1,Number(row.cycles||entry.cycles||1));
            const speed=Number.isFinite(Number(row.speed))?Number(row.speed):Number(entry.speed||0);
            const holdSeconds=Math.max(.033,Number(row.hold_seconds||row.holdSeconds||entry.clipLength*cycles||1));
            return {...entry,kind,cycles,speed,holdSeconds};
        });
    }

    static BlockifyBehaviorSignature(folder,version){
        const assigned=(Array.isArray(folder?.assigned_animations)?folder.assigned_animations:[]).map(row=>({folder:String(row?.folder||''),file:String(row?.file||''),name:String(row?.name||'')}));
        if(!assigned.length&&folder?.default_animation?.folder&&folder?.default_animation?.file)assigned.push({folder:String(folder.default_animation.folder),file:String(folder.default_animation.file),name:'Default'});
        const embedded=Array.isArray(version?.inspection?.animation_names)?version.inspection.animation_names.map(String):[];
        return JSON.stringify({assigned,embedded,blockify_transform_version:'world-flat-authored-rest-bedrock-xyz-v4'});
    }

    static BlockifyWantsAnimation(folder,version){
        const path=String(folder?.folder||'');
        if (path.startsWith('Animated/Mixamo/')) {
            return true;
        }
        if (path.startsWith('Animated/Fixed/')) {
            return Array.isArray(version?.inspection?.animation_names)&&version.inspection.animation_names.length>0;
        }
        return false;
    }

    static BlockifyQuaternionToBedrockXYZ(q){
        // Geometry export negates X and Z, equivalent to a 180-degree Y basis
        // rotation. Convert the sampled quaternion into that basis first, then
        // decompose explicitly in Bedrock's X-then-Y-then-Z Euler order.
        let x=-Number(q?.x||0),y=Number(q?.y||0),z=-Number(q?.z||0),w=Number(q?.w??1);
        const n=Math.hypot(x,y,z,w)||1;
        x/=n;
        y/=n;
        z/=n;
        w/=n;
        const r00=1-2*(y*y+z*z),r10=2*(x*y+z*w),r20=2*(x*z-y*w);
        const r11=1-2*(x*x+z*z),r12=2*(y*z-x*w),r21=2*(y*z+x*w),r22=1-2*(x*x+y*y);
        const yy=Math.asin(Math.max(-1,Math.min(1,-r20))),cy=Math.cos(yy);
        let xx,zz;
        if(Math.abs(cy)>1e-7){xx=Math.atan2(r21,r22);
        zz=Math.atan2(r10,r00);
        }
        else {xx=Math.atan2(-r12,r11);
        zz=0;
        }
        const d=180/Math.PI;
        return [xx*d,yy*d,zz*d];
    }

    static SetupAnimationBehaviorCycle(card,scene,roots,entries,key,behaviorProfile=null,continuityAnchor=null){
        const usable=this.ApplyAnimationBehaviorProfile((entries||[]).filter(row=>row?.group?.targetedAnimations?.length),behaviorProfile);
            if (!usable.length) {
                return;
            }
        const motionRoot=new BABYLON.TransformNode(`asset-motion-${String(key).replace(/[^a-z0-9_-]+/gi,'-')}`,scene);
        for(const root of (roots||[])){try{root.setParent(motionRoot,true);
            }catch(_){try{root.parent=motionRoot;
            } catch (__) {
                }}
            }
        const prior=this.embeddedAnimationControllers.get(key);
        if(prior){try{prior.dispose?.();
        } catch (_) {
            }
        }
        const state={key,scene,entries:usable,motionRoot,current:null,manual:'',paused:false,beforeObserver:null,elapsed:0,lastName:'',turnTimer:0};
        const choose=()=>{
            // motionRoot is the sole owner of global X/Z locomotion.  Clip position
            // channels are normalized against their locomotion root before they are
            // attached to Babylon, so switching clips must never reposition this
            // wrapper.  In particular, do not use Hips as a continuity anchor: its
            // authored Y motion is pose animation (jump/crouch/bob), not world motion.
            usable.forEach(row=>{try{row.group.stop();}catch(_){}});
            let next=null;
            state.paused=false;
            if (state.manual) {
                next=usable.find(row=>row.name===state.manual)||usable[0];
            }
            else if(usable.length===1)next=usable[0];
            else {const choices=usable.filter(row=>row!==state.current);
                next=choices[Math.floor(Math.random()*choices.length)]||usable[0];
                }
            state.current=next;
            state.elapsed=0;
            state.turnTimer=0;
            state.lastName=next?.name||'Animation';
            if (!next) {
                return;
            }
            const loop=state.manual||next.kind==='walk'||next.kind==='run';
            try{
                next.group.start(loop,1,next.group.from,next.group.to,false);
                next.group.goToFrame?.(Number(next.group.from||0));
            }catch(_){return;
            }
            this.SetProgress(card,state.manual?`Playing ${state.lastName}`:`Auto animation \u00b7 ${state.lastName}${next.kind!=='stationary'?` \u00b7 ${next.kind}`:''}`);
        };
        state.beforeObserver=scene.onBeforeRenderObservable.add(()=>{
            const dt=Math.min(.1,Math.max(0,Number(scene.getEngine?.().getDeltaTime?.()||16)/1000));
                const row=state.current;
                if (!row||state.paused) {
                    return;
                }
            state.elapsed+=dt;
            state.turnTimer+=dt;
            if(row.kind==='walk'||row.kind==='run'){
                if(state.turnTimer>.9+Math.random()*1.4){state.turnTimer=0;
                    motionRoot.rotation.y+=((Math.random()-.5)*.9);
                    }
                if(motionRoot.position.length()>3.5){motionRoot.rotation.y=Math.atan2(-motionRoot.position.x,-motionRoot.position.z);
                    }
                const yaw=Number(motionRoot.rotation?.y||0),speed=Number(row.speed||0);
                    motionRoot.position.x+=Math.sin(yaw)*speed*dt;
                    motionRoot.position.z+=Math.cos(yaw)*speed*dt;
            }
            if (!state.manual&&state.elapsed>=Number(row.holdSeconds||row.clipLength||1)) {
                choose();
            }
        });
        state.select=name=>{state.manual=String(name||'');
        state.paused=false;
        choose();
        };
        state.pause=()=>{if(!state.current)return;
            state.paused=true;
            try{state.current.group.pause();
            }catch(_){try{state.current.group.stop();
            }catch(__){}}this.SetProgress(card,`Paused \u00b7 ${state.current.name||'Animation'}`);
            };
        state.dispose=()=>{try{scene.onBeforeRenderObservable.remove(state.beforeObserver);
            }catch(_){}usable.forEach(row=>{try{row.group.stop();
            }catch(_){}});
            try{motionRoot.dispose();
            }catch(_){}this.embeddedAnimationControllers.delete(key);
            };
        this.embeddedAnimationControllers.set(key,state);
            scene.onDisposeObservable?.addOnce?.(()=>{if(this.embeddedAnimationControllers.get(key)===state)this.embeddedAnimationControllers.delete(key);
            });
            choose();
    }

    static async ImageMinecraftDimensions(card){
        const image=card?.querySelector('.saved-image-source');
        if (!image) {
            throw new Error('Saved image source is unavailable.');
        }
        if(!image.complete||!image.naturalWidth||!image.naturalHeight){await new Promise((resolve,reject)=>{const done=()=>{cleanup();
            resolve();
            },fail=()=>{cleanup();
            reject(new Error('Saved image could not be loaded.'));
            },cleanup=()=>{image.removeEventListener('load',done);
            image.removeEventListener('error',fail);
            };
            image.addEventListener('load',done,{once:true});
            image.addEventListener('error',fail,{once:true});
            });
            }
        const height=Math.max(1,Math.min(64,Math.round(Number(card.querySelector('.image-minecraft-height')?.value||4))));
        const width=Math.max(1,Math.round(height*image.naturalWidth/image.naturalHeight));
        if (width>256) {
            throw new Error(`This aspect ratio is ${width} blocks wide at ${height} blocks high. Reduce height until width is 256 blocks or less.`);
        }
        const label=card.querySelector('.image-minecraft-derived-width');
        if(label)label.textContent=`W ${width}`;
        return {width,height,depth:1,image};
    }

    static async BuildImageMinecraftPayload(card){
        const {width,height,image}=await this.ImageMinecraftDimensions(card);
        // Image statues are block-resolution geometry with native Minecraft
        // 16x16 texels on every block face. H=4 therefore means a 4-block-high
        // statue carrying 64 vertical image texels -- not four flat-color pixels
        // and not 16x16x16 micro-voxels inside each block.
        const pixelsPerBlock=16,statueWidth=width*pixelsPerBlock,statueHeight=height*pixelsPerBlock;
        const statue=document.createElement('canvas');
        statue.width=statueWidth;
        statue.height=statueHeight;
        const sx=statue.getContext('2d',{willReadFrequently:true});
            sx.imageSmoothingEnabled=true;
            sx.imageSmoothingQuality='high';
            sx.clearRect(0,0,statueWidth,statueHeight);
            sx.drawImage(image,0,0,statueWidth,statueHeight);
        const imageData=sx.getImageData(0,0,statueWidth,statueHeight),data=imageData.data,pixels=[];
        for(let blockRow=0;blockRow<height;blockRow++)for(let blockX=0;blockX<width;blockX++){
            let visible=false,count=0,r=0,g=0,b=0,a=0;
            const y0=blockRow*pixelsPerBlock,x0=blockX*pixelsPerBlock;
            for(let py=0;py<pixelsPerBlock;py++)for(let px=0;px<pixelsPerBlock;px++){
                const i=((y0+py)*statueWidth+(x0+px))*4,alpha=data[i+3];
                if (alpha<16) {
                    continue;
                }
                visible=true;
                count++;
                r+=data[i];
                g+=data[i+1];
                b+=data[i+2];
                a+=alpha;
            }
            if (!visible) {
                continue;
            }
            const br=Math.round(r/count),bg=Math.round(g/count),bb=Math.round(b/count);
            // A statue cell is one full Minecraft block. Once a tile is present the
            // cube must remain opaque; source alpha must not punch holes through the
            // block and expose its rear/interior faces. Composite translucent source
            // texels over that tile's own average colour, while retaining all 16x16
            // source detail on the outward face. Picture/Wall keeps source alpha.
            for(let py=0;py<pixelsPerBlock;py++)for(let px=0;px<pixelsPerBlock;px++){
                const i=((y0+py)*statueWidth+(x0+px))*4,alpha=data[i+3]/255;
                data[i]=Math.round(data[i]*alpha+br*(1-alpha));
                data[i+1]=Math.round(data[i+1]*alpha+bg*(1-alpha));
                data[i+2]=Math.round(data[i+2]*alpha+bb*(1-alpha));
                data[i+3]=255;
            }
            pixels.push({x:blockX,y:height-1-blockRow,r:br,g:bg,b:bb,a:255});
        }
        sx.putImageData(imageData,0,0);
        const scale=Math.min(1,1024/image.naturalWidth,1024/image.naturalHeight),tw=Math.max(1,Math.round(image.naturalWidth*scale)),th=Math.max(1,Math.round(image.naturalHeight*scale));
        const texture=document.createElement('canvas');
            texture.width=tw;
            texture.height=th;
            const tx=texture.getContext('2d');
            tx.imageSmoothingEnabled=true;
            tx.imageSmoothingQuality='high';
            tx.clearRect(0,0,tw,th);
            tx.drawImage(image,0,0,tw,th);
        return {width,height,pixels,pixels_per_block:pixelsPerBlock,statue_texture_data_url:statue.toDataURL('image/png'),texture_data_url:texture.toDataURL('image/png')};
    }

    static async EnsureImageMinecraft(card){
        const folder=this.SavedFolderForCard(card),versions=folder?.versions||[],version=versions[Number(card?.dataset?.versionIndex||0)]||versions[0];
        if (!folder||!version?.file) {
            throw new Error('No saved image version is available.');
        }
        const dimensions=await this.ImageMinecraftDimensions(card),cached=version.blockify||{};
        if (cached.image_asset&&cached.image_texture_contract==='opaque-block-v4'&&Number(cached.width||0)===dimensions.width&&Number(cached.height||0)===dimensions.height&&Number(cached.pixels_per_block||0)===16&&cached.file) {
            return {folder,version,meta:cached};
        }
        const payload=await this.BuildImageMinecraftPayload(card);
        this.SetProgress(card,`Building Minecraft image \u00b7 ${payload.width} \u00d7 ${payload.height} blocks \u00b7 ${payload.width*16} \u00d7 ${payload.height*16} statue texels\u2026`);
        const saved=await CIFRuntime.assetCommand({operation:'saved-image-minecraft-save',folder:folder.folder,file:version.file,...payload,product_name:String(card?.dataset?.productName||folder.name||version.file||'').trim()});
        version.blockify=saved.blockify||{};
        return {folder,version,meta:version.blockify};
    }

    static BlockifyDimensions(card){
        const value=axis=>Math.max(1,Math.min(64,Math.round(Number(card?.querySelector(`.asset-blockify-dimension[data-axis="${axis}"]`)?.value||1))));
        return {width:value('width'),height:value('height'),depth:value('depth')};
    }

    static async TexturePixels(texture){
        if (!texture||typeof texture.readPixels!=='function') {
            return null;
        }
        try{
            const size=texture.getSize?.()||{};
            const width=Math.max(1,Number(size.width||0)),height=Math.max(1,Number(size.height||0));
            const pixels=await texture.readPixels();
            if (!pixels||!width||!height) {
                return null;
            }
            return {pixels,width,height,invertY:Boolean(texture.invertY)};
        }catch(_){return null;
        }
    }

    static BlockifySetFrameZero(scene){
        // Statue sampling is exactly authored animation frame zero.
        for(const group of (scene?.animationGroups||[])){
            try{
                if (!group.isStarted) {
                    group.start(false,1,group.from,group.to,false);
                }
                group.goToFrame(0);
                group.pause();
            } catch (_) {
            }
        }
        try{scene.render();
        } catch (_) {
        }
    }

    static BlockifySetAPose(scene){
        // Put the source into its authored rest/bind pose first.  Do not try to
        // manufacture the A-pose by mutating Babylon bones here: for several
        // Objaverse/Mixamo imports those transforms did not propagate to the CPU
        // vertex positions that Blockify samples.  The actual A-pose deformation
        // is applied directly to the skinned world-space vertices below.
        for(const group of (scene?.animationGroups||[])){
            try{group.stop();
            } catch (_) {
            }
        }
        for(const skeleton of (scene?.skeletons||[])){
            try{skeleton.returnToRest();
            } catch (_) {
            }
            try{skeleton.prepare();
            } catch (_) {
            }
        }
        try{scene.render();
        } catch (_) {
        }
    }

    static BlockifyApplyAPoseToWorld(world,boneWeights,rigSource=null){
        if (!Array.isArray(world)||!world.length||!Array.isArray(boneWeights)||boneWeights.length!==world.length) {
            return world;
        }
        const result=world.map(p=>p.clone());
        const applySide=(armName,handName)=>{
            const candidates=[];
            for(let i=0;i<world.length;i++){
                const row=boneWeights[i]||{};
                let w=0;
                for(const [boneName,weight] of Object.entries(row)){
                    const canonical=this.BlockifyCanonicalBoneName(boneName);
                    if (canonical===armName||canonical===handName) {
                        w+=Number(weight||0);
                    }
                }
                if(w>=0.45)candidates.push({i,p:world[i],w});
            }
            if (candidates.length<8) {
                return;
            }
            // Use the source skeleton shoulder pivot when available so the
            // voxelized A-pose and the exported Bedrock bone pivots are derived
            // from the same source rig.  Only fall back to the inner arm surface
            // when the source skeleton did not expose a usable pivot.
            const rigBones=Array.isArray(rigSource?.bones)?rigSource.bones:[];
            const byName=new Map(rigBones.map(b=>[b.name,b]));
            const shoulder=rigBones.find(b=>{
                if (b.canonical!==armName||!b.position) {
                    return false;
                }
                const parent=byName.get(b.parent);
                 return !parent||parent.canonical==='body';
            })||rigBones.find(b=>b.canonical===armName&&b.position);
            const meanX=candidates.reduce((a,v)=>a+v.p.x,0)/candidates.length;
            const side=Math.sign(shoulder?.position?.x??meanX)||1;
            let pivot=null;
            if (shoulder?.position) {
                pivot=new BABYLON.Vector3(shoulder.position.x,shoulder.position.y,shoulder.position.z);
            }
            if(!pivot){
                const ordered=[...candidates].sort((a,b)=>side>0?a.p.x-b.p.x:b.p.x-a.p.x);
                const inner=ordered.slice(0,Math.max(4,Math.ceil(ordered.length*0.08)));
                pivot=new BABYLON.Vector3(
                    inner.reduce((a,v)=>a+v.p.x,0)/inner.length,
                    inner.reduce((a,v)=>a+v.p.y,0)/inner.length,
                    inner.reduce((a,v)=>a+v.p.z,0)/inner.length
                );
            }
            // Rotate the arm downward 30 degrees in world XY.  The sign is
            // determined from the arm's actual side, so this works regardless of
            // source left/right naming or character facing.
            const angle=side>0?-Math.PI/6:Math.PI/6;
            const c=Math.cos(angle),sn=Math.sin(angle);
            for(const {i,p,w} of candidates){
                const dx=p.x-pivot.x,dy=p.y-pivot.y;
                const rotated=new BABYLON.Vector3(
                    pivot.x+dx*c-dy*sn,
                    pivot.y+dx*sn+dy*c,
                    p.z
                );
                const influence=Math.max(0,Math.min(1,w));
                result[i]=BABYLON.Vector3.Lerp(p,rotated,influence);
            }
        };
        applySide('left_arm','left_hand');
        applySide('right_arm','right_hand');
        return result;
    }

    static BlockifyApplyAPoseToRig(rigSource){
        if (!rigSource?.bones?.length) {
            return rigSource;
        }
        const bones=rigSource.bones.map(b=>({...b,position:b.position?{...b.position}:null,apose_world_rotation:null}));
        const byName=new Map(bones.map(b=>[b.name,b]));
        const rotateFamily=(armCanonical,handCanonical)=>{
            const shoulder=bones.find(b=>b.canonical===armCanonical&&b.position&&(!byName.get(b.parent)||byName.get(b.parent)?.canonical==='body'))||bones.find(b=>b.canonical===armCanonical&&b.position);
            if (!shoulder?.position) {
                return;
            }
            const side=Math.sign(shoulder.position.x)||1,angle=side>0?-Math.PI/6:Math.PI/6,c=Math.cos(angle),sn=Math.sin(angle),p=shoulder.position;
            // The A-pose voxelizer rotates the whole arm family in WORLD space
            // around the shoulder.  Record the same world correction on every
            // member of that family.  Capture converts WORLD deltas back to LOCAL
            // hierarchy rotations, so the shared correction cancels between
            // parent/child bones instead of accumulating down the chain.
            const correction=[0,0,Math.sin(angle/2),Math.cos(angle/2)];
            for(const b of bones){
                if (!b.position||(b.canonical!==armCanonical&&b.canonical!==handCanonical)) {
                    continue;
                }
                b.apose_world_rotation=[...correction];
                const dx=b.position.x-p.x,dy=b.position.y-p.y;
                b.position={x:p.x+dx*c-dy*sn,y:p.y+dx*sn+dy*c,z:b.position.z};
            }
        };
        rotateFamily('left_arm','left_hand');
         rotateFamily('right_arm','right_hand');
        return {bones};
    }

    static BlockifyWorldPositions(mesh,positions){
        const count=Math.floor((positions?.length||0)/3),world=[],matrix=mesh.computeWorldMatrix(true);
        const skeleton=mesh.skeleton;
        const indices=mesh.getVerticesData?.(BABYLON.VertexBuffer.MatricesIndicesKind);
        const weights=mesh.getVerticesData?.(BABYLON.VertexBuffer.MatricesWeightsKind);
        const indicesExtra=mesh.getVerticesData?.(BABYLON.VertexBuffer.MatricesIndicesExtraKind);
        const weightsExtra=mesh.getVerticesData?.(BABYLON.VertexBuffer.MatricesWeightsExtraKind);
        let boneMatrices=null;
        const validBonePalette=Boolean(
            skeleton&&
            Array.isArray(skeleton.bones)&&
            skeleton.bones.length&&
            skeleton.bones.every(bone=>bone&&typeof bone.getFinalMatrix==='function')
        );
        if(validBonePalette&&indices?.length&&weights?.length){
            try{boneMatrices=skeleton.getTransformMatrices(mesh);
            } catch (_) {
            }
        }
        const transformByBone=(p,index)=>{
            const base=(Number(index)||0)*16;
            if (!boneMatrices||base+15>=boneMatrices.length) {
                return p;
            }
            const m=BABYLON.Matrix.FromArray(boneMatrices,base);
            return BABYLON.Vector3.TransformCoordinates(p,m);
        };
        for(let i=0;i<count;i++){
            const basePoint=new BABYLON.Vector3(positions[i*3],positions[i*3+1],positions[i*3+2]);
            let local=basePoint;
            if(boneMatrices){
                let sum=new BABYLON.Vector3(0,0,0),total=0;
                for(let k=0;k<4;k++){
                    const w=Number(weights[i*4+k]||0);
                    if (w<=0) {
                        continue;
                    }
                    sum.addInPlace(transformByBone(basePoint,indices[i*4+k]).scale(w));
                    total+=w;
                }
                if(indicesExtra?.length&&weightsExtra?.length){
                    for(let k=0;k<4;k++){
                        const w=Number(weightsExtra[i*4+k]||0);
                        if (w<=0) {
                            continue;
                        }
                        sum.addInPlace(transformByBone(basePoint,indicesExtra[i*4+k]).scale(w));
                        total+=w;
                    }
                }
                if (total>1e-9) {
                    local=sum.scale(1/total);
                }
            }
            world.push(BABYLON.Vector3.TransformCoordinates(local,matrix));
        }
        return world;
    }

    static BlockifyCanonicalBoneName(name){
        const raw=String(name||'').toLowerCase().replace(/[^a-z0-9]/g,'');
        if (!raw) {
            return null;
        }
        // Bone exporters do not agree on side syntax. Mixamo commonly uses
        // LeftArm/RightArm, while Blender/Rigify-style GLTFs commonly use
        // upper_arm.L / thigh.R. Punctuation is stripped above, so those become
        // upperarml / thighr. Recognize a trailing side marker only for known
        // bilateral anatomy; this avoids treating arbitrary names ending in l/r
        // as limbs.
        const bilateral=raw.includes('shoulder')||raw.includes('clavicle')||raw.includes('arm')||raw.includes('forearm')||raw.includes('hand')||raw.includes('wrist')||raw.includes('upleg')||raw.includes('thigh')||raw.includes('leg')||raw.includes('calf')||raw.includes('shin')||raw.includes('foot')||raw.includes('toe');
        const left=raw.includes('left')||raw.startsWith('l')||(bilateral&&raw.endsWith('l'));
        const right=raw.includes('right')||raw.startsWith('r')||(bilateral&&raw.endsWith('r'));
        if (raw.includes('head')||raw.includes('neck')) {
            return 'head';
        }
        // Preserve hands as distinct attachment targets.  A rigid club/axe parented
        // under a Mixamo hand/wrist must not be collapsed into the generic arm and
        // later reclassified by height as a leg.
        if (left&&(raw.includes('hand')||raw.includes('wrist'))) {
            return 'left_hand';
        }
        if (right&&(raw.includes('hand')||raw.includes('wrist'))) {
            return 'right_hand';
        }
        if (left&&(raw.includes('shoulder')||raw.includes('clavicle')||raw.includes('arm')||raw.includes('forearm'))) {
            return 'left_arm';
        }
        if (right&&(raw.includes('shoulder')||raw.includes('clavicle')||raw.includes('arm')||raw.includes('forearm'))) {
            return 'right_arm';
        }
        if (left&&(raw.includes('upleg')||raw.includes('thigh')||raw.includes('leg')||raw.includes('calf')||raw.includes('shin')||raw.includes('foot')||raw.includes('toe'))) {
            return 'left_leg';
        }
        if (right&&(raw.includes('upleg')||raw.includes('thigh')||raw.includes('leg')||raw.includes('calf')||raw.includes('shin')||raw.includes('foot')||raw.includes('toe'))) {
            return 'right_leg';
        }
        if (raw.includes('hips')||raw.includes('pelvis')||raw.includes('spine')||raw.includes('chest')||raw.includes('abdomen')||raw.includes('torso')||raw.includes('root')) {
            return 'body';
        }
        return null;
    }

    static BlockifyCanonicalBoneFromBone(bone){
        let current=bone,guard=0;
        while(current&&guard++<16){
            const mapped=this.BlockifyCanonicalBoneName(current.name||current.id);
            if (mapped) {
                return mapped;
            }
            try{current=current.getParent?.()||current.parent||null;
            }catch(_){current=null;
            }
        }
        return null;
    }

    static BlockifyCanonicalBoneFromNode(node){
        let current=node,guard=0;
        while(current&&guard++<16){
            const mapped=this.BlockifyCanonicalBoneName(current.name||current.id);
            if (mapped) {
                return mapped;
            }
            current=current.parent||null;
        }
        return null;
    }

    static BlockifyBoneKey(name,index=0){
        const raw=String(name||`bone_${index}`).trim().replace(/^mixamorig[:_]?/i,'');
        const safe=raw.replace(/[^A-Za-z0-9_]/g,'_').replace(/^_+|_+$/g,'');
        return safe||`bone_${index}`;
    }

    static BlockifyVertexBoneWeights(mesh,count){
        const skeleton=mesh?.skeleton;
        if (!skeleton||!count) {
            return null;
        }
        const indices=mesh.getVerticesData?.(BABYLON.VertexBuffer.MatricesIndicesKind);
        const weights=mesh.getVerticesData?.(BABYLON.VertexBuffer.MatricesWeightsKind);
        const indicesExtra=mesh.getVerticesData?.(BABYLON.VertexBuffer.MatricesIndicesExtraKind);
        const weightsExtra=mesh.getVerticesData?.(BABYLON.VertexBuffer.MatricesWeightsExtraKind);
        if (!indices?.length||!weights?.length) {
            return null;
        }
        const indexToBone=new Map();
        for(const [arrayIndex,bone] of (skeleton.bones||[]).entries()){
            let paletteIndex=null;
            try{paletteIndex=bone.getIndex?.();
            } catch (_) {
            }
            if(Number.isInteger(paletteIndex)&&paletteIndex>=0)indexToBone.set(paletteIndex,{bone,arrayIndex});
            if(!indexToBone.has(arrayIndex))indexToBone.set(arrayIndex,{bone,arrayIndex});
        }
        const rows=new Array(count);
        for(let i=0;i<count;i++){
            const row={};
            const add=(boneIndex,weight)=>{
                const w=Number(weight||0);
                if (w<=0) {
                    return;
                }
                const entry=indexToBone.get(Math.trunc(Number(boneIndex)));
                if (!entry?.bone) {
                    return;
                }
                const key=this.BlockifyBoneKey(entry.bone.name||entry.bone.id,entry.arrayIndex);
                row[key]=(row[key]||0)+w;
            };
            for(let k=0;k<4;k++)add(indices[i*4+k],weights[i*4+k]);
            if (indicesExtra?.length&&weightsExtra?.length) {
                for(let k=0;k<4;k++)add(indicesExtra[i*4+k],weightsExtra[i*4+k]);
            }
            rows[i]=row;
        }
        return rows;
    }

    static BlockifyRigSource(scene){
        // Some non-humanoid GLTFs carry a skeleton-like object whose entries are
        // transform nodes rather than Babylon Bone instances.  Babylon's
        // Skeleton.prepare() assumes every entry implements getFinalMatrix(), so
        // treating those assets as skinned rigs crashes generic Blockify.  A
        // Blockify rig is valid only when it is an actual Babylon bone palette.
        const skeleton=(scene?.skeletons||[]).find(s=>
            Array.isArray(s?.bones)&&
            s.bones.length&&
            s.bones.every(bone=>bone&&typeof bone.getFinalMatrix==='function')
        );
        if (!skeleton) {
            return null;
        }
        const mesh=(scene?.meshes||[]).find(m=>m?.skeleton===skeleton)||null;
        const bones=[];
        for(const [index,bone] of skeleton.bones.entries()){
            const key=this.BlockifyBoneKey(bone.name||bone.id,index);
            let parent=null;
            try{parent=bone.getParent?.()||bone.parent||null;
            } catch (_) {
            }
            let position=null;
            try{
                const p=bone.getAbsolutePosition?.(mesh)||bone.getPosition?.(BABYLON.Space.WORLD,mesh);
                if(p)position={x:Number(p.x),y:Number(p.y),z:Number(p.z)};
            } catch (_) {
            }
            bones.push({
                name:key,
                source_name:String(bone.name||bone.id||key),
                parent:parent?this.BlockifyBoneKey(parent.name||parent.id,(skeleton.bones||[]).indexOf(parent)):null,
                canonical:this.BlockifyCanonicalBoneFromBone(bone),
                position
            });
        }
        return {bones,skeleton};
    }

    static async VoxelizeBlockifyScene(scene,dimensions,pose='frame0',options={}){
        const rigPose=pose==='apose'||pose==='rigrest';
        if (rigPose) {
            this.BlockifySetAPose(scene);
        }
        else this.BlockifySetFrameZero(scene);
        const rawRigSource=rigPose?this.BlockifyRigSource(scene):null;
        // The world-space animation bake is relative to the authored rest pose.
        // Keep animated voxel geometry and pivots in that exact same rest frame;
        // moving only the voxel geometry into a synthetic A-pose creates joint gaps.
        const rigSource=pose==='apose'?this.BlockifyApplyAPoseToRig(rawRigSource):rawRigSource;
        const gx=dimensions.width*16,gy=dimensions.height*16,gz=dimensions.depth*16;
        const meshes=[];
        let minX=Infinity,minY=Infinity,minZ=Infinity,maxX=-Infinity,maxY=-Infinity,maxZ=-Infinity;
        for(const mesh of scene.meshes||[]){
            if (!mesh?.getVerticesData||mesh.isEnabled?.()===false||mesh.isVisible===false) {
                continue;
            }
            const positions=mesh.getVerticesData(BABYLON.VertexBuffer.PositionKind);
            if (!positions?.length) {
                continue;
            }
            const indices=mesh.getIndices?.()||null;
            const colors=mesh.getVerticesData(BABYLON.VertexBuffer.ColorKind);
            const uvs=mesh.getVerticesData(BABYLON.VertexBuffer.UVKind);
            const material=mesh.material;
            const texture=material?.albedoTexture||material?.diffuseTexture||material?.baseTexture||null;
            const textureData=await this.TexturePixels(texture);
            const base=material?.albedoColor||material?.diffuseColor||new BABYLON.Color3(1,1,1);
            const world=this.BlockifyWorldPositions(mesh,positions);
            // The source rig is authoritative for animated-character ownership.
            // Every skinned sample carries its exact source/Mixamo bone weights all
            // the way into the voxel result.  Rigid meshes parented under a source
            // bone (weapons/props are commonly parented under a hand) inherit that
            // bone directly.  There is no geometric body-part partition here.
            const parentBone=(()=>{const valid=new Set((rawRigSource?.bones||[]).map(b=>b.name));
                let n=mesh.parent,guard=0;
                while(n&&guard++<24){const key=this.BlockifyBoneKey(n.name||n.id,guard);
                if (valid.has(key)) {
                    return key;
                }
                n=n.parent||null;
                }return null;
                })();
            const hasSkin=Boolean(
                mesh.skeleton&&
                Array.isArray(mesh.skeleton.bones)&&
                mesh.skeleton.bones.length&&
                mesh.skeleton.bones.every(bone=>bone&&typeof bone.getFinalMatrix==='function')&&
                mesh.getVerticesData?.(BABYLON.VertexBuffer.MatricesIndicesKind)?.length&&
                mesh.getVerticesData?.(BABYLON.VertexBuffer.MatricesWeightsKind)?.length
            );
            const boneWeights=hasSkin?this.BlockifyVertexBoneWeights(mesh,Math.floor(positions.length/3)):null;
            const rigidBone=(!hasSkin&&parentBone)?parentBone:null;
            // Some authored character GLBs contain both the original static source
            // meshes and one or more skinned export meshes.  During rig voxelization
            // those static duplicates have no bone ownership and can dominate the
            // surface (the Fixed dwarf is one such asset).  Once a valid skin exists,
            // character geometry must come only from skinned meshes or rigid meshes
            // explicitly parented to a source bone.  The ordinary frame-0/static pass
            // still sees every visible mesh, so nothing is removed from the Solid or
            // statue representation.
            if(rigPose&&rawRigSource&&!hasSkin&&!rigidBone){
                continue;
            }
            // Exporters can also leave duplicate skinned copies driven by a second
            // equivalent skeleton.  BlockifyRigSource deliberately selects one
            // authoritative Babylon skeleton; do not mix another palette's bone
            // indices/names into that ownership map.
            if(rigPose&&hasSkin&&rawRigSource?.skeleton&&mesh.skeleton!==rawRigSource.skeleton){
                continue;
            }
            const posedWorld=(pose==='apose'&&boneWeights)?this.BlockifyApplyAPoseToWorld(world,boneWeights,rawRigSource):world;
            for(const p of posedWorld){
                minX=Math.min(minX,p.x);
                    minY=Math.min(minY,p.y);
                    minZ=Math.min(minZ,p.z);
                    maxX=Math.max(maxX,p.x);
                    maxY=Math.max(maxY,p.y);
                    maxZ=Math.max(maxZ,p.z);
            }
            meshes.push({world:posedWorld,indices,colors,uvs,textureData,base,rigidBone,boneWeights,hasSkin});
        }
        if (!meshes.length) {
            throw new Error('The GLB has no readable mesh vertices.');
        }
        const sx=maxX-minX,sy=maxY-minY,sz=maxZ-minZ;
        const scales=[];
            if (sx>1e-9) {
                scales.push((gx-1)/sx);
            }
            if (sy>1e-9) {
                scales.push((gy-1)/sy);
            }
            if (sz>1e-9) {
                scales.push((gz-1)/sz);
            }
        const scale=scales.length?Math.min(...scales):1,cx=(minX+maxX)/2,cy=(minY+maxY)/2,cz=(minZ+maxZ)/2;
        const buckets=new Map();
        const clamp=(v,a,b)=>Math.max(a,Math.min(b,v));
        const sampleColor=(m,i0,i1,i2,a,b,c)=>{
            if(m.colors&&m.colors.length>=Math.max(i0,i1,i2)*4+3){
                return [clamp(m.colors[i0*4]*a+m.colors[i1*4]*b+m.colors[i2*4]*c,0,1),clamp(m.colors[i0*4+1]*a+m.colors[i1*4+1]*b+m.colors[i2*4+1]*c,0,1),clamp(m.colors[i0*4+2]*a+m.colors[i1*4+2]*b+m.colors[i2*4+2]*c,0,1)];
            }
            if(m.textureData&&m.uvs&&m.uvs.length>=Math.max(i0,i1,i2)*2+2){
                let u=m.uvs[i0*2]*a+m.uvs[i1*2]*b+m.uvs[i2*2]*c,v=m.uvs[i0*2+1]*a+m.uvs[i1*2+1]*b+m.uvs[i2*2+1]*c;
                u=((u%1)+1)%1;
                v=((v%1)+1)%1;
                if (m.textureData.invertY) {
                    v=1-v;
                }
                const x=clamp(Math.round(u*(m.textureData.width-1)),0,m.textureData.width-1),y=clamp(Math.round(v*(m.textureData.height-1)),0,m.textureData.height-1),o=(y*m.textureData.width+x)*4,px=m.textureData.pixels;
                if (o+2<px.length) {
                    return [Number(px[o])/255,Number(px[o+1])/255,Number(px[o+2])/255];
                }
            }
            return [Number(m.base.r??1),Number(m.base.g??1),Number(m.base.b??1)];
        };
        const voxelPoint=p=>new BABYLON.Vector3((p.x-cx)*scale+(gx-1)/2,(p.y-cy)*scale+(gy-1)/2,(p.z-cz)*scale+(gz-1)/2);
        const addVoxelPoint=(vp,color,ownership)=>{
            const x=clamp(Math.round(vp.x),0,gx-1),y=clamp(Math.round(vp.y),0,gy-1),z=clamp(Math.round(vp.z),0,gz-1),key=`${x},${y},${z}`;
            let v=buckets.get(key);
                if(!v){v={x,y,z,r:0,g:0,b:0,n:0,bones:{}};
                buckets.set(key,v);
                }v.r+=color[0];
                v.g+=color[1];
                v.b+=color[2];
                v.n++;
            if(ownership?.bone){
                const amount=Math.max(0.0001,Number(ownership.weight||1));
                v.bones[ownership.bone]=(v.bones[ownership.bone]||0)+amount;
            }
        };
        const sampleOwnership=(m,i0,i1,i2,a,b,c)=>{
            if (m.rigidBone) {
                return {bone:m.rigidBone,weight:1};
            }
            if (!m.boneWeights) {
                return null;
            }
            const names=new Set([...Object.keys(m.boneWeights[i0]||{}),...Object.keys(m.boneWeights[i1]||{}),...Object.keys(m.boneWeights[i2]||{})]);
            let bestBone=null,best=-1,total=0;
            for(const name of names){
                const score=Number(m.boneWeights[i0]?.[name]||0)*a+Number(m.boneWeights[i1]?.[name]||0)*b+Number(m.boneWeights[i2]?.[name]||0)*c;
                total+=score;
                if(score>best){best=score;
                bestBone=name;
                }
            }
            return bestBone&&best>1e-6?{bone:bestBone,weight:best/Math.max(total,1e-9)}:null;
        };
        // Surface sampling is performed in the 1/16-block voxel grid. Small props
        // benefit from dense sub-voxel sampling, but using that same density for a
        // forty-block castle multiplies triangle work dramatically without adding
        // useful Minecraft detail. Scale the sampling interval with requested model
        // size while never exceeding one voxel. This keeps small-object fidelity and
        // makes architectural Blockify workloads tractable.
        const largestBlockDimension=Math.max(Number(dimensions.width||1),Number(dimensions.height||1),Number(dimensions.depth||1));
        const EDGE_THRESHOLD=largestBlockDimension<=16?.35:(largestBlockDimension<=32?.55:.85);
        const EDGE_SAMPLE_STEP=EDGE_THRESHOLD;
        const midpoint=(a,b)=>[(a[0]+b[0])*.5,(a[1]+b[1])*.5,(a[2]+b[2])*.5];
        const baryPoint=(p0,p1,p2,w)=>new BABYLON.Vector3(p0.x*w[0]+p1.x*w[1]+p2.x*w[2],p0.y*w[0]+p1.y*w[1]+p2.y*w[2],p0.z*w[0]+p1.z*w[1]+p2.z*w[2]);
        const rasterTriangle=(m,i0,i1,i2)=>{
            const p0=m.world[i0],p1=m.world[i1],p2=m.world[i2];
            if (!p0||!p1||!p2) {
                return;
            }
            const v0=voxelPoint(p0),v1=voxelPoint(p1),v2=voxelPoint(p2);
            const stack=[{v:[v0,v1,v2],w:[[1,0,0],[0,1,0],[0,0,1]],depth:0}];
            let leaves=0;
            while(stack.length){
                const t=stack.pop(),v=t.v,w=t.w;
                const d01=BABYLON.Vector3.Distance(v[0],v[1]),d12=BABYLON.Vector3.Distance(v[1],v[2]),d20=BABYLON.Vector3.Distance(v[2],v[0]);
                const longest=Math.max(d01,d12,d20);
                if(longest<=EDGE_THRESHOLD||t.depth>=24||leaves>500000){
                    for(let k=0;k<3;k++)addVoxelPoint(v[k],sampleColor(m,i0,i1,i2,w[k][0],w[k][1],w[k][2]),sampleOwnership(m,i0,i1,i2,w[k][0],w[k][1],w[k][2]));
                    leaves++;
                    continue;
                }
                let a=0,b=1,c=2;
                if(d12>=d01&&d12>=d20){a=1;
                b=2;
                c=0;
                }else if(d20>=d01&&d20>=d12){a=2;
                b=0;
                c=1;
                }
                const vm=v[a].add(v[b]).scale(.5),wm=midpoint(w[a],w[b]);
                stack.push({v:[v[a],vm,v[c]],w:[w[a],wm,w[c]],depth:t.depth+1});
                stack.push({v:[vm,v[b],v[c]],w:[wm,w[b],w[c]],depth:t.depth+1});
            }
            // Explicitly supersample all original triangle edges too.  Degenerate or
            // extremely thin GLB triangles can otherwise contribute almost no area;
            // edge DDA guarantees visual continuity for those authored members.
            const edges=[[v0,v1,[1,0,0],[0,1,0]],[v1,v2,[0,1,0],[0,0,1]],[v2,v0,[0,0,1],[1,0,0]]];
            for(const [a,b,wa,wb] of edges){
                const count=Math.max(1,Math.ceil(BABYLON.Vector3.Distance(a,b)/EDGE_SAMPLE_STEP));
                for(let n=0;n<=count;n++){
                    const q=n/count,v=BABYLON.Vector3.Lerp(a,b,q),w=[wa[0]*(1-q)+wb[0]*q,wa[1]*(1-q)+wb[1]*q,wa[2]*(1-q)+wb[2]*q];
                    addVoxelPoint(v,sampleColor(m,i0,i1,i2,w[0],w[1],w[2]),sampleOwnership(m,i0,i1,i2,w[0],w[1],w[2]));
                }
            }
        };
        for(const m of meshes){
            if(m.indices?.length>=3){for(let i=0;
                i+2<m.indices.length;
                i+=3)rasterTriangle(m,m.indices[i],m.indices[i+1],m.indices[i+2]);
                }
            else{for(let i=0;i+2<m.world.length;i+=3)rasterTriangle(m,i,i+1,i+2);
            }
        }
        const occupied=Array.from(buckets.values());
        if (!occupied.length) {
            throw new Error('Blockify produced no occupied voxels.');
        }
        // Normalize the persisted Blockify coordinate system itself, not merely
        // the camera: the lowest occupied voxel starts at Y=0.  X/Z remain in
        // the authored voxel grid and are centered from their occupied footprint
        // when rendered/exported.
        // Do not spread a large voxel array into Math.min/Math.max. A castle can
        // contain hundreds of thousands of occupied samples; spreading that many
        // arguments exceeds Safari's JavaScript call-stack/argument limit.
        let occupiedMinY=Infinity;
        for(const v of occupied)occupiedMinY=Math.min(occupiedMinY,Number(v.y));
        const resolved=occupied.map(v=>{
            let bone=null,best=0,total=0;
            for(const [name,score] of Object.entries(v.bones||{})){const n=Number(score||0);
                total+=n;
                if(n>best){bone=name;
                best=n;
                }}
            return {x:v.x,y:v.y-occupiedMinY,z:v.z,r:Math.round((v.r/v.n)*255),g:Math.round((v.g/v.n)*255),b:Math.round((v.b/v.n)*255),samples:v.n,...(bone?{bone,bone_confidence:best/Math.max(total,1e-9)}:{})};
        });
        if(rigPose&&meshes.some(m=>m.hasSkin)){
            const counts={};
            let unresolved=0;
            for(const v of resolved){if(v.bone)counts[v.bone]=(counts[v.bone]||0)+1;
            else unresolved++;
            }
            const unresolvedRatio=resolved.length?unresolved/resolved.length:1;
            const canonCounts={};
            for(const b of (rigSource?.bones||[])){if(b.canonical)canonCounts[b.canonical]=(canonCounts[b.canonical]||0)+Number(counts[b.name]||0);
                }
            // Mixamo retargeted characters require the canonical humanoid limbs.
            // Fixed/embedded assets may have arbitrary source rigs (trolls, creatures,
            // machinery, etc.); for those, exact skin ownership is sufficient and we
            // must not reject a valid rig merely because it is not humanoid.
            const required=options?.requireHumanoid?['left_arm','right_arm','left_leg','right_leg']:[];
            const missing=required.filter(name=>Number(canonCounts[name]||0)<8);
            if(missing.length||unresolvedRatio>0.08){
                throw new Error(`Blockify source-rig mapping failed: missing ${missing.join(', ')||'none'}; unresolved ${(unresolvedRatio*100).toFixed(1)}%. Refusing geometric fallback.`);
            }
            const rig={source_to_voxel_scale:scale,bones:(rigSource?.bones||[]).map(b=>{
                let pivot=null;
                if(b.position){const vp=voxelPoint(new BABYLON.Vector3(b.position.x,b.position.y,b.position.z));
                    pivot=[vp.x,vp.y-occupiedMinY,vp.z];
                    }
                return {...b,pivot};
            })};
            console.info('[IFPlayer Assets] blockify-source-rig',{canonCounts,unresolved,total:resolved.length,bones:rig.bones.length});
            return {
                voxels:resolved,
                rig,
                source_to_voxel_scale:scale,
                source_bounds:{width:sx,height:sy,depth:sz},
                source_center:{x:cx,y:cy,z:cz},
                voxel_y_offset:occupiedMinY
            };
        }
        return {
            voxels:resolved,
            rig:null,
            source_to_voxel_scale:scale,
            source_bounds:{width:sx,height:sy,depth:sz},
            source_center:{x:cx,y:cy,z:cz},
            voxel_y_offset:occupiedMinY
        };
    }

    static BlockifyTargetWorldMatrix(target){
        const B=window.BABYLON;
        if (!B||!target) {
            return null;
        }
        let node=target;
        try{node=target.getTransformNode?.()||target;
        } catch (_) {
        }
        try{
            const matrix=node.computeWorldMatrix?.(true)||node.getWorldMatrix?.()||target.getAbsoluteTransform?.();
            if (matrix?.clone) {
                return matrix.clone();
            }
        } catch (_) {
        }
        try{
            const q=target.getRotationQuaternion?.(B.Space.WORLD);
            if (q) {
                return B.Matrix.Compose(B.Vector3.One(),q,B.Vector3.Zero());
            }
        } catch (_) {
        }
        try{
            if (node.rotationQuaternion) {
                return B.Matrix.Compose(B.Vector3.One(),node.rotationQuaternion,B.Vector3.Zero());
            }
            if (node.rotation) {
                return B.Matrix.Compose(B.Vector3.One(),B.Quaternion.FromEulerAngles(node.rotation.x||0,node.rotation.y||0,node.rotation.z||0),B.Vector3.Zero());
            }
        } catch (_) {
        }
        return null;
    }

    static BlockifyRotationMatrix(matrix){
        const B=window.BABYLON;
        if (!B||!matrix) {
            return null;
        }
        try{
            const scale=new B.Vector3(),rotation=new B.Quaternion(),translation=new B.Vector3();
            if (matrix.decompose?.(scale,rotation,translation)===false) {
                return null;
            }
            return B.Matrix.Compose(B.Vector3.One(),rotation.normalize(),B.Vector3.Zero());
        }catch(_){return null;
        }
    }

    static BlockifyTargetWorldQuaternion(target){
        const B=window.BABYLON,matrix=this.BlockifyTargetWorldMatrix(target);
        if (!B||!matrix) {
            return null;
        }
        try{
            const scale=new B.Vector3(),rotation=new B.Quaternion(),translation=new B.Vector3();
            if (matrix.decompose?.(scale,rotation,translation)!==false) {
                return rotation.normalize();
            }
        } catch (_) {
        }
        return null;
    }

    static BlockifyRestRotationMap(character,characterNodes){
        const B=window.BABYLON;
        if (!B) {
            throw new Error('Babylon.js is unavailable.');
        }
        // Capture both LOCAL and WORLD authored-rest rotations.  The voxel rig is
        // built with world-aligned TransformNodes, so animation export must be
        // derived from sampled WORLD pose deltas and only then converted back into
        // the voxel hierarchy's LOCAL rotations.  Reusing source-bone local Euler
        // deltas directly is incorrect whenever a bone's authored axes are rotated.
        (character?.skeletons||[]).forEach(s=>{try{s.returnToRest();s.prepare?.();}catch(_){}});
        const map=new Map(),remember=target=>{
            if (!target) {
                return;
            }
            const n=this.NormalizeRigName(target.name||target.id);
            if (!n||map.has(n)) {
                return;
            }
            let localQ=null;
            try{if(target.rotationQuaternion)localQ=target.rotationQuaternion.clone();
            } catch (_) {
            }
            try{if(!localQ&&target.rotation)localQ=B.Quaternion.FromEulerAngles(target.rotation.x||0,target.rotation.y||0,target.rotation.z||0);
                } catch (_) {
                }
            try{if(!localQ&&typeof target.getRotationQuaternion==='function')localQ=target.getRotationQuaternion(B.Space.LOCAL);
                } catch (_) {
                }
            const worldM=this.BlockifyTargetWorldMatrix(target),worldQ=worldM?this.BlockifyTargetWorldQuaternion(target):null;
            let worldP=null;
            try{
                if(worldM){const scale=new B.Vector3(),rotation=new B.Quaternion(),translation=new B.Vector3();
                    if (worldM.decompose?.(scale,rotation,translation)!==false) {
                        worldP=translation.clone();
                    }
                    }
            } catch (_) {
            }
            if(localQ||worldQ||worldP||worldM)map.set(n,{target,localQ:localQ||B.Quaternion.Identity(),worldQ:worldQ||localQ||B.Quaternion.Identity(),worldM:worldM?.clone?.()||null,worldP:worldP||B.Vector3.Zero()});
        };
        characterNodes.forEach(remember);
        (character?.skeletons||[]).forEach(s=>(s.bones||[]).forEach(b=>{remember(b);
            try{remember(b.getTransformNode?.());
            }catch(_){}}));
        return map;
    }

    static BlockifyClipFromAnimationGroup(group,rig,rest,label,options={}){
        const B=window.BABYLON;
        if (!B) {
            throw new Error('Babylon.js is unavailable.');
        }
        if (!group?.targetedAnimations?.length) {
            if (options.allowExactEmbeddedRig) {
                console.info('[IFPlayer Assets] blockify-embedded-clip-rejected', {
                    group: String(group?.name || label || ''),
                    reason: 'no-targeted-animations'
                });
            }
            return null;
        }
        const rigBones=Array.isArray(rig?.bones)?rig.bones:[];
        if (options.allowExactEmbeddedRig) {
            console.info('[IFPlayer Assets] blockify-embedded-clip-discovered', {
                group: String(group.name || label || ''),
                from: Number(group.from || 0),
                to: Number(group.to || 0),
                targets: group.targetedAnimations.length,
                properties: Array.from(new Set(group.targetedAnimations.map(row => String(row?.animation?.targetProperty || '')))).filter(Boolean),
                targetNames: group.targetedAnimations.slice(0, 12).map(row => String(row?.target?.name || row?.target?.id || ''))
            });
        }
        const rigByName=new Map(rigBones.map(b=>[String(b.name||''),b]));
        const rigByNormalized=new Map();
        for(const b of rigBones)rigByNormalized.set(this.NormalizeRigName(b.source_name||b.name),b);

        // Require at least one actual rotation channel to prove that this clip
        // drives the destination rig, but sample the complete destination skeleton
        // below.  Sampling only the directly-keyed child misses inherited parent
        // rotation when converting WORLD pose back into the voxel hierarchy.
        let mappedTargets=0;
        for(const targeted of (group.targetedAnimations||[])){
            const anim=targeted?.animation,target=targeted?.target;
            if (!anim||!target) {
                continue;
            }
            const prop=String(anim.targetProperty||'').toLowerCase();
                if (prop!=='rotationquaternion'&&prop!=='rotation') {
                    continue;
                }
            const normalized=this.NormalizeRigName(target.name||target.id);
                if (rigByNormalized.has(normalized)) {
                    mappedTargets++;
                }
        }
        if (!mappedTargets && !options.allowExactEmbeddedRig) {
            return null;
        }

        // Fixed Animated assets own their embedded AnimationGroups and those groups
        // already drive this exact loaded character. Some authored GLBs target
        // transform nodes whose names intentionally differ from the normalized
        // voxel-rig source names, so name matching is not a valid compatibility
        // test for that self-contained case. The clip sampler below evaluates the
        // loaded skeleton's world transforms directly; permit that exact-rig path
        // without weakening external/retargeted animation validation.
        if (!mappedTargets && options.allowExactEmbeddedRig) {
            // Fixed Animated assets are authoritative self-contained clips.  Do not
            // require their Babylon AnimationGroup to expose a directly-keyed
            // rotation channel: authored GLBs may animate parent transform nodes,
            // translations, or other channels whose evaluated motion reaches the
            // skinned skeleton through the existing hierarchy.  The sampler below
            // reads the evaluated world transforms of the exact loaded rig, which is
            // the compatibility proof for this path.
            if (!(group.targetedAnimations || []).some(targeted => targeted?.animation && targeted?.target)) {
                return null;
            }
        }

        const rows=[];
        for(const bone of rigBones){
            const normalized=this.NormalizeRigName(bone.source_name||bone.name),restPose=rest.get(normalized);
            if (!restPose?.target||!restPose.worldM) {
                continue;
            }
            const restWorldM=this.BlockifyRotationMatrix(restPose.worldM);
            if (!restWorldM) {
                continue;
            }
            rows.push({bone,normalized,target:restPose.target,restWorldM});
        }
        if (!rows.length) {
            return null;
        }

        // Bake the evaluated destination skeleton into WORLD-SPACE bone deltas.
        // Every voxel bone is later parented directly under one neutral wrapper, so
        // there is no second hierarchy pass that can accidentally re-apply parent
        // rotation or reinterpret a Mixamo bind axis as a voxel-local axis.  This is
        // intentionally a rigid world-space bake: source parent motion is already
        // present in each sampled bone's world transform.  The synthetic A-pose is
        // the voxel character's authored rest pose, so identity source motion must
        // remain identity here rather than being cancelled by an arm correction.
        const sourceFps=Math.max(1,Number(group.targetedAnimations.find(row=>row?.animation)?.animation?.framePerSecond||30));
        const from=Number(group.from||0),to=Math.max(from,Number(group.to||from));
        const length=Math.max(.033,this.AnimationGroupSeconds(group));
        const sampleHz=30,sampleCount=Math.max(1,Math.ceil(length*sampleHz));
        const bones={};
        for(const row of rows)bones[row.bone.name]={rotation:{},rotation_quaternion:{},position:{}};
        const sourceToVoxelScale=Math.max(1e-9,Number(rig?.source_to_voxel_scale||1));
        const toDegrees=v=>Number(v||0)*180/Math.PI,round=v=>Math.round(Number(v||0)*1000)/1000,timeKey=t=>t.toFixed(3).replace(/0+$/,'').replace(/\.$/,'')||'0';
        const previousEuler=new Map(),unwrap=(value,previous)=>{if(!Number.isFinite(previous))return value;
            let out=value;
            while(out-previous>180)out-=360;
            while(out-previous<-180)out+=360;
            return out;
            };

        try{group.stop?.();
        group.start(false,1,from,to,false);
        group.pause?.();
        } catch (_) {
        }
        try{
            for(let sample=0;sample<=sampleCount;sample++){
                const t=Math.min(length,sample/sampleHz),frame=Math.min(to,from+t*sourceFps);
                try{group.goToFrame(frame);
                } catch (_) {
                }
                // Force matrices after goToFrame so inherited parent transforms are
                // reflected in every child before the world pose is sampled.
                for(const row of rows){try{row.target.computeWorldMatrix?.(true);
                } catch (_) {
                    }
                }
                for(const row of rows){
                    const currentFull=this.BlockifyTargetWorldMatrix(row.target),currentWorld=this.BlockifyRotationMatrix(currentFull)||row.restWorldM.clone();
                    // Babylon row-vector convention: restWorld * worldDelta = currentWorld.
                    // Because the voxel bones are flat under a neutral wrapper, this
                    // WORLD delta can be applied directly with no parent/local
                    // reconstruction and no bind-basis ambiguity.
                    const restInverse=row.restWorldM.clone();
                    restInverse.invert();
                    const worldDelta=restInverse.multiply(currentWorld),unitScale=new B.Vector3(),localQ=new B.Quaternion(),unitTranslation=new B.Vector3();
                    if (worldDelta.decompose?.(unitScale,localQ,unitTranslation)===false) {
                        continue;
                    }
                    localQ.normalize();
                    // Preserve the exact sampled parent-relative quaternion for the
                    // Babylon Block/Minecraft preview.  Do NOT round-trip through Euler
                    // angles there: Mixamo rigs commonly carry ~90-degree bind rotations
                    // (Pirate's Hips is one example), and Euler decomposition/interpolation
                    // can change rotation order or cross a gimbal boundary even though the
                    // sampled quaternion itself is correct.  The Euler channel remains only
                    // as the Bedrock-format representation used by .mcaddon export.
                    bones[row.bone.name].rotation_quaternion[timeKey(t)]=[round(localQ.x),round(localQ.y),round(localQ.z),round(localQ.w)];
                    const raw=this.BlockifyQuaternionToBedrockXYZ(localQ),prev=previousEuler.get(row.bone.name)||[NaN,NaN,NaN],continuous=raw.map((v,i)=>unwrap(v,prev[i]));
                    previousEuler.set(row.bone.name,continuous);
                    bones[row.bone.name].rotation[timeKey(t)]=continuous.map(round);
                    let currentP=null;
                    try{
                        const node=row.target.getTransformNode?.()||row.target;
                        const matrix=node.computeWorldMatrix?.(true)||node.getWorldMatrix?.()||row.target.getAbsoluteTransform?.();
                        if(matrix){const scale=new B.Vector3(),rotation=new B.Quaternion(),translation=new B.Vector3();
                            if (matrix.decompose?.(scale,rotation,translation)!==false) {
                                currentP=translation;
                            }
                            }
                    } catch (_) {
                    }
                    if(currentP){
                        const restP=rest.get(row.normalized)?.worldP||B.Vector3.Zero(),delta=currentP.subtract(restP).scale(sourceToVoxelScale);
                        // Persist every bone's sampled WORLD translation.  The flat
                        // voxel rig needs this inherited parent movement explicitly;
                        // otherwise a rotating shoulder would move the upper arm in
                        // the GLB but leave the voxel upper-arm pivot behind.
                        bones[row.bone.name].position[timeKey(t)]=[-round(delta.x),round(delta.y),-round(delta.z)];
                    }
                }
            }
        } finally {
            try{group.stop?.();
            } catch (_) {
            }
            // Blockify operates on a temporary scene, but restore the authored rest
            // state anyway so one captured clip cannot contaminate the next clip.
            for(const row of rows){
                try{const pose=rest.get(row.normalized);
                    if (row.target.rotationQuaternion&&pose?.localQ) {
                        row.target.rotationQuaternion.copyFrom(pose.localQ);
                    }
                    else if(row.target.rotation&&pose?.localQ){const e=pose.localQ.toEulerAngles();
                    row.target.rotation.copyFrom(e);
                    }}catch(_){}
            }
        }
        for(const [name,channels] of Object.entries(bones)){
            const rotationValues=Object.values(channels.rotation||{}),quaternionValues=Object.values(channels.rotation_quaternion||{}),positionValues=Object.values(channels.position||{});
            const meaningfulRotation=rotationValues.some(v=>Array.isArray(v)&&v.some(axis=>Math.abs(Number(axis||0))>0.001));
            const meaningfulQuaternion=quaternionValues.some(v=>Array.isArray(v)&&v.length>=4&&(Math.abs(Number(v[0]||0))>0.00001||Math.abs(Number(v[1]||0))>0.00001||Math.abs(Number(v[2]||0))>0.00001||Math.abs(Number(v[3]??1)-1)>0.00001));
            const meaningfulPosition=positionValues.some(v=>Array.isArray(v)&&v.some(axis=>Math.abs(Number(axis||0))>0.001));
            if (!meaningfulRotation) {
                delete channels.rotation;
            }
            if (!meaningfulQuaternion) {
                delete channels.rotation_quaternion;
            }
            if (!meaningfulPosition) {
                delete channels.position;
            }
            if (!channels.rotation&&!channels.rotation_quaternion&&!channels.position) {
                delete bones[name];
            }
        }
        if (!Object.keys(bones).length) {
            if (options.allowExactEmbeddedRig) {
                console.info('[IFPlayer Assets] blockify-embedded-clip-rejected', {
                    group: String(group.name || label || ''),
                    reason: 'evaluated-rig-did-not-move',
                    sampledRigBones: rows.length,
                    animationLength: length,
                    from,
                    to
                });
            }
            return null;
        }
        if (options.allowExactEmbeddedRig) {
            console.info('[IFPlayer Assets] blockify-embedded-clip-accepted', {
                group: String(group.name || label || ''),
                animatedRigBones: Object.keys(bones).length,
                sampledRigBones: rows.length,
                animationLength: length
            });
        }
        return {label:String(label||group.name||'Animation'),animation_length:length,bones,sample_rate:sampleHz,transform_space:'voxel_world_delta_flat'};
    }

    static async BlockifyCaptureMixamoClip(scene,character,characterNodes,rig,ref,label,{returnGroup=false}={}){
        const B=window.BABYLON;
        if (!B) {
            throw new Error('Babylon.js is unavailable.');
        }
        if (!ref?.folder || !ref?.file) {
            throw new Error(`Saved animation source is missing a local file: ${label}`);
        }
        const rest=this.BlockifyRestRotationMap(character,characterNodes),groupsBefore=new Set(scene.animationGroups),nodesBefore=new Set(scene.getNodes());
        // Both imported-rig and Blockify pose sources are local saved assets.
        // Never resolve motion through the retired Mixamo search index.
        const animationUrl = await CIFRuntime.stagedAssetUrl('saved-runtime-animation', {
            path: `${ref.folder}/${ref.file}`
        });
        const extension = String(ref.file).toLowerCase().endsWith('.gltf') ? '.gltf' : '.glb';
        await B.SceneLoader.ImportMeshAsync('', '', animationUrl, scene, undefined, extension);
        const sourceNodes=scene.getNodes().filter(n=>!nodesBefore.has(n)),sourceGroups=scene.animationGroups.filter(g=>!groupsBefore.has(g));
            const retarget=this.RetargetAnimation(scene,character,characterNodes,sourceNodes,sourceGroups,`blockify-${label}`),playing=retarget.playing;
        const clip=this.BlockifyClipFromAnimationGroup(playing,rig,rest,String(ref.name||ref.file||label));
            try{playing.stop();
            }catch(_){}if(!clip)throw new Error(`Mixamo ${label} produced no retargeted bone rotations for Blockify.`);
            return returnGroup?{clip,playing}:clip;
    }

    static async BlockifyApplyMixamoPose(scene, character, characterNodes, animationAssets, stableKey) {
        // Reuse motion in the local Assets library rather than the optional
        // downloaded Mixamo search index. Do not provision missing catalogs.
        const payload = await CIFRuntime.assetCommand({
            operation: 'saved-animation-candidates'
        });
        const candidates = (Array.isArray(payload?.results) ? payload.results : [])
            .filter(row => row?.folder && row?.file && row?.id);
        if (!candidates.length) {
            return null;
        }

        let hash = 2166136261;
        for (const characterCode of String(stableKey || '')) {
            hash ^= characterCode.charCodeAt(0);
            hash = Math.imul(hash, 16777619);
        }

        // Choose one deterministic saved animation for ordinary Blockify jobs.
        // If retargeting fails, report the real problem rather than guessing at
        // another source or silently completing without movement.
        const selectedIndex = (hash >>> 0) % candidates.length;
        const animationAsset = candidates[selectedIndex];
        const animationId = String(animationAsset.id);
        const animationName = String(animationAsset.name || animationAsset.file);

        const groupsBefore = new Set(scene.animationGroups);
        const nodesBefore = new Set(scene.getNodes());

        try {
            const animationUrl = await CIFRuntime.stagedAssetUrl('saved-runtime-animation', {
                path: `${animationAsset.folder}/${animationAsset.file}`
            });
            const extension = String(animationAsset.file).toLowerCase().endsWith('.gltf')
                ? '.gltf'
                : '.glb';
            await BABYLON.SceneLoader.ImportMeshAsync('', '', animationUrl, scene, undefined, extension);

            const sourceNodes = scene.getNodes().filter(node => !nodesBefore.has(node));
            const sourceGroups = scene.animationGroups.filter(group => !groupsBefore.has(group));
            const retarget = this.RetargetAnimation(
                scene,
                character,
                characterNodes,
                sourceNodes,
                sourceGroups,
                `blockify-pose-${animationName}`
            );

            const playing = retarget.playing;
            const poseFraction = 0.42;
            playing.start(false, 1, playing.from, playing.to, false);
            playing.goToFrame(playing.from + ((playing.to - playing.from) * poseFraction));
            playing.pause();

            for (const node of sourceNodes) {
                try {
                    node.setEnabled?.(false);
                } catch (_) {
                }
            }

            scene.render();
            await new Promise(resolve => requestAnimationFrame(resolve));
            scene.render();

            return {
                animation: animationName,
                animation_id: animationId,
                pose_fraction: poseFraction
            };
        } catch (error) {
            for (const group of scene.animationGroups.filter(group => !groupsBefore.has(group))) {
                try {
                    group.dispose?.();
                } catch (_) {
                }
            }
            for (const node of scene.getNodes().filter(node => !nodesBefore.has(node))) {
                try {
                    node.dispose?.();
                } catch (_) {
                }
            }
            throw new Error(`Mixamo pose animation ${animationName} (${animationId}) failed: ${error?.message || error}`);
        }
    }

    static BlockifyBehaviorProfile(clips){
        return Object.entries(clips||{}).map(([clip,definition])=>{const name=String(definition?.label||clip),kind=this.AnimationMotionKind(name),clipLength=Math.max(.033,Number(definition?.animation_length||1)),cycles=(kind==='walk'||kind==='run')?2:1;
            return {clip,name,kind,clip_length:clipLength,cycles,hold_seconds:clipLength*cycles,speed:kind==='run'?1.65:(kind==='walk' ? .72 : 0)};
            });
    }

    static async BlockifySavedAsset(card){
        const folder=this.SavedFolderForCard(card),canvas=card?.querySelector('.saved-asset-canvas');
            if (!folder||!canvas) {
                return;
            }
            const versions=folder.versions||[],version=versions[Number(card.dataset.versionIndex||0)]||versions[0];
            if (!version?.file) {
                throw new Error('No 3D asset version is available.');
            }
        let dimensions=this.BlockifyDimensions(card);
        const rel=`${folder.folder}/${version.file}`,isAnimated=this.BlockifyWantsAnimation(folder,version),behaviorSignature=this.BlockifyBehaviorSignature(folder,version);
        // Bedrock block geometry is always authored in the canonical 16×16×16 voxel space.
        // W/H/D select the asset's size in blocks; resolution is not an authoring control.
        const requestedResolution=16;
        let existingBlockify=version.blockify||{};
        const localPath=String(card?.dataset?.localAssetPath||'').replace(/\\/g,'/').replace(/^\/+/, '');
        if (localPath) {
            try {
                const response=await CIFRuntime.assetCommand({operation:'local-blockify-load',path:localPath});
                if (response?.blockify) {
                    existingBlockify=response.blockify;
                    version.blockify=existingBlockify;
                }
            } catch (error) {
                if (!String(error?.message||error).includes('Blockify artifact')) {
                    throw error;
                }
            }
        }
        const hasDimensionControls=Boolean(card?.querySelector('.asset-blockify-dimension'));
        if (!hasDimensionControls && existingBlockify?.file) {
            dimensions={
                width:Math.max(1,Number(existingBlockify.width||1)),
                height:Math.max(1,Number(existingBlockify.height||1)),
                depth:Math.max(1,Number(existingBlockify.depth||1)),
            };
        }
        const existingMatches=Boolean(existingBlockify?.file) &&
            Number(existingBlockify.width||0)===dimensions.width &&
            Number(existingBlockify.height||0)===dimensions.height &&
            Number(existingBlockify.depth||0)===dimensions.depth &&
            (!isAnimated||String(existingBlockify.behavior_signature||'')===behaviorSignature) &&
            existingBlockify.source_up_normalized===true;
        if (existingMatches) {
            const warning=`A Blockify representation already exists at ${dimensions.width}×${dimensions.height}×${dimensions.depth} blocks. Re-Blockifying will replace it and any Blockbench edits to that representation will be lost. Continue?`;
            if (!window.confirm(warning)) {
                this.SetProgress(card,'Blockify cancelled; existing representation was preserved.');
                return existingBlockify;
            }
        }
            this.SetProgress(card,`Blockifying ${folder.name||version.file} at ${dimensions.width}\u00d7${dimensions.height}\u00d7${dimensions.depth} blocks in Bedrock 16\u00d716\u00d716 voxel space\u2026`);
        await ensureBabylon();
            // Conversion does not need to replace the live preview canvas. Keep
            // the existing Solid view until the original voxel renderer is ready.
            // A failed model load/voxel save must not strand an empty canvas.
            const workCanvas=document.createElement('canvas');
            workCanvas.width=Math.max(1,canvas.width||canvas.clientWidth||512);
            workCanvas.height=Math.max(1,canvas.height||canvas.clientHeight||512);
            const engine=new BABYLON.Engine(workCanvas,true,{preserveDrawingBuffer:true,stencil:false}),scene=new BABYLON.Scene(engine);
        try{
            const url=await CIFRuntime.stagedAssetUrl('saved-file',{path:rel}),sourceExtension=this.AssetLoaderExtension(version.file),beforeCharacterNodes=new Set(scene.getNodes()),character=await BABYLON.SceneLoader.ImportMeshAsync('','',url,scene,undefined,sourceExtension);
                await scene.whenReadyAsync?.();
                const characterNodes=scene.getNodes().filter(n=>!beforeCharacterNodes.has(n));
                this.NormalizeStaticPreviewUp(scene,characterNodes);
            const embeddedGroups=[...(character.animationGroups||[])];
                const statueResult=await this.VoxelizeBlockifyScene(scene,dimensions,'frame0');
                let characterResult={voxels:[],rig:null},animationPayload={};
            if(isAnimated){characterResult=await this.VoxelizeBlockifyScene(scene,dimensions,'rigrest',{requireHumanoid:String(folder.folder||'').startsWith('Animated/Mixamo/')});
                if (!characterResult.rig?.bones?.length) {
                    throw new Error('Animated Blockify requires a real source rig; no source skeleton was found.');
                }
                if(String(folder.folder||'').startsWith('Animated/Mixamo/')){const refs=Array.isArray(folder.assigned_animations)?folder.assigned_animations.filter(row=>row?.folder&&row?.file):[];
                    if(!refs.length&&folder.default_animation?.folder&&folder.default_animation?.file)refs.push({...folder.default_animation,name:'Idle'});
                    for(let i=0;
                    i<refs.length;
                    i++){const key=`clip_${i+1}`,clip=await this.BlockifyCaptureMixamoClip(scene,character,characterNodes,characterResult.rig,refs[i],key);
                    if (clip) {
                        animationPayload[key]=clip;
                    }
                    }}
                else {const rest=this.BlockifyRestRotationMap(character,characterNodes);
                    let i=0;
                    for(const group of embeddedGroups){const clip=this.BlockifyClipFromAnimationGroup(group,characterResult.rig,rest,group.name);
                    if(clip)animationPayload[`clip_${++i}`]=clip;
                    }}
            }
            const assetUCI=String(card?.dataset?.assignmentUci||folder.uci||version.uci||'').trim();
            if (!assetUCI) {
                throw new Error('Blockify requires a UCI-native local asset identity.');
            }
            if (!statueResult.voxels.length) {
                throw new Error('Blockify produced no occupied voxels; source preview was preserved.');
            }
            const behaviorProfile=this.BlockifyBehaviorProfile(animationPayload),saved=await CIFRuntime.assetCommand({operation:'local-blockify-save',uci:assetUCI,...dimensions,resolution:requestedResolution,voxels:statueResult.voxels,character_voxels:characterResult.voxels,character_rig:characterResult.rig||{},character_animations:animationPayload,behavior_profile:behaviorProfile,behavior_signature:behaviorSignature,source_up_normalized:true,product_name:String(card?.dataset?.productName||folder.name||version.file||'').trim(),regenerate_base:true});
                version.blockify=saved.blockify||{...dimensions,resolution:requestedResolution,file:saved.file,behavior_signature:behaviorSignature};
            /* Re-Blockify changed the canonical managed Terrain asset. Reload any live Build instance immediately; the same canonical artifact is also what collaboration publishes, so every collaborator receives the identical base+authored-edit result. */
            const collaborationPaths=Array.isArray(saved.collaboration_asset_paths)?saved.collaboration_asset_paths:[];
            /* Re-Blockify is a local authored world change: keep the complete managed asset
               dependency set pending until Save World explicitly commits collaboration. */
            if(window.CIFMapTerrain){CIFMapTerrain.pendingCollaborationAssetPaths=CIFMapTerrain.pendingCollaborationAssetPaths||new Set();
                for(const path of collaborationPaths)CIFMapTerrain.pendingCollaborationAssetPaths.add(String(path));
                }
            if (window.CIFMapTerrain?.RefreshCollaborationAssets) {
                await window.CIFMapTerrain.RefreshCollaborationAssets(collaborationPaths);
            }
            if (window.CIFWorldStore?.markWorldDataDirty) {
                CIFWorldStore.markWorldDataDirty();
            }
            window.dispatchEvent(new CustomEvent('cinterforge-terrain-asset-changed',{detail:{folder:folder.folder,file:version.file,local:true}}));
            try{scene.dispose();
            }catch(_){}try{engine.dispose();
            } catch (_) {
            }
            if(isAnimated&&characterResult.voxels.length&&Object.keys(animationPayload).length)this.RenderAnimatedBlockifiedBehavior(card,characterResult.voxels,characterResult.rig,animationPayload,behaviorProfile,dimensions,`blockify:${rel}:${dimensions.width}x${dimensions.height}x${dimensions.depth}:animated`);
                else this.RenderBlockifiedVoxels(card,statueResult.voxels,dimensions,`blockify:${rel}:${dimensions.width}x${dimensions.height}x${dimensions.depth}`);
            this.SetProgress(card,isAnimated?`Blockified frame-0 statue ${statueResult.voxels.length.toLocaleString()} voxels + source-rig character ${characterResult.voxels.length.toLocaleString()} voxels + ${Object.keys(animationPayload).length} animations.`:`Blockified ${statueResult.voxels.length.toLocaleString()} occupied voxels at ${dimensions.width}\u00d7${dimensions.height}\u00d7${dimensions.depth} blocks.`);
            const button=card.querySelector('[data-action="saved-blockify"]');
                if(button){button.textContent='Re-Blockify';
                button.disabled=false;
                }
        }catch(error){try{scene.dispose();
        }catch(_){}try{engine.dispose();
        }catch(_){}
            // A failed voxel renderer may have disposed the previous canvas
            // owner. Best-effort restore the ORIGINAL Solid preview; do not
            // create a substitute viewer or mask the Blockify exception.
            const active=Array.from(this.engines?.values?.()||[]).some(e=>e?.getRenderingCanvas?.()===canvas);
            if(card.classList.contains('viewer-active')&&!active){
                try{await this.LoadSavedPreview(card);}
                catch(previewError){console.error('[IFPlayer Assets] Solid preview restore failed: '+String(previewError?.message||previewError));}
            }
            this.SetProgress(card,`Blockify failed: ${error?.message||error}. Solid preview left intact or restored where available.`);
            throw error;
        }
    }

    static async EnsureBlockified(card){
        const folder=this.SavedFolderForCard(card),versions=folder?.versions||[],version=versions[Number(card?.dataset?.versionIndex||0)]||versions[0];
            if (!folder||!version?.file) {
                throw new Error('No 3D asset version is available.');
            }
            const localPath=String(card?.dataset?.localAssetPath||'').replace(/\\/g,'/').replace(/^\/+/, '');
            if (localPath) {
                try {
                    const response=await CIFRuntime.assetCommand({operation:'local-blockify-load',path:localPath});
                    if (response?.blockify) {
                        version.blockify=response.blockify;
                        return version.blockify;
                    }
                } catch (error) {
                    if (!String(error?.message||error).includes('Blockify artifact')) {
                        throw error;
                    }
                }
            }
            const requested=this.BlockifyDimensions(card),cached=version.blockify||{},dimensionsMatch=Number(cached.width||0)===requested.width&&Number(cached.height||0)===requested.height&&Number(cached.depth||0)===requested.depth,isAnimated=this.BlockifyWantsAnimation(folder,version),behaviorSignature=this.BlockifyBehaviorSignature(folder,version),behaviorMatch=!isAnimated||String(cached.behavior_signature||'')===behaviorSignature,upnessCurrent=cached.source_up_normalized===true;
            if (!cached.file||!dimensionsMatch||!behaviorMatch||!upnessCurrent) {
                await this.BlockifySavedAsset(card);
            }
            return version.blockify;
    }

    static async PrepareSavedBlockifyPayload(card,purpose='Blockify'){
        const folder=this.SavedFolderForCard(card);
        const versions=folder?.versions||[];
        const version=versions[Number(card?.dataset?.versionIndex||0)]||versions[0];
        if (!folder||!version?.file) {
            throw new Error('Saved asset version is unavailable.');
        }

        const path=`${folder.folder}/${version.file}`.replace(/^\/+/, '');
        const uci=String(card?.dataset?.assignmentUci||version?.uci||folder?.uci||'').trim();
        if (!uci) {
            throw new Error('Saved asset UCI is unavailable.');
        }
        const dimensions=this.BlockifyDimensions(card);
        const productName=String(card?.dataset?.productName||folder.name||version.file||'Asset').trim();

        // Fresh local assignments are intentionally absent from the Cloudflare-backed
        // saved-list until Push. Ask the local manifest for an existing Blockify
        // artifact before regenerating from the source GLB, so Blockbench edits remain
        // authoritative across assignment re-renders.
        if (String(card?.dataset?.localAssetPath||'')) {
            try {
                const response=await CIFRuntime.assetCommand({operation:'local-blockify-load',uci});
                const blockify=response?.blockify||{};
                if (Array.isArray(blockify.voxels)) {
                    version.blockify=blockify;
                    return {
                        uci,
                        path,
                        width:Number(blockify.width||dimensions.width),
                        height:Number(blockify.height||dimensions.height),
                        depth:Number(blockify.depth||dimensions.depth),
                        resolution:Number(blockify.resolution||16),
                        voxels:blockify.voxels,
                        product_name:productName,
                    };
                }
            } catch (error) {
                if (!String(error?.message||error).includes('Blockify artifact')) {
                    throw error;
                }
            }
        }

        // A local Blockbench Save Back deliberately preserves the source GLB and
        // stores the edited representation as owned Blockbench + Blockify artifacts.
        // Previewing the GLB again here would therefore throw away the just-saved
        // editor result.  When Blockbench owns the current local edit, consume the
        // current local Blockify artifact read-only.  This is still a non-mutating
        // Preview path; Push remains the only publication boundary.
        if (version.blockify?.blockbench_edited===true) {
            this.SetProgress(card,`Preparing ${purpose} from local Blockbench edits…`);
            const response=await CIFRuntime.assetCommand({operation:'local-blockify-load',uci});
            const blockify=response?.blockify||{};
            return {
                uci,
                path,
                width:Number(blockify.width||version.blockify.width||dimensions.width),
                height:Number(blockify.height||version.blockify.height||dimensions.height),
                depth:Number(blockify.depth||version.blockify.depth||dimensions.depth),
                resolution:Number(blockify.resolution||version.blockify.resolution||16),
                voxels:Array.isArray(blockify.voxels)?blockify.voxels:[],
                product_name:productName,
            };
        }

        const canvas=card?.querySelector('.saved-asset-canvas');
        if (!canvas) {
            throw new Error('Saved asset preview canvas is unavailable.');
        }

        this.SetProgress(card,`Preparing ${purpose} at ${dimensions.width}×${dimensions.height}×${dimensions.depth} blocks…`);
        await ensureBabylon();
        if (!window.BABYLON) {
            throw new Error('Babylon.js is unavailable.');
        }

        const engine=new BABYLON.Engine(canvas,true,{preserveDrawingBuffer:true,stencil:false});
        const scene=new BABYLON.Scene(engine);
        try {
            const before=new Set(scene.getNodes());
            const sourceUrl=await CIFRuntime.stagedAssetUrl('saved-file',{path});
            await BABYLON.SceneLoader.ImportMeshAsync('','',sourceUrl,scene,undefined,this.AssetLoaderExtension(version.file));
            await scene.whenReadyAsync?.();
            const nodes=scene.getNodes().filter(node=>!before.has(node));
            this.NormalizeStaticPreviewUp(scene,nodes);
            const result=await this.VoxelizeBlockifyScene(scene,dimensions,'frame0');
            return {
                uci,
                path,
                ...dimensions,
                resolution:16,
                voxels:result.voxels||[],
                product_name:productName,
            };
        } finally {
            try { scene.dispose(); } catch (_) {}
            try { engine.dispose(); } catch (_) {}
        }
    }

}

console.info('[CIF Asset] CIFBlockify loaded');

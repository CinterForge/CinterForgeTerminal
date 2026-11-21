'use strict';

// Batch Blockify and migration/proof authority.
class CIFBlockifyBatch extends CIFBlockifyPreview {
    static async MigrationExportGltf(scene,name){
        await CIFAssetBrowser.EnsureBabylonSerializer();
        const exported=await BABYLON.GLTF2Export.GLTFAsync(scene,name,{shouldExportNode:node=>node&&node.isEnabled&&node.isEnabled()});
        const rows=[];
        for(const [filename,value] of Object.entries(exported.glTFFiles||{})){
            if(value instanceof Blob){
                const bytes=new Uint8Array(await value.arrayBuffer());
                let binary='';
                for(let offset=0;offset<bytes.length;offset+=0x8000) binary+=String.fromCharCode(...bytes.subarray(offset,offset+0x8000));
                rows.push({name:filename,encoding:'base64',data:btoa(binary)});
            }else rows.push({name:filename,encoding:'text',data:String(value)});
        }
        if(!rows.some(row=>String(row.name).toLowerCase().endsWith('.gltf'))) throw new Error(`${name} GLTF export produced no .gltf file.`);
        return rows;
    }

    // Embedded presentation belongs to the batch migration path. Keep this
    // helper with its caller so Runtime Session cannot load a newer batch class
    // that depends on a method from a stale base-class script.
    static BlockifyApplyEmbeddedPresentationPose(scene, character, label, poseFraction = 0.42) {
        const groups = (character?.animationGroups || []).filter(group =>
            group && Array.isArray(group.targetedAnimations) && group.targetedAnimations.length > 0
        );
        if (!groups.length) {
            return null;
        }

        // The embedded clip already targets this exact character rig, so it is the
        // strongest possible presentation-animation identity. No retargeting or
        // provider compatibility assumption belongs on this path.
        const group = groups[0];
        const from = Number.isFinite(Number(group.from)) ? Number(group.from) : 0;
        const to = Number.isFinite(Number(group.to)) ? Number(group.to) : from;
        if (!(to > from)) {
            return null;
        }

        for (const candidate of character.animationGroups || []) {
            try {
                candidate.stop();
            } catch (_) {
            }
        }
        for (const skeleton of character.skeletons || []) {
            try {
                skeleton.returnToRest();
            } catch (_) {
            }
            try {
                skeleton.prepare();
            } catch (_) {
            }
        }

        group.start(false, 1, from, to, false);
        group.goToFrame(from + ((to - from) * poseFraction));
        group.pause();
        scene.render();

        return {
            animation: String(group.name || `${label || 'character'} embedded animation`),
            animation_id: 'embedded',
            pose_fraction: poseFraction
        };
    }

    static MigrationThumbnail(canvas){
        // Keep thumbnail capture on the engine's existing canvas.  Babylon's
        // render-target screenshot helper aborts the native IFPlayer process on
        // the macOS WebGL/Metal path used by Blockify All.
        const data=canvas?.toDataURL?.('image/png');
        if(!String(data||'').startsWith('data:image/png;base64,')) throw new Error('Could not capture migration thumbnail.');
        return data;
    }

    static MigrationBlockScene(voxels,dimensions,engine=null,voxelWorldSize=1/16,sourceCenter=null,voxelYOffset=0){
        // Runtime Blockify is a render representation, not the editable voxel identity.
        // Export one vertex-colored mesh for the complete static asset instead of one
        // Babylon mesh per voxel. The 16x16x16 Bedrock voxel contract is unchanged.
        const B=window.BABYLON;
        const scene=new B.Scene(engine||new B.NullEngine());
        scene.clearColor=new B.Color4(.39,.68,.93,1);
        const positions=[],indices=[],normals=[],colors=[];
        const faces=[
            [[-1,-1, 1],[ 1,-1, 1],[ 1, 1, 1],[-1, 1, 1],[ 0, 0, 1]],
            [[ 1,-1,-1],[-1,-1,-1],[-1, 1,-1],[ 1, 1,-1],[ 0, 0,-1]],
            [[ 1,-1, 1],[ 1,-1,-1],[ 1, 1,-1],[ 1, 1, 1],[ 1, 0, 0]],
            [[-1,-1,-1],[-1,-1, 1],[-1, 1, 1],[-1, 1,-1],[-1, 0, 0]],
            [[-1, 1, 1],[ 1, 1, 1],[ 1, 1,-1],[-1, 1,-1],[ 0, 1, 0]],
            [[-1,-1,-1],[ 1,-1,-1],[ 1,-1, 1],[-1,-1, 1],[ 0,-1, 0]],
        ];
        const centerX=Number(dimensions.width||1)*8;
        const centerY=Number(dimensions.height||1)*8;
        const centerZ=Number(dimensions.depth||1)*8;
        const sourceX=Number(sourceCenter?.x||0);
        const sourceY=Number(sourceCenter?.y||0);
        const sourceZ=Number(sourceCenter?.z||0);
        const worldScale=Math.max(1e-9,Number(voxelWorldSize)||1/16);
        for(const voxel of voxels||[]){
            // Invert the exact source -> voxel coordinate-frame mapping used by
            // VoxelizeBlockifyScene.  Density changes, but the exported Block
            // representation stays in the Solid asset's authored local frame.
            const cx=(Number(voxel.x)+.5-centerX)*worldScale+sourceX;
            const cy=(Number(voxel.y)+Number(voxelYOffset||0)+.5-centerY)*worldScale+sourceY;
            const cz=(Number(voxel.z)+.5-centerZ)*worldScale+sourceZ;
            const r=Number(voxel.r||0)/255,g=Number(voxel.g||0)/255,b=Number(voxel.b||0)/255;
            for(const face of faces){
                const base=positions.length/3,n=face[4];
                for(let index=0;index<4;index++){
                    const q=face[index];
                    positions.push(cx+q[0]*.5*worldScale,cy+q[1]*.5*worldScale,cz+q[2]*.5*worldScale);
                    normals.push(n[0],n[1],n[2]);
                    colors.push(r,g,b,1);
                }
                indices.push(base,base+2,base+1,base,base+3,base+2);
            }
        }
        const mesh=new B.Mesh('BlockifiedStatic',scene);
        const data=new B.VertexData();
        data.positions=positions;data.indices=indices;data.normals=normals;data.colors=colors;
        data.applyToMesh(mesh,true);
        mesh.useVertexColors=true;
        const material=new B.StandardMaterial('BlockifiedStaticMaterial',scene);
        material.diffuseColor=new B.Color3(1,1,1);
        material.specularColor=B.Color3.Black();
        mesh.material=material;
        return scene;
    }

    static MigrationAnimatedBlockScene(voxels,rig,animations,dimensions,engine=null,voxelWorldSize=1/16,sourceCenter=null,voxelYOffset=0){
        const B=window.BABYLON;
        if(!B)throw new Error('Babylon.js is unavailable.');
        const occupied=Array.isArray(voxels)?voxels:[],bones=Array.isArray(rig?.bones)?rig.bones:[];
        if(!occupied.length||!bones.length)return this.MigrationBlockScene(occupied,dimensions,engine,voxelWorldSize,sourceCenter,voxelYOffset);
        const scene=new B.Scene(engine||new B.NullEngine());
        scene.clearColor=new B.Color4(.39,.68,.93,1);
        const skeleton=new B.Skeleton('BlockifiedRig','BlockifiedRig',scene),boneByName=new Map(),nodeByName=new Map();
        let minX=Infinity,maxX=-Infinity,minY=Infinity,minZ=Infinity,maxZ=-Infinity;
        for(const v of occupied){const x=Number(v.x),y=Number(v.y),z=Number(v.z);minX=Math.min(minX,x);maxX=Math.max(maxX,x);minY=Math.min(minY,y);minZ=Math.min(minZ,z);maxZ=Math.max(maxZ,z);}
        const centerX=Number(dimensions.width||1)*8-.5,centerY=Number(dimensions.height||1)*8-.5,centerZ=Number(dimensions.depth||1)*8-.5,scale=Math.max(1e-9,Number(voxelWorldSize)||1/16),half=scale/2;
        const sourceX=Number(sourceCenter?.x||0),sourceY=Number(sourceCenter?.y||0),sourceZ=Number(sourceCenter?.z||0);
        for(const definition of bones){
            const name=String(definition.name||'');if(!name)continue;
            const pivot=Array.isArray(definition.pivot)?definition.pivot:[centerX,minY,centerZ];
            const position=new B.Vector3((Number(pivot[0])-centerX)*scale+sourceX,(Number(pivot[1])+Number(voxelYOffset||0)-centerY)*scale+sourceY,(Number(pivot[2])-centerZ)*scale+sourceZ);
            const matrix=B.Matrix.Translation(position.x,position.y,position.z);
            const bone=new B.Bone(name,skeleton,null,matrix,matrix.clone());
            const node=new B.TransformNode(`block-bone-${name}`,scene);node.position.copyFrom(position);node.rotationQuaternion=B.Quaternion.Identity();bone.linkTransformNode(node);
            boneByName.set(name,bone);nodeByName.set(name,node);
        }
        const positions=[],indices=[],normals=[],colors=[],matrixIndices=[],matrixWeights=[];
        const faces=[[[ -1,-1,1],[1,-1,1],[1,1,1],[-1,1,1],[0,0,1]],[[1,-1,-1],[-1,-1,-1],[-1,1,-1],[1,1,-1],[0,0,-1]],[[1,-1,1],[1,-1,-1],[1,1,-1],[1,1,1],[1,0,0]],[[-1,-1,-1],[-1,-1,1],[-1,1,1],[-1,1,-1],[-1,0,0]],[[-1,1,1],[1,1,1],[1,1,-1],[-1,1,-1],[0,1,0]],[[-1,-1,-1],[1,-1,-1],[1,-1,1],[-1,-1,1],[0,-1,0]]];
        const boneIndex=new Map(skeleton.bones.map((bone,index)=>[bone.name,index]));
        for(const v of occupied){
            const index=boneIndex.get(String(v.bone||''));if(index===undefined)continue;
            const cx=(Number(v.x)-centerX)*scale+sourceX,cy=(Number(v.y)+Number(voxelYOffset||0)-centerY)*scale+sourceY,cz=(Number(v.z)-centerZ)*scale+sourceZ,r=Number(v.r||0)/255,g=Number(v.g||0)/255,b=Number(v.b||0)/255;
            for(const face of faces){const base=positions.length/3,n=face[4];for(let i=0;i<4;i++){const q=face[i];positions.push(cx+q[0]*half,cy+q[1]*half,cz+q[2]*half);normals.push(n[0],n[1],n[2]);colors.push(r,g,b,1);matrixIndices.push(index,0,0,0);matrixWeights.push(1,0,0,0);}indices.push(base,base+2,base+1,base,base+3,base+2);}
        }
        const mesh=new B.Mesh('BlockifiedCharacter',scene),data=new B.VertexData();data.positions=positions;data.indices=indices;data.normals=normals;data.colors=colors;data.matricesIndices=matrixIndices;data.matricesWeights=matrixWeights;data.applyToMesh(mesh,true);mesh.skeleton=skeleton;mesh.useVertexColors=true;
        const material=new B.StandardMaterial('BlockifiedCharacterMaterial',scene);material.diffuseColor=new B.Color3(1,1,1);material.specularColor=B.Color3.Black();mesh.material=material;
        for(const [clipKey,clip] of Object.entries(animations||{})){
            const fps=Math.max(1,Number(clip?.sample_rate||30)),length=Math.max(.033,Number(clip?.animation_length||1)),group=new B.AnimationGroup(String(clip?.label||clipKey),scene);
            for(const [name,channels] of Object.entries(clip?.bones||{})){
                const node=nodeByName.get(name);if(!node)continue;
                if(channels?.rotation_quaternion){const animation=new B.Animation(`${clipKey}-${name}-rotation`,'rotationQuaternion',fps,B.Animation.ANIMATIONTYPE_QUATERNION,B.Animation.ANIMATIONLOOPMODE_CYCLE);animation.setKeys(Object.entries(channels.rotation_quaternion).map(([time,value])=>({frame:Number(time)*fps,value:new B.Quaternion(Number(value?.[0]||0),Number(value?.[1]||0),Number(value?.[2]||0),Number(value?.[3]??1)).normalize()})).sort((a,b)=>a.frame-b.frame));if(animation.getKeys().length)group.addTargetedAnimation(animation,node);}
                if(channels?.position){const base=node.position.clone(),animation=new B.Animation(`${clipKey}-${name}-position`,'position',fps,B.Animation.ANIMATIONTYPE_VECTOR3,B.Animation.ANIMATIONLOOPMODE_CYCLE);animation.setKeys(Object.entries(channels.position).map(([time,value])=>({frame:Number(time)*fps,value:base.add(new B.Vector3(-Number(value?.[0]||0)*scale,Number(value?.[1]||0)*scale,-Number(value?.[2]||0)*scale))})).sort((a,b)=>a.frame-b.frame));if(animation.getKeys().length)group.addTargetedAnimation(animation,node);}
            }
            group.from=0;group.to=length*fps;
            if(!group.targetedAnimations.length)group.dispose();
        }
        return scene;
    }

    static async MigrateCatalogAsset(target,assets,write){
        const resolution=Number(target.blockify_resolution||0);
        const blocks=Math.max(1,resolution/16);
        const dimensions={width:blocks,height:blocks,depth:blocks};
        let engine=null,scene=null,blockScene=null,canvas=null;
        write(`Asset: ${target.name} · ${target.source_path}${resolution>0?` · block_${resolution}`:' · solid only'}`);
        try{
            await ensureBabylon();
            await CIFAssetBrowser.EnsureBabylonSerializer();
            if(!this.migrationEngine||!this.migrationCanvas){
                this.migrationCanvas=document.createElement('canvas');
                this.migrationCanvas.width=512;
                this.migrationCanvas.height=512;
                this.migrationCanvas.style.cssText='position:fixed;left:-10000px;top:-10000px;width:512px;height:512px';
                document.body.appendChild(this.migrationCanvas);
                this.migrationEngine=new BABYLON.Engine(this.migrationCanvas,true,{preserveDrawingBuffer:true,stencil:false});
            }
            canvas=this.migrationCanvas;
            engine=this.migrationEngine;
            scene=new BABYLON.Scene(engine);
            scene.clearColor=new BABYLON.Color4(.39,.68,.93,1);
            const url=await CIFRuntime.stagedAssetUrl('saved-file',{path:target.source_path});
            const before=new Set(scene.getNodes());
            const loaded=await BABYLON.SceneLoader.ImportMeshAsync('','',url,scene,undefined,target.source_format==='gltf'?'.gltf':'.glb');
            await scene.whenReadyAsync?.();
            const nodes=scene.getNodes().filter(node=>!before.has(node));
            CIFAssetBrowser.NormalizeStaticPreviewUp(scene,nodes);
            const camera=new BABYLON.ArcRotateCamera('migration-camera',Math.PI/2,Math.PI/2.5,4,BABYLON.Vector3.Zero(),scene);
            camera.attachControl(canvas,false);
            scene.activeCamera=camera;
            const thumbnailFillLight=new BABYLON.HemisphericLight('migration-thumbnail-fill',new BABYLON.Vector3(0,1,0),scene);
            thumbnailFillLight.diffuse=new BABYLON.Color3(1,1,1);
            thumbnailFillLight.groundColor=new BABYLON.Color3(.55,.55,.55);
            thumbnailFillLight.intensity=1.8;
            const thumbnailKeyLight=new BABYLON.DirectionalLight('migration-thumbnail-key',new BABYLON.Vector3(-.45,-1,.65),scene);
            thumbnailKeyLight.diffuse=new BABYLON.Color3(1,1,1);
            thumbnailKeyLight.intensity=1.55;
            scene.ambientColor=new BABYLON.Color3(.42,.42,.42);
            scene.imageProcessingConfiguration.exposure=1.3;
            this.BlockifySetFrameZero(scene);
            for(const node of scene.getNodes()){
                try{node.computeWorldMatrix?.(true);}catch(_){}
            }
            for(const mesh of scene.meshes){
                try{mesh.refreshBoundingInfo?.(true);mesh.computeWorldMatrix?.(true);}catch(_){}
            }
            const visibleMeshes=scene.meshes.filter(mesh=>mesh.isEnabled?.()!==false&&mesh.isVisible!==false&&mesh.getBoundingInfo&&Number(mesh.getTotalVertices?.()||0)>0);
            const bounds=visibleMeshes.map(mesh=>mesh.getBoundingInfo().boundingBox);

            // Resolution-zero rows are reusable animation resources, not placeable
            // objects.  Some valid animation GLBs intentionally contain only a rig
            // and animation channels, so they have no mesh to frame or thumbnail.
            // Preserve/export those assets without imposing the placeable-object
            // thumbnail contract.
            if(resolution===0&&!bounds.length){
                const solidFiles=await this.MigrationExportGltf(scene,'model');
                const saved=await CIFRuntime.assetCommand({operation:'asset-migration-save',...target,solid_files:solidFiles,solid_thumbnail:''});
                write(`Animation resource preserved · ${target.name} · no visible mesh thumbnail required.`);
                write(`Complete · ${saved.destination}`);
                return {saved,addon:''};
            }

            if(!bounds.length)throw new Error('Solid thumbnail has no visible mesh bounds.');
            let min=bounds[0].minimumWorld.clone(),max=bounds[0].maximumWorld.clone();
            for(const boundsEntry of bounds.slice(1)){
                min=BABYLON.Vector3.Minimize(min,boundsEntry.minimumWorld);
                max=BABYLON.Vector3.Maximize(max,boundsEntry.maximumWorld);
            }
            const center=min.add(max).scale(.5),extent=max.subtract(min),size=Math.max(extent.x,extent.y,extent.z,.001);
            camera.target.copyFrom(center);
            camera.radius=Math.max(size*1.8,.25);
            camera.minZ=Math.max(camera.radius/1000,.001);
            camera.maxZ=Math.max(camera.radius*20,100);
            scene.render();
            await new Promise(resolve=>requestAnimationFrame(()=>resolve()));
            scene.render();
            // Export the canonical solid representation while the character is still
            // in its rest pose. Mixamo presentation thumbnails are captured later,
            // after T-pose voxelization and animation capture have completed.
            let solidThumbnail=target.mixamo&&resolution>0?'':this.MigrationThumbnail(canvas);
            let solidFiles=await this.MigrationExportGltf(scene,'model');

            if(resolution===0){
                const saved=await CIFRuntime.assetCommand({operation:'asset-migration-save',...target,solid_files:solidFiles,solid_thumbnail:solidThumbnail});
                write(`Complete · ${saved.destination}`);
                return {saved,addon:''};
            }

            let character={voxels:[],rig:null},animations={};
            let importedAnimation=null,importedAnimationGroup=null;
            let presentationHumanoid=false;
            const fixedAnimatedAsset=String(target.source_path||'').startsWith('Animated/Fixed/');
            const embeddedGroups=(loaded.animationGroups||[]).filter(group=>group?.targetedAnimations?.length);
            const animated=embeddedGroups.length>0||Boolean(this.BlockifyRigSource(scene));
            const animationAssets=assets.filter(row=>Number(row.blockify_resolution||0)===0&&String(row.source_path||'').toLowerCase().endsWith('.glb'));

            // The voxel identity is always authored from the character's bind/rest
            // pose.  Animation is a validation/presentation step after the voxel rig
            // exists; never bake an arbitrary animation pose into the voxel geometry.
            write(`Voxelizing T-pose · ${target.name}…`);
            const statue=await this.VoxelizeBlockifyScene(scene,dimensions,'frame0');
            if(animated){
                character=await this.VoxelizeBlockifyScene(scene,dimensions,'rigrest',{requireHumanoid:Boolean(target.mixamo)});
                const canonicalRigRoles = new Set(
                    (character.rig?.bones || [])
                        .map(bone => String(bone?.canonical || '').trim())
                        .filter(Boolean)
                );
                presentationHumanoid = ['left_arm', 'right_arm', 'left_leg', 'right_leg']
                    .every(role => canonicalRigRoles.has(role));
                const rest=this.BlockifyRestRotationMap(loaded,nodes);
                let index=0;
                for(const group of loaded.animationGroups||[]){
                    const clip = this.BlockifyClipFromAnimationGroup(
                        group,
                        character.rig || {},
                        rest,
                        group.name,
                        { allowExactEmbeddedRig: fixedAnimatedAsset },
                    );
                    if(clip)animations[`clip_${++index}`]=clip;
                }
                // Fixed Animated assets are self-contained by contract. Their embedded
                // animation groups are the only legal animation source; never fall back
                // to the external Mixamo catalog merely because the rig is humanoid.
                if(fixedAnimatedAsset&&presentationHumanoid&&!Object.keys(animations).length){
                    // A Fixed asset is never allowed to borrow an external animation.
                    // Some legacy Fixed GLBs are rigged but contain no embedded clips at
                    // all (Erika is one such source). That is source truth, not a
                    // retargeting failure: preserve the rigged/rest representation and
                    // continue as an unanimated Block asset rather than inventing motion.
                    write(`Fixed asset has no embedded animation · ${target.name} · preserving rigged rest representation.`);
                }
                // Existing embedded animation is authoritative on any rig.
                // Only a clipless, positively identified Mixamo-compatible
                // local import may borrow a random saved animation. Its original
                // source bytes are never changed; the motion is exported into
                // the generated Solid GLTF and Blockify representation.
                if(!fixedAnimatedAsset&&!embeddedGroups.length&&!Object.keys(animations).length&&
                   character.rig?.bones?.length&&presentationHumanoid&&
                   (!target.local_import||target.mixamo_compatible_rig===true)){
                    // Choose from actual saved Assets, never the optional
                    // MixamoSearch SQLite index. The server only lists existing
                    // animation-bearing GLB/glTF representations.
                    const available = await CIFRuntime.assetCommand({
                        operation: 'saved-animation-candidates'
                    });
                    const catalog = (Array.isArray(available?.results) ? available.results : [])
                        .filter(row => row?.folder && row?.file && row?.id);
                    if (!catalog.length && target.local_import && target.mixamo_compatible_rig === true) {
                        throw new Error(`No playable saved animation is available for ${target.name} in the local Assets library; the source model is preserved.`);
                    }
                    if (catalog.length) {
                        let hash = 2166136261;
                        for (const characterCode of String(target.uci || target.source_path || target.name || '')) {
                            hash ^= characterCode.charCodeAt(0);
                            hash = Math.imul(hash, 16777619);
                        }
                        // Local imports pick randomly; normal migrations retain
                        // their existing stable choice for repeatable output.
                        const selectedIndex = target.local_import
                            ? Math.floor(Math.random() * catalog.length)
                            : (hash >>> 0) % catalog.length;
                        const animationAsset = catalog[selectedIndex];
                        const ref = {
                            folder: String(animationAsset.folder),
                            file: String(animationAsset.file),
                            name: String(animationAsset.name || animationAsset.file)
                        };
                        // Saved motion capture is a presentation/animation
                        // phase. Release the frame-zero sampler before retargeting or
                        // its paused group can overwrite the retargeted bone values
                        // while BlockifyCaptureMixamoClip samples the animation.
                        for(const group of loaded.animationGroups||[]){
                            try{group.stop();}catch(_){}
                        }
                        for(const skeleton of loaded.skeletons||[]){
                            try{skeleton.returnToRest();}catch(_){}
                            try{skeleton.prepare();}catch(_){}
                        }
                        const captured=await this.BlockifyCaptureMixamoClip(scene,loaded,nodes,character.rig,ref,ref.name,{returnGroup:Boolean(target.local_import)});
                        if(!(target.local_import?captured?.clip:captured)){
                            throw new Error(`Mixamo ${ref.name} produced no voxel-rig animation clip for ${target.name}.`);
                        }
                        const clip=target.local_import?captured?.clip:captured;
                        if(clip){
                            animations[`clip_${++index}`]=clip;
                            if(target.local_import){
                                importedAnimation={id:String(animationAsset.id),name:ref.name};
                                importedAnimationGroup=captured.playing;
                            }
                            write(`Animation captured · ${target.name} · ${ref.name}`);
                        }
                    }
                }
                if(target.local_import&&!embeddedGroups.length&&!importedAnimation){
                    if(target.mixamo_compatible_rig===true){
                        throw new Error(`Mixamo-compatible rig ${target.name} did not receive an animation; the migration cannot be marked complete.`);
                    }
                    write(`No embedded animation on non-Mixamo rig · ${target.name} · importing as Objects.`);
                }
                if(importedAnimationGroup){
                    // The original GLB remains byte-identical. Re-export the
                    // imported rig together with the newly retargeted animation.
                    // Imported source motion nodes are disabled by RetargetAnimation;
                    // MigrationExportGltf excludes disabled nodes.
                    solidFiles=await this.MigrationExportGltf(scene,'model');
                    const gltf=solidFiles.find(row=>String(row.name||'').toLowerCase().endsWith('.gltf')&&row.encoding==='text');
                    if(!gltf)throw new Error(`Imported Solid animation export is missing GLTF: ${target.name}`);
                    const document=JSON.parse(String(gltf.data||'{}'));
                    const joints=new Set((document.skins||[]).flatMap(skin=>skin.joints||[]));
                    const attached=(document.animations||[]).some(animation=>(animation.channels||[]).some(channel=>joints.has(channel.target?.node)));
                    if(!attached)throw new Error(`Imported Solid GLTF contains no animation targeting the character's exported skin joints: ${target.name}`);
                    write(`Solid GLTF animation verified · ${target.name} · ${importedAnimation.name}`);
                }
            }
            write(`Voxelized ${target.name} · ${statue.voxels.length} statue voxels${character.voxels.length?` · ${character.voxels.length} rigged voxels`:''} · ${Object.keys(animations).length} animations · humanoid ${presentationHumanoid?'yes':'no'}.`);

            // BlockifySetFrameZero() deliberately leaves the character's authored
            // AnimationGroups paused at frame zero while the rest/statue geometry is
            // sampled. A paused Babylon AnimationGroup still owns its animation
            // targets and is evaluated on render. If it remains active while the
            // presentation animation is retargeted, frame zero and the retargeted
            // animation fight over the same bones; the later render can therefore
            // put the visible character straight back into its T-pose.
            //
            // The rest geometry and animation clips have now been captured, so end
            // the frame-zero sampling phase completely before presentation. This is
            // the same state boundary used by the working interactive Mixamo preview:
            // target skeleton at rest, then exactly one retargeted AnimationGroup.
            if(presentationHumanoid){
                for(const group of loaded.animationGroups||[]){
                    try{group.stop();}catch(_){}
                }
                for(const skeleton of loaded.skeletons||[]){
                    try{skeleton.returnToRest();}catch(_){}
                    try{skeleton.prepare();}catch(_){}
                }
                for(const node of nodes){
                    try{node.computeWorldMatrix?.(true);}catch(_){}
                }
                scene.render();
                write(`Frame-zero hold released · ${target.name} · presentation animation owns the rig.`);
            }

            if(presentationHumanoid&&Object.keys(animations).length){
                if(!character.voxels.length||!character.rig?.bones?.length){
                    throw new Error(`Animated humanoid thumbnail validation requires a completed voxel rig: ${target.name}`);
                }
                // Prefer animation authored into the asset itself. Fixed/packaged
                // humanoids such as dragon3 can have a perfectly valid local rig and
                // embedded clips without being Mixamo-compatible. Retargeting an
                // arbitrary Mixamo presentation clip onto those rigs is both
                // unnecessary and can legitimately fail bone matching.
                //
                // Only assets without a usable embedded animation need the external
                // deterministic Mixamo presentation path used by characters such as
                // DefaultCharacter.
                let pose=importedAnimationGroup?
                    this.BlockifyApplyEmbeddedPresentationPose(
                        scene,{animationGroups:[importedAnimationGroup],skeletons:loaded.skeletons},
                        target.name,0,
                    ):
                    this.BlockifyApplyEmbeddedPresentationPose(scene,loaded,target.name,target.local_import?0:0.42);
                if(fixedAnimatedAsset&&!pose){
                    throw new Error(`Fixed Animated asset has no usable embedded presentation animation: ${target.name}`);
                }
                if(!pose){
                    pose=await this.BlockifyApplyMixamoPose(
                        scene,
                        loaded,
                        nodes,
                        animationAssets,
                        target.uci||target.source_path||target.name,
                    );
                }
                if(!pose){
                    throw new Error(`Animated humanoid solid thumbnail could not select an animation: ${target.name}`);
                }
                // Importing the presentation animation can add cameras to the scene.
                // Restore the migration camera explicitly before capture; otherwise a
                // camera embedded in the animation GLB can become active and produce
                // a perfectly valid PNG containing only the blue clear color.
                scene.activeCamera=camera;

                for(const node of nodes){
                    try{node.computeWorldMatrix?.(true);}catch(_){}
                }
                for(const mesh of loaded.meshes||[]){
                    try{mesh.refreshBoundingInfo?.(true);mesh.computeWorldMatrix?.(true);}catch(_){}
                }

                // Re-frame after posing. Root motion and the animated extremities can
                // move outside the rest-pose bounds used for voxelization. This only
                // affects presentation; canonical voxel/rest geometry remains T-pose.
                const posedBounds=(loaded.meshes||[])
                    .filter(mesh=>mesh.isEnabled?.()!==false&&mesh.isVisible!==false&&mesh.getBoundingInfo&&Number(mesh.getTotalVertices?.()||0)>0)
                    .map(mesh=>mesh.getBoundingInfo().boundingBox);
                if(!posedBounds.length){
                    throw new Error(`Solid thumbnail pose has no visible mesh bounds: ${target.name}`);
                }
                let posedMinimum=posedBounds[0].minimumWorld.clone();
                let posedMaximum=posedBounds[0].maximumWorld.clone();
                for(const posedBoundsEntry of posedBounds.slice(1)){
                    posedMinimum=BABYLON.Vector3.Minimize(posedMinimum,posedBoundsEntry.minimumWorld);
                    posedMaximum=BABYLON.Vector3.Maximize(posedMaximum,posedBoundsEntry.maximumWorld);
                }
                const posedCenter=posedMinimum.add(posedMaximum).scale(.5);
                const posedExtent=posedMaximum.subtract(posedMinimum);
                const posedSize=Math.max(posedExtent.x,posedExtent.y,posedExtent.z,.001);
                camera.target.copyFrom(posedCenter);
                camera.radius=Math.max(posedSize*1.8,.25);
                camera.minZ=Math.max(camera.radius/1000,.001);
                camera.maxZ=Math.max(camera.radius*20,100);

                scene.render();
                await new Promise(resolve=>requestAnimationFrame(resolve));
                scene.render();
                solidThumbnail=this.MigrationThumbnail(canvas);
                write(`Solid thumbnail posed · ${target.name} · ${pose.animation} @ 42%`);
            }

            blockScene=(character.voxels.length&&character.rig?.bones?.length)
                ? this.MigrationAnimatedBlockScene(
                    character.voxels,
                    character.rig,
                    animations,
                    dimensions,
                    engine,
                    1/Math.max(1e-9,Number(character.source_to_voxel_scale||character.rig?.source_to_voxel_scale||16)),
                    character.source_center,
                    character.voxel_y_offset
                )
                : this.MigrationBlockScene(
                    statue.voxels,
                    dimensions,
                    engine,
                    1/Math.max(1e-9,Number(statue.source_to_voxel_scale||16)),
                    statue.source_center,
                    statue.voxel_y_offset
                );
            const blockFiles=await this.MigrationExportGltf(blockScene,'model');
            if(Object.keys(animations).length){
                const blockGltf=blockFiles.find(row=>String(row?.name||'').toLowerCase().endsWith('.gltf'));
                if(!blockGltf||String(blockGltf.encoding||'text')!=='text')throw new Error('Animated Block GLTF export produced no readable GLTF document.');
                const blockDocument=JSON.parse(String(blockGltf.data||'{}'));
                const exportedAnimations=Array.isArray(blockDocument.animations)?blockDocument.animations:[];
                const exportedSkins=Array.isArray(blockDocument.skins)?blockDocument.skins:[];
                if(!exportedSkins.length||exportedAnimations.length!==Object.keys(animations).length){
                    throw new Error(`Animated Block GLTF export lost rig/animations: expected ${Object.keys(animations).length} animations, exported ${exportedAnimations.length}, skins ${exportedSkins.length}.`);
                }
                write(`Block GLTF verified · ${target.name} · ${exportedSkins.length} skin · ${exportedAnimations.length} animations.`);
            }
            let blockThumbnail='';
            const blockAnimation=blockScene.animationGroups?.find(group=>group.targetedAnimations?.length);
            if(presentationHumanoid&&Object.keys(animations).length&&!blockAnimation){
                throw new Error(`Animated humanoid Block thumbnail has no playable voxel-rig animation: ${target.name}`);
            }
            if(blockAnimation){
                const blockCamera=new BABYLON.ArcRotateCamera('block-thumbnail-camera',Math.PI/2,Math.PI/2.25,3,BABYLON.Vector3.Zero(),blockScene);
                blockScene.activeCamera=blockCamera;
                const blockLight=new BABYLON.HemisphericLight('block-thumbnail-light',new BABYLON.Vector3(.4,1,.2),blockScene);
                blockLight.intensity=1.8;
                blockLight.groundColor=new BABYLON.Color3(.6,.6,.6);
                const blockKeyLight=new BABYLON.DirectionalLight('block-thumbnail-key',new BABYLON.Vector3(-.45,-1,.65),blockScene);
                blockKeyLight.intensity=1.45;
                blockScene.ambientColor=new BABYLON.Color3(.42,.42,.42);
                blockScene.imageProcessingConfiguration.exposure=1.3;
                const blockMeshes=blockScene.meshes.filter(mesh=>mesh.getTotalVertices?.()>0&&mesh.isEnabled?.());
                if(blockMeshes.length){
                    let minimum=null,maximum=null;
                    for(const mesh of blockMeshes){
                        mesh.computeWorldMatrix?.(true);
                        const bounds=mesh.getBoundingInfo?.().boundingBox;
                        if(!bounds)continue;
                        minimum=minimum?BABYLON.Vector3.Minimize(minimum,bounds.minimumWorld):bounds.minimumWorld.clone();
                        maximum=maximum?BABYLON.Vector3.Maximize(maximum,bounds.maximumWorld):bounds.maximumWorld.clone();
                    }
                    if(minimum&&maximum){
                        const center=minimum.add(maximum).scale(.5),extent=maximum.subtract(minimum),size=Math.max(extent.x,extent.y,extent.z,.001);
                        blockCamera.target.copyFrom(center);
                        blockCamera.radius=Math.max(size*1.8,.25);
                        blockCamera.minZ=Math.max(blockCamera.radius/1000,.001);
                        blockCamera.maxZ=Math.max(blockCamera.radius*20,100);
                    }
                }
                blockAnimation.start(false,1,blockAnimation.from,blockAnimation.to,false);
                blockScene.render();
                await new Promise(resolve=>requestAnimationFrame(resolve));
                const poseFrame=blockAnimation.from+((blockAnimation.to-blockAnimation.from)*(target.local_import?0:.42));
                blockAnimation.goToFrame(poseFrame);
                blockScene.render();
                await new Promise(resolve=>requestAnimationFrame(resolve));
                blockScene.render();
                blockAnimation.pause();
                await new Promise(resolve=>requestAnimationFrame(resolve));
                blockScene.render();
                blockThumbnail=this.MigrationThumbnail(canvas);
                write(`Block thumbnail posed · ${target.name} · ${blockAnimation.name} @ 42%`);
            }else{
                const blockThumbBytes=await CIFRuntime.assetCommand({operation:'asset-migration-block-thumbnail',voxels:statue.voxels});
                blockThumbnail=blockThumbBytes.data_url;
            }
            const saved=await CIFRuntime.assetCommand({operation:'asset-migration-save',...target,solid_files:solidFiles,block_files:blockFiles,solid_thumbnail:solidThumbnail,block_thumbnail:blockThumbnail,voxels:statue.voxels,character_voxels:character.voxels||[],character_rig:character.rig||{},character_animations:animations,behavior_profile:this.BlockifyBehaviorProfile(animations),import_animation:importedAnimation});
            const addon=`${String(saved.destination||'').replace(/\/$/,'')}/block_${resolution}/${target.name}.mcaddon`;
            write(`Complete · ${saved.destination}`);
            return {saved,addon};
        }finally{
            // Scenes own the per-asset meshes, materials and textures. Dispose them
            // after every conversion, but deliberately keep the migration engine and
            // its single WebGL context alive for the run. Repeated context creation is
            // what exhausts Safari's texture/context pool on large migrations.
            try{blockScene?.dispose();}catch(_){}
            try{scene?.dispose();}catch(_){}
        }
    }

    static DisposeMigrationEngine(){
        const engine=this.migrationEngine;
        const canvas=this.migrationCanvas;
        const gl=engine?._gl||null;
        this.migrationEngine=null;
        this.migrationCanvas=null;
        try{engine?.dispose?.();}catch(_){}
        try{gl?.getExtension?.('WEBGL_lose_context')?.loseContext?.();}catch(_){}
        try{canvas?.remove?.();}catch(_){}
    }

    static async BlockifyVisibleAssets(){
        const rows=this.AssignmentSavedRows().filter(row=>row.kind==='3d'||row.kind==='character');
        if(!rows.length){this.SetProgress(null,'No saved 3D assets to Blockify.');
        return;
        }
        const button=this.elements().blockifyButton;
        if (button) {
            button.disabled=true;
        }
        let done=0,skipped=0,failed=0;
        const host=document.createElement('div');
            host.style.cssText='position:fixed;left:-10000px;top:-10000px;width:96px;height:96px;overflow:hidden;';
            document.body.appendChild(host);
        try{
            for(const row of rows){
                if(this.HasBlockifyArtifact(row.version)){skipped++;
                continue;
                }
                const versions=row.folder?.versions||[],versionIndex=Math.max(0,versions.indexOf(row.version));
                const card=document.createElement('article');
                    card.className='saved-asset-card';
                    card.dataset.folder=String(row.folder?.folder||'');
                    card.dataset.versionIndex=String(versionIndex);
                card.innerHTML='<canvas class="saved-asset-canvas" width="96" height="96"></canvas><input class="asset-blockify-dimension" data-axis="width" value="1"><input class="asset-blockify-dimension" data-axis="height" value="1"><input class="asset-blockify-dimension" data-axis="depth" value="1">';
                host.appendChild(card);
                try{await this.BlockifySavedAsset(card);
                    done++;
                    }catch(error){failed++;
                    console.error('[IFPlayer Assets] blockify-failed',{folder:row.folder?.folder,file:row.version?.file,error:String(error?.message||error)});
                    }
                card.remove();
            }
        }finally{host.remove();
            if (button) {
                button.disabled=false;
            }
            this.SetProgress(null,`Blockify complete \u00b7 ${done} converted \u00b7 ${skipped} already converted${failed?` \u00b7 ${failed} failed`:''}.`);
            }
        await this.LoadSaved();
        if (this.mode==='assign') {
            await this.LoadAssignments();
        }
    }

}

console.info('[CIF Asset] CIFBlockifyBatch loaded');

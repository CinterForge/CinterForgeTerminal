'use strict';

// Interactive Blockify preview/rendering authority.
class CIFBlockifyPreview extends CIFBlockify {
    static async LoadCloudflareMinecraftPreview(card){
        const payload=await this.PrepareCloudflareBlockify(card);
         card._cloudflareBlockifyPayload=payload;
        this.RenderBlockifiedVoxels(card,payload.voxels,{width:payload.width,height:payload.height,depth:payload.depth},`cloudflare-blockify:${payload.path}:${payload.width}x${payload.height}x${payload.depth}`);
        this.SetProgress(card,`Minecraft Preview · ${payload.voxels.length.toLocaleString()} occupied voxels.`);
    }

    static async LoadImageMinecraftPreview(card){
        const {folder,version,meta}=await this.EnsureImageMinecraft(card);
        await ensureBabylon();
        if (!window.BABYLON) {
            throw new Error('Babylon.js is unavailable.');
        }
        const payload=await this.BuildImageMinecraftPayload(card);
        const dimensions={width:Number(meta.width||1),height:Number(meta.height||1),depth:1};
        this.RenderImageMinecraftBlocks(card,payload.pixels||[],dimensions,payload.statue_texture_data_url,`image-minecraft:${folder.folder}/${version.file}:${dimensions.width}x${dimensions.height}`);
        card.classList.add('image-minecraft-preview-active');
        this.SetProgress(card,`Minecraft Preview \u00b7 ${dimensions.width} \u00d7 ${dimensions.height} blocks \u00b7 ${dimensions.width*16} \u00d7 ${dimensions.height*16} statue texels \u00b7 picture texture \u22641024px.`);
    }

    static DisposeCanvasPreview(canvas){
        for(const [key,engine] of Array.from(this.engines.entries())){
            if (engine?.getRenderingCanvas?.()===canvas) {
                this.DisposePreviewKey(key);
            }
        }
    }

    static RenderImageMinecraftBlocks(card,blocks,dimensions,textureDataUrl,key){
        const canvas=card?.querySelector('.saved-asset-canvas');
        if (!canvas) {
            return;
        }
        const B=window.BABYLON;
        if (!B) {
            throw new Error('Babylon.js is unavailable.');
        }
        const occupied=Array.isArray(blocks)?blocks:[];
            if (!occupied.length) {
                throw new Error('Minecraft image preview has no visible blocks.');
            }
        this.DisposeCanvasPreview(canvas);
        card.classList.add('viewer-active');
        const engine=new B.Engine(canvas,true,{preserveDrawingBuffer:true,stencil:false}),scene=new B.Scene(engine);
        scene.clearColor=new B.Color4(0.39,0.68,0.93,1);
        const camera=new B.ArcRotateCamera('camera',Math.PI/2,Math.PI/2.25,Math.max(3,Number(dimensions.width||1),Number(dimensions.height||1))*1.6,new B.Vector3(0,Number(dimensions.height||1)/2,0),scene);
        camera.wheelPrecision=90;
        camera.panningSensibility=140;
        camera.attachControl(canvas,true);
        canvas.addEventListener('wheel',event=>{event.preventDefault();event.stopPropagation();},{passive:false});

        // Image-statue PREVIEW blocks are one Bedrock model pixel thick (1/16 block).
        // Geometry resolution is still one Minecraft block per image tile; each tile
        // carries all 16x16 source texels on its outward faces.  The exported Statue
        // remains full-block geometry -- this thin depth is preview-only.
        const previewDepth=1/16,previewHalfDepth=previewDepth/2,faceOffset=previewHalfDepth+.002;
        const cx0=(Number(dimensions.width||1)-1)/2;
        const sideRoot=new B.TransformNode('image-minecraft-solid-blocks',scene);
        for(const block of occupied){
            const bx=Number(block.x)-cx0,by=Number(block.y)+.5,bz=0;
            const box=B.MeshBuilder.CreateBox(`image-block-${block.x}-${block.y}`,{width:1,height:1,depth:previewDepth},scene);
                box.position.set(bx,by,bz);
                box.parent=sideRoot;
            const mat=new B.StandardMaterial(`image-block-solid-${block.x}-${block.y}`,scene);
            mat.disableLighting=true;
                mat.diffuseColor=new B.Color3(Number(block.r||0)/255,Number(block.g||0)/255,Number(block.b||0)/255);
                mat.emissiveColor=mat.diffuseColor;
                mat.specularColor=new B.Color3(0,0,0);
                box.material=mat;
            try{box.enableEdgesRendering();
            box.edgesWidth=.35;
            box.edgesColor=new B.Color4(.03,.03,.03,.5);
            } catch (_) {
            }
        }

        const texW=Math.max(16,Number(dimensions.width||1)*16),texH=Math.max(16,Number(dimensions.height||1)*16);
        const positions=[],indices=[],normals=[],uvs=[];
        for(const block of occupied){
            const bx=Number(block.x)-cx0,by=Number(block.y)+.5;
            const tileX=Math.max(0,Math.min(Number(dimensions.width||1)-1,Number(block.x)));
            // block.y is already bottom-up while the canvas atlas is top-down.
            // Babylon's ordinary texture upload flips image Y, so use block.y
            // directly here rather than compensating a second time in UV space.
            const u0=tileX*16/texW,u1=(tileX+1)*16/texW;
            const v0=Number(block.y)*16/texH,v1=(Number(block.y)+1)*16/texH;
            const addFace=(z,mirror)=>{
                const base=positions.length/3;
                if(!mirror){positions.push(bx-.5,by-.5,z,bx+.5,by-.5,z,bx+.5,by+.5,z,bx-.5,by+.5,z);
                    normals.push(0,0,1,0,0,1,0,0,1,0,0,1);
                    uvs.push(u0,v0,u1,v0,u1,v1,u0,v1);
                    }
                else{positions.push(bx+.5,by-.5,z,bx-.5,by-.5,z,bx-.5,by+.5,z,bx+.5,by+.5,z);
                    normals.push(0,0,-1,0,0,-1,0,0,-1,0,0,-1);
                    uvs.push(u1,v0,u0,v0,u0,v1,u1,v1);
                    }
                indices.push(base,base+2,base+1,base,base+3,base+2);
            };
            addFace(faceOffset,false);
            addFace(-faceOffset,true);
        }
        const faceMesh=new B.Mesh('image-minecraft-picture-faces',scene),data=new B.VertexData();
            data.positions=positions;
            data.indices=indices;
            data.normals=normals;
            data.uvs=uvs;
            data.applyToMesh(faceMesh,true);
            faceMesh.renderingGroupId=1;
        const faceMat=new B.StandardMaterial('image-minecraft-face-material',scene),texture=new B.Texture(textureDataUrl,scene,false,true,B.Texture.NEAREST_SAMPLINGMODE);
        texture.hasAlpha=false;
            faceMat.diffuseTexture=texture;
            faceMat.emissiveTexture=texture;
            faceMat.useAlphaFromDiffuseTexture=false;
            faceMat.transparencyMode=B.Material.MATERIAL_OPAQUE;
            faceMat.diffuseColor=new B.Color3(1,1,1);
            faceMat.emissiveColor=new B.Color3(1,1,1);
            faceMat.specularColor=new B.Color3(0,0,0);
            faceMat.disableLighting=true;
            faceMesh.material=faceMat;

        const groundSize=Math.max(4,Number(dimensions.width||1)*2.5,4),ground=B.MeshBuilder.CreateGround('image-minecraft-ground',{width:groundSize,height:groundSize},scene);
        const groundMat=new B.StandardMaterial('image-minecraft-ground-material',scene);
            groundMat.disableLighting=true;
            groundMat.emissiveColor=new B.Color3(.24,.56,.18);
            groundMat.diffuseColor=groundMat.emissiveColor;
            ground.material=groundMat;
        const gridLines=[],half=Math.ceil(groundSize/2),gridY=.002;
        for(let i=-half;i<=half;i++){gridLines.push([new B.Vector3(i,gridY,-half),new B.Vector3(i,gridY,half)]);
            gridLines.push([new B.Vector3(-half,gridY,i),new B.Vector3(half,gridY,i)]);
            }
        const grid=B.MeshBuilder.CreateLineSystem('image-minecraft-grid',{lines:gridLines},scene);
            grid.color=new B.Color3(.06,.19,.06);
        camera.setTarget(new B.Vector3(0,Number(dimensions.height||1)/2,0));
            camera.radius=Math.max(3,Math.max(Number(dimensions.width||1),Number(dimensions.height||1))*1.45);
            camera.lowerRadiusLimit=.5;
            camera.upperRadiusLimit=Math.max(20,camera.radius*8);
        const resize=()=>engine.resize();
            window.addEventListener('resize',resize);
            engine.runRenderLoop(()=>scene.render());
        this.engines.set(key,engine);
            this.scenes.set(key,scene);
            this.previewCleanup.set(key,()=>{window.removeEventListener('resize',resize);
            });
        requestAnimationFrame(()=>engine.resize());
    }

    static RenderBlockifiedVoxels(card,voxels,dimensions,key,voxelResolution=16){
        const canvas=card?.querySelector('.saved-asset-canvas');
         if (!canvas) {
             return;
         }
        const B=window.BABYLON;
        if (!B) {
            throw new Error('Babylon.js is unavailable.');
        }
        this.DisposeCanvasPreview(canvas);
         card.classList.add('viewer-active');
        const engine=new B.Engine(canvas,true,{preserveDrawingBuffer:true,stencil:false});
        const scene=new B.Scene(engine);
        // Minecraft-like inspection environment. The Blockified voxels themselves
        // are intentionally unlit so their generated source colors are shown exactly.
        scene.clearColor=new B.Color4(0.39,0.68,0.93,1);
        const camera=new B.ArcRotateCamera('camera',Math.PI/2,Math.PI/2.25,3,B.Vector3.Zero(),scene);
        camera.wheelPrecision=90;
        camera.panningSensibility=140;
        camera.attachControl(canvas,true);
        canvas.addEventListener('wheel',event=>{event.preventDefault();event.stopPropagation();},{passive:false});

        // Build the preview geometry directly instead of using SolidParticleSystem.
        // SPS was failing before display with an undefined particle position, so a
        // successful Blockify was saved but the original GLB remained visible.
        // One indexed mesh also keeps the preview lightweight and deterministic.
        // Minecraft blocks are contiguous cubes. Build and Preview intentionally use
        // CIFMapTerrain.BuildVoxelGeometry so face order can never diverge again.
        const resolution=Math.max(1,Number(voxelResolution||16)),size=0.998/resolution,half=size/2,occupied=Array.isArray(voxels)?voxels:[];
        if (!occupied.length) {
            throw new Error('Block Preview has no occupied voxels.');
        }
        let minX=Infinity,maxX=-Infinity,minY=Infinity,minZ=Infinity,maxZ=-Infinity;
        for(const v of occupied){const x=Number(v.x),y=Number(v.y),z=Number(v.z);
            minX=Math.min(minX,x);
            maxX=Math.max(maxX,x);
            minY=Math.min(minY,y);
            minZ=Math.min(minZ,z);
            maxZ=Math.max(maxZ,z);
            }
        const centerX=(minX+maxX)/2,centerZ=(minZ+maxZ)/2;
        if (typeof CIFMapTerrain==='undefined'||!CIFMapTerrain.BuildVoxelGeometry) {
            throw new Error('Shared voxel geometry renderer is unavailable.');
        }
        const geometry=CIFMapTerrain.BuildVoxelGeometry(occupied,v=>[(Number(v.x)-centerX)/resolution,(Number(v.y)-minY)/resolution+half,(Number(v.z)-centerZ)/resolution,half]);
        const {positions,indices,normals,colors,uvs}=geometry;
        const mesh=new B.Mesh('blockified',scene),data=new B.VertexData();
        data.positions=positions;
            data.indices=indices;
            data.normals=normals;
            data.colors=colors;
            data.uvs=uvs;
            data.applyToMesh(mesh,true);
        // Use Babylon's default material-side orientation so Preview uses the same
        // canonical voxel winding as Map and Test without reversing face culling.
        mesh.hasVertexAlpha=false;
        mesh.useVertexColors=true;
        /* Same single-pass screen-space stroke used by Build Terrain. This restores the
           accepted heavy Minecraft block look without a second EdgesRenderer draw. */
        B.Effect.ShadersStore['ifMinecraftPreviewVoxelVertexShader']=`precision highp float;attribute vec3 position;attribute vec4 color;attribute vec2 uv;uniform mat4 worldViewProjection;varying vec4 vColor;varying vec2 vUV;void main(){vColor=color;vUV=uv;gl_Position=worldViewProjection*vec4(position,1.0);}`;
        B.Effect.ShadersStore['ifMinecraftPreviewVoxelFragmentShader']=`#extension GL_OES_standard_derivatives : enable\nprecision highp float;varying vec4 vColor;varying vec2 vUV;uniform vec4 edgeColor;uniform float edgeWidth;void main(){vec2 d=min(vUV,1.0-vUV);float e=min(d.x,d.y);float px=max(fwidth(e)*edgeWidth,0.00001);float interior=smoothstep(0.0,px,e);gl_FragColor=mix(edgeColor,vColor,interior);}`;
        const mat=new B.ShaderMaterial('blockified-material',scene,{vertex:'ifMinecraftPreviewVoxel',fragment:'ifMinecraftPreviewVoxel'},{attributes:['position','color','uv'],uniforms:['worldViewProjection','edgeColor','edgeWidth']});
        mat.backFaceCulling=true;
            mat.alphaMode=B.Engine.ALPHA_DISABLE;
            mat.setVector4('edgeColor',new B.Vector4(.03,.03,.03,.72));
            mat.setFloat('edgeWidth',2.0);
            mesh.material=mat;
        const groundSize=Math.max(4,dimensions.width*2.5,dimensions.depth*2.5);
        const ground=B.MeshBuilder.CreateGround('blockified-ground',{width:groundSize,height:groundSize},scene);
        ground.position.y=0;
        ground.metadata={...(ground.metadata||{}),previewEnvironment:true};
        const groundMat=new B.StandardMaterial('blockified-ground-material',scene);
            groundMat.disableLighting=true;
            groundMat.emissiveColor=new B.Color3(0.24,0.56,0.18);
            groundMat.diffuseColor=groundMat.emissiveColor;
            ground.material=groundMat;
        // Minecraft-block footprint grid: one square is exactly one Minecraft block.
        // Keep it aligned to the Blockified object's block-space bounds so W/H/D is
        // visually obvious while orbiting or zooming the preview.
        const gridHalf=Math.ceil(groundSize/2),gridY=ground.position.y+0.002,gridLines=[];
        // Origin is the center of the middle Minecraft block, therefore grid
        // boundaries are at half-block offsets rather than crossing X=0/Z=0.
        for(let x=-gridHalf;x<gridHalf;x++){const gx=x+.5;
            gridLines.push([new B.Vector3(gx,gridY,-gridHalf),new B.Vector3(gx,gridY,gridHalf)]);
            }
        for(let z=-gridHalf;z<gridHalf;z++){const gz=z+.5;
            gridLines.push([new B.Vector3(-gridHalf,gridY,gz),new B.Vector3(gridHalf,gridY,gz)]);
            }
        const blockGrid=B.MeshBuilder.CreateLineSystem('blockified-minecraft-block-grid',{lines:gridLines},scene);
        blockGrid.color=new B.Color3(0.08,0.20,0.06);
            blockGrid.isPickable=false;
            blockGrid.metadata={...(blockGrid.metadata||{}),previewEnvironment:true};
        card.classList.add('blockified-preview-active');
        const previewMeshes=scene.meshes.filter(q=>q!==mesh);
            previewMeshes.forEach(q=>q.setEnabled(false));
            this.FrameScene(scene,camera);
            previewMeshes.forEach(q=>q.setEnabled(true));
            this.engines.set(key,engine);
            this.scenes.set(key,scene);
            this.ActivatePreviewEngine(key);
            requestAnimationFrame(()=>{engine.resize();
            this.FrameScene(scene,camera);
            });
        this.InstallMinecraftPreviewEditor(card,{engine,scene,camera,mesh,voxels:occupied,dimensions,resolution,minX,maxX,minY,minZ,maxZ,centerX,centerZ,key});
    }

    static InstallMinecraftPreviewEditor(card,state){
        const B=window.BABYLON,canvas=state.engine.getRenderingCanvas();
        if (!B||!canvas) {
            return;
        }
        try{card._minecraftEditorCleanup?.();
        } catch (_) {
        }
        const palette=Array.isArray(card._blockifyPalette)?card._blockifyPalette.slice(0,27):[];
        let selected=0,hover=null;
        const ui=document.createElement('div');
        ui.className='minecraft-preview-editor';
        ui.innerHTML=`<div class="minecraft-preview-hotbar"></div><button class="objaverse-action minecraft-preview-palette-toggle" title="Creative Block Picker">Blocks</button><div class="minecraft-preview-palette" hidden></div><button class="objaverse-action minecraft-preview-save">Save Blocks</button>`;
        const hot=ui.querySelector('.minecraft-preview-hotbar'),picker=ui.querySelector('.minecraft-preview-palette');
        const slotItems=Array.from({length:9},(_,i)=>palette[i]||null);
        const renderUI=()=>{hot.innerHTML='';
            slotItems.forEach((q,i)=>{const d=document.createElement('div');
            d.className=`minecraft-preview-slot${i===selected?' selected':''}`;
            d.dataset.slot=String(i);
            d.draggable=!!q;
            if(q){d.style.backgroundColor=`rgb(${q.r},${q.g},${q.b})`;
            d.title=`${i+1}: rgb(${q.r}, ${q.g}, ${q.b})`;
            d.addEventListener('dragstart',e=>e.dataTransfer.setData('application/x-cinter-preview-block',JSON.stringify(q)));
            }d.addEventListener('click',()=>{selected=i;
            renderUI();
            });
            d.addEventListener('dragover',e=>e.preventDefault());
            d.addEventListener('drop',e=>{e.preventDefault();
            try{slotItems[i]=JSON.parse(e.dataTransfer.getData('application/x-cinter-preview-block'));
            selected=i;
            renderUI();
            }catch(_){}});
            hot.appendChild(d);
            });
            };
        palette.forEach(q=>{const d=document.createElement('div');
            d.className='minecraft-preview-palette-block';
            d.draggable=true;
            d.style.backgroundColor=`rgb(${q.r},${q.g},${q.b})`;
            d.title=`rgb(${q.r}, ${q.g}, ${q.b})`;
            d.addEventListener('dragstart',e=>e.dataTransfer.setData('application/x-cinter-preview-block',JSON.stringify(q)));
            picker.appendChild(d);
            });
        renderUI();
        card.appendChild(ui);
        ui.querySelector('.minecraft-preview-palette-toggle').addEventListener('click',()=>{picker.hidden=!picker.hidden;
            });
        const hoverBox=B.MeshBuilder.CreateBox('minecraft-preview-hover',{size:.998/state.resolution},state.scene);
            hoverBox.isPickable=false;
            hoverBox.renderingGroupId=3;
            const hm=new B.StandardMaterial('minecraft-preview-hover-mat',state.scene);
            hm.wireframe=true;
            hm.emissiveColor=new B.Color3(1,1,1);
            hm.disableLighting=true;
            hoverBox.material=hm;
            hoverBox.setEnabled(false);
        const key=v=>`${v.x|0},${v.y|0},${v.z|0}`;
        const updateHover=e=>{const rect=canvas.getBoundingClientRect(),pick=state.scene.pick(e.clientX-rect.left,e.clientY-rect.top,m=>m===state.mesh);
            if(!pick?.hit||!pick.pickedPoint){hover=null;
            hoverBox.setEnabled(false);
            return;
            }let n=pick.getNormal?.(true)||new B.Vector3(0,1,0);
            n=new B.Vector3(Math.round(n.x),Math.round(n.y),Math.round(n.z));
            const p=pick.pickedPoint.subtract(n.scale(.15/state.resolution));
            const x=Math.round(p.x*state.resolution+state.centerX),y=Math.round((p.y-(.998/state.resolution)/2)*state.resolution+state.minY),z=Math.round(p.z*state.resolution+state.centerZ);
            const index=state.voxels.findIndex(v=>(v.x|0)===x&&(v.y|0)===y&&(v.z|0)===z);
            if(index<0){hover=null;
            hoverBox.setEnabled(false);
            return;
            }hover={index,voxel:state.voxels[index],dir:[n.x,n.y,n.z]};
            hoverBox.position.set((x-state.centerX)/state.resolution,(y-state.minY)/state.resolution+(.998/state.resolution)/2,(z-state.centerZ)/state.resolution);
            hoverBox.setEnabled(true);
            };
        const rebuild=()=>{const folder=this.SavedFolderForCard(card),versions=folder?.versions||[],version=versions[Number(card.dataset.versionIndex||0)]||versions[0];
            this.DisposeCanvasPreview(canvas);
            card._blockifyPalette=palette;
            this.RenderBlockifiedVoxels(card,state.voxels,state.dimensions,state.key,state.resolution);
            };
        const edit=place=>{if(!hover)return;
            const h=hover;
            if(place){const q=slotItems[selected];
            if (!q) {
                return;
            }
            const v={x:(h.voxel.x|0)+h.dir[0],y:(h.voxel.y|0)+h.dir[1],z:(h.voxel.z|0)+h.dir[2],r:q.r|0,g:q.g|0,b:q.b|0,palette_index:Number(q.index??selected),samples:1};
            if (state.voxels.some(x=>key(x)===key(v))) {
                return;
            }
            state.voxels.push(v);
            }else state.voxels.splice(h.index,1);
            hover=null;
            card._minecraftPreviewDirty=true;
            rebuild();
            };
        const onKey=e=>{if(!card.classList.contains('asset-fullview')&&!card.classList.contains('asset-inspect-fullview'))return;
            if(/^Digit[1-9]$/.test(e.code)){selected=Number(e.code.slice(-1))-1;
            renderUI();
            return;
            }if(e.code==='ShiftLeft'){e.preventDefault();
            edit(true);
            }else if(e.code==='ShiftRight'){e.preventDefault();
            edit(false);
            }};
        canvas.addEventListener('pointermove',updateHover);
        window.addEventListener('keydown',onKey,true);
        ui.querySelector('.minecraft-preview-save').addEventListener('click',async()=>{const folder=this.SavedFolderForCard(card),versions=folder?.versions||[],version=versions[Number(card.dataset.versionIndex||0)]||versions[0];
            if (!folder||!version) {
                return;
            }
            this.SetProgress(card,'Saving Minecraft block edits\u2026');
            try{const result=await CIFRuntime.assetCommand({operation:'saved-blockify-save',folder:folder.folder,file:version.file,width:state.dimensions.width,height:state.dimensions.height,depth:state.dimensions.depth,resolution:state.resolution,voxels:state.voxels,product_name:String(card.dataset.productName||folder.name||version.file||'')});
            version.blockify=result.blockify||version.blockify;
            card._minecraftPreviewDirty=false;
            this.SetProgress(card,`Minecraft Preview saved \u00b7 ${state.voxels.length.toLocaleString()} blocks.`);
            }catch(error){this.SetProgress(card,`Save failed: ${error.message||error}`);
            }});
        card._minecraftEditorCleanup=()=>{window.removeEventListener('keydown',onKey,true);
            try{ui.remove();
            hoverBox.dispose();
            }catch(_){}};
    }

    static RenderAnimatedBlockifiedBehavior(card,voxels,rig,clips,behavior,dimensions,key){
        const canvas=card?.querySelector('.saved-asset-canvas');
            if (!canvas) {
                return;
            }
            const occupied=Array.isArray(voxels)?voxels:[],bones=Array.isArray(rig?.bones)?rig.bones:[];
        if (!occupied.length) {
            throw new Error('Minecraft Preview has no animated character voxels.');
        }
            if (!bones.length) {
                throw new Error('Minecraft Preview has no source rig.');
            }
        this.DisposeCanvasPreview(canvas);
            card.classList.add('viewer-active','blockified-preview-active');
            const engine=new BABYLON.Engine(canvas,true,{preserveDrawingBuffer:true,stencil:false}),scene=new BABYLON.Scene(engine);
            scene.clearColor=new BABYLON.Color4(.39,.68,.93,1);
        const camera=new BABYLON.ArcRotateCamera('camera',Math.PI/2,Math.PI/2.25,3,BABYLON.Vector3.Zero(),scene);
            camera.wheelPrecision=90;
            camera.panningSensibility=140;
            camera.attachControl(canvas,true);
            canvas.addEventListener('wheel',event=>{event.preventDefault();
            event.stopPropagation();
            },{passive:false});
        let minX=Infinity,maxX=-Infinity,minY=Infinity,minZ=Infinity,maxZ=-Infinity;
            for(const v of occupied){const x=Number(v.x),y=Number(v.y),z=Number(v.z);
            minX=Math.min(minX,x);
            maxX=Math.max(maxX,x);
            minY=Math.min(minY,y);
            minZ=Math.min(minZ,z);
            maxZ=Math.max(maxZ,z);
            }const centerX=(minX+maxX)/2,centerZ=(minZ+maxZ)/2,size=.88/16,half=size/2;
        const pivotWorld=new Map();
            for(const b of bones){const p=Array.isArray(b.pivot)?b.pivot:[centerX,minY,centerZ];
            pivotWorld.set(String(b.name||''),new BABYLON.Vector3((Number(p[0])-centerX)/16,(Number(p[1])-minY)/16,(Number(p[2])-centerZ)/16));
            }
        // The sampled animation is a WORLD-space bake. Keep every voxel bone flat
        // under one neutral wrapper so Babylon cannot apply source ancestry twice.
        const rigRoot=new BABYLON.TransformNode('block-rig-root',scene),nodes=new Map();
        for(const b of bones){const name=String(b.name||'');
            if (!name) {
                continue;
            }
            const node=new BABYLON.TransformNode(`block-bone-${name}`,scene);
            node.rotationQuaternion=BABYLON.Quaternion.Identity();
            node.position=(pivotWorld.get(name)||BABYLON.Vector3.Zero()).clone();
            node.parent=rigRoot;
            nodes.set(name,node);
            }
        const byBone=new Map();
            for(const v of occupied){const name=String(v.bone||'');
            if (!nodes.has(name)) {
                continue;
            }
            if (!byBone.has(name)) {
                byBone.set(name,[]);
            }
            byBone.get(name).push(v);
            }const faces=[[[ -1,-1,1],[1,-1,1],[1,1,1],[-1,1,1],[0,0,1]],[[1,-1,-1],[-1,-1,-1],[-1,1,-1],[1,1,-1],[0,0,-1]],[[1,-1,1],[1,-1,-1],[1,1,-1],[1,1,1],[1,0,0]],[[-1,-1,-1],[-1,-1,1],[-1,1,1],[-1,1,-1],[-1,0,0]],[[-1,1,1],[1,1,1],[1,1,-1],[-1,1,-1],[0,1,0]],[[-1,-1,-1],[1,-1,-1],[1,-1,1],[-1,-1,1],[0,-1,0]]];
        for(const [name,rows] of byBone){const node=nodes.get(name),pivot=pivotWorld.get(name)||BABYLON.Vector3.Zero(),positions=[],indices=[],normals=[],colors=[];
            for(const v of rows){const cx=(Number(v.x)-centerX)/16-pivot.x,cy=(Number(v.y)-minY)/16+half-pivot.y,cz=(Number(v.z)-centerZ)/16-pivot.z,r=Number(v.r||0)/255,g=Number(v.g||0)/255,b=Number(v.b||0)/255;
            for(const face of faces){const base=positions.length/3,n=face[4];
            for(let i=0;
            i<4;
            i++){const q=face[i];
            positions.push(cx+q[0]*half,cy+q[1]*half,cz+q[2]*half);
            normals.push(n[0],n[1],n[2]);
            colors.push(r,g,b,1);
            }indices.push(base,base+2,base+1,base,base+3,base+2);
            }}const mesh=new BABYLON.Mesh(`block-bone-mesh-${name}`,scene),data=new BABYLON.VertexData();
            data.positions=positions;
            data.indices=indices;
            data.normals=normals;
            data.colors=colors;
            data.applyToMesh(mesh,true);
            mesh.parent=node;
            mesh.hasVertexAlpha=false;
            mesh.useVertexColors=true;
            const mat=new BABYLON.StandardMaterial(`block-mat-${name}`,scene);
            mat.diffuseColor=new BABYLON.Color3(1,1,1);
            mat.emissiveColor=new BABYLON.Color3(1,1,1);
            mat.specularColor=BABYLON.Color3.Black();
            mat.disableLighting=true;
            mesh.material=mat;
            }
        const profileByClip=new Map((behavior||[]).map(row=>[String(row.clip||''),row])),entries=[];
        const locomotionBone=bones.find(b=>String(b.canonical||'').toLowerCase()==='hips')||bones.find(b=>/^(hips|pelvis|root)$/i.test(String(b.source_name||b.name||'')))||bones[0]||null;
        for(const [clipKey,clip] of Object.entries(clips||{})){
            if (!clip?.bones) {
                continue;
            }
            const group=new BABYLON.AnimationGroup(`minecraft-${clipKey}`,scene),fps=30,length=Math.max(.033,Number(clip.animation_length||1));
            const locomotionPositions=locomotionBone?clip.bones?.[String(locomotionBone.name||'')]?.position:null;
            const locomotionAt=time=>{
                if (!locomotionPositions) {
                    return [0,0,0];
                }
                const exact=locomotionPositions[time];
                if (Array.isArray(exact)) {
                    return exact;
                }
                const t=Number(time),pairs=Object.entries(locomotionPositions).map(([k,v])=>[Number(k),v]).filter(row=>Number.isFinite(row[0])&&Array.isArray(row[1])).sort((a,b)=>a[0]-b[0]);
                if (!pairs.length) {
                    return [0,0,0];
                }
                    let best=pairs[0];
                    for(const row of pairs){if(Math.abs(row[0]-t)<Math.abs(best[0]-t))best=row;
                    }return best[1];
            };
            for(const [name,channels] of Object.entries(clip.bones||{})){
                const node=nodes.get(name);
                    if (!node) {
                        continue;
                    }
                    const quaternion=channels?.rotation_quaternion,rotation=channels?.rotation;
                if(quaternion){const anim=new BABYLON.Animation(`block-clip-${clipKey}-${name}`,'rotationQuaternion',fps,BABYLON.Animation.ANIMATIONTYPE_QUATERNION,BABYLON.Animation.ANIMATIONLOOPMODE_CYCLE),keys=Object.entries(quaternion).map(([time,value])=>{const v=Array.isArray(value)?value:[0,0,0,1],q=new BABYLON.Quaternion(Number(v[0]||0),Number(v[1]||0),Number(v[2]||0),Number(v[3]??1));
                    return {frame:Number(time)*fps,value:q.normalize()};
                    }).sort((a,b)=>a.frame-b.frame);
                    if(keys.length){anim.setKeys(keys);
                    group.addTargetedAnimation(anim,node);
                    }}
                else if(rotation){const anim=new BABYLON.Animation(`block-clip-${clipKey}-${name}`,'rotation',fps,BABYLON.Animation.ANIMATIONTYPE_VECTOR3,BABYLON.Animation.ANIMATIONLOOPMODE_CYCLE),keys=Object.entries(rotation).map(([time,value])=>{const v=Array.isArray(value)?value:[0,0,0];
                    return {frame:Number(time)*fps,value:new BABYLON.Vector3(-BABYLON.Tools.ToRadians(Number(v[0]||0)),BABYLON.Tools.ToRadians(Number(v[1]||0)),-BABYLON.Tools.ToRadians(Number(v[2]||0)))};
                    }).sort((a,b)=>a.frame-b.frame);
                    if(keys.length){anim.setKeys(keys);
                    group.addTargetedAnimation(anim,node);
                    }}
                const position=channels?.position;
                    if(position){const base=node.position.clone(),anim=new BABYLON.Animation(`block-clip-${clipKey}-${name}-position`,'position',fps,BABYLON.Animation.ANIMATIONTYPE_VECTOR3,BABYLON.Animation.ANIMATIONLOOPMODE_CYCLE),keys=Object.entries(position).map(([time,value])=>{const v=Array.isArray(value)?value:[0,0,0],root=locomotionAt(time);
                    // The flat world-space bake includes the root's authored travel in
                    // every child position. Remove only that shared X/Z component so
                    // motionRoot owns global locomotion continuously across clips. Keep
                    // Y exactly as sampled so jumps/crouches/bob remain local animation.
                    const delta=new BABYLON.Vector3(-(Number(v[0]||0)-Number(root?.[0]||0))/16,Number(v[1]||0)/16,-(Number(v[2]||0)-Number(root?.[2]||0))/16);
                    return {frame:Number(time)*fps,value:base.add(delta)};
                        }).sort((a,b)=>a.frame-b.frame);
                        if(keys.length){anim.setKeys(keys);
                        group.addTargetedAnimation(anim,node);
                        }}
            }
            if(!group.targetedAnimations.length){group.dispose();
                continue;
                }group.from=0;
                group.to=length*fps;
                const profile=profileByClip.get(clipKey)||{},name=String(profile.name||clip.label||clipKey),entry=this.AnimationBehaviorEntry(group,name);
                if (profile.kind) {
                    entry.kind=String(profile.kind);
                }
                if (Number.isFinite(Number(profile.speed))) {
                    entry.speed=Number(profile.speed);
                }
                if (Number.isFinite(Number(profile.cycles))) {
                    entry.cycles=Math.max(1,Number(profile.cycles));
                }
                entry.holdSeconds=Number(profile.hold_seconds||profile.holdSeconds||entry.clipLength*entry.cycles);
                entries.push(entry);
                }
        const groundSize=Math.max(4,dimensions.width*2.5,dimensions.depth*2.5),ground=BABYLON.MeshBuilder.CreateGround('blockified-ground',{width:groundSize,height:groundSize},scene),gm=new BABYLON.StandardMaterial('blockified-ground-material',scene);
            gm.disableLighting=true;
            gm.emissiveColor=new BABYLON.Color3(.24,.56,.18);
            gm.diffuseColor=gm.emissiveColor;
            ground.material=gm;
        const roots=[rigRoot];
        this.SetupAnimationBehaviorCycle(card,scene,roots,entries,key,behavior);
            this.FrameAnimatedBlockifiedIdle(camera,occupied,bones,clips);
            this.engines.set(key,engine);
            this.scenes.set(key,scene);
            this.ActivatePreviewEngine(key);
            requestAnimationFrame(()=>engine.resize());
    }

    static async LoadBlockifiedPreview(card){
        // All local/saved Minecraft previews consume the canonical local Blockify
        // representation.  Provider cards are promoted to a saved-asset card before
        // reaching this method, so there must not be a second cached-preview path
        // that mutates or reloads a different representation.
        return this.LoadSavedMinecraftPreview(card);
    }

    static async LoadSavedMinecraftPreview(card){
        const folder=this.SavedFolderForCard(card);
        const versions=folder?.versions||[];
        const version=versions[Number(card.dataset.versionIndex||0)]||versions[0];
        if (!folder||!version) {
            throw new Error('Saved asset identity is unavailable.');
        }

        // UCI is the asset identity. Block Preview resolves the current local
        // Blockify representation from that identity and previews the persisted
        // representation package; it never regenerates or falls back to source.
        const uci=String(card.dataset.assignmentUci||version.uci||folder.uci||'').trim();
        if (!uci) {
            throw new Error('Saved asset UCI is unavailable.');
        }
        const response=await CIFRuntime.assetCommand({
            operation:'local-blockify-load',
            uci,
        });
        const blockify=response?.blockify||{};
        const voxels=Array.isArray(blockify.voxels)?blockify.voxels:[];
        if (voxels.length) {
            // Reopen the original Server's canonical UCI-owned voxel data using
            // the existing Blockify renderer, never an invented GLTF export.
            const dimensions={width:Number(blockify.width||1),height:Number(blockify.height||1),depth:Number(blockify.depth||1)};
            const animated=Array.isArray(blockify.character_voxels)&&blockify.character_voxels.length>0
                &&Array.isArray(blockify.character_rig?.bones)&&blockify.character_rig.bones.length>0
                &&Object.keys(blockify.character_animations||{}).length>0;
            const key=`saved-voxel:${uci}:${blockify.behavior_signature||''}:${dimensions.width}x${dimensions.height}x${dimensions.depth}`;
            if (animated) this.RenderAnimatedBlockifiedBehavior(card,blockify.character_voxels,blockify.character_rig,blockify.character_animations,blockify.behavior_profile||[],dimensions,key);
            else this.RenderBlockifiedVoxels(card,voxels,dimensions,key,Number(blockify.resolution||16));
            this.SetProgress(card,`Block Preview · ${voxels.length.toLocaleString()} occupied voxels · resolution ${Number(response?.resolution||blockify.resolution||16)}.`);
            return;
        }
        const modelPath=String(response?.model_path||'').trim();
        if (!modelPath) {
            throw new Error(`No saved Blockify voxels or model for ${uci}.`);
        }
        await this.LoadSavedPreview(card,modelPath);
        this.SetProgress(card,`Block Preview · resolution ${Number(response?.resolution||version.blockify?.resolution||0)||'unknown'}.`);
    }
}

console.info('[CIF Asset] CIFBlockifyPreview loaded');

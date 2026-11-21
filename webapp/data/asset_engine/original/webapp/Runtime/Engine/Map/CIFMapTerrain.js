/*
 * CIFMapTerrain
 * ----------------
 * Owns the authored castle/terrain shell used by Build and Play.
 *
 * Responsibilities:
 *   - Load story Terrain rows and the canonical Blockify voxel payload.
 *   - Build Babylon geometry for those voxels.
 *   - Apply/persist the authored castle transform.
 *   - Apply the authored cross-section used by the Build map view.
 *   - Pick/place/remove terrain voxels in Build.
 *   - Preserve the Build camera/view state.
 *   - Install the same authored Terrain into Play.
 *
 * Important ownership rule:
 *   The Terrain object owns only placement, visibility, and view state.  The
 *   assigned Blockify asset owns the voxel/palette data.  Build edits are saved
 *   back to that canonical asset; there is intentionally no Map-private voxel
 *   delta.
 *
 * Coordinate rule:
 *   Cross-section tests operate in Terrain-local voxel coordinates.  World
 *   placement is supplied by the Terrain root transform (position, Y rotation,
 *   and uniform scale).  Keep those two coordinate spaces separate.
 */
class CIFMapTerrain {
    static instances=[];
    static pendingAssetSaves=new Set();
    static selected=null;
    static root=null;
    static mesh=null;
    static config=null;
    static hoverBox=null;
    static hoverHit=null;
    static visible=true;
    static persistedSettings=null;
    static axis='z';
    static keep='plus';
    static block=0;
    static authoredSection={
        axis:'z',keep:'plus',block:0
    };

    static localCameraSaveTimer=null;
    static localViewSaveTimer=null;
    static STYLE={
        blockSize:1,edgeWidth:2.0,edgeColor:[.03,.03,.03],edgeAlpha:.72
    };
    // ---------------------------------------------------------------------
    // Terrain discovery and canonical voxel geometry
    // ---------------------------------------------------------------------

    static IsDracula() {
        return this.instances.length>0;
    }
    // Historical compatibility: old callers use this as a generic "has terrain" test.
    // Return the four DOM controls that define the authored cross-section.
    static Controls() {
        return {
            axis:ifGetElementById('map-shell-axis'),slice:ifGetElementById('map-shell-slice'),value:ifGetElementById('map-shell-slice-value'),flip:ifGetElementById('map-shell-flip')
        };
    }
    // Resolve the authoritative Terrain list for local projects, packaged runtime,
    // or the desktop authoring bridge.  The returned rows describe castle assets;
    // they do not duplicate the voxel payload into map state.
    static async TerrainRows() {
        const story=String(typeof activeGameStory==='function'?activeGameStory():'');
        const title=String(typeof activeGameTitle==='function'?activeGameTitle():story);
        let body= {};
        const authoringBase=String(window.CINTERFORGE_IF_AUTHORING_BASE||'').trim();
        const hasAuthoringCommandPlane=window.CINTERFORGE_IF_LOCAL_WORLD_ACTIVE===true||Boolean(authoringBase);
        if(hasAuthoringCommandPlane) {
            // A server-launched desktop Editor is intentionally RESOURCE_ONLY for
            // packaged story resources, but it still has an AUTHORING_BASE command
            // plane. Terrain must use that live authoring plane just like the other
            // asset commands do. LOCAL_WORLD_ACTIVE is only set by the browser-only
            // Project picker and therefore cannot be the desktop-authoring test.
            body=await CIFRuntime.assetCommand({
                operation:'terrain-world',alias:story,story_id:story,title
            });
        } else if(window.CINTERFORGE_IF_RESOURCE_ONLY) {
            // Session/Play and the live read-only Build inspector consume the same
            // packaged Terrain snapshot as every other story-owned resource.  They
            // must never depend on the optional Python authoring command bridge.
            body=await CIFRuntime.storyResourceJson( typeof activeGameKey==='function'?activeGameKey():story, 'terrain.json',{
                ok:true,terrain:[]
            } );
        }else {
            body=await CIFRuntime.assetCommand({
                operation:'terrain-world',alias:story,story_id:story,title
            });
        }
        const rows=Array.isArray(body.terrain)?body.terrain:[];
        // terrain-world is the authoritative post-migration Terrain list. Keep it
        // available to the World panel so that panel-manifest/WorldData timing cannot
        // temporarily report Terrain (0) while Build already has the same Castle.
        window.CIFPLAYER_TERRAIN_ROWS_BY_GAME=window.CIFPLAYER_TERRAIN_ROWS_BY_GAME|| {
        };
        /* World Browser keys its caches by activeGameKey(), while terrain-world is
                   addressed by story alias.  In single-story mode those are not guaranteed
                   to be the same string (for example STORY-Dracula vs dracula).  Publish the
                   authoritative rows under both identities so the World panel consumes the
                   exact Terrain list Build just loaded. */ const keys=new Set([String(story),String(typeof activeGameKey==='function'?activeGameKey():'')].filter(Boolean));
        for(const key of keys)window.CIFPLAYER_TERRAIN_ROWS_BY_GAME[key]=rows.map(row=>({
            ...row
        }));
        return rows;
    }

    static _bounds(voxels) {
        let minX=Infinity,minY=Infinity,minZ=Infinity,maxX=-Infinity,maxY=-Infinity,maxZ=-Infinity;
        for(const v of voxels) {
            const x=+v.x,y=+v.y,z=+v.z;
            if (![x,y,z].every(Number.isFinite)) {
                continue;
            }
            minX=Math.min(minX,x);
            minY=Math.min(minY,y);
            minZ=Math.min(minZ,z);
            maxX=Math.max(maxX,x);
            maxY=Math.max(maxY,y);
            maxZ=Math.max(maxZ,z);
        }
        return {
            minX,minY,minZ,maxX,maxY,maxZ
        };
    }
    // Convert Blockify voxels into one Babylon vertex/index buffer.  `transform`,
    // when supplied, maps each source voxel to [x, y, z, halfExtent].
    static BuildVoxelGeometry(voxels,transform=null) {
        const positions=[],indices=[],normals=[],colors=[],uvs=[],faceUV=[[0,0],[1,0],[1,1],[0,1]],faces=[[[ -1,-1,1],[1,-1,1],[1,1,1],[-1,1,1],[0,0,1]],[[1,-1,-1],[-1,-1,-1],[-1,1,-1],[1,1,-1],[0,0,-1]],[[1,-1,1],[1,-1,-1],[1,1,-1],[1,1,1],[1,0,0]],[[-1,-1,-1],[-1,-1,1],[-1,1,1],[-1,1,-1],[-1,0,0]],[[-1,1,1],[1,1,1],[1,1,-1],[-1,1,-1],[0,1,0]],[[-1,-1,-1],[1,-1,-1],[1,-1,1],[-1,-1,1],[0,-1,0]]];
        for(const v of (Array.isArray(voxels)?voxels:[])) {
            const c=transform?transform(v):[+v.x||0,+v.y||0,+v.z||0,.5],cx=+c[0]||0,cy=+c[1]||0,cz=+c[2]||0,h=Math.max(.000001,+c[3]||.5),r=(+v.r||0)/255,g=(+v.g||0)/255,b=(+v.b||0)/255;
            for(const f of faces) {
                const bi=positions.length/3,n=f[4];
                for(let j=0; j<4; j++) {
                    const q=f[j];
                    positions.push(cx+q[0]*h,cy+q[1]*h,cz+q[2]*h);
                    normals.push(...n);
                    colors.push(r,g,b,1);
                    uvs.push(...faceUV[j]);
                }
                /* This is the single canonical cube winding used by Build and Minecraft Preview. */indices.push(bi,bi+2,bi+1,bi,bi+3,bi+2);
            }
        }
        return {
            positions,indices,normals,colors,uvs
        };
    }

    static _geometry(mesh,voxels) {
        /* Every geometry rebuild must atomically replace the voxel source used by PickBlock. Canonical asset reload replaces inst.voxels with a fresh array; leaving metadata pointed at the pre-save array made newly placed/reloaded blocks visible but unselectable. */mesh.metadata={
            ...(mesh.metadata|| {
            }),terrainVoxelSource:voxels
        };
        const g=this.BuildVoxelGeometry(voxels),vd=new BABYLON.VertexData();
        vd.positions=g.positions;
        /* Minecraft Preview is the runtime-verified winding reference. Babylon already incorporates scene handedness in the view/projection transform; reversing the index buffer again for the Build scene made Build cull the opposite faces. Apply the exact canonical Preview indices in every scene. */vd.indices=g.indices;
        vd.normals=g.normals;
        vd.colors=g.colors;
        vd.uvs=g.uvs;
        vd.applyToMesh(mesh,true);
        mesh.refreshBoundingInfo();
    }

    static _material(scene,name) {
        /* Restore the accepted pre-Terrain renderer: face color + heavy voxel stroke +
                   section discard happen in ONE fragment pass.  EdgesRenderer was both thinner
                   and a second draw, so it could survive a section cut and was slower. */ /* Sectioning is authored in Terrain voxel coordinates, so render clipping must
                   use those exact same local coordinates as BlockVisible()/PickBlock(). */ BABYLON.Effect.ShadersStore['ifTerrainVoxelVertexShader']=`precision highp float;attribute vec3 position;attribute vec4 color;attribute vec2 uv;uniform mat4 world;uniform mat4 worldViewProjection;varying vec4 vColor;varying vec2 vUV;varying vec3 vTerrain;void main(){vTerrain=position;vColor=color;vUV=uv;gl_Position=worldViewProjection*vec4(position,1.0);}`;
        BABYLON.Effect.ShadersStore['ifTerrainVoxelFragmentShader']=`#extension GL_OES_standard_derivatives : enable\nprecision highp float;varying vec4 vColor;varying vec2 vUV;varying vec3 vTerrain;uniform vec4 sectionPlane;uniform float sectionEnabled;uniform vec4 edgeColor;uniform float edgeWidth;void main(){if(sectionEnabled>0.5&&dot(sectionPlane.xyz,vTerrain)+sectionPlane.w<0.0)discard;vec2 d=min(vUV,1.0-vUV);float e=min(d.x,d.y);float px=max(fwidth(e)*edgeWidth,0.00001);float interior=smoothstep(0.0,px,e);gl_FragColor=mix(edgeColor,vColor,interior);}`;
        const m=new BABYLON.ShaderMaterial(name,scene,{
            vertex:'ifTerrainVoxel',fragment:'ifTerrainVoxel'
        },{
            attributes:['position','color','uv'],uniforms:['world','worldViewProjection','sectionPlane','sectionEnabled','edgeColor','edgeWidth']
        });
        m.backFaceCulling=true;
        // IMPORTANT: do not override sideOrientation here. Minecraft Preview uses the
        // Babylon material default with this exact shared geometry and is the visual
        // reference. Babylon already accounts for scene handedness in its transform;
        // forcing the opposite cull convention in Build made the same cubes inside-out.
        m.alphaMode=BABYLON.Engine.ALPHA_DISABLE;
        m.setVector4('edgeColor',new BABYLON.Vector4(this.STYLE.edgeColor[0],this.STYLE.edgeColor[1],this.STYLE.edgeColor[2],this.STYLE.edgeAlpha));
        m.setFloat('edgeWidth',Math.max(.5,this.STYLE.edgeWidth));
        m.setFloat('sectionEnabled',0);
        m.setVector4('sectionPlane',new BABYLON.Vector4(0,1,0,0));
        return m;
    }

    static async _create(scene,row,index,forPlay=false) {
        if (row?.error) {
            console.error('[terrain] canonical Terrain Blockify data is incomplete', {
                id: row.id,
                uci: row.uci,
                error: row.error
            });
            return null;
        }
        const meta=row.blockify||{};
        const voxels=Array.isArray(meta.voxels)?meta.voxels.map(v=>({...v})):[];
        const root=new BABYLON.TransformNode(`terrain:${row.id}`,scene);
        // Every Terrain instance is a first-class placed World asset.  Put its stable
        // identity on the transform root at creation time, not only on the render
        // mesh or on the independent Play installation path.  Map/Test share these
        // exact instances, and World > Objects resolves transforms from this root.
        root.metadata={
            ...(root.metadata||{}),
            cinterforgeAssetObject:true,
            cinterforgeWorldTerrain:true,
            terrainId:String(row.id||''),
            recordId:String(row.id||''),
            dataset:'objects',
            cinterforgeAssetName:String(row.name||row.id||'Terrain'),
            cinterforgeAssetUCI:String(row.uci||'')
        };

        if(voxels.length) {
            const mesh=new BABYLON.Mesh(`terrain-blocks:${row.id}`,scene);
            mesh.parent=root;
            mesh.useVertexColors=true;
            mesh.hasVertexAlpha=false;
            mesh.material=this._material(scene,`terrain-material:${row.id}`);
            mesh.isPickable=!forPlay;
            mesh.metadata={terrain:true,terrainId:row.id,asset3d:row.asset3d,uci:row.uci};
            this._geometry(mesh,voxels);
            // Use Babylon's default material-side orientation. The canonical voxel
            // indices are already shared with Preview/Test; overriding the mesh side
            // orientation in a right-handed scene reverses culling and exposes cube interiors.
            const b=this._bounds(voxels);
            return {id:row.id,name:row.name,uci:String(row.uci||''),asset3d:row.asset3d,folder:row.folder,file:row.file,meta,voxels,root,mesh,meshes:[mesh],bounds:b,visible:true,voxelTerrain:true};
        }

        let asset3d='';
        try {
            if (forPlay) {
                asset3d=String(row.asset3d||'').trim();
            } else {
                const uci=String(row.uci||'').trim();
                if (uci) {
                    const resolved=await CIFRuntime.assetCommand({operation:'local-asset-resolve',uci});
                    asset3d=String(resolved?.block_model||'').trim();
                }
            }
        } catch (error) {
            console.error('[terrain] Terrain UCI resolution failed',{id:row.id,uci:row.uci,error:String(error?.message||error)});
        }
        if(!asset3d) {
            root.dispose();
            console.error(forPlay
                ? '[terrain] Terrain object has no loadable Play representation'
                : '[terrain] Terrain object has no canonical Blockify representation',
                {id:row.id,uci:row.uci});
            return null;
        }
        try {
            const modelUrl=await CIFRuntime.stagedAssetUrl('saved-file',{path:asset3d});
            const imported=await BABYLON.SceneLoader.ImportMeshAsync('','',modelUrl,scene);
            const importedMeshes=[];
            for(const node of imported.meshes||[]) {
                if(!node.parent) node.parent=root;
                if(node.getTotalVertices?.()>0) {
                    node.isPickable=true;
                    node.metadata={...(node.metadata||{}),terrain:true,terrainId:row.id,asset3d,uci:row.uci};

                    // This branch is for non-voxel Terrain representations only.
                    // Canonical Blockify Terrain is rendered above from blockify.json
                    // through _geometry() + the accepted Terrain shader.
                    importedMeshes.push(node);
                }
            }
            if(!importedMeshes.length) throw new Error('Terrain model contains no renderable meshes');
            root.computeWorldMatrix(true);
            let min=new BABYLON.Vector3(Infinity,Infinity,Infinity),max=new BABYLON.Vector3(-Infinity,-Infinity,-Infinity);
            for(const mesh of importedMeshes) {
                mesh.computeWorldMatrix(true);
                const box=mesh.getBoundingInfo().boundingBox;
                min=BABYLON.Vector3.Minimize(min,box.minimumWorld);
                max=BABYLON.Vector3.Maximize(max,box.maximumWorld);
            }
            const b={minX:min.x,minY:min.y,minZ:min.z,maxX:max.x,maxY:max.y,maxZ:max.z};
            return {id:row.id,name:row.name,uci:String(row.uci||''),asset3d,folder:row.folder,file:row.file,meta,voxels:[],root,mesh:importedMeshes[0],meshes:importedMeshes,bounds:b,visible:true,voxelTerrain:false};
        } catch(error) {
            root.dispose();
            console.error('[terrain] Terrain object model load failed',{id:row.id,uci:row.uci,asset3d,error:String(error?.message||error)});
            return null;
        }
    }
    // ---------------------------------------------------------------------
    // Authored Terrain placement
    // ---------------------------------------------------------------------

    static AuthoredTransformForRow(row, index = 0, allowLegacyPosition = true) {
        const state = this.persistedSettings || {};
        const version = Number(state.terrain_layout_version) || 0;
        const saved = state.terrain?.[row?.id];
        if (version >= 2 && saved) {
            return {
                position: Array.isArray(saved.position) ? saved.position.map(value => Number(value) || 0) : [0, 0, 0],
                rotation_y: Number(saved.rotation_y) || 0,
                scale: Math.max(0.01, Number(saved.scale) || 1),
                visible: saved.visible !== false
            };
        }

        // Legacy anonymous Terrain transforms are intentionally not consumed here.
        // Runtime Terrain placement is per asset only.  Build/update-data is responsible
        // for converting legacy story seed data to an explicit transform keyed by the
        // Terrain object's stable World id before that story is distributed.
        return null;
    }

    static _applySaved(inst, index, allowLegacyPosition = true) {
        const transform = this.AuthoredTransformForRow(inst, index, allowLegacyPosition);
        if (!transform) {
            return false;
        }
        inst.root.position.set(transform.position[0], transform.position[1], transform.position[2]);
        inst.root.rotationQuaternion = BABYLON.Quaternion.FromEulerAngles(0, transform.rotation_y, 0);
        inst.root.scaling.setAll(transform.scale);
        inst.visible = transform.visible !== false;
        inst.root.setEnabled(inst.visible);
        console.info('[terrain] applied authored transform', {
            id: inst.id,
            position: transform.position.slice(),
            rotation_y: transform.rotation_y,
            scale: transform.scale
        });
        return true;
    }

    static _defaultPlace(inst,index) {
        if (index!==0) {
            return false;
        }
        const rooms=[...babylonRoomNodes.values()];
        let cx=0,cz=0,ground=0;
        if(rooms.length) {
            let low=Infinity;
            for(const r of rooms) {
                const p=r.getAbsolutePosition?r.getAbsolutePosition():r.position;
                cx+=p.x;
                cz+=p.z;
                low=Math.min(low,p.y);
            }
            cx/=rooms.length;
            cz/=rooms.length;
            ground=Number.isFinite(low)?low:0;
        }
        const b=inst.bounds,lx=(b.minX+b.maxX)/2,lz=(b.minZ+b.maxZ)/2;
        /* Root translation places the Terrain LOCAL center on the room/world center. The old +localCenter moved it away by the same offset instead of cancelling that offset. */inst.root.position.set(cx-lx,ground-b.minY,cz-lz);
        inst.root.rotationQuaternion=BABYLON.Quaternion.FromEulerAngles(0,Math.PI,0);
        console.info('[terrain] default placement',{
            id:inst.id,roomCenter:[cx,ground,cz],localCenter:[lx,(b.minY+b.maxY)/2,lz],position:[inst.root.position.x,inst.root.position.y,inst.root.position.z]
        });
        return true;
    }
    // ---------------------------------------------------------------------
    // Map installation, selection, section visibility, and picking
    // ---------------------------------------------------------------------

    static async Install() {
        this.Dispose();
        this.buildPlacementReconciled=false;
        if (!window.BABYLON||!babylonScene) {
            return;
        }
        await this.LoadPersistedSettings();
        /* Persisted checkerboard size is loaded here, after the base Map scene may already have built its floor planes. Rebuild them now so the rendered checker geometry and the World controls use the same restored global X/Z cell counts. */if(typeof CIFMapRuntime!=='undefined'&&CIFMapRuntime.rebuildFloorSceneryAfterAuthoring&&typeof mapLayerLevels!=='undefined'&&mapLayerLevels.length)CIFMapRuntime.rebuildFloorSceneryAfterAuthoring();
        if (typeof CIFMapRuntime!=='undefined'&&CIFMapRuntime.createMapEnvironment) {
            CIFMapRuntime.createMapEnvironment();
        }
        let rows=[];
        try {
            rows=await this.TerrainRows();
            /* terrain-world may perform the one-time legacy-shell promotion on the server. Refresh the client WorldData cache immediately so World > Objects and Assets see the exact same Terrain records instead of the pre-migration snapshot. */const game=String(typeof activeGameKey==='function'?activeGameKey():'');
            if (game&&window.CINTERFORGE_IF_LOCAL_WORLD_ACTIVE!==true&&window.CIFWorldStore?.loadWorldDataForGame) {
                await CIFWorldStore.loadWorldDataForGame(game,true);
            }
            if (window.CIFPanels?.renderObjectBrowser) {
                await CIFPanels.renderObjectBrowser();
            }
        }catch(e) {
            console.error('[terrain] load failed',e);
            return;
        }
        for(let i=0; i<rows.length; i++) {
            const inst=await this._create(babylonScene,rows[i],i,false);
            if (!inst) {
                continue;
            }
            if(!this._applySaved(inst,i)) {
                if (this._defaultPlace(inst,i)) {
                    this.buildPlacementReconciled=true;
                }
            }
            this.instances.push(inst);
        }

        /*
         * Terrain is loaded after the base Map room/floor scene. Until the authored
         * Terrain transform exists, CIFWorldGridAuthority cannot know the actual
         * Blockify lattice and the Map can temporarily build its checker from room
         * spacing instead. That creates exactly the split frame we must avoid: the
         * castle blocks use the authored Terrain transform while the checker/rooms
         * use a synthetic cell size/origin.
         *
         * Now that every Terrain root has its final authored transform, establish the
         * Terrain block lattice as the single X/Z authority, snap canonical rooms to
         * block centers, and rebuild the checker from that same authority. This is
         * generic for any voxel Terrain; there is no story-specific coordinate fix.
         */
        if (this.instances.some(instance => instance?.voxelTerrain)) {
            if (window.CIFWorldEditorSpatial?.alignRoomsToTerrainBlockLattice) {
                CIFWorldEditorSpatial.alignRoomsToTerrainBlockLattice();
            }
            if (
                typeof CIFMapRuntime !== 'undefined' &&
                CIFMapRuntime.rebuildFloorSceneryAfterAuthoring &&
                typeof mapLayerLevels !== 'undefined' &&
                mapLayerLevels.length
            ) {
                CIFMapRuntime.rebuildFloorSceneryAfterAuthoring();
            }
        }

        this.Select(this.instances[0]||null);
        this.visible=true;
        this.BindControls();
        this.BindAssetSelection();
        this.Apply();
        if(this.instances.length) {
            if (typeof mapViewMode!=='undefined') {
                mapViewMode='chess';
            }
            if (typeof mapLayerFilter!=='undefined') {
                mapLayerFilter=null;
            }
            /* Install() owns Terrain only. It must never mutate the Map camera.
               Camera restore/frame policy runs once, at the end of Map activation,
               after Terrain reconciliation and controller setup are complete.  Framing
               here used to overwrite the machine-local view before RestoreBuildCamera()
               had a chance to read it. */
        }
        console.info('[terrain] Map installed',{
            instances:this.instances.length,voxels:this.instances.reduce((n,v)=>n+v.voxels.length,0)
        });
    }

    static Select(inst) {
        if (!inst) {
            return;
        }
        this.selected=inst;
        this.root=inst.root;
        this.mesh=inst.mesh;
        this.config={
            meta:inst.meta,voxels:inst.voxels,...inst.bounds
        };
        if (window.CIFMapTerrainTransformCage?.Attach) {
            CIFMapTerrainTransformCage.Attach(inst.root);
        }
        /* The transform panel is created by Attach(), after the 3D toolbar may already have rendered. Re-render once so the original numeric X/Y/Z/Y°/Scale editor is mounted immediately instead of remaining detached/hidden until a category change. */
        if (typeof renderMapContextControls==='function') {
            renderMapContextControls();
        }
    }

    static Palette() {
        const out=[],seen=new Set();
        for(const inst of this.instances) {
            const rows=(inst.meta.palette||[]).length?inst.meta.palette:inst.voxels||[];
            for(const p of rows) {
                const k=`${p.r|0},${p.g|0},${p.b|0}`;
                if(!seen.has(k)) {
                    seen.add(k);
                    out.push({
                        r:p.r|0,g:p.g|0,b:p.b|0,index:out.length
                    });
                }
            }
        }
        return out;
    }

    static WorldBounds() {
        let min=null,max=null;
        for(const i of this.instances) {
            if (!i.visible||!i.root.isEnabled()) {
                continue;
            }
            try {
                i.root.computeWorldMatrix(true);
                i.mesh.computeWorldMatrix(true);
                const b=i.mesh.getHierarchyBoundingVectors(true);
                min=min?BABYLON.Vector3.Minimize(min,b.min):b.min.clone();
                max=max?BABYLON.Vector3.Maximize(max,b.max):b.max.clone();
            }catch(_) {
            }
        }
        return min&&max? {
            min,max
        }
        :null;
    }
    // Cross-section visibility is evaluated in Terrain-local voxel coordinates.
    static BlockVisible(inst,v) {
        if (!inst.visible||!inst.root.isEnabled()) {
            return false;
        }
        if (this.axis==='off') {
            return true;
        }
        const a=this.axis==='x'?+v.x:this.axis==='y'?+v.y:+v.z;
        return this.keep==='minus'?a<=this.block:a>=this.block;
    }
    // Ray-pick the visible voxel under the pointer.  The result remains in the
    // canonical voxel coordinate system so edits can be written directly back.
    static PickBlock(clientX,clientY,options=null) {
        const pickingScene=options?.scene||babylonScene;
        const pickingCanvas=options?.canvas||mapCanvas;
        const pickingCamera=options?.camera||pickingScene?.activeCamera||babylonCamera;
        const pickingInstances=Array.isArray(options?.instances)?options.instances:this.instances;
        const ignoreSectionVisibility=options?.ignoreSectionVisibility===true;
        if (!pickingScene||!pickingCanvas||!pickingCamera||!window.BABYLON) {
            return null;
        }
        const canvasBounds = pickingCanvas.getBoundingClientRect();
        const clientLocalX = clientX - canvasBounds.left;
        const clientLocalY = clientY - canvasBounds.top;
        if (clientLocalX < 0 || clientLocalY < 0 || clientLocalX > canvasBounds.width || clientLocalY > canvasBounds.height) {
            return null;
        }

        // Map already supplies coordinates in the contract its Babylon scene expects.
        // Test's shared scene can render at a different backing-store size than the CSS
        // canvas (Retina / hardware scaling).  Only Test opts into this conversion so the
        // established Map picker remains byte-for-byte behaviorally unchanged.
        let x = clientLocalX;
        let y = clientLocalY;
        if (options?.scaleClientCoordinatesToRender === true) {
            const renderWidth = Number(pickingScene.getEngine?.()?.getRenderWidth?.()) || Number(pickingCanvas.width) || canvasBounds.width;
            const renderHeight = Number(pickingScene.getEngine?.()?.getRenderHeight?.()) || Number(pickingCanvas.height) || canvasBounds.height;
            if (canvasBounds.width > 0 && canvasBounds.height > 0) {
                x = clientLocalX * renderWidth / canvasBounds.width;
                y = clientLocalY * renderHeight / canvasBounds.height;
            }
        }
        const instances=pickingInstances.filter(i=>i.visible!==false&&i.root?.isEnabled?.()!==false&&i.mesh?.isEnabled?.()!==false);
        const byMesh=new Map(instances.map(i=>[i.mesh,i]));
        /* Build one unbounded camera ray.  Mesh picking is the fast path; the voxel
                   fallback below uses this SAME ray and therefore has no camera-distance gate. */ /* Use Babylon's canvas-space picker as the primary path. This is the same
                   coordinate contract used by the working room/checker targeting code. The
                   collaboration refresh regression switched Terrain to an explicit ray first;
                   on Safari that ray could miss the visible Castle while the checker picker
                   continued to work. */ let hits=[];
        if (typeof pickingScene.multiPick==='function') {
            hits=pickingScene.multiPick(x,y,m=>byMesh.has(m))||[];
        }
        if(!hits.length&&typeof pickingScene.pick==='function') {
            const one=pickingScene.pick(x,y,m=>byMesh.has(m));
            if (one&&one.hit) {
                hits.push(one);
            }
        }
        let ray=null;
        /* Section rendering discards fragments in the Terrain shader, while Babylon's
                   mesh picker still sees those triangles.  Use Babylon's canonical picking
                   ray for the exact pointer coordinate, then let the CPU voxel fallback skip
                   section-hidden voxels.  Do not derive the ray from an arbitrary scene hit
                   (often the checkerboard): that changed the ray and broke Castle hover. */ if(typeof pickingScene.createPickingRay==='function') {
            ray=pickingScene.createPickingRay(x,y,BABYLON.Matrix.Identity(),pickingCamera);
            if (ray) {
                ray.length=Number.MAX_VALUE;
            }
        }
        hits.sort((a,b)=>(Number.isFinite(Number(a?.distance))?Number(a.distance):Infinity)-(Number.isFinite(Number(b?.distance))?Number(b.distance):Infinity));
        const dirs=[[0,0,1],[0,0,-1],[1,0,0],[-1,0,0],[0,1,0],[0,-1,0]];
        for(const pick of hits) {
            if (!pick?.hit||pick.faceId<0) {
                continue;
            }
            const inst=byMesh.get(pick.pickedMesh);
            if (!inst) {
                continue;
            }
            const source=(pick.pickedMesh.metadata&&pick.pickedMesh.metadata.terrainVoxelSource)||inst.voxels,index=Math.floor(pick.faceId/12),v=source[index];
            if (!v||(!ignoreSectionVisibility&&!this.BlockVisible(inst,v))) {
                continue;
            }
            const fi=Math.floor((pick.faceId%12)/2);
            return {
                inst,index,voxel:v,dir:dirs[fi]||[0,1,0],pick
            };
        }
        /* Do not let Babylon's mesh-pick implementation become an accidental reach
                   limit.  At long orbit radii some browser/GPU combinations have returned no
                   triangle hit even though the terrain is plainly visible.  Fall back to an
                   exact ray-vs-voxel test in each Terrain instance's LOCAL space.  This runs
                   only after the normal mesh pick misses, so ordinary hover stays cheap. */ if(!ray)return null;
        let best=null;
        for(const inst of instances) {
            const inv=inst.root.getWorldMatrix().clone();
            inv.invert();
            const o=BABYLON.Vector3.TransformCoordinates(ray.origin,inv);
            const d=BABYLON.Vector3.TransformNormal(ray.direction,inv);
            const dl=d.length();
            if (!(dl>1e-12)) {
                continue;
            }
            d.scaleInPlace(1/dl);
            const source=(inst.mesh.metadata&&inst.mesh.metadata.terrainVoxelSource)||inst.voxels;
            for(let index=0; index<source.length; index++) {
                const v=source[index];
                if (!v||(!ignoreSectionVisibility&&!this.BlockVisible(inst,v))) {
                    continue;
                }
                const mins=[+v.x-.5,+v.y-.5,+v.z-.5],maxs=[+v.x+.5,+v.y+.5,+v.z+.5],ov=[o.x,o.y,o.z],dv=[d.x,d.y,d.z];
                let t0=0,t1=Number.MAX_VALUE,axis=-1,entrySign=0,miss=false;
                for(let a=0; a<3; a++) {
                    if(Math.abs(dv[a])<1e-12) {
                        if(ov[a]<mins[a]||ov[a]>maxs[a]) {
                            miss=true;
                            break;
                        }
                        continue;
                    }
                    let near=(mins[a]-ov[a])/dv[a],far=(maxs[a]-ov[a])/dv[a],sign=-1;
                    if(near>far) {
                        const q=near;
                        near=far;
                        far=q;
                        sign=1;
                    }
                    if(near>t0) {
                        t0=near;
                        axis=a;
                        entrySign=sign;
                    }
                    t1=Math.min(t1,far);
                    if(t0>t1) {
                        miss=true;
                        break;
                    }
                }
                if (miss||t1<0) {
                    continue;
                }
                const localT=t0>=0?t0:t1;
                const lp=o.add(d.scale(localT));
                const wp=BABYLON.Vector3.TransformCoordinates(lp,inst.root.getWorldMatrix());
                const worldDistance=BABYLON.Vector3.Distance(ray.origin,wp);
                if (best&&worldDistance>=best.distance) {
                    continue;
                }
                const dir=[0,0,0];
                if (axis>=0) {
                    dir[axis]=entrySign;
                }
                best={
                    inst,index,voxel:v,dir,distance:worldDistance,pick:{
                        hit:true,pickedMesh:inst.mesh,pickedPoint:wp,distance:worldDistance,faceId:-1
                    }
                };
            }
        }
        return best;
    }

    static UpdateHover(x,y) {
        const hit=this.PickBlock(x,y);
        this.hoverHit=hit;
        if (hit) {
            this.Select(hit.inst);
        }
        if(!this.hoverBox&&babylonScene) {
            this.hoverBox=BABYLON.MeshBuilder.CreateBox('terrain-build-hover',{
                size:1.025
            },babylonScene);
            const m=new BABYLON.StandardMaterial('terrain-build-hover-mat',babylonScene);
            m.wireframe=true;
            m.emissiveColor=new BABYLON.Color3(1,1,1);
            m.disableLighting=true;
            this.hoverBox.material=m;
            this.hoverBox.isPickable=false;
        }
        if(this.hoverBox) {
            this.hoverBox.setEnabled(!!hit);
            if(hit) {
                this.hoverBox.parent=hit.inst.root;
                this.hoverBox.position.set(+hit.voxel.x,+hit.voxel.y,+hit.voxel.z);
            }
        }
        return hit;
    }
    // ---------------------------------------------------------------------
    // Voxel editing and collaboration refresh
    // ---------------------------------------------------------------------

    static async _persistAsset(inst) {
        /* Block edits can arrive faster than a blockify + mcaddon save.  Serialize and
                   coalesce saves per Terrain instance so an older response can never reload
                   stale voxels over a newer on-screen edit (the visible flicker/lost edit). */ inst._saveRequested=(inst._saveRequested||0)+1;
        if (inst._savePromise) {
            return inst._savePromise;
        }
        inst._savePromise=(async()=> {
            let result=null;
             while(inst._saveCompleted!==inst._saveRequested) {
                const generation=inst._saveRequested,m=inst.meta,voxels=inst.voxels.map(v=>({
                    ...v
                }));
                 result=await CIFRuntime.assetCommand({
                    operation:'local-blockify-save',uci:inst.uci,width:+m.width||1,height:+m.height||1,depth:+m.depth||1,resolution:+m.resolution||16,voxels,product_name:inst.name
                });
                    this.pendingCollaborationAssetPaths=this.pendingCollaborationAssetPaths||new Set();
                    for(const path of (result?.cloudflare_paths||[]))this.pendingCollaborationAssetPaths.add(String(path));
                    inst._saveCompleted=generation;
            }
            const loaded=await CIFRuntime.assetCommand({
                operation:'local-blockify-load',uci:inst.uci
            });
             const fresh=loaded?.blockify||{};
             inst.meta=fresh;
             inst.voxels=Array.isArray(fresh.voxels)?fresh.voxels.map(v=>({
                ...v
            })):[];
                this._geometry(inst.mesh,inst.voxels);
                if (this.selected===inst) {
                    this.Select(inst);
                }
                window.dispatchEvent(new CustomEvent('cinterforge-terrain-asset-changed',{
                detail:{
                    id:inst.id,asset3d:inst.asset3d
                }
            }));
             return result;
        })().finally(()=> {
            this.pendingAssetSaves.delete(inst._savePromise);
            inst._savePromise=null;
            if (inst._saveCompleted!==inst._saveRequested) {
                this._persistAsset(inst).catch(error =>
                    console.error('[terrain] canonical asset save failed', error)
                );
            }
        });
        this.pendingAssetSaves.add(inst._savePromise);
        return inst._savePromise;
    }

    static dirtyAssetInstances = new Set();

    static async _saveAsset(inst) {
        // Editing changes the live mesh only. Save World is the explicit local-disk
        // reconciliation boundary; Push is separately responsible for cloud transfer.
        if (inst) this.dirtyAssetInstances.add(inst);
        return {ok:true,deferred:true};
    }

    static async FlushPendingAssetSaves() {
        // Save World calls this explicitly. Persist every edited managed asset to
        // local disk and do not contact Cloudflare.
        const dirty=Array.from(this.dirtyAssetInstances);
        this.dirtyAssetInstances.clear();
        for (const inst of dirty) await this._persistAsset(inst);
        while (this.pendingAssetSaves.size) {
            await Promise.all(Array.from(this.pendingAssetSaves));
        }
        return true;
    }

    static async ReloadCanonicalAssets() {
        // Map is a local-disk view. Re-entering Map deliberately discards the
        // previous live mesh and reconstructs each managed asset from local disk.
        await this.FlushPendingAssetSaves();
        let reloaded = 0;
        for (const inst of this.instances) {
            if (!inst.asset3d) {
                continue;
            }
            await inst._savePromise;
            const loaded = await CIFRuntime.assetCommand({
                operation: 'local-blockify-load',
                uci: inst.uci
            });
            const fresh = loaded?.blockify || {};
            inst.meta = fresh;
            inst.voxels = Array.isArray(fresh.voxels)
                ? fresh.voxels.map(voxel => ({ ...voxel }))
                : [];
            inst.bounds = this._bounds(inst.voxels);
            this._geometry(inst.mesh, inst.voxels);
            reloaded += 1;
        }
        if (this.selected) {
            this.Select(this.selected);
        }
        this.Apply();
        this.UpdateStrokeForCamera();
        return reloaded;
    }

    static async RefreshCollaborationAssets(paths) {
        const changed=(Array.isArray(paths)?paths:[]).map(p=>String(p||'').replace(/\\/g,'/').replace(/^\/+/,''));
        if(!changed.length||!this.instances.length)return {
            matched:0,reloaded:0
        };
        let matched=0,reloaded=0;
        for(const inst of this.instances) {
            const folder=String(inst.folder||'').replace(/\\/g,'/').replace(/^\/+|\/+$/g,'');
            if (!folder) {
                continue;
            }
            const prefix=`${folder}/`.toLowerCase();
            const touches=changed.some(path=> {
                const rel=path.toLowerCase();
                    return rel.startsWith(prefix)||rel.includes(`/assets/${prefix}`)||rel.includes(`/builds/assets/${prefix}`);
            });
            if (!touches) {
                continue;
            }
            matched++;
            try {
                const loaded=await CIFRuntime.assetCommand({
                    operation:'local-blockify-load',uci:inst.uci
                });
                const fresh=loaded?.blockify||{};
                inst.meta=fresh;
                inst.voxels=Array.isArray(fresh.voxels)?fresh.voxels.map(v=>({
                    ...v
                })):[];
                inst.mesh.isPickable=true;
                inst.bounds=this._bounds(inst.voxels);
                this._geometry(inst.mesh,inst.voxels);
                if (this.selected===inst) {
                    this.Select(inst);
                }
                reloaded++;
                console.info('[terrain] collaboration asset reloaded',{
                    id:inst.id,folder:inst.folder,file:inst.file,voxels:inst.voxels.length
                });
            }catch(error) {
                console.error('[terrain] collaboration asset reload failed',{
                    id:inst.id,folder:inst.folder,file:inst.file,error:String(error?.message||error)
                });
            }
        }
        if(reloaded) {
            this.Apply();
            this.UpdateStrokeForCamera();
        }
        return {
            matched,reloaded
        };
    }

    static ConsumeCollaborationAssetPaths() {
        const paths=Array.from(this.pendingCollaborationAssetPaths||[]);
        this.pendingCollaborationAssetPaths=new Set();
        return paths;
    }

    static EditAtHover(place,item) {
        let hit=this.hoverHit;
        if (!hit&&Number.isFinite(Number(window.CIFPLAYER_LAST_MAP_POINTER_X))&&Number.isFinite(Number(window.CIFPLAYER_LAST_MAP_POINTER_Y))) {
            hit=this.UpdateHover(Number(window.CIFPLAYER_LAST_MAP_POINTER_X),Number(window.CIFPLAYER_LAST_MAP_POINTER_Y));
        }
        if (!hit) {
            return false;
        }
        const inst=hit.inst,key=v=>`${v.x|0},${v.y|0},${v.z|0}`;
        if(place) {
            if (!item||item.type!=='block') {
                return false;
            }
            /* The hover target is already the exact front-most voxel/face selected by UpdateHover. Do not perform a second pick from cached pointer coordinates here: after a collaboration reload/canvas resize those coordinates can be stale, which made the visible Castle target reject placement. */const [dx,dy,dz]=hit.dir,v={
                x:(hit.voxel.x|0)+dx,y:(hit.voxel.y|0)+dy,z:(hit.voxel.z|0)+dz,r:item.r|0,g:item.g|0,b:item.b|0,palette_index:item.palette_index|0,samples:1
            };
            if (inst.voxels.some(q=>key(q)===key(v))) {
                return false;
            }
            inst.voxels.push(v);
        }else {
            const k0=key(hit.voxel),ri=inst.voxels.findIndex(q=>key(q)===k0);
            if (ri<0) {
                return false;
            }
            inst.voxels.splice(ri,1);
        }
        this.hoverHit=null;
        if (this.hoverBox) {
            this.hoverBox.setEnabled(false);
        }
        this._geometry(inst.mesh,inst.voxels);
        this._saveAsset(inst).catch(e=>console.error('[terrain] canonical asset save failed',e));
        this.MarkAuthoredSettingsDirty();
        return true;
    }
    // ---------------------------------------------------------------------
    // Map view/camera persistence
    // ---------------------------------------------------------------------

    static StoreSettings() {
        const terrain= {};
        const authoredInstances = this.instances.length ? this.instances : (this.playInstances || []);
        for(const i of authoredInstances) {
            const presentedTransform = window.CITestRuntime?.AuthoredTransformForPresentedNode?.(i.root) || null;
            const e=i.root.rotationQuaternion?i.root.rotationQuaternion.toEulerAngles():i.root.rotation;
            terrain[i.id]={
                position:presentedTransform?.position || [i.root.position.x,i.root.position.y,i.root.position.z],
                rotation_y:presentedTransform?.rotation_y ?? e.y,
                scale:presentedTransform?.scale ?? i.root.scaling.x,
                visible:i.visible!==false,
                asset3d:i.asset3d
            };
        }
        this.persistedSettings={
            terrain_layout_version:3,terrain,section:{
                ...this.authoredSection
            },build_camera:this.CaptureBuildCamera()||this.persistedSettings?.build_camera||null,floor_offsets:(typeof mapFloorRelativeOffsets!=='undefined'?Object.fromEntries(mapFloorRelativeOffsets):{
            }),checkerboard_cells:(typeof mapCheckerboardCellCounts!=='undefined'&&mapCheckerboardCellCounts? {
                x:Math.max(1,Math.round(+mapCheckerboardCellCounts.x||1)),z:Math.max(1,Math.round(+mapCheckerboardCellCounts.z||1))
            }
            :null),floor_names:(typeof mapFloorNames!=='undefined'?Object.fromEntries(mapFloorNames):{
            }),authored_floors:(typeof mapAuthoredFloors!=='undefined'?Object.fromEntries(mapAuthoredFloors):{
            }),floor_spacing:(typeof mapLayerSpacingSetting!=='undefined'?Math.max(.01,Math.min(5,+mapLayerSpacingSetting||1)):1),build_inventory:window.CIPlayerInventory?.BuildSnapshot?.()||this.persistedSettings?.build_inventory||null
        };
        if (typeof unifiedSpatialScene!=='undefined'&&unifiedSpatialScene) {
            unifiedSpatialScene.map_shell_transform=this.persistedSettings;
        }
        return true;
    }

    static CaptureBuildCamera() {
        if (typeof babylonCamera==='undefined'||!babylonCamera||!window.BABYLON) {
            return null;
        }

        // Fly uses a temporary UniversalCamera.  Persist the view the user is
        // actually looking through, not the dormant ArcRotate camera underneath
        // it.  Store the result in the existing orbit representation so Map can
        // restore the same eye/heading on the next application launch.
        const activeCamera = babylonScene?.activeCamera || babylonCamera;
        if (activeCamera !== babylonCamera && activeCamera?.position) {
            const radius = Math.max(.01, Number(babylonCamera.radius) || 10);
            const forward = activeCamera.getForwardRay?.(1)?.direction?.clone?.();
            if (forward && forward.lengthSquared() > .000001) {
                forward.normalize();
                const target = activeCamera.position.add(forward.scale(radius));
                const offset = activeCamera.position.subtract(target);
                const horizontal = Math.sqrt(offset.x * offset.x + offset.z * offset.z);
                return {
                    alpha: Math.atan2(offset.z, offset.x),
                    beta: Math.atan2(horizontal, offset.y),
                    radius,
                    position: [+activeCamera.position.x, +activeCamera.position.y, +activeCamera.position.z],
                    target: [+target.x, +target.y, +target.z],
                    fov: +activeCamera.fov
                };
            }
        }

        if (!babylonCamera.target) {
            return null;
        }
        return {
            alpha:+babylonCamera.alpha,beta:+babylonCamera.beta,radius:+babylonCamera.radius,position:[+babylonCamera.position.x,+babylonCamera.position.y,+babylonCamera.position.z],target:[+babylonCamera.target.x,+babylonCamera.target.y,+babylonCamera.target.z],fov:+babylonCamera.fov
        };
    }

    static RestorePersistedBuildCamera() {
        const c=this.persistedSettings?.build_camera;
        if (!c||typeof babylonCamera==='undefined'||!babylonCamera||!window.BABYLON) {
            return false;
        }
        const t=Array.isArray(c.target)?c.target:null;
        if (!t||t.length<3||![c.alpha,c.beta,c.radius,t[0],t[1],t[2]].every(v=>Number.isFinite(Number(v)))) {
            return false;
        }
        this.restoringLocalCamera=true;
        try {
            const target = new BABYLON.Vector3(+t[0], +t[1], +t[2]);
            const position = Array.isArray(c.position) && c.position.length >= 3 && c.position.every(value => Number.isFinite(Number(value)))
                ? new BABYLON.Vector3(+c.position[0], +c.position[1], +c.position[2])
                : null;
            babylonCamera.setTarget(target);
            if (position) {
                // Position is the authoritative persisted Map view. ArcRotate's
                // alpha/beta convention is easy to reconstruct incorrectly from a
                // Fly camera; setPosition lets Babylon derive those values itself.
                babylonCamera.setPosition(position);
            } else {
                // Backward compatibility for camera records written before exact
                // eye position became part of the personal Map-view schema.
                babylonCamera.alpha=+c.alpha;
                babylonCamera.beta=+c.beta;
                babylonCamera.radius=Math.max(.01,+c.radius);
            }
            if (Number.isFinite(Number(c.fov))) {
                babylonCamera.fov=Math.max(.01,Math.min(Math.PI-.01,+c.fov));
            }
            if (typeof CIFMapRuntime!=='undefined'&&CIFMapRuntime.configureOrbitCameraControls) {
                CIFMapRuntime.configureOrbitCameraControls();
            }
        }finally {
            this.restoringLocalCamera=false;
        }
        console.info('[build-camera] restored saved World view');
        return true;
    }

    static BuildViewSpatialSignature() {
        const roomRows = [...(babylonRoomNodes?.entries?.() || [])]
            .map(([roomId, roomNode]) => {
                roomNode?.computeWorldMatrix?.(true);
                const position = roomNode?.getAbsolutePosition?.();
                if (!position) {
                    return null;
                }
                const round = value => Math.round(Number(value || 0) * 1000) / 1000;
                return [String(roomId), round(position.x), round(position.y), round(position.z)];
            })
            .filter(Boolean)
            .sort((left, right) => left[0].localeCompare(right[0]));

        const terrainRows = (this.instances || [])
            .map(instance => {
                const root = instance?.root;
                if (!root) {
                    return null;
                }
                root.computeWorldMatrix?.(true);
                const round = value => Math.round(Number(value || 0) * 1000) / 1000;
                const position = root.position || {};
                const scaling = root.scaling || {};
                return [
                    String(instance.id || ''),
                    round(position.x),
                    round(position.y),
                    round(position.z),
                    round(scaling.x),
                    round(scaling.y),
                    round(scaling.z)
                ];
            })
            .filter(Boolean)
            .sort((left, right) => left[0].localeCompare(right[0]));

        return JSON.stringify({ rooms: roomRows, terrain: terrainRows });
    }

    static CaptureLocalBuildView() {
        return {
            camera:this.CaptureBuildCamera(),
            section:{
                ...this.authoredSection
            },
            spatialSignature:this.BuildViewSpatialSignature()
        };
    }

    static LocalBuildViewStorageKey() {
        return `cinterforge.if.localBuildView.${String(typeof activeGameKey==='function'?activeGameKey():'story')}`;
    }

    static SaveLocalBuildViewMirror(view) {
        if (!view?.camera) {
            return false;
        }
        try {
            localStorage.setItem(this.LocalBuildViewStorageKey(), JSON.stringify(view));
            return true;
        } catch (_) {
            return false;
        }
    }

    static LoadLocalBuildViewMirror() {
        try {
            return JSON.parse(localStorage.getItem(this.LocalBuildViewStorageKey()) || 'null');
        } catch (_) {
            return null;
        }
    }

    static async SaveLocalBuildView() {
        const view=this.CaptureLocalBuildView();
        if (!view.camera) {
            return false;
        }
        // Camera position is personal editor state and must survive an application
        // quit.  Mirror it synchronously before any asynchronous authoring-server
        // request so page/process teardown cannot race the only copy of the view.
        const mirrored=this.SaveLocalBuildViewMirror(view);
        console.info('[build-view] saved local mirror', {
            key: this.LocalBuildViewStorageKey(),
            position: view.camera?.position || null,
            target: view.camera?.target || null
        });
        if(window.CINTERFORGE_IF_LOCAL_WORLD_ACTIVE===true) {
            try {
                return mirrored;
            }catch(_) {
                return false;
            }
        }
        try {
            const r=await CIFRuntime.authoringFetch('/world/local-camera',{
                method:'POST',headers:{
                    'Content-Type':'application/json'
                },body:JSON.stringify({
                    view
                }),cache:'no-store'
            }),b=await r.json();
            if(!r.ok||b.ok===false) {
                console.error('[build-view] local save failed',b.error||`HTTP ${r.status}`);
                return false;
            }
            return true;
        }catch(e) {
            console.error('[build-view] local save failed',e);
            return false;
        }
    }

    static async SaveLocalBuildCamera() {
        return this.SaveLocalBuildView();
    }

    static QueueLocalBuildViewSave() {
        if (this.restoringLocalCamera || window.CIFMapRuntime?.IsMapActive?.() !== true) {
            return;
        }
        // The local mirror is the quit-safe authority for personal Map view.
        // Update it immediately; only debounce the authoring-server copy. This is
        // especially important in Fly, whose temporary UniversalCamera can keep
        // moving until the application closes.
        const currentView = this.CaptureLocalBuildView();
        if (currentView?.camera) {
            this.SaveLocalBuildViewMirror(currentView);
        }
        if (this.localViewSaveTimer) {
            clearTimeout(this.localViewSaveTimer);
        }
        this.localViewSaveTimer=setTimeout(()=> {
            this.localViewSaveTimer=null;
             void this.SaveLocalBuildView();
        },250);
    }

    static QueueLocalBuildCameraSave() {
        this.QueueLocalBuildViewSave();
    }

    static BindLocalBuildCameraAutosave() {
        if (typeof babylonCamera==='undefined'||!babylonCamera) {
            return;
        }
        if (this.localCameraObservedCamera===babylonCamera) {
            return;
        }
        if(this.localCameraObserver&&this.localCameraObservedCamera?.onViewMatrixChangedObservable)try {
            this.localCameraObservedCamera.onViewMatrixChangedObservable.remove(this.localCameraObserver);
        }catch(_) {
        }
        this.localCameraObservedCamera=babylonCamera;
        this.localCameraObserver=babylonCamera.onViewMatrixChangedObservable?.add?.(()=>this.QueueLocalBuildViewSave())||null;
    }

    static async RestoreBuildCamera() {
        if (typeof babylonCamera==='undefined'||!babylonCamera||!window.BABYLON) {
            return false;
        }
        // Prefer the synchronous per-machine mirror.  It is updated on every
        // camera autosave and therefore survives Quit even when the final async
        // authoring-server request never gets a chance to complete.
        const storageKey = this.LocalBuildViewStorageKey();
        let view=this.LoadLocalBuildViewMirror();
        console.info('[build-view] loading local mirror', {
            key: storageKey,
            found: !!view,
            position: view?.camera?.position || null,
            target: view?.camera?.target || null
        });
        if(!view && window.CINTERFORGE_IF_LOCAL_WORLD_ACTIVE!==true) try {
            const r=await CIFRuntime.authoringFetch('/world/local-camera',{
                method:'POST',headers:{
                    'Content-Type':'application/json'
                },body:JSON.stringify({
                    load:true
                }),cache:'no-store'
            }),b=await r.json();
            if(!r.ok||b.ok===false) {
                console.error('[build-view] local load failed',b.error||`HTTP ${r.status}`);
                return false;
            }
            view=(b.view&&typeof b.view==='object')?b.view:(b.camera? {
                camera:b.camera
            }
            :null);
        }catch(e) {
            console.error('[build-view] local load failed',e);
            return false;
        }
        if (!view) {
            return false;
        }

        const currentSpatialSignature = this.BuildViewSpatialSignature();
        if (!view.spatialSignature || view.spatialSignature !== currentSpatialSignature) {
            console.info('[build-view] discarded stale local view after spatial presentation change', {
                key: storageKey,
                hadSignature: !!view.spatialSignature
            });
            return false;
        }

        const q=view.section;
        if(q&&typeof q==='object') {
            this.authoredSection={
                axis:['x','y','z'].includes(q.axis)?q.axis:'z',keep:q.keep==='minus'?'minus':'plus',block:Number.isFinite(Number(q.block))?Number(q.block):0
            };
            this.RestoreAuthoredSectionFor3D();
        }
        const c=view.camera;
        if (!c) {
            return !!q;
        }
        const t=Array.isArray(c.target)?c.target:null;
        if (!t||t.length<3||![c.alpha,c.beta,c.radius,t[0],t[1],t[2]].every(v=>Number.isFinite(Number(v)))) {
            return !!q;
        }
        this.restoringLocalCamera=true;
        try {
            const target = new BABYLON.Vector3(+t[0], +t[1], +t[2]);
            const position = Array.isArray(c.position) && c.position.length >= 3 && c.position.every(value => Number.isFinite(Number(value)))
                ? new BABYLON.Vector3(+c.position[0], +c.position[1], +c.position[2])
                : null;
            babylonCamera.setTarget(target);
            if (position) {
                // Position is the authoritative persisted Map view. ArcRotate's
                // alpha/beta convention is easy to reconstruct incorrectly from a
                // Fly camera; setPosition lets Babylon derive those values itself.
                babylonCamera.setPosition(position);
            } else {
                // Backward compatibility for camera records written before exact
                // eye position became part of the personal Map-view schema.
                babylonCamera.alpha=+c.alpha;
                babylonCamera.beta=+c.beta;
                babylonCamera.radius=Math.max(.01,+c.radius);
            }
            if (Number.isFinite(Number(c.fov))) {
                babylonCamera.fov=Math.max(.01,Math.min(Math.PI-.01,+c.fov));
            }
            if (typeof CIFMapRuntime!=='undefined'&&CIFMapRuntime.configureOrbitCameraControls) {
                CIFMapRuntime.configureOrbitCameraControls();
            }
        }finally {
            this.restoringLocalCamera=false;
        }
        console.info('[build-view] restored local view', {
            key: storageKey,
            position: [+babylonCamera.position.x, +babylonCamera.position.y, +babylonCamera.position.z],
            target: [+babylonCamera.target.x, +babylonCamera.target.y, +babylonCamera.target.z]
        });
        return true;
    }
    // ---------------------------------------------------------------------
    // Terrain transform and authored settings persistence
    // ---------------------------------------------------------------------

    static UpdateStrokeForCamera() {
        if (typeof babylonCamera==='undefined'||!babylonCamera||!window.BABYLON) {
            return;
        }
        const h=Math.max(1,Number(mapCanvas?.clientHeight)||1),fov=Math.max(.01,Math.min(Math.PI-.01,Number(babylonCamera.fov)||.8)),focalPixels=h/(2*Math.tan(fov*.5)),base=Math.max(.5,this.STYLE.edgeWidth);
        for(const i of this.instances) {
            // Camera-dependent edge width belongs only to the voxel Terrain shader
            // created by _material(). Ordinary Terrain objects keep their imported
            // GLTF materials unchanged.
            if (!i?.voxelTerrain || !i?.mesh?.material?.setFloat || !i.root?.isEnabled?.()) {
                continue;
            }
            const b=i.bounds;
            if (!b) {
                continue;
            }
            const localCenter=new BABYLON.Vector3((b.minX+b.maxX)/2,(b.minY+b.maxY)/2,(b.minZ+b.maxZ)/2);
            i.root.computeWorldMatrix(true);
            const center=BABYLON.Vector3.TransformCoordinates(localCenter,i.root.getWorldMatrix()),distance=Math.max(.001,BABYLON.Vector3.Distance(babylonCamera.position,center)),worldBlock=Math.max(.001,Math.abs(Number(i.root.scaling?.x)||1)),projectedPixels=focalPixels*worldBlock/distance;
            /* The shader stroke is screen-space.  At long range a fixed pixel width consumes most of each tiny voxel face, making the model read as a dark mass. Preserve the accepted heavy close-up stroke, then taper only as a voxel projects below ~12 px. */const taper=Math.max(.4,Math.min(1,Math.sqrt(projectedPixels/12)));
            i.mesh.material.setFloat('edgeWidth',base*taper);
        }
    }

    static StoreTransform() {
        return this.StoreSettings();
    }

    static async LoadPersistedSettings() {
        try {
            let b=null;
            if(window.CINTERFORGE_IF_LOCAL_WORLD_ACTIVE===true&&window.CINTERFORGE_IF_LOCAL_WORLD_FILES instanceof Map) {
                const shell=window.CINTERFORGE_IF_LOCAL_WORLD_FILES.get('Data/map_shell.json')||window.CINTERFORGE_IF_LOCAL_WORLD_FILES.get('Story/map_shell.json');
                b={
                    map_shell:shell?JSON.parse(await shell.text()):null
                };
            }else if(typeof ifplayerFrobozzMode==='function'&&ifplayerFrobozzMode()) {
                /* Frobozz is one composite authored world. Its Map shell belongs to the
                 * shared STORY-Frobozz spatial authority, not to whichever child game
                 * (CCA/Zork I/II/III) is currently active. The shell is persisted beside
                 * the shared room/group placement in map.json as map_shell_transform. */
                const response=await CIFRuntime.authoringFetch('/host/spatial-map',{
                    cache:'no-store'
                });
                const spatialMap=await response.json().catch(()=>({}));
                if(!response.ok) {
                    throw new Error(`Frobozz spatial map load failed: HTTP ${response.status}`);
                }
                b={
                    map_shell:(spatialMap?.map_shell_transform&&typeof spatialMap.map_shell_transform==='object')
                        ?spatialMap.map_shell_transform
                        :null
                };
                console.info('[terrain] Frobozz map shell loaded',{
                    hasShell:!!b.map_shell,
                    terrainIds:Object.keys(b.map_shell?.terrain||{}),
                    floorNames:b.map_shell?.floor_names||{}
                });
            }else if(window.CINTERFORGE_IF_RESOURCE_ONLY) {
                /* Session/Play/Build read authored world layout as a static story resource. */const shellUrl=(typeof storyResourceUrl==='function'&&typeof activeGameKey==='function')?storyResourceUrl(activeGameKey(),'map_shell.json'):'../WorldData/map_shell.json';
                const r=await fetch(shellUrl,{
                    cache:'no-store'
                });
                b={
                    map_shell:r.ok?await r.json():null
                };
            }else {
                /* Authoring reads the same Story-owned WorldData authority that Save writes.
                 * Do not use the legacy /world/map-shell endpoint here: it is a second
                 * persistence boundary and can return the pre-edit shell transform after
                 * CIFWorldRepository has already saved map_shell.json through CIFWorldData. */
                const game=(typeof activeGameKey==='function')?String(activeGameKey()||'').trim():'';
                // Map shell is Project Data authority. Read it directly from the
                // authoring WorldData boundary instead of a canonical-world cache:
                // that cache is allowed to rebuild Story room structure and must not
                // become a second persistence boundary for floor/castle state.
                const worldData=(game&&window.CIFWorldData?.Load)
                    ? await CIFWorldData.Load(game)
                    : null;
                b={
                    map_shell:(worldData?.map_shell&&typeof worldData.map_shell==='object')?worldData.map_shell:null
                };
                console.info('[terrain] authored map shell loaded',{
                    game,
                    hasShell:!!b.map_shell,
                    terrainIds:Object.keys(b.map_shell?.terrain||{}),
                    floorNames:b.map_shell?.floor_names||{}
                });
            }
            this.persistedSettings=b.map_shell&&typeof b.map_shell==='object'?b.map_shell:null;
            /* Checkerboard X/Z extent is authored Build state.  StoreSettings has always
                       persisted it as floor_scales, but Terrain migration stopped restoring that
                       map, causing every reload to recompute the default footprint from rooms.
                       Restore the authored whole-cell counts before floor scenery is rebuilt. */ /* One persisted X/Z checkerboard footprint for the whole Build world.
                       Migrate old per-floor floor_scales once by taking the first valid entry;
                       thereafter only checkerboard_cells is written. */ if(typeof mapCheckerboardCellCounts!=='undefined') {
                const saved=this.persistedSettings?.checkerboard_cells;
                let cells=(saved&&typeof saved==='object')?saved:null;
                if(!cells) {
                    const legacy=this.persistedSettings?.floor_scales;
                    if (legacy&&typeof legacy==='object') {
                        cells=Object.values(legacy).find(v=>v&&typeof v==='object')||null;
                    }
                }
                mapCheckerboardCellCounts=cells? {
                    x:Math.max(1,Math.round(Number(cells.x)||1)),z:Math.max(1,Math.round(Number(cells.z)||1))
                }
                :null;
            }
            /* Floor names are authored Build state too.  Restore them before rebuilding
                       floor scenery/environment so semantic names such as Ground survive reload
                       and can drive the shared Build/Play environment. */ if(typeof mapFloorNames!=='undefined') {
                mapFloorNames.clear();
                const names=this.persistedSettings?.floor_names;
                if(names&&typeof names==='object')for(const [layer,value] of Object.entries(names)) {
                    const k=Number(layer),v=String(value||'').trim();
                    if (Number.isFinite(k)&&v) {
                        mapFloorNames.set(k,v);
                    }
                }
            }
            /* Authored floors are Project-owned spatial state.  Older Project shells
             * predate the authored_floors dictionary but already carry the same floor
             * identities in floor_names/floor_offsets/floor_scales.  Reconstruct that
             * dictionary from those persisted semantic floor keys instead of reporting
             * Floors (0) and letting generated room layout redefine the floor model. */
            if (typeof mapAuthoredFloors!=='undefined') {
                mapAuthoredFloors.clear();
                const authored = this.persistedSettings?.authored_floors;
                if (authored && typeof authored === 'object') {
                    for (const [layer, metadata] of Object.entries(authored)) {
                        const floor = Number(layer);
                        const worldY = Number(metadata?.world_y);
                        if (Number.isFinite(floor)) {
                            mapAuthoredFloors.set(floor, {
                                ...(metadata && typeof metadata === 'object' ? metadata : {}),
                                world_y: Number.isFinite(worldY) ? worldY : floor
                            });
                        }
                    }
                }
                if (!mapAuthoredFloors.size) {
                    const legacyFloorKeys = new Set();
                    for (const collection of [
                        this.persistedSettings?.floor_names,
                        this.persistedSettings?.floor_offsets,
                        this.persistedSettings?.floor_scales
                    ]) {
                        if (!collection || typeof collection !== 'object') {
                            continue;
                        }
                        for (const key of Object.keys(collection)) {
                            const floor = Number(key);
                            if (Number.isFinite(floor)) {
                                legacyFloorKeys.add(floor);
                            }
                        }
                    }
                    for (const floor of legacyFloorKeys) {
                        mapAuthoredFloors.set(floor, { world_y: floor });
                    }
                }
            }
            if (typeof mapFloorRelativeScales!=='undefined') {
                mapFloorRelativeScales.clear();
            }
            /* Per-floor dY is independent authored state.  Restore it before global Floor
                       Spacing is applied so mapLayerOffset() always evaluates:
                         authored spacing scale + this floor's saved dY.
                       Terrain migration had kept saving floor_offsets but stopped loading them. */ if(typeof mapFloorRelativeOffsets!=='undefined') {
                mapFloorRelativeOffsets.clear();
                const offsets=this.persistedSettings?.floor_offsets;
                if(offsets&&typeof offsets==='object')for(const [layer,value] of Object.entries(offsets)) {
                    const k=Number(layer),v=Number(value);
                    if (Number.isFinite(k)&&Number.isFinite(v)&&Math.abs(v)>1e-9) {
                        mapFloorRelativeOffsets.set(k,v);
                    }
                }
            }
            /* Floor Spacing is one persisted GLOBAL Build setting.  Restore the authored
                       factor here, before CIFMapRuntime builds/applies floor transforms.  Merely
                       restoring the control later is too late: the floor stack has already been
                       positioned using the default 100% value. */ if(typeof mapLayerSpacingSetting!=='undefined') {
                const savedSpacing=Number(this.persistedSettings?.floor_spacing);
                mapLayerSpacingSetting=Number.isFinite(savedSpacing)?Math.max(.01,Math.min(5,savedSpacing)):1;
                if (typeof mapLayerSpacingFactor!=='undefined') {
                    mapLayerSpacingFactor=mapLayerSpacingSetting;
                }
            }
            const q=this.persistedSettings?.section;
            if(q) {
                this.authoredSection={
                    axis:q.axis||'off',keep:q.keep||'plus',block:+q.block||0
                };
                this.axis=this.authoredSection.axis;
                this.keep=this.authoredSection.keep;
                this.block=this.authoredSection.block;
            }
            return !!this.persistedSettings;
        }catch(e) {
            console.error('[terrain] transform load failed',e);
            this.persistedSettings=null;
            return false;
        }
    }

    static async PersistSettings() {
        if(!this.instances.length)return {
            ok:true,skipped:true
        };
        this.StoreSettings();
        if(window.CINTERFORGE_IF_LOCAL_WORLD_ACTIVE===true)return {
            ok:true,local:true,read_only:true
        };
        const r=await CIFRuntime.authoringFetch('/world/map-shell',{
            method:'POST',headers:{
                'Content-Type':'application/json'
            },body:JSON.stringify({
                map_shell:this.persistedSettings
            }),cache:'no-store'
        }),b=await r.json();
        if (!r.ok||b.ok===false) {
            throw new Error(b.error||`HTTP ${r.status}`);
        }
        return b;
    }

    static MarkAuthoredSettingsDirty() {
        if (typeof markSpatialDirty==='function') {
            markSpatialDirty();
        }
    }

    static ToggleTransformCage() {
        if (!this.selected) {
            return false;
        }
        if (window.CIFMapTerrainTransformCage?.Toggle) {
            return CIFMapTerrainTransformCage.Toggle(this.selected.root);
        }
        return false;
    }

    static SetVisible(v) {
        this.visible=!!v;
        for(const i of this.instances) {
            i.visible=this.visible;
            i.root.setEnabled(this.visible);
        }
        this.Apply();
    }

    static ToggleVisible() {
        this.SetVisible(!this.visible);
        return this.visible;
    }
    // ---------------------------------------------------------------------
    // Cross-section presentation
    // ---------------------------------------------------------------------

    // Push the current authored section plane into every Terrain shader.
    static Apply() {
        /* Normalize the authored section against the currently selected Terrain BEFORE
           sending the plane to the shader. Persisted section coordinates can predate a
           Terrain representation/scale change. Previously SyncControls() clamped the UI
           only after the shader had already received the stale plane, which could clip the
           entire resident Terrain (the Castle) while the slider itself looked valid. */
        const sectionInstance = (this.selected?.voxelTerrain ? this.selected : null)
            || this.instances.find(instance => instance?.voxelTerrain)
            || null;
        if (sectionInstance && this.axis !== 'off') {
            const bounds = sectionInstance.bounds || {};
            const rawLo = this.axis === 'x' ? bounds.minX : (this.axis === 'y' ? bounds.minY : bounds.minZ);
            const rawHi = this.axis === 'x' ? bounds.maxX : (this.axis === 'y' ? bounds.maxY : bounds.maxZ);
            if (Number.isFinite(+rawLo) && Number.isFinite(+rawHi)) {
                const lo = Math.floor(+rawLo - 1);
                const hi = Math.ceil(+rawHi + 1);
                this.block = Math.max(lo, Math.min(hi, Math.round(+this.block || 0)));
                this.authoredSection = { axis: this.axis, keep: this.keep, block: this.block };
            }
        }
        /* Sectioning is a GPU render operation, never a voxel-geometry rebuild. This is
                   the fast accepted shell behavior: the same shader fragment that draws the
                   thick block stroke also discards the clipped half, so no orphan edge lines. */ for(const i of this.instances) {
            i.root.setEnabled(this.visible&&i.visible!==false);
            if (!i.voxelTerrain||!i.mesh||!i.mesh.material?.setFloat) {
                continue;
            }
            let plane=null;
            if(this.axis!=='off' && i === sectionInstance) {
                const axis=['x','y','z'].includes(this.axis)?this.axis:'x',sign=this.keep==='minus'?-1:1;
                const n=axis==='x'?new BABYLON.Vector3(sign,0,0):(axis==='y'?new BABYLON.Vector3(0,sign,0):new BABYLON.Vector3(0,0,sign));
                plane=new BABYLON.Plane(n.x,n.y,n.z,-sign*this.block);
            }
            i.mesh.material.setFloat('sectionEnabled',plane?1:0);
            i.mesh.material.setVector4('sectionPlane',plane?new BABYLON.Vector4(plane.normal.x,plane.normal.y,plane.normal.z,plane.d):new BABYLON.Vector4(0,1,0,0));
            i.mesh.metadata={
                ...(i.mesh.metadata|| {
                }),terrainVoxelSource:i.voxels
            };
        }
        this.SyncControls();
    }

    static SetAuthoringPresentationEnabled(enabled) {
        const authoringVisible = enabled === true;
        // Terrain is shared world geometry and therefore remains enabled in both
        // Map and Test. Only Map's authored cross-section is presentation state.
        // Test always sees the complete Terrain without changing the persisted
        // authored section that Map restores when it becomes active again.
        for (const instance of this.instances) {
            instance.root?.setEnabled?.(this.visible && instance.visible !== false);
            if (!instance.voxelTerrain || !instance.mesh?.material?.setFloat) {
                continue;
            }
            if (!authoringVisible) {
                instance.mesh.material.setFloat('sectionEnabled', 0);
            }
        }
        if (authoringVisible) {
            this.RestoreAuthoredSectionFor3D();
        }
    }

    static RestoreAuthoredSectionFor3D() {
        const q=this.authoredSection|| {
            axis:'z',keep:'plus',block:0
        };
        this.axis=['x','y','z'].includes(q.axis)?q.axis:'z';
        this.keep=q.keep==='minus'?'minus':'plus';
        this.block=+q.block||0;
        this.Apply();
    }

    static ShowWholeFor3D() {
        /* Historical name retained for callers only. 3D must never destroy or temporarily replace the user's authored cross-section. X/Y/Z can each represent a fully visible model at the end of its slider, so there is no separate Whole state. */this.RestoreAuthoredSectionFor3D();
    }

    static SetFloorSection(layer) {
        /* Terrain remains present; semantic floor visibility controls picking elsewhere. */
    }

    static SyncControls() {
        const c=this.Controls(), inst=this.selected||this.instances[0];
        if (!c.axis) {
            return;
        }
        const has=!!inst;
        c.axis.hidden=!has;
        if(has&&this.axis==='off') {
            /* Migrate the removed legacy Whole state to the equivalent fully-visible Z section. */const b=inst.bounds|| {
            },lo=Math.floor((Number.isFinite(+b.minZ)?+b.minZ:0)-1);
            this.axis='z';
            this.keep='plus';
            this.block=lo;
            this.authoredSection={
                axis:'z',keep:'plus',block:lo
            };
        }
        c.axis.value=['x','y','z'].includes(this.axis)?this.axis:'z';
        if(!has) {
            c.slice.hidden=true;
            c.flip.hidden=true;
            return;
        }
        const active=has, b=inst.bounds|| {
        };
        c.slice.hidden=!active;
        c.flip.hidden=!active;
        if(active) {
            /* Bounds are voxel CENTER coordinates.  A section plane at maxZ therefore cuts through the outer half of the last voxel row.  Give the section scrubber one full block of clearance on both ends so its far endpoint really means 'past the model', not 'through the back face'. */const rawLo=(this.axis==='x'?b.minX:this.axis==='y'?b.minY:b.minZ),rawHi=(this.axis==='x'?b.maxX:this.axis==='y'?b.maxY:b.maxZ),lo=Math.floor(rawLo-1),hi=Math.ceil(rawHi+1);
            c.slice.min=String(lo);
            c.slice.max=String(Math.max(lo,hi));
            c.slice.step='1';
            this.block=Math.max(lo,Math.min(hi,Math.round(+this.block||0)));
            c.slice.value=String(this.block);
            c.value.textContent=`${this.axis.toUpperCase()} ${this.block}`;
            c.flip.textContent=this.keep==='minus'?'Keep \u2212':'Keep +';
        }else c.value.textContent='Z';
    }

    static BindAssetSelection() {
        const canvas = typeof mapCanvas !== 'undefined' ? mapCanvas : babylonScene?.getEngine?.()?.getRenderingCanvas?.();
        if (!canvas || this.assetSelectionCanvas === canvas) return;
        this.UnbindAssetSelection();
        let down = null;
        const pointerDown = event => {
            if (event.button !== 0) return;
            down = {x:event.clientX,y:event.clientY,id:event.pointerId};
        };
        const pointerUp = event => {
            if (!down || event.pointerId !== down.id) return;
            const distance = Math.hypot(event.clientX-down.x,event.clientY-down.y);
            down = null;
            if (distance > 5) return; // camera orbit/drag, not selection
            const hit = this.PickBlock(event.clientX,event.clientY);
            if (!hit?.inst) return;
            // Whole-asset selection and voxel editing intentionally coexist. A plain
            // click selects the Terrain asset; Left/Right Shift still place/remove
            // the hovered voxel through CIFPlayerInput. Selection never edits blocks.
            this.Select(hit.inst);
        };
        canvas.addEventListener('pointerdown',pointerDown,false);
        canvas.addEventListener('pointerup',pointerUp,false);
        this.assetSelectionCanvas=canvas;
        this.assetSelectionHandlers={pointerDown,pointerUp};
    }

    static UnbindAssetSelection() {
        const canvas=this.assetSelectionCanvas, handlers=this.assetSelectionHandlers;
        if(canvas&&handlers){
            canvas.removeEventListener('pointerdown',handlers.pointerDown,false);
            canvas.removeEventListener('pointerup',handlers.pointerUp,false);
        }
        this.assetSelectionCanvas=null;
        this.assetSelectionHandlers=null;
    }

    static BindControls() {
        const c=this.Controls();
        if (!c.axis) {
            return;
        }
        c.axis.onchange=()=> {
            this.axis=['x','y','z'].includes(c.axis.value)?c.axis.value:'z';
            {
                const i=this.selected||this.instances[0],b=i?.bounds|| {
                };
                const rawLo=(this.axis==='x'?b.minX:this.axis==='y'?b.minY:b.minZ),rawHi=(this.axis==='x'?b.maxX:this.axis==='y'?b.maxY:b.maxZ),lo=Math.floor(rawLo-1),hi=Math.ceil(rawHi+1);
                this.block=Math.round((lo+hi)/2);
            }
            this.authoredSection={
                axis:this.axis,keep:this.keep,block:this.block
            };
            this.Apply();
            this.MarkAuthoredSettingsDirty();
            this.QueueLocalBuildViewSave();
        };
        c.slice.oninput=()=> {
            this.block=Math.round(+c.slice.value||0);
            this.authoredSection={
                axis:this.axis,keep:this.keep,block:this.block
            };
            this.Apply();
            this.MarkAuthoredSettingsDirty();
            this.QueueLocalBuildViewSave();
        };
        c.flip.onclick=()=> {
            this.keep=this.keep==='minus'?'plus':'minus';
            this.authoredSection={
                axis:this.axis,keep:this.keep,block:this.block
            };
            this.Apply();
            this.MarkAuthoredSettingsDirty();
            this.QueueLocalBuildViewSave();
        };
        this.SyncControls();
    }
    // ---------------------------------------------------------------------
    // Framing, Play installation, and teardown
    // ---------------------------------------------------------------------

    static FrameAll() {
        const b=this.WorldBounds();
        if (!b||typeof frameMapBounds!=='function') {
            return;
        }
        try {
            frameMapBounds(b.min,b.max);
        }catch(_) {
        }
    }
    // Recreate canonical Terrain in a Play scene using the same saved transforms
    // as Build; Play gets no separate castle placement authority.
    static async InstallIntoPlay(scene, { parentRoot = null, excludeIds = [] } = {}) {
        if (!scene||!window.BABYLON) {
            return [];
        }

        // Map and Test now share one Babylon scene. Terrain is part of that live
        // world and must never be instantiated a second time merely because Test
        // is being enabled. Reuse the exact Map Terrain instances, including their
        // authored transforms and live voxel edits. Do not re-parent them under the
        // Test root: that would change their world transforms and make Map/Test own
        // different placement authorities again.
        if (scene === babylonScene && this.instances.length) {
            const excluded = new Set((excludeIds || []).map(value => String(value || '')));
            const shared = this.instances.filter(instance => !excluded.has(String(instance?.id || '')));
            console.info('[terrain] Test reused live Terrain', {
                instances: shared.length,
                voxels: shared.reduce((count, instance) => count + (instance?.voxels?.length || 0), 0)
            });
            return shared;
        }

        // Non-IFPlayer callers may still supply an independent scene. Preserve the
        // explicit installation API for that case only.
        let rows=[];
        try {
            rows=await this.TerrainRows();
        }catch(e) {
            console.error('[terrain] Play load failed',e);
            return [];
        }
        if (!this.persistedSettings) {
            await this.LoadPersistedSettings();
        }
        const excluded = new Set((excludeIds || []).map(value => String(value || '')));
        const result=[];
        for(let i=0; i<rows.length; i++) {
            if (excluded.has(String(rows[i]?.id || ''))) {
                continue;
            }
            const inst=await this._create(scene,rows[i],i,true);
            if (!inst) {
                continue;
            }
            /* Test/Play must use the same placement contract as Build. A migrated Terrain with no authored Terrain-v2 transform previously stayed at raw model origin here, which made Test appear blank while Build showed the asset elsewhere. */if(!this._applySaved(inst,i))this._defaultPlace(inst,i);
            if (parentRoot) {
                inst.root.parent = parentRoot;
            }
            inst.root.metadata = {
                ...(inst.root.metadata || {}),
                cinterforgeAssetObject: true,
                cinterforgeWorldTerrain: true,
                terrainId: inst.id,
                cinterforgeAssetName: inst.name,
                cinterforgeAssetUCI: inst.uci
            };
            for(const terrainMesh of (inst.meshes||[inst.mesh]).filter(Boolean)) {
                terrainMesh.isPickable=true;
                terrainMesh.metadata={...(terrainMesh.metadata||{}),cinterforgeWorldTerrain:true,terrainId:inst.id};
            }
            result.push(inst);
        }
        console.info('[terrain] Play installed',{
            instances:result.length
        });
        return result;
    }

    static Dispose() {
        this.UnbindAssetSelection();
        if(this.localViewSaveTimer) {
            clearTimeout(this.localViewSaveTimer);
            this.localViewSaveTimer=null;
            void this.SaveLocalBuildView();
        }
        if(this.localCameraSaveTimer) {
            clearTimeout(this.localCameraSaveTimer);
            this.localCameraSaveTimer=null;
        }
        if(this.localCameraObservedCamera===babylonCamera&&this.localCameraObserver&&babylonCamera?.onViewMatrixChangedObservable)try {
            babylonCamera.onViewMatrixChangedObservable.remove(this.localCameraObserver);
        }catch(_) {
        }
        this.localCameraObserver=null;
        this.localCameraObservedCamera=null;
        if (window.CIFMapTerrainTransformCage) {
            CIFMapTerrainTransformCage.Dispose();
        }
        for(const i of this.instances) {
            try {
                i.root.dispose(false,true);
            }catch(_) {
            }
            try {
                i.mesh.material?.dispose?.();
            }catch(_) {
            }
        }
        this.instances=[];
        this.selected=null;
        this.root=null;
        this.mesh=null;
        this.config=null;
        this.hoverHit=null;
        if(this.hoverBox) {
            try {
                this.hoverBox.dispose(false,true);
            }catch(_) {
            }
        }
        this.hoverBox=null;
    }
}
window.CIFMapTerrain=CIFMapTerrain;

/*
 * CINTERFORGE MODULE PURPOSE
 * Asset editing coordinator: CIFAssetEditor
 *
 * Owns asset editing extracted intact from CIFAssetBrowser. Provider search,
 * browsing, acquisition, assignment, and ordinary preview remain with the asset
 * browser for this pass. Editor integrations can now be separated beneath this
 * Asset-owned boundary without putting editing back into Player.
 */

//========= Copyright 2025+, CinterForge, Inc. All rights reserved. ============

class CIFAssetEditor extends CIFBlockifyMinecraftServer {
    static async OpenCloudflareBlockbench(card){
        const payload=card._cloudflareBlockifyPayload||await this.PrepareCloudflareBlockify(card);
            card._cloudflareBlockifyPayload=payload;
        this.CloseBlockbench(false);
            const root=ifGetElementById('if_root')||document.body, productName=payload.product_name;
        const host=document.createElement('div');
            host.className='asset-blockbench-overlay';
            host.innerHTML=`<div class="asset-blockbench-toolbar"><strong>CinterForge Voxel · ${this.escapeHtml(productName)}</strong><span class="asset-blockbench-status">Preparing CinterForge Voxel from Cloudflare asset…</span><span class="asset-blockbench-spacer"></span><button class="objaverse-action asset-blockbench-save">Save Back</button><button class="objaverse-action asset-blockbench-cancel">Cancel</button></div><iframe class="asset-blockbench-frame" title="CinterForge Voxel editor"></iframe>`;
            root.appendChild(host);
            ifGetElementById('if_root')?.classList.add('if_asset-preview-fullview');
        const frame=host.querySelector('.asset-blockbench-frame'),status=host.querySelector('.asset-blockbench-status'),save=host.querySelector('.asset-blockbench-save'),cancel=host.querySelector('.asset-blockbench-cancel');
        const cleanup=()=>{window.removeEventListener('message',onMessage);
            window.removeEventListener('keydown',onEscape,true);
            };
            const dismiss=()=>{cleanup();
            host.remove();
            ifGetElementById('if_root')?.classList.remove('if_asset-preview-fullview');
            this.blockbenchHost=null;
            };
            const onEscape=e=>{if(e.key==='Escape'){e.preventDefault();
            dismiss();
            }};
        let session=null,sent=false;
            const sendSession=()=>{if(sent||!session||!frame.contentWindow)return;
            sent=true;
            frame.contentWindow.postMessage({channel:'cinterforge-blockbench',type:'load-session',session},'*');
            };
        const onMessage=async event=>{if(event.source!==frame.contentWindow)return;
            const msg=event.data||{};
            if (msg.channel!=='cinterforge-blockbench') {
                return;
            }
            if (msg.type==='bridge-ready') {
                sendSession();
            }
            else if(msg.type==='ready-for-edit'){status.textContent='Editing Cloudflare asset';
            save.disabled=false;
            }else if(msg.type==='save-result'){save.disabled=true;
            status.textContent='Publishing edits to Cloudflare…';
            try{await CIFRuntime.assetCommand({operation:'cloudflare-blockbench-save',...payload,geometry:msg.payload?.geometry||{},animations:msg.payload?.animations||{},texture_data_url:msg.payload?.texture_data_url||''});
            this.SetProgress(card,'CinterForge Voxel edits saved to Cloudflare.');
            dismiss();
            }catch(error){status.textContent=`Save failed: ${error.message||error}`;
            save.disabled=false;
            }}else if(msg.type==='cancel-result')dismiss();
            else if(msg.type==='error'){status.textContent=`CinterForge Voxel: ${msg.message||'error'}`;
            save.disabled=false;
            }};
        window.addEventListener('message',onMessage);
            window.addEventListener('keydown',onEscape,true);
            save.disabled=true;
            cancel.addEventListener('click',()=>dismiss());
            this.blockbenchHost={host,frame,cleanup};
        try{
            frame.src=CIFRuntime.presentationRootUrl('blockbench/index.html?cinterforge=1');
            const response=await CIFRuntime.assetCommand({operation:'cloudflare-blockbench-session',...payload});
                session=response.session||{};
                status.textContent='Loading CinterForge Voxel…';
                sendSession();
        }catch(error){status.textContent=`CinterForge Voxel preparation failed: ${error.message||error}`;
            this.SetProgress(card,`CinterForge Voxel preparation failed: ${error.message||error}`);
            return;
            }
        save.addEventListener('click',()=>{save.disabled=true;
            status.textContent='Collecting CinterForge Voxel edits…';
            frame.contentWindow?.postMessage({channel:'cinterforge-blockbench',type:'save-request'},'*');
            });
    }

    static async PrepareSavedBlockbenchPayload(card){
        const folder=this.SavedFolderForCard(card);
        const versions=folder?.versions||[];
        const version=versions[Number(card?.dataset?.versionIndex||0)]||versions[0];
        const uci=String(card?.dataset?.assignmentUci||version?.uci||folder?.uci||'').trim();
        if (!uci) throw new Error('Saved asset UCI is unavailable.');
        return {uci,product_name:String(card?.dataset?.productName||folder?.name||version?.file||uci).trim()};
    }

    static async VoxelTextureRGBA(dataUrl){
        if(!dataUrl)return '';
        if(!String(dataUrl).startsWith('data:image/png;base64,'))throw new Error('Voxel texture must be PNG.');
        const image=new Image();
        image.src=dataUrl;
        await image.decode();
        const width=image.naturalWidth,height=image.naturalHeight;
        if(!width||!height||width*height>16*1024*1024)throw new Error('Voxel texture exceeds the local authoring limit.');
        const canvas=document.createElement('canvas');
        canvas.width=width;canvas.height=height;
        const context=canvas.getContext('2d',{willReadFrequently:true});
        context.drawImage(image,0,0);
        const rgba=context.getImageData(0,0,width,height).data;
        let binary='';
        for(let offset=0;offset<rgba.length;offset+=0x8000)binary+=String.fromCharCode(...rgba.subarray(offset,offset+0x8000));
        return btoa(binary);
    }

    static async ExportVoxelSavePreview(prepared){
        await ensureBabylon();
        const B=window.BABYLON,blockify=prepared.blockify;
        const dimensions={width:Number(blockify.width),height:Number(blockify.height),depth:Number(blockify.depth)};
        const engine=new B.NullEngine();
        let previousScene=null,scene=null;
        try {
            // Reuse the original migration exporter. Preserve the Block GLTF's
            // existing local unit scale and origin; never reopen/voxelize Solid.
            let frame=prepared.export_frame;
            if(!frame){
                previousScene=new B.Scene(engine);
                const url=await CIFRuntime.stagedAssetUrl('saved-file',{path:prepared.model_path});
                const loaded=await B.SceneLoader.ImportMeshAsync('','',url,previousScene,undefined,'.gltf');
                for(const group of loaded.animationGroups||[])group.stop();
                for(const skeleton of loaded.skeletons||[])skeleton.returnToRest();
                let minimum=null,scale=0;
                for(const mesh of loaded.meshes||[]){
                    const positions=mesh.getVerticesData?.(B.VertexBuffer.PositionKind);
                    if(!positions||positions.length<12)continue;
                    const matrix=mesh.computeWorldMatrix(true);
                    const vertex=i=>B.Vector3.TransformCoordinates(B.Vector3.FromArray(positions,i),matrix);
                    // The existing Block exporter emits six quad faces per cube.
                    // The first adjacent vertices give the authored cube edge.
                    if(!scale)scale=B.Vector3.Distance(vertex(0),vertex(3));
                    for(let i=0;i<positions.length;i+=3){
                        const point=vertex(i);
                        minimum=minimum?B.Vector3.Minimize(minimum,point):point;
                    }
                }
                const reference=prepared.reference_voxels||[];
                if(!minimum||!(scale>0)||!reference.length)throw new Error('Cannot recover the existing Block GLTF coordinate frame.');
                const min={x:Infinity,y:Infinity,z:Infinity};
                for(const v of reference)for(const k of ['x','y','z'])min[k]=Math.min(min[k],Number(v[k]));
                frame={scale,center:{x:minimum.x-min.x*scale,y:minimum.y-min.y*scale,z:minimum.z-min.z*scale}};
                previousScene.dispose();previousScene=null;
            }
            const center={x:frame.center.x+dimensions.width*8*frame.scale,
                          y:frame.center.y+dimensions.height*8*frame.scale,
                          z:frame.center.z+dimensions.depth*8*frame.scale};
            const animated=blockify.character_voxels?.length&&blockify.character_rig?.bones?.length;
            scene=animated?this.MigrationAnimatedBlockScene(blockify.character_voxels,blockify.character_rig,blockify.character_animations,dimensions,engine,frame.scale,center)
                          :this.MigrationBlockScene(blockify.voxels,dimensions,engine,frame.scale,center);
            return {preview_files:await this.MigrationExportGltf(scene,'model'),export_frame:frame};
        }finally{
            previousScene?.dispose();scene?.dispose();engine.dispose();
        }
    }

    static async OpenBlockbench(card){
        // Edit In Blockbench is local authoring. Build the editor session from the
        // canonical local asset and save edits back locally. Cloudflare publication
        // remains an explicit Push operation outside the editor.
        const payload=await this.PrepareSavedBlockbenchPayload(card);
        this.CloseBlockbench(false);

        const root=ifGetElementById('if_root')||document.body;
        const host=document.createElement('div');
        host.className='asset-blockbench-overlay';
        host.innerHTML=`<div class="asset-blockbench-toolbar"><strong>CinterForge Voxel · ${this.escapeHtml(payload.product_name)}</strong><span class="asset-blockbench-status">Preparing CinterForge Voxel…</span><span class="asset-blockbench-spacer"></span><button class="objaverse-action asset-blockbench-save">Save Back</button><button class="objaverse-action asset-blockbench-cancel">Cancel</button></div><iframe class="asset-blockbench-frame" title="CinterForge Voxel editor"></iframe>`;
        root.appendChild(host);
        ifGetElementById('if_root')?.classList.add('if_asset-preview-fullview');

        const frame=host.querySelector('.asset-blockbench-frame');
        const status=host.querySelector('.asset-blockbench-status');
        const save=host.querySelector('.asset-blockbench-save');
        const cancel=host.querySelector('.asset-blockbench-cancel');
        const cleanup=()=>{
            window.removeEventListener('message',onMessage);
            window.removeEventListener('keydown',onEscape,true);
        };
        const dismiss=()=>{
            cleanup();
            host.remove();
            ifGetElementById('if_root')?.classList.remove('if_asset-preview-fullview');
            this.blockbenchHost=null;
        };
        const onEscape=event=>{
            if (event.key==='Escape') {
                event.preventDefault();
                dismiss();
            }
        };

        let session=null;
        let sent=false;
        const sendSession=()=>{
            if (sent||!session||!frame.contentWindow) return;
            sent=true;
            frame.contentWindow.postMessage({channel:'cinterforge-blockbench',type:'load-session',session},'*');
        };
        const onMessage=async event=>{
            if (event.source!==frame.contentWindow) return;
            const message=event.data||{};
            if (message.channel!=='cinterforge-blockbench') return;

            if (message.type==='bridge-ready') {
                sendSession();
            } else if (message.type==='ready-for-edit') {
                status.textContent='Editing asset';
                save.disabled=false;
            } else if (message.type==='save-result') {
                save.disabled=true;
                status.textContent='Saving edits locally…';
                try {
                    const edits={...payload,geometry:message.payload?.geometry||{},
                        animations:message.payload?.animations||{},texture_data_url:message.payload?.texture_data_url||session.texture_data_url||'',
                        revision:session.revision};
                    edits.texture_rgba=await this.VoxelTextureRGBA(edits.texture_data_url);
                    const preparation=await CIFRuntime.assetCommand({operation:'local-blockbench-prepare-preview',...edits});
                    status.textContent='Regenerating Blockify preview…';
                    const exported=await this.ExportVoxelSavePreview(preparation.result);
                    const response=await CIFRuntime.assetCommand({operation:'local-blockbench-save',...edits,
                        ...exported,revision:preparation.result.revision});
                    const folder=this.SavedFolderForCard(card);
                    const versions=folder?.versions||[];
                    const version=versions[Number(card.dataset.versionIndex||0)]||versions[0];
                    if (version&&response?.result?.blockify) version.blockify=response.result.blockify;
                    dismiss();
                    // Save succeeded. Refresh the existing card and gallery without
                    // reopening the editor or navigating away from the Asset View.
                    try {
                        const image=card.querySelector('.saved-asset-thumb');
                        if(image&&response.result.thumbnail){
                            const url=await CIFRuntime.stagedAssetUrl('saved-file',{path:response.result.thumbnail});
                            image.dataset.stagedResource='saved-file';
                            image.dataset.stagedPath=response.result.thumbnail;
                            delete image.dataset.thumbnailRecipe;
                            if(url)image.src=url+(url.includes('?')?'&':'?')+'refresh='+Date.now();
                        }
                        await this.LoadSavedMinecraftPreview(card);
                        this.SetProgress(card,'CinterForge Voxel edits saved locally. Push when you are ready to publish.');
                    }catch(refreshError){
                        this.SetProgress(card,'Voxel edits saved; preview refresh failed: '+String(refreshError.message||refreshError));
                    }
                    if(window.parent!==window)window.parent.postMessage({type:'cinterforge-assets-refresh-report'},window.location.origin);
                } catch (error) {
                    status.textContent=`Save failed: ${error.message||error}`;
                    save.disabled=false;
                }
            } else if (message.type==='cancel-result') {
                dismiss();
            } else if (message.type==='error') {
                status.textContent=`CinterForge Voxel: ${message.message||'error'}`;
                save.disabled=false;
            }
        };

        window.addEventListener('message',onMessage);
        window.addEventListener('keydown',onEscape,true);
        save.disabled=true;
        cancel.addEventListener('click',()=>dismiss());
        this.blockbenchHost={host,frame,cleanup};

        try {
            frame.src=CIFRuntime.presentationRootUrl('blockbench/index.html?cinterforge=1');
            const response=await CIFRuntime.assetCommand({operation:'local-blockbench-session',...payload});
            session=response.session||{};
            status.textContent='Loading CinterForge Voxel…';
            sendSession();
        } catch (error) {
            status.textContent=`CinterForge Voxel preparation failed: ${error.message||error}`;
            this.SetProgress(card,`CinterForge Voxel preparation failed: ${error.message||error}`);
        }

        save.addEventListener('click',()=>{
            save.disabled=true;
            status.textContent='Collecting CinterForge Voxel edits…';
            frame.contentWindow?.postMessage({channel:'cinterforge-blockbench',type:'save-request'},'*');
        });
    }

    static CloseBlockbench(cancel=true){
        const active=this.blockbenchHost;
        if (!active) {
            return;
        }
        try{if(cancel)active.frame?.contentWindow?.postMessage({channel:'cinterforge-blockbench',type:'cancel-request'},'*');
            } catch (_) {
            }
        try{active.cleanup?.();
        }catch(_){} try{active.host?.remove?.();
        } catch (_) {
        }
        this.blockbenchHost=null;
        ifGetElementById('if_root')?.classList.remove('if_asset-preview-fullview');
    }

}

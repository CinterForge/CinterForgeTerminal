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
        return this.PrepareSavedBlockifyPayload(card,'CinterForge Voxel');
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
                    const response=await CIFRuntime.assetCommand({
                        operation:'local-blockbench-save',
                        ...payload,
                        geometry:message.payload?.geometry||{},
                        animations:message.payload?.animations||{},
                        texture_data_url:message.payload?.texture_data_url||'',
                    });
                    const folder=this.SavedFolderForCard(card);
                    const versions=folder?.versions||[];
                    const version=versions[Number(card.dataset.versionIndex||0)]||versions[0];
                    if (version&&response?.result?.blockify) {
                        version.blockify=response.result.blockify;
                    }
                    this.SetProgress(card,'CinterForge Voxel edits saved locally. Push when you are ready to publish.');
                    dismiss();
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

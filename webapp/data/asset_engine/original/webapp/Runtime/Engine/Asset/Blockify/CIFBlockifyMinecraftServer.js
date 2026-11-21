'use strict';

// Minecraft export, world/pack discovery, and local test-server authority.
class CIFBlockifyMinecraftServer extends CIFBlockifyBatch {
    static async ExportImageMinecraftAddon(card){
        const {folder,version,meta}=await this.EnsureImageMinecraft(card);
        const payload=await CIFRuntime.assetCommand({operation:'saved-image-minecraft-export',folder:folder.folder,file:version.file});
        if(payload.url){const a=document.createElement('a');
            a.href=payload.url;
            a.download=payload.file||meta.mcaddon_file||`${version.file}.mcaddon`;
            document.body.appendChild(a);
            a.click();
            a.remove();
            }
        this.SetProgress(card,`Exported ${payload.file||meta.mcaddon_file||'Minecraft image Add-On'} \u00b7 Statue and Picture items included.`);
    }

    static async ExportBlockifiedAddon(card){
        const folder=this.SavedFolderForCard(card),versions=folder?.versions||[],version=versions[Number(card?.dataset?.versionIndex||0)]||versions[0];
        if (!folder||!version?.file||!this.HasBlockifyArtifact(version)) {
            throw new Error('Blockify this asset before exporting an Add-On.');
        }
        this.SetProgress(card,`Downloading ${folder.name||version.file} Minecraft Add-On\u2026`);
        const payload=await CIFRuntime.assetCommand({operation:'saved-blockify-export',folder:folder.folder,file:version.file,product_name:String(card?.dataset?.productName||folder.name||version.file||'').trim()});
        const url=CIFRuntime.assetUrl(payload.url||'');
        if (!url) {
            throw new Error('Minecraft Add-On export did not return a file.');
        }
        const link=document.createElement('a');
        const productFile=String(card?.dataset?.productName||folder.name||version.file||'CinterForge Asset').trim().replace(/[^A-Za-z0-9._ -]+/g,'_');
            link.href=url;
            link.download=`${productFile.replace(/\.glb$/i,'')}.mcaddon`;
        link.style.display='none';
        document.body.appendChild(link);
        link.click();
        link.remove();
        if(payload.exported){version.blockify.mcaddon_file=payload.exported.file;
            version.blockify.minecraft_identifier=payload.exported.identifier;
            }
        this.SetProgress(card,`Downloaded ${payload.file||'Minecraft Add-On'}${payload.exported?.identifier?` \u00b7 ${payload.exported.identifier}`:''}.`);
    }

    static async RefreshMinecraftWorlds(){
        const picker=this.elements().worldPicker;
        if (!picker) {
            return [];
        }
        const previous=picker.value||String(localStorage.getItem('cinterforge.minecraft.world')||'');
        const payload=await CIFRuntime.assetCommand({operation:'asset-test-worlds'});
        const worlds=Array.isArray(payload?.worlds)?payload.worlds:[];
        picker.replaceChildren();
        if (!worlds.length) {
            const option=document.createElement('option');
            option.value='';
            option.textContent='No .mcworld files';
            picker.appendChild(option);
            picker.disabled=true;
            return worlds;
        }
        for (const world of worlds) {
            const option=document.createElement('option');
            option.value=String(world.path||'');
            option.textContent=`${String(world.name||world.relative_path||'Minecraft World')} — ${String(world.source||'CraftMaster')}`;
            option.title=String(world.relative_path||world.path||'');
            picker.appendChild(option);
        }
        const remembered=worlds.find(world=>String(world.path||'')===previous);
        if (remembered) {
            picker.value=String(remembered.path);
        }
        picker.disabled=false;
        picker.onchange=()=>{
            if (picker.value) localStorage.setItem('cinterforge.minecraft.world',picker.value);
        };
        if (picker.value) localStorage.setItem('cinterforge.minecraft.world',picker.value);
        return worlds;
    }

    static async RefreshMinecraftPacks() {
        // Retired: themed-pack selection was backed exclusively by
        // AssetMigration.csv. Do not issue the obsolete asset-migration-packs
        // request or imply that the current UCI catalog uses that spreadsheet.
        const picker = this.elements().packPicker;
        if (picker) {
            picker.replaceChildren();
            const option = document.createElement('option');
            option.value = '';
            option.textContent = 'No asset pack selected';
            picker.appendChild(option);
            picker.disabled = true;
        }
        return [];
    }

    static async RefreshAssetTestServerState(){
        const button=this.elements().serverButton;
        if (!button) {
            return;
        }
        const state=await CIFRuntime.assetCommand({operation:'asset-test-server-status'});
        const running=Boolean(state?.running);
        button.dataset.running=running?'1':'0';
        if (this.elements().worldPicker) {
            this.elements().worldPicker.disabled=running;
        }
        if (this.elements().packPicker) {
            this.elements().packPicker.disabled=running;
        }
        button.textContent=running
            ? this.text('asset_server_stop_button','Stop Server')
            : this.text('asset_server_start_button','Start Server');
        button.title=String(state?.detail||'');
    }

    static async PresentMinecraftEula(termsPayload){
        const eulaText=String(termsPayload?.text||'').trim();
        if (!eulaText) {
            throw new Error('Minecraft EULA text was empty; acceptance was not recorded.');
        }

        return await new Promise(resolve=>{
            const overlay=document.createElement('div');
            overlay.style.position='fixed';
            overlay.style.inset='0';
            overlay.style.zIndex='2147483646';
            overlay.style.background='rgba(0,0,0,0.72)';
            overlay.style.display='flex';
            overlay.style.alignItems='center';
            overlay.style.justifyContent='center';
            overlay.style.padding='28px';

            const dialog=document.createElement('div');
            dialog.style.width='min(900px, 92vw)';
            dialog.style.height='min(760px, 88vh)';
            dialog.style.background='#202124';
            dialog.style.color='#f1f3f4';
            dialog.style.border='1px solid #5f6368';
            dialog.style.borderRadius='8px';
            dialog.style.boxShadow='0 18px 70px rgba(0,0,0,0.55)';
            dialog.style.display='flex';
            dialog.style.flexDirection='column';
            dialog.style.overflow='hidden';

            const heading=document.createElement('div');
            heading.textContent='Minecraft End User License Agreement';
            heading.style.fontSize='18px';
            heading.style.fontWeight='600';
            heading.style.padding='18px 20px 10px';
            dialog.appendChild(heading);

            const explanation=document.createElement('div');
            explanation.textContent='Read the current official Minecraft EULA below. CinterForge will not install or start Bedrock Dedicated Server until you explicitly accept it.';
            explanation.style.padding='0 20px 12px';
            explanation.style.color='#bdc1c6';
            dialog.appendChild(explanation);

            const documentText=document.createElement('pre');
            documentText.textContent=eulaText;
            documentText.tabIndex=0;
            documentText.style.flex='1';
            documentText.style.margin='0 20px';
            documentText.style.padding='16px';
            documentText.style.overflow='auto';
            documentText.style.whiteSpace='pre-wrap';
            documentText.style.wordBreak='normal';
            documentText.style.fontFamily='system-ui, -apple-system, sans-serif';
            documentText.style.fontSize='13px';
            documentText.style.lineHeight='1.45';
            documentText.style.background='#171717';
            documentText.style.border='1px solid #3c4043';
            documentText.style.borderRadius='4px';
            dialog.appendChild(documentText);

            const source=document.createElement('div');
            source.textContent=`Official source: ${String(termsPayload?.source||'')}`;
            source.style.padding='10px 20px 0';
            source.style.fontSize='11px';
            source.style.color='#9aa0a6';
            dialog.appendChild(source);

            const actions=document.createElement('div');
            actions.style.display='flex';
            actions.style.justifyContent='flex-end';
            actions.style.gap='10px';
            actions.style.padding='14px 20px 18px';

            const decline=document.createElement('button');
            decline.type='button';
            decline.textContent='Decline';

            const accept=document.createElement('button');
            accept.type='button';
            accept.textContent='Accept';

            const finish=accepted=>{
                document.removeEventListener('keydown',onKeyDown,true);
                overlay.remove();
                resolve(Boolean(accepted));
            };
            const onKeyDown=event=>{
                if (event.key==='Escape') {
                    event.preventDefault();
                    finish(false);
                }
            };
            decline.addEventListener('click',()=>finish(false));
            accept.addEventListener('click',()=>finish(true));
            document.addEventListener('keydown',onKeyDown,true);
            actions.append(decline,accept);
            dialog.appendChild(actions);
            overlay.appendChild(dialog);
            document.body.appendChild(overlay);
            documentText.focus();
        });
    }

    static async ToggleAssetTestServer(){
        const button=this.elements().serverButton;
        if (!button||button.disabled) {
            return;
        }
        const wasRunning=button.dataset.running==='1';
        if (!wasRunning) {
            const projectTab=ifGetElementById('code-tab');
            if (projectTab) {
                projectTab.click();
            }
            CIFCommands.SendCodeCommand('asset-server-terminal');
        }
        button.disabled=true;
        button.textContent=wasRunning?'Stopping…':'Starting…';
        this.SetProgress(null,wasRunning?'Stopping Minecraft Asset Test server…':'Starting Minecraft Asset Test server…');
        try {
            if (!wasRunning) {
                await this.RefreshMinecraftWorlds();
                await this.RefreshMinecraftPacks();
                const selectedWorld=String(this.elements().worldPicker?.value||'');
                if (!selectedWorld) {
                    throw new Error('Select a Minecraft world before starting the server.');
                }
                localStorage.setItem('cinterforge.minecraft.world',selectedWorld);
                const status=await CIFRuntime.assetCommand({operation:'asset-test-server-status'});
                if (!status?.terms_accepted) {
                    this.SetProgress(null,'Loading current Minecraft EULA…');
                    const termsPayload=await CIFRuntime.assetCommand({operation:'asset-test-server-terms'});
                    const accepted=await this.PresentMinecraftEula(termsPayload);
                    if (!accepted) {
                        CIFCommands.SendCodeCommand('asset-server-terminal-write',{text:'[CinterForge] Minecraft EULA declined. Nothing was downloaded, installed, or started.'});
                        this.SetProgress(null,'Minecraft server installation cancelled.');
                        return;
                    }
                    CIFCommands.SendCodeCommand('asset-server-terminal-write',{text:'[CinterForge] Minecraft EULA accepted.'});
                    this.SetProgress(null,status?.installed?'Recording Minecraft EULA acceptance…':'Installing Minecraft Bedrock Dedicated Server…');
                    await CIFRuntime.assetCommand({operation:'asset-test-server-install',accepted:true});
                }
            }
            const selectedWorld=String(this.elements().worldPicker?.value||'');
            const selectedPack=String(this.elements().packPicker?.value||'');
            if (!wasRunning&&!selectedPack) {
                throw new Error('Select a Minecraft asset pack before starting the server.');
            }
            const result=await CIFRuntime.assetCommand(wasRunning
                ? {operation:'asset-test-server-stop'}
                : {operation:'asset-test-server-start',world:selectedWorld,pack:selectedPack});
            const running=Boolean(result?.running);
            button.dataset.running=running?'1':'0';
            button.textContent=running
                ? this.text('asset_server_stop_button','Stop Server')
                : this.text('asset_server_start_button','Start Server');
            this.SetProgress(null,String(result?.message||result?.detail||(running?'Minecraft server running.':'Minecraft server stopped.')));
        } catch (error) {
            console.error('[IFPlayer Assets] server-control-failed',{error:String(error?.message||error)});
            this.SetProgress(null,`Minecraft server FAILED · ${String(error?.message||error)}`);
            await this.RefreshAssetTestServerState().catch(()=>{});
        } finally {
            button.disabled=false;
        }
    }

}

console.info('[CIF Asset] CIFBlockifyMinecraftServer loaded');

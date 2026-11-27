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
        const uci=String(card?.dataset?.assignmentUci||version.uci||folder.uci||'').trim();
        const payload=await CIFRuntime.assetCommand({operation:'saved-blockify-export',uci,folder:folder.folder,file:version.file,product_name:String(card?.dataset?.productName||folder.name||version.file||'').trim()});
        const url=payload.path?await CIFRuntime.stagedAssetUrl('saved-file',{path:payload.path}):CIFRuntime.assetUrl(payload.url||'');
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
        if(payload.exported){if(!uci)version.blockify.mcaddon_file=payload.exported.file;
            if(payload.exported.identifier)version.blockify.minecraft_identifier=payload.exported.identifier;
            }
        this.SetProgress(card,`Downloaded ${payload.file||'Minecraft Add-On'}${payload.exported?.identifier?` \u00b7 ${payload.exported.identifier}`:''}.`);
    }

}

console.info('[CIF Asset] CIFBlockifyMinecraftServer loaded');

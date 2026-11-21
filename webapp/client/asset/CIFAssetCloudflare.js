/**
 * CIFAssetCloudflare ownership extracted from the legacy Asset Library controller.
 *
 * Methods intentionally execute with the controller as `this` while the refactor
 * proceeds, preserving current state without duplicating provider/preview state.
 */
class CIFAssetCloudflare {
    static VERSION = 'provider-v1';

    static async LoadCloudflare() {
        const el=this.elements();
        if (el.status) {
            el.status.textContent='Loading Cloudflare asset hierarchy…';
        }
        this.cloudflareTree=await CIFRuntime.assetCommand({operation:'cloudflare-tree'});
        this.RenderCloudflare();
    }

    static CloudflareFolderTree(files) {
        const root={folders:new Map(),files:[]};
        for(const file of files||[]){
            const parts=String(file.path||'').split('/').filter(Boolean), name=parts.pop();
             let node=root;
            for(const part of parts){if(!node.folders.has(part))node.folders.set(part,{folders:new Map(),files:[]});
                node=node.folders.get(part);
                }
            node.files.push({...file,name:name||file.name});
        }
        return root;
    }

    static RenderCloudflare() {
        if (this.mode!=='cloudflare') {
            return;
        }
        const el=this.elements(), files=Array.isArray(this.cloudflareTree?.files)?this.cloudflareTree.files:[];
        if (!el.results) {
            return;
        }
        if(el.status)el.status.textContent=`${files.length.toLocaleString()} Cloudflare assets · Assets/`;
        const tree=this.CloudflareFolderTree(files);
        const render=(node,prefix='')=>{
            const folders=[...node.folders.entries()].sort((a,b)=>a[0].localeCompare(b[0])).map(([name,child])=>`<details class="assignment-browser-group cloudflare-folder" open><summary>${this.escapeHtml(name)} <span>${child.files.length+child.folders.size}</span></summary><div class="cloudflare-folder-children">${render(child,prefix+name+'/')}</div></details>`).join('');
            const rows=node.files.sort((a,b)=>String(a.name).localeCompare(String(b.name))).map(file=>`<div class="cloudflare-asset-row" data-cloudflare-path="${this.escapeHtml(file.path)}" data-cloudflare-kind="${this.escapeHtml(file.kind||'asset')}"><span class="cloudflare-asset-name">${this.escapeHtml(file.name)}</span><small>${this.escapeHtml(file.path)} · ${Number(file.size||0).toLocaleString()} bytes</small><button type="button" class="objaverse-action" data-cloudflare-assign="1">Assign…</button></div>`).join('');
            return folders+rows;
        };
        el.results.classList.remove('assignment-workspace');
        el.results.innerHTML=`<div class="cloudflare-browser"><details class="assignment-browser-game" open><summary>Assets <span>${files.length}</span></summary>${render(tree)}</details></div>`;
        el.results.querySelectorAll('[data-cloudflare-assign]').forEach(button=>button.addEventListener('click',async()=>{
            const row=button.closest('[data-cloudflare-path]');
            this.pendingCloudflareAsset={path:String(row?.dataset.cloudflarePath||''),kind:String(row?.dataset.cloudflareKind||'asset')};
            await this.SetMode('assign');
        }));
        el.results.dataset.loaded='true';
    }
}

window.CIFAssetCloudflare = CIFAssetCloudflare;
console.info('[CIF Asset] CIFAssetCloudflare loaded', CIFAssetCloudflare.VERSION);

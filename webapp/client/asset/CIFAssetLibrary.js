/**
 * Asset Library/catalog ownership.
 *
 * Extracted from the legacy Asset Library controller. Methods intentionally
 * execute with the active Asset Library controller as `this` while the larger
 * refactor continues, so ownership moves without duplicating runtime state.
 */
class CIFAssetLibrary {
    static VERSION = 'refactor-v3';

    static AssignmentSavedRows(){
        const groups=this.savedLibrary?.groups||{}, rows=[];
        const add=(folders,kind)=>{for(const folder of (folders||[])) for(const version of (folder.versions||[])) rows.push({folder,version,kind,uci:String(version.uci||folder.uci||''),path:`${folder.folder}/${version.file}`});
            };
        add(groups.Images,'image');
            add(groups.Objects,'3d');
            add(groups.Animated?.Mixamo,'character');
            add(groups.Animated?.Fixed,'character');
            add(groups.Animations,'animation');
        return rows;
    }

    static AssignmentSearchRows(query){
        const q=String(query||'').trim().toLowerCase();
         if (!q) {
             return [];
         }
        const terms=q.split(/\s+/).filter(Boolean);
        return this.AssignmentSavedRows().map(row=>{
            const f=row.folder||{},v=row.version||{};
            const hay=`${f.name||''} ${f.search_query||''} ${v.search_query||''} ${v.file||''} ${v.provider||''} ${v.asset_id||''}`.toLowerCase();
            const score=terms.reduce((n,t)=>n+(hay.includes(t)?1:0),0)+(hay.includes(q)?5:0);
            return {...row,score};
        }).filter(r=>r.score>0).sort((a,b)=>b.score-a.score||String(a.version.file).localeCompare(String(b.version.file))).slice(0,24);
    }

    static async RefreshAssignmentSavedLibrary(){
        this.savedLibrary=await window.CIFRuntime.assetCommand({operation:'saved-list'});
        const mixamo=this.savedLibrary?.groups?.Animated?.Mixamo||[];
        let healed=false;
        for(const folder of mixamo){
            const assigned=Array.isArray(folder?.assigned_animations)?folder.assigned_animations.filter(row=>row?.folder&&row?.file):[];
            if (assigned.length) {
                continue;
            }
            const version=(folder?.versions||[])[0];
            if (!folder?.folder||!version?.file) {
                continue;
            }
            await this.EnsureInitialMixamoIdle(`${folder.folder}/${version.file}`,null);
            healed=true;
        }
        if(healed)this.savedLibrary=await window.CIFRuntime.assetCommand({operation:'saved-list'});
        this.RenderAssignments();
    }
    static async RefreshLocalAssignmentState() {
        if (!window.CIFWorldStore?.storyEntries || !window.CIFWorldStore?.loadWorldDataForGame || !window.CIFWorldData?.AssignmentDataset) {
            this.assignmentGames=[];
            return;
        }
        const assignmentGames=[];
        for (const [game,title] of window.CIFWorldStore.storyEntries()) {
            const worldData=await window.CIFWorldStore.loadWorldDataForGame(game);
            assignmentGames.push(window.CIFWorldData.AssignmentDataset(game,title,worldData));
        }
        this.assignmentGames=assignmentGames;
    }

    static ActiveAssignedUCIs() {
        const assigned=new Set();
        for (const game of (this.assignmentGames||[])) {
            for (const collection of [game.rooms,game.objects,game.characters]) {
                for (const record of (collection||[])) {
                    for (const asset of (Array.isArray(record?.assets)?record.assets:[])) {
                        const uci=String(asset?.uci||'').trim();
                        if (uci) assigned.add(uci);
                    }
                    const primaryUCI=String(record?.asset3d_uci||'').trim();
                    if (primaryUCI) assigned.add(primaryUCI);
                }
            }
        }
        return assigned;
    }

    static async LoadSaved() {
        const el=this.elements();
        if (el.status) {
            el.status.textContent=this.text('asset_saved_loading','Loading saved assets\u2026');
        }
        const payload=await window.CIFRuntime.assetCommand({operation:'saved-list'});
        this.savedLibrary=payload;
        await this.RefreshLocalAssignmentState();
        if(window.CINTERFORGE_IF_LOCAL_WORLD_ACTIVE===true){
            const g=payload?.groups||{}, counts={Images:(g.Images||[]).length,Objects:(g.Objects||[]).length,Animated:((g.Animated?.Mixamo||[]).length+(g.Animated?.Fixed||[]).length)};
            if(!counts[this.savedCategory]){const first=['Objects','Animated','Images'].find(k=>counts[k]>0);
                if (first) {
                    this.savedCategory=first;
                }
                }
            if (this.savedCategory==='Animated' && !(g.Animated?.[this.savedAnimated]||[]).length) {
                this.savedAnimated=(g.Animated?.Mixamo||[]).length?'Mixamo':'Fixed';
            }
        }
        this.RenderSaved();
        if(window.CINTERFORGE_IF_LOCAL_WORLD_ACTIVE===true && el.status){
            const g=payload?.groups||{}, total=(g.Images||[]).length+(g.Objects||[]).length+(g.Animated?.Mixamo||[]).length+(g.Animated?.Fixed||[]).length+(g.Animations||[]).length;
            el.status.textContent=`${total.toLocaleString()} local asset folders`;
        }
    }

    static SavedRows() {
        const groups=this.savedLibrary?.groups||{};
        // Terminal Asset View is one selected UCI, never the old category gallery.
        const selected=String(window.CINTERFORGE_ASSET_VIEW_UCI||'').trim();
        if(selected){
            return [
                ...(groups.Images||[]),...(groups.Objects||[]),
                ...(groups.Animated?.Mixamo||[]),...(groups.Animated?.Fixed||[]),
                ...(groups.Animations||[])
            ].filter(folder=>folder?.uci===selected);
        }
        if (this.savedCategory==='Unassigned') {
            const assigned=this.ActiveAssignedUCIs();
            const folders=[
                ...(groups.Images||[]),
                ...(groups.Objects||[]),
                ...(groups.Animated?.Mixamo||[]),
                ...(groups.Animated?.Fixed||[]),
                ...(groups.Animations||[]),
            ];
            return folders.filter(folder=>{
                const identities=new Set([
                    String(folder?.uci||'').trim(),
                    ...(folder?.versions||[]).map(version=>String(version?.uci||'').trim()),
                ].filter(Boolean));
                return identities.size>0 && [...identities].every(uci=>!assigned.has(uci));
            });
        }
        if (this.savedCategory==='Animated') {
            return Array.isArray(groups.Animated?.[this.savedAnimated])?groups.Animated[this.savedAnimated]:[];
        }
        return Array.isArray(groups[this.savedCategory])?groups[this.savedCategory]:[];
    }

    static SavedAnimations() { return Array.isArray(this.savedLibrary?.groups?.Animations)?this.savedLibrary.groups.Animations:[];
        }

    static SavedAttributionHtml(version) {
        const m=version?.metadata&&typeof version.metadata==='object'?version.metadata:{};
        const readable=value=>{
            if (value==null) {
                return '';
            }
            if (typeof value==='string'||typeof value==='number') {
                return String(value).trim();
            }
            if (typeof value==='object') {
                return String(value.name||value.displayName||value.username||value.label||value.fullName||value.slug||'').trim();
            }
            return String(value).trim();
        };
        const creator=readable(m.creator||m.author||m.uploader||'');
        const source=readable(m.source||version?.provider||'');
        const license=readable(m.license||'');
        const sourceUrl=String(m.source_url||m.viewer_url||'').trim();
        const licenseUrl=String(m.license_uri||m.license_url||'').trim();
        const parts=[creator,source,license].filter((v,i,a)=>v&&a.indexOf(v)===i);
        const links=[];
        if(sourceUrl)links.push(`<a href="${this.escapeHtml(sourceUrl)}" target="_blank" rel="noopener">${this.escapeHtml(this.text('objaverse_source','Source'))}</a>`);
        if(licenseUrl)links.push(`<a href="${this.escapeHtml(licenseUrl)}" target="_blank" rel="noopener">${this.escapeHtml(this.text('asset_license_link','License'))}</a>`);
        const detail=[parts.map(v=>this.escapeHtml(v)).join(' \u00b7 '),links.join(' \u00b7 ')].filter(Boolean).join(' \u00b7 ');
        return `<div class="saved-attribution"><strong>${this.escapeHtml(this.text('asset_attribution_label','Attribution'))}:</strong> ${detail||this.escapeHtml(this.text('asset_attribution_unknown','Not supplied by provider'))}</div>`;
    }

    static DisposeSavedPreviews() {
        for(const key of Array.from(this.engines.keys())){
            if (String(key).startsWith('saved:')) {
                this.DisposePreviewKey(key);
            }
        }
    }

    static RenderSaved() {
        if (this.mode!=='saved') {
            return;
        }
        this.DisposeSavedPreviews();
        const el=this.elements(), rows=this.SavedRows();
        if (!el.results) {
            return;
        }
        if(el.status) el.status.textContent=this.text('asset_saved_count','{count} saved asset folders',{count:rows.length.toLocaleString()});
        if(!rows.length){el.results.innerHTML=`<div class="objaverse-empty">${this.escapeHtml(this.text('asset_saved_empty','No saved assets in this category.'))}</div>`;
            return;
            }
        const animations=this.SavedAnimations();
        const options=['<option value="">'+this.escapeHtml(this.text('asset_default_animation_none','Default animation\u2026'))+'</option>'];
        for(const folder of animations) for(const v of (folder.versions||[])) options.push(`<option value="${this.escapeHtml(folder.folder||'')}|${this.escapeHtml(v.file||'')}">${this.escapeHtml(folder.name||'animation')} \u00b7 ${this.escapeHtml(v.file||'')}</option>`);
        el.results.innerHTML=rows.map(folder=>{
            const versions=Array.isArray(folder.versions)?folder.versions:[];
            let index=Math.max(0,versions.findIndex(v=>String(v.file||'')===String(folder.last_viewed||'')));
                if (index<0) {
                    index=0;
                }
            const current=versions[index]||{};
            const rel=`${folder.folder}/${current.file||''}`;
            const currentCategory=String(folder.category||'').toLowerCase();
            const isImage=this.savedCategory==='Images'||(this.savedCategory==='Unassigned'&&currentCategory==='images');
            const thumbnailRel=current.thumbnail_file?`${folder.folder}/${current.thumbnail_file}`:'';
            // Every 3D saved asset gets a thumbnail request. If no cached/provider
            // thumbnail exists, saved-thumb renders the model once, persists the
            // JPEG in the managed asset manifest, and all later views reuse it.
            const animated=String(folder.category||'').toLowerCase()==='animated'||(Array.isArray(current.inspection?.animation_names)&&current.inspection.animation_names.length>0);
            const editedThumb=current.blockify?.blockbench_edited&&current.blockify?.thumbnail?`${folder.folder}/${current.blockify.thumbnail}`:'';
            const thumbnail=!isImage?`<img class="objaverse-thumb saved-asset-thumb" data-staged-resource="${editedThumb?'saved-file':'saved-thumb'}" data-staged-path="${this.escapeHtml(editedThumb||rel)}" ${!editedThumb&&animated?'data-thumbnail-recipe="animation-frame-zero-v1"':''} alt="${this.escapeHtml(folder.name||'asset')}">`:'';
            const visual=isImage?`<img class="objaverse-thumb saved-image-source" data-staged-resource="saved-file" data-staged-path="${this.escapeHtml(rel)}" alt="${this.escapeHtml(folder.name||'asset')}"><canvas class="objaverse-canvas saved-asset-canvas image-minecraft-canvas" aria-label="Minecraft preview for ${this.escapeHtml(folder.name||'asset')}"></canvas>`:`${thumbnail}<canvas class="objaverse-canvas saved-asset-canvas" aria-label="${this.escapeHtml(folder.name||'asset')}"></canvas>`;
            const mixamo=this.savedCategory==='Animated'&&this.savedAnimated==='Mixamo';
            const fixed=this.savedCategory==='Animated'&&this.savedAnimated==='Fixed';
            return `<article class="objaverse-card saved-asset-card" data-folder="${this.escapeHtml(folder.folder||'')}" data-version-index="${index}" data-version-count="${versions.length}">
              <div class="objaverse-visual">${visual}<span class="objaverse-license">${this.escapeHtml(folder.subgroup||folder.category||this.savedCategory)}</span></div>
              <div class="objaverse-card-body"><h2 class="objaverse-title">${this.escapeHtml(folder.name||'asset')}</h2><div class="objaverse-byline">${this.escapeHtml(current.provider||'')} \u00b7 ${this.escapeHtml(current.file||'')}</div>${this.SavedAttributionHtml(current)}
              <div class="saved-version-nav"><button class="objaverse-action" data-action="saved-prev">\u25c0</button><span class="saved-version-label">${index+1} / ${versions.length}</span><button class="objaverse-action" data-action="saved-next">\u25b6</button></div>
              ${mixamo?`<select class="saved-animation-picker" data-action="saved-default-animation">${options.join('')}</select>`:''}
              ${!isImage?`<div class="asset-blockify-controls"><label>W <input class="asset-blockify-dimension" data-axis="width" type="number" min="1" max="64" value="${Number(current.blockify?.width||1)}"></label><label>H <input class="asset-blockify-dimension" data-axis="height" type="number" min="1" max="64" value="${Number(current.blockify?.height||1)}"></label><label>D <input class="asset-blockify-dimension" data-axis="depth" type="number" min="1" max="64" value="${Number(current.blockify?.depth||1)}"></label></div>`:`<div class="asset-blockify-controls image-minecraft-size"><label>H <input class="image-minecraft-height" type="number" min="1" max="64" value="${Number(current.blockify?.image_asset?current.blockify.height:4)}"></label><span class="image-minecraft-derived-width">W \u2014</span></div>`}<div class="objaverse-actions">${!isImage?`<button class="objaverse-action" data-action="saved-preview">GLB Preview</button><button class="objaverse-action" data-action="${this.HasBlockifyArtifact(current)?'saved-minecraft-preview':'saved-blockify'}">Blockify</button><button class="objaverse-action" data-action="saved-blockbench">CinterForge Voxel</button><button class="objaverse-action" data-action="saved-export">Export</button>`:`<button class="objaverse-action" data-action="saved-image-minecraft-preview">Blockify</button><button class="objaverse-action" data-action="saved-image-blockbench">CinterForge Voxel</button><button class="objaverse-action" data-action="saved-image-minecraft-export">Export Add-On</button>`}<button class="objaverse-action" data-action="saved-delete">Remove</button></div></div></article>`;
        }).join('');
        rows.forEach((folder,i)=>{
            const card=el.results.children[i];
             if (!card) {
                 return;
             }
            const picker=card.querySelector('.saved-animation-picker');
            if(picker&&folder.default_animation?.folder&&folder.default_animation?.file) picker.value=`${folder.default_animation.folder}|${folder.default_animation.file}`;
        });
        if(window.CINTERFORGE_IF_LOCAL_WORLD_ACTIVE===true){
            const allowed=new Set(['saved-prev','saved-next','saved-preview','saved-image-minecraft-preview','saved-minecraft-preview','saved-block-preview']);
            el.results.querySelectorAll('button[data-action]').forEach(button=>{
                if (allowed.has(String(button.dataset.action||''))) {
                    return;
                }
                button.disabled=true;
                 button.title='Available in the CinterForge Desktop app';
            });
            el.results.querySelectorAll('.asset-blockify-dimension,.image-minecraft-height,.saved-animation-picker').forEach(control=>{control.disabled=true;
                control.title='Saving changes is available in the CinterForge Desktop app';
                });
        }
        this.HydrateStagedImages();
        Array.from(el.results.querySelectorAll('.saved-asset-card')).forEach(card=>{
            const image=card.querySelector('.saved-image-source');
            if (!image) {
                return;
            }
            const update=()=>this.ImageMinecraftDimensions(card).catch(()=>{});
            if (image.complete&&image.naturalWidth) {
                update();
            }
            else image.addEventListener('load',update,{once:true});
        });
        this.SetupSavedCharacterPreviews();
    }

    static SavedFolderForCard(card){
        const wanted=String(card?.dataset?.folder||'');
        const catalogFolder=this.SavedRows().find(row=>String(row.folder||'')===wanted)||this.AssignmentSavedRows().map(row=>row.folder).find(row=>String(row?.folder||'')===wanted);
        if (catalogFolder) {
            return catalogFolder;
        }

        // A newly acquired assignment is canonical local authoring state before Push,
        // so it may intentionally be absent from the Cloudflare-backed saved-list.
        // Synthesize only the minimal folder/version identity from the card's portable
        // local Assets path. All mutations still go through the local asset commands.
        const localPath=String(card?.dataset?.localAssetPath||'').replace(/\\/g,'/').replace(/^\/+/, '');
        if (!localPath || localPath.includes('..')) {
            return null;
        }
        const slash=localPath.lastIndexOf('/');
        const folder=slash>=0?localPath.slice(0,slash):'';
        const file=slash>=0?localPath.slice(slash+1):localPath;
        if (!file) {
            return null;
        }
        return {
            folder,
            name:String(card?.dataset?.productName||file.replace(/\.[^.]+$/,'')),
            versions:[{file,provider:'Local Assets',kind:String(card?.dataset?.assetKind||'3d')}]
        };
        }

    static async CycleSaved(card, delta){
        const folder=this.SavedFolderForCard(card);
         if (!folder) {
             return;
         }
        const versions=folder.versions||[];
         if (!versions.length) {
             return;
         }
        let index=Number(card.dataset.versionIndex||0);
         index=(index+delta+versions.length)%versions.length;
        folder.last_viewed=versions[index].file;
         card.dataset.versionIndex=String(index);
        if(window.CINTERFORGE_IF_LOCAL_WORLD_ACTIVE!==true) await this.SavedPost('/assets/saved/viewed',{folder:folder.folder,file:folder.last_viewed});
        this.RenderSaved();
    }

    static async SavedPost(path, body){
        const operations={
            '/assets/saved/viewed':'saved-viewed',
            '/assets/saved/delete':'saved-delete',
            '/assets/saved/default-animation':'saved-default-animation',
            '/assets/saved/assign-animation':'saved-assign-animation',
            '/assets/saved/promote':'saved-promote',
            '/assets/sync':'sync'
        };
        const operation=operations[String(path||'')];
        if (!operation) {
            throw new Error(`Unsupported saved asset command: ${path}`);
        }
        return await window.CIFRuntime.assetCommand({operation,...(body||{})});
    }


}

window.CIFAssetLibrary = CIFAssetLibrary;
console.info('[CIF Asset] CIFAssetLibrary loaded', CIFAssetLibrary.VERSION);

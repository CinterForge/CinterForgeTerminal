/**
 * CinterForge Asset Assignment subsystem.
 *
 * Owns Asset Library assignment search, acquisition handoff, assignment UI,
 * and assignment interaction. Story/Series storage remains owned by Player;
 * this subsystem edits the Player-provided in-memory WorldData records.
 *
 * Methods execute with the Asset Library controller as `this` so provider,
 * preview, and common UI services can be extracted independently without
 * duplicating state during this refactor.
 */
class CIFAssetAssignment {
    static async LoadAssignments(){
        const el=this.elements();
        if (el.status) {
            el.status.textContent=this.text('asset_assign_loading','Loading room/object/character assignments\u2026');
        }
        try{
            if (!window.CIFWorldStore?.storyEntries || !window.CIFWorldStore?.loadWorldDataForGame || !window.CIFWorldData?.AssignmentDataset) {
                throw new Error('WorldData assignment boundary unavailable');
            }
            const assignmentGames=[];
            for (const [game,title] of window.CIFWorldStore.storyEntries()) {
                const worldData=await window.CIFWorldStore.loadWorldDataForGame(game);
                assignmentGames.push(window.CIFWorldData.AssignmentDataset(game,title,worldData));
            }
            this.assignmentGames=assignmentGames;
            this.LoadAssignmentUiState();
            // Assignment browsing is a presentation of the same in-memory WorldData
            // edited by Assign and persisted by Save. There is no second server-side
            // assignment authority in the editor.
            this.assignmentGame=String(this.assignmentGames[0]?.alias||'');
            if (this.assignmentSelection&&!this.assignmentGames.some(g=>String(g.alias)===String(this.assignmentSelection.alias))) {
                this.assignmentSelection=null;
            }
            const saved=await window.CIFRuntime.assetCommand({operation:'saved-list'});
            this.savedLibrary=saved;
            this.cloudflareTree={files:[]};
            try { await this.LoadCharacters(true);
             } catch (_) {
             }
            this.RenderAssignments();
            if (el.results) {
                el.results.dataset.loaded='true';
            }
            if(el.status){
                el.status.textContent=this.text('asset_assign_ready','Assign saved assets to rooms, game objects, and characters.');
            }
        }catch(error){
            if(el.results) el.results.innerHTML=`<div class="objaverse-empty">${this.escapeHtml(error.message||error)}</div>`;
            if (el.status) {
                el.status.textContent=this.text('asset_assign_failed','Assignments unavailable');
            }
        }
    }

    static AssignmentSavedRows(){
        return window.CIFAssetLibrary.AssignmentSavedRows.call(this);
    }

    static AssignmentSearchRows(query){
        return window.CIFAssetLibrary.AssignmentSearchRows.call(this, query);
    }

    static async SearchAssignmentInline(key, query, source, offset=0){
        const requestedSource = String(source || '');
        const supportedSources = ['objaverse','pd12m','character','openclipart','heroicons','lucide','kenney','freesound','incompetech','saved'];
        const q = String(query || '').trim();
        const which = supportedSources.includes(requestedSource) ? requestedSource : String(this.assignmentSource || 'objaverse');
        this.assignmentSource=which;
        this.assignmentAnimationSearchActiveFolder='';
        offset=Math.max(0,Number(offset)||0);
            const state={query:q,source:which,offset,total:0,results:[]};
            this.assignmentSearch.set(key,state);
        this.SaveAssignmentUiState();
        const searchOne=async(name)=>{
            if (name==='saved') {
                return this.AssignmentSearchRows(q);
            }
            if(name==='saved') return this.AssignmentSearchRows(q);
            const map={
                objaverse:{provider:'objaverse',kind:'3d',assetKind:'3d'},
                pd12m:{provider:'library',kind:'image',assetKind:'image'},
                character:{provider:'library',kind:'3d',assetKind:'character'},
                animation:{provider:'library',kind:'animation',assetKind:'animation'},
                openclipart:{provider:'openclipart',kind:'image',assetKind:'image'},
                heroicons:{provider:'heroicons',kind:'image',assetKind:'image'},
                lucide:{provider:'lucide',kind:'image',assetKind:'image'},
                kenney:{provider:'kenney',kind:'audio',assetKind:'audio'},
                freesound:{provider:'freesound',kind:'audio',assetKind:'audio'},
                incompetech:{provider:'incompetech',kind:'audio',assetKind:'audio'}
            };
            const cfg=map[name];
             if (!cfg) {
                 return [];
             }
            const payload=await window.CIFRuntime.assetCommand({operation:'search',provider:cfg.provider,kind:cfg.kind,q,limit:this.searchPageSize,offset});
            state.total=Math.max((payload.results||[]).length,Number(payload.total??(offset+(payload.results||[]).length))||0);
            return (payload.results||[]).map(row=>({external:true,provider:cfg.provider,sourceKind:cfg.kind,kind:cfg.assetKind,id:String(row.id||row.uid||''),title:String(row.title||row.name||row.file||row.id||row.uid||'Asset'),description:String(row.description||''),source:String(row.provider||cfg.provider),sourceUrl:String(row.source_url||row.viewer_url||row.url||''),row}));
        };
        state.results=await searchOne(which);
        this.SaveAssignmentUiState();
        this.RenderAssignments();
    }

    static HydrateAssignmentMediaThumbnails(root) {
        if (!root) {
            return;
        }
        root.querySelectorAll('[data-media-inline-thumbnail]').forEach(image => {
            const card=image.closest('[data-media-provider][data-media-id]');
            const provider=String(card?.dataset.mediaProvider||'');
            const id=String(card?.dataset.mediaId||'');
            if (!provider || !id || image.dataset.mediaLoading==='1') {
                return;
            }
            image.dataset.mediaLoading='1';
            window.CIFRuntime.assetCommand({operation:'media-preview',provider,id})
                .then(result => {
                    if (!String(result?.mime_type||'').startsWith('image/')) {
                        return;
                    }
                    image.src=`data:${result.mime_type};base64,${result.data_base64}`;
                    image.dataset.mediaLoaded='1';
                })
                .catch(error => {
                    image.alt=`Preview unavailable: ${String(error?.message||error)}`;
                    console.warn('[IFPlayer Assets] media thumbnail failed',{provider,id,error:String(error?.message||error)});
                })
                .finally(() => {
                    delete image.dataset.mediaLoading;
                });
        });
    }

    static async SaveExternalAssignment(button){
        return await window.CIFAssetDownload.SaveExternalAssignment.call(this, button);
    }

    static AssignmentAssetKindLabel(kind){return ({'3d':'3D object','character':'Animated character','image':'Image','animation':'Animation','audio':'Audio'}[String(kind)]||String(kind||'Asset'));
        }

    static AssignmentAnimationPanelHtml(folder,version){
        const folderPath=String(folder?.folder||''), isMixamo=folderPath.startsWith('Animated/Mixamo/'), isFixed=folderPath.startsWith('Animated/Fixed/');
        if (!isMixamo&&!isFixed) {
            return '';
        }
        const playButton=name=>`<button type="button" class="asset-animation-icon asset-animation-manage-play" data-animation-name="${this.escapeHtml(name)}" aria-label="Play or pause ${this.escapeHtml(name)}" title="Play / Pause">\u25b6</button>`;
        if(isFixed){
            const all=Array.isArray(version?.inspection?.animation_names)?version.inspection.animation_names.map(String).filter(Boolean):[];
            if (!all.length) {
                return '';
            }
            const defaultName=all[0];
            const rows=all.map(name=>`<div class="asset-animation-manage-row"><span title="${this.escapeHtml(name)}">${this.escapeHtml(name)}${name===defaultName?' <em>Default</em>':''}</span><div>${playButton(name)}</div></div>`).join('');
            return `<section class="asset-animation-manager" data-animation-manager="embedded"><header><strong>Animations</strong><small>${all.length} \u00b7 auto cycle</small></header><div class="asset-animation-manage-list">${rows}</div></section>`;
        }
        const assigned=Array.isArray(folder?.assigned_animations)?folder.assigned_animations.filter(row=>row?.folder&&row?.file):[];
        const defaultAnimation=folder?.default_animation&&folder.default_animation.folder&&folder.default_animation.file?folder.default_animation:null;
        const key=folderPath, state=this.assignmentAnimationSearch.get(key)||{query:'',results:[]};
        const isDefault=row=>!!defaultAnimation&&String(row.folder)===String(defaultAnimation.folder)&&String(row.file)===String(defaultAnimation.file);
        const rows=assigned.map((row,index)=>{const name=String(row.name||row.file||`Animation ${index+1}`),required=isDefault(row)||assigned.length===1;
            return `<div class="asset-animation-manage-row"><span title="${this.escapeHtml(name)}">${this.escapeHtml(name)}${isDefault(row)?' <em>Default</em>':''}</span><div>${playButton(name)}${required?'':`<button type="button" class="asset-animation-icon asset-animation-remove" data-animation-folder="${this.escapeHtml(row.folder)}" data-animation-file="${this.escapeHtml(row.file)}" aria-label="Remove animation ${this.escapeHtml(name)}" title="Remove Animation">\u00d7</button>`}</div></div>`;
            }).join('');
        return `<section class="asset-animation-manager" data-animation-manager="mixamo"><header><strong>Animations</strong><small>${Math.max(1,assigned.length)} \u00b7 auto cycle</small></header><div class="asset-animation-manage-list">${rows||'<div class="asset-animation-manage-row asset-animation-required"><span>Idle <em>Required</em></span><div></div></div>'}</div><form class="asset-animation-search-form"><div class="asset-animation-search-row"><input type="search" value="${this.escapeHtml(state.query||'')}" placeholder="Search animations\u2026" aria-label="Search Mixamo animations"><button type="submit" class="objaverse-action">Search</button></div></form></section>`;
    }

    static async RefreshAssignmentSavedLibrary(){
        return await window.CIFAssetLibrary.RefreshAssignmentSavedLibrary.call(this);
    }

    static SavedPreviewKey(card){
        const folder=this.SavedFolderForCard(card),versions=folder?.versions||[],version=versions[Number(card?.dataset?.versionIndex||0)]||versions[0];
        if (!folder||!version?.file) {
            return '';
        }
        return `saved:${folder.folder}/${version.file}:${this.BlockifyBehaviorSignature(folder,version)}`;
    }

    static async PlayManagedAnimation(card,name,button=null){
        if (!card||!name) {
            return;
        }
        // Animation controls belong to the preview the user is currently looking
        // at.  Never switch a Minecraft/Block preview back to GLB just because
        // Play/Pause was pressed.
        let key='';
        if(card.classList.contains('blockified-preview-active')){
            const folder=this.SavedFolderForCard(card),versions=folder?.versions||[],version=versions[Number(card?.dataset?.versionIndex||0)]||versions[0];
            if(folder&&version?.file)key=`blockify:${folder.folder}/${version.file}:minecraft`;
            if(!key||!this.embeddedAnimationControllers.get(key)){await this.LoadBlockifiedPreview(card);
            }
        }else{
            await this.LoadSavedPreview(card);
            key=this.SavedPreviewKey(card);
        }
        const controller=this.embeddedAnimationControllers.get(key);
        if (!controller?.select) {
            return;
        }
        const requested=String(name);
        const pausing=controller.manual===requested && !controller.paused;
        if (pausing) {
            controller.pause?.();
        }
         else controller.select(requested);
        card.querySelectorAll('.asset-animation-manage-play').forEach(btn=>{btn.textContent='\u25b6';
            btn.classList.remove('active');
            });
        if(button){button.textContent=pausing?'\u25b6':'\u23f8';
        button.classList.toggle('active',!pausing);
        }
        const picker=card.querySelector('.embedded-animation-picker');
        if(picker){picker.value=pausing?'':requested;
        }
    }

    static async SearchManagedAnimations(card,query){
        const folder=this.SavedFolderForCard(card);
        if (!folder) {
            return;
        }
        const q=String(query||'').trim();
        const payload=await window.CIFRuntime.assetCommand({operation:'search',provider:'library',kind:'animation',q,limit:24});
        const folderPath=String(folder.folder||'');
        this.assignmentAnimationSearch.set(folderPath,{query:q,results:Array.isArray(payload.results)?payload.results:[]});
        this.assignmentAnimationSearchActiveFolder=folderPath;
        this.RenderAssignments();
    }

    static ManagedAnimationResult(folder,id){
        const state=this.assignmentAnimationSearch.get(String(folder?.folder||''))||{};
        return (Array.isArray(state.results)?state.results:[]).find(row=>String(row?.id||'')===String(id||''))||null;
    }

    static async AddManagedAnimation(card,id){
        const folder=this.SavedFolderForCard(card),row=this.ManagedAnimationResult(folder,id);
            if (!folder||!row) {
                throw new Error('Animation result is no longer available.');
            }
        const state=await this.EnsureManagedAsset('animation',String(row.id||''),String(row.title||row.file||row.id||'animation'),card),rel=String(state?.relative_path||'');
        if (!rel) {
            throw new Error('Saved animation path was not returned.');
        }
        const parts=rel.split('/'),file=parts.pop()||'',animationFolder=parts.join('/');
        const animationName=String(row.title||row.file||row.id||'Animation');
        await window.CIFRuntime.assetCommand({operation:'saved-assign-animation',folder:folder.folder,animation_folder:animationFolder,animation_file:file,name:animationName});
        await this.RefreshAssignmentSavedLibrary();
        await new Promise(resolve=>requestAnimationFrame(resolve));
        const freshCard=Array.from(this.elements().results?.querySelectorAll('.assignment-current-card')||[]).find(candidate=>String(candidate.dataset.folder||'')===String(folder.folder||''));
        if(freshCard)await this.PlayManagedAnimation(freshCard,animationName,freshCard.querySelector(`.asset-animation-manage-play[data-animation-name="${CSS.escape(animationName)}"]`));
    }

    static async PreviewManagedAnimationResult(card,id){
        const folder=this.SavedFolderForCard(card),row=this.ManagedAnimationResult(folder,id);
            if (!folder||!row) {
                throw new Error('Animation result is no longer available.');
            }
        const state=await this.EnsureManagedAsset('animation',String(row.id||''),String(row.title||row.file||row.id||'animation'),card),rel=String(state?.relative_path||'');
        if (!rel) {
            throw new Error('Saved animation path was not returned.');
        }
        const parts=rel.split('/'),file=parts.pop()||'',animationFolder=parts.join('/');
        await window.CIFRuntime.assetCommand({operation:'saved-assign-animation',folder:folder.folder,animation_folder:animationFolder,animation_file:file,name:String(row.title||row.file||row.id||'Animation')});
        this.savedLibrary=await window.CIFRuntime.assetCommand({operation:'saved-list'});
        await this.PlayManagedAnimation(card,String(row.title||row.file||row.id||'Animation'));
        await window.CIFRuntime.assetCommand({operation:'saved-remove-animation',folder:folder.folder,animation_folder:animationFolder,animation_file:file});
        this.savedLibrary=await window.CIFRuntime.assetCommand({operation:'saved-list'});
    }

    static LoadAssignmentUiState(){
        if (this.assignmentStateLoaded) {
            return;
        }
        this.assignmentStateLoaded=true;
        try{
            // UI state is browser-local and can grow with the expanded asset tree.
            // Never put it in a cookie: cookies are retransmitted on every localhost
            // request, turning Code-OSS filesystem traffic into hundreds of MB.
            let serialized=localStorage.getItem(this.assignmentStateKey);
            if(!serialized){
                const prefix=`${this.assignmentStateKey}=`;
                const cookie=String(document.cookie||'').split(';').map(v=>v.trim()).find(v=>v.startsWith(prefix));
                if (cookie) {
                    serialized=decodeURIComponent(cookie.slice(prefix.length));
                }
            }
            // Remove the legacy Path=/ cookie even when there was no saved state.
            document.cookie=`${this.assignmentStateKey}=; Path=/; Max-Age=0; SameSite=Lax`;
            if (!serialized) {
                return;
            }
            const state=JSON.parse(serialized);
            this.assignmentSelection=state?.selection&&typeof state.selection==='object'?state.selection:null;
            this.assignmentOpenKeys=new Set(Array.isArray(state?.openKeys)?state.openKeys.map(String):[]);
            this.assignmentBrowserScrollTop=Number.isFinite(Number(state?.scrollTop))?Number(state.scrollTop):0;
            this.assignmentSource=String(state?.source||'objaverse');
                if (!['objaverse','pd12m','character','openclipart','heroicons','lucide','kenney','freesound','incompetech','saved'].includes(this.assignmentSource)) {
                    this.assignmentSource='objaverse';
                }
            this.assignmentSearch=new Map();
            if(this.assignmentSelection&&state?.query!==undefined){
                const sel=this.assignmentSelection, key=`${sel.alias}:${sel.nodeKind}:${sel.nodeId}`;
                this.assignmentSearch.set(key,{query:String(state.query||''),source:String(state.source||'all'),results:[]});
            }
        }catch(error){
            console.warn('[IFPlayer Assets Assign] failed to restore UI state',error);
            this.assignmentSelection=null;
            this.assignmentOpenKeys=new Set();
            this.assignmentBrowserScrollTop=0;
            this.assignmentSource='objaverse';
            this.assignmentSearch=new Map();
        }
    }

    static SaveAssignmentUiState(){
        try{
            const sel=this.assignmentSelection||null;
            const key=sel?`${sel.alias}:${sel.nodeKind}:${sel.nodeId}`:'';
            const search=key?this.assignmentSearch.get(key):null;
            const state={selection:sel,openKeys:[...this.assignmentOpenKeys],scrollTop:Number(this.assignmentBrowserScrollTop||0),source:String(search?.source||this.assignmentSource||'objaverse'),query:String(search?.query||'')};
            localStorage.setItem(this.assignmentStateKey,JSON.stringify(state));
            // Clean up pre-migration cookies so asset UI state is never attached to
            // /code/command, terminal, runtime, or Cloudflare-control requests.
            document.cookie=`${this.assignmentStateKey}=; Path=/; Max-Age=0; SameSite=Lax`;
        }catch(error){console.warn('[IFPlayer Assets Assign] failed to persist UI state',error);
        }
    }

    static BindAssignmentBrowser(el,games,displayName){
        for(const d of el.results.querySelectorAll('details[data-tree-key]')){
            const k=String(d.dataset.treeKey||'');
            d.open=!!(k&&this.assignmentOpenKeys.has(k));
            d.addEventListener('toggle',()=>{
                const key=String(d.dataset.treeKey||'');
                 if (!key) {
                     return;
                 }
                if (d.open) {
                    this.assignmentOpenKeys.add(key);
                }
                 else this.assignmentOpenKeys.delete(key);
                this.SaveAssignmentUiState();
            });
        }
        const browser=el.results.querySelector('.assignment-browser');
        if(browser){
            browser.scrollTop=this.assignmentBrowserScrollTop;
            browser.addEventListener('scroll',()=>{this.assignmentBrowserScrollTop=browser.scrollTop;
                this.SaveAssignmentUiState();
                },{passive:true});
        }
        el.results.querySelectorAll('[data-assignment-select]').forEach(btn=>btn.addEventListener('click',()=>{
            this.assignmentSelection={alias:String(btn.dataset.alias||''),nodeKind:String(btn.dataset.nodeKind||'rooms'),nodeId:String(btn.dataset.nodeId||''),assetUCI:String(btn.dataset.assignmentAssetUci||'')};
            const g=games.find(x=>String(x.alias)===String(btn.dataset.alias));
            const rr=String(btn.dataset.nodeKind)==='objects'?(g?.objects||[]):(g?.rooms||[]);
            const n=rr.find(x=>String(x.id)===String(btn.dataset.nodeId));
            if(n){const nk=`${btn.dataset.alias}:${btn.dataset.nodeKind}:${btn.dataset.nodeId}`,q=displayName(n).trim(),stickySource=String(this.assignmentSource||'objaverse');
                this.assignmentSearch.set(nk,{query:q,source:stickySource,results:[]});
                }
            this.SaveAssignmentUiState();
            this.RenderAssignments();
        }));
    }

    static RenderAssignments(){
        const el=this.elements();
         if (!el.results) {
             return;
         }
        // This method replaces the card DOM wholesale. Dispose previews before
        // their canvases become detached, otherwise Babylon keeps GPU resources
        // alive behind dead assignment cards.
        for(const [key,engine] of Array.from(this.engines.entries())){
            const canvas=engine?.getRenderingCanvas?.();
            if (canvas&&el.results.contains(canvas)) {
                this.DisposePreviewKey(key);
            }
        }
        this.DisposeDetachedPreviews();
        const browserBefore=el.results.querySelector('.assignment-browser');
        if (browserBefore) {
            this.assignmentBrowserScrollTop=browserBefore.scrollTop;
        }
        for(const d of el.results.querySelectorAll('details[data-tree-key]')){
            const k=String(d.dataset.treeKey||'');
             if (!k) {
                 continue;
             }
            if (d.open) {
                this.assignmentOpenKeys.add(k);
            }
             else this.assignmentOpenKeys.delete(k);
        }
        // Render the Story datasets projected directly from the authoritative
        // in-memory WorldData. A Series intentionally contributes one dataset
        // per constituent Story; it does not introduce another storage authority.
        const games=this.assignmentGames.slice();
        if(!games.length){el.results.innerHTML='<div class="objaverse-empty">No authored IF room/object/character data is available for the loaded game.</div>';
            return;
            }
        const displayName=node=>{const raw=String(node.display_name||node.title||node.name||node.id||'').trim();
            if (/^\([^)]*maze[^)]*\)$/i.test(raw)) {
                return 'Maze';
            }
            return raw.replace(/^\((.*)\)$/,'$1').replaceAll('_',' ');
            };
        if (this.assignmentSelection&&!games.some(g=>String(g.alias)===String(this.assignmentSelection.alias))) {
            this.assignmentSelection=null;
        }
        const groupBy=(rows,keyFn)=>{const m=new Map();
            for(const row of(rows||[])){const k=String(keyFn(row)||'Unassigned');
            if (!m.has(k)) {
                m.set(k,[]);
            }
            m.get(k).push(row);
            }return [...m.entries()].sort((a,b)=>a[0].localeCompare(b[0]));
            };
        const roomGroup=r=>String(r.group||r.group_name||r.area||r.region||'Ungrouped');
        const roomFloor=r=>String(r.floor||r.floor_name||r.level||'Unassigned');
        const objectCategory=o=>String(o.category||o.subcategory||o.type||'Objects');
        const characterCategory=c=>String(c.category||c.subcategory||c.type||'Characters');
        const savedByUCI=new Map(this.AssignmentSavedRows().filter(row=>row.uci).map(row=>[String(row.uci),row]));
        const assignedForNode=node=>{
            const out=[],seen=new Set();
            for(const entry of (Array.isArray(node.assets)?node.assets:[])){
                const uci=String(entry?.uci||'').trim();
                if (!uci||seen.has(uci)) continue;
                seen.add(uci);
                out.push({...entry,uci});
            }
            const asset3dUCI=String(node.asset3d_uci||'').trim();
            if(asset3dUCI&&!seen.has(asset3dUCI)) out.unshift({uci:asset3dUCI,kind:'3d',primary:true});
            return out;
        };
        const selectButton=(game,node,nodeKind)=>{
            const active=this.assignmentSelection&&String(this.assignmentSelection.alias)===String(game.alias)&&String(this.assignmentSelection.nodeKind)===nodeKind&&String(this.assignmentSelection.nodeId)===String(node.id);
            const assigned=assignedForNode(node),assetCount=assigned.length,entityKey=`entity:${game.alias}:${nodeKind}:${node.id}`;
            if (!assetCount) {
                return `<button type="button" class="assignment-browser-item${active?' active':''} unassigned" data-assignment-select="1" data-alias="${this.escapeHtml(game.alias)}" data-node-kind="${nodeKind}" data-node-id="${this.escapeHtml(node.id)}">${this.escapeHtml(displayName(node))}</button>`;
            }
            const children=assigned.map((entry,index)=>{const saved=savedByUCI.get(String(entry.uci)),label=String(saved?.folder?.name||entry.label||entry.uci||`Asset ${index+1}`),selected=active&&String(this.assignmentSelection?.assetUCI||'')===String(entry.uci||'');
                return `<button type="button" class="assignment-browser-asset${selected?' active':''}" data-assignment-select="1" data-assignment-asset-uci="${this.escapeHtml(entry.uci)}" data-alias="${this.escapeHtml(game.alias)}" data-node-kind="${nodeKind}" data-node-id="${this.escapeHtml(node.id)}">${this.escapeHtml(label)}</button>`;
                }).join('');
            return `<details class="assignment-browser-entity" data-tree-key="${this.escapeHtml(entityKey)}"><summary><span class="assignment-browser-entity-name" data-assignment-select="1" data-alias="${this.escapeHtml(game.alias)}" data-node-kind="${nodeKind}" data-node-id="${this.escapeHtml(node.id)}">${this.escapeHtml(displayName(node))}</span><span>${assetCount}</span></summary><div class="assignment-browser-assets">${children}</div></details>`;
        };
        const section=(label,groups,game,nodeKind)=>{const sk=`${game.alias}:${label}`;
            return `<details class="assignment-browser-section" data-tree-key="${this.escapeHtml(sk)}"><summary>${this.escapeHtml(label)} <span>${groups.reduce((n,p)=>n+p[1].length,0)}</span></summary>${groups.map(([name,rows])=>{const gk=`${sk}:${name}`;return `<details class="assignment-browser-group" data-tree-key="${this.escapeHtml(gk)}"><summary>${this.escapeHtml(name)} <span>${rows.length}</span></summary><div class="assignment-browser-items">${rows.map(row=>selectButton(game,row,nodeKind)).join('')}</div></details>`;}).join('')}</details>`;
            };
        const gameTree=game=>{const k=`game:${game.alias}`,total=Number(game.room_count||0)+Number(game.object_count||0)+Number(game.character_count||0);
            return `<details class="assignment-browser-game" data-tree-key="${this.escapeHtml(k)}"><summary>${this.escapeHtml(game.title||game.alias)} <span>${total}</span></summary>${section('Groups',groupBy(game.rooms,roomGroup),game,'rooms')}${section('Floors',groupBy(game.rooms,roomFloor),game,'rooms')}${section('Objects',groupBy(game.objects,objectCategory),game,'objects')}${section('Characters',groupBy(game.characters,characterCategory),game,'characters')}</details>`;
            };

        let selectedGame=this.assignmentSelection?games.find(g=>String(g.alias)===String(this.assignmentSelection.alias)):null;
        let selectedKind=String(this.assignmentSelection?.nodeKind||'');
        let rows=selectedGame?(selectedKind==='objects'?(selectedGame.objects||[]):selectedKind==='characters'?(selectedGame.characters||[]):(selectedGame.rooms||[])):[];
        let node=selectedGame?rows.find(n=>String(n.id)===String(this.assignmentSelection?.nodeId)):null;
        if(!node&&this.assignmentSelection){this.assignmentSelection=null;
            this.SaveAssignmentUiState();
            selectedGame=null;
            selectedKind='';
            }
        if(!node){
            el.results.classList.add('assignment-workspace');
            el.results.innerHTML=`<div class="assignment-shell"><aside class="assignment-browser">${games.map(gameTree).join('')}</aside><main class="assignment-editor assignment-empty-editor"><div class="assignment-no-results">Select a room, game object, or character to assign assets.</div></main></div>`;
            this.BindAssignmentBrowser(el,games,displayName);
            return;
        }
        const key=`${selectedGame.alias}:${selectedKind}:${node.id}`;
        const pendingCloudflare=this.pendingCloudflareAsset;
        const pendingCloudflareHtml='';
        if(!this.assignmentSearch.has(key)){const q=displayName(node).trim(), stickySource=String(this.assignmentSource||'objaverse');
            this.assignmentSearch.set(key,{query:q,source:stickySource,results:[]});
            }
        const searchState=this.assignmentSearch.get(key)||{}, query=String(searchState.query||displayName(node)).trim(), source=String(searchState.source||this.assignmentSource||'objaverse'), results=Array.isArray(searchState.results)?searchState.results:[];
        const assets=Array.isArray(node.assets)?node.assets:[], defaultPath=String(node.asset3d||''), defaultEntry=assets.find(a=>String(a.path||'')===defaultPath)||null;
        const assignedEntries=assignedForNode(node);
        const defaultUCI=String(node.asset3d_uci||'').trim();
        const visibleAssignedEntries=assignedEntries;
        const assignedHtml=visibleAssignedEntries.length?visibleAssignedEntries.map(entry=>{
            const uci=String(entry.uci||'').trim();
            const saved=savedByUCI.get(uci);
            const kind=String(entry.kind||saved?.kind||(uci===defaultUCI?'3d':'asset'));
            const isDefault=uci===defaultUCI;
            if(!saved){
                return `<article class="objaverse-card assignment-current-card assignment-missing-card" data-assignment-uci="${this.escapeHtml(uci)}"><div class="objaverse-card-body"><h2 class="objaverse-title">${this.escapeHtml(entry.label||uci)}</h2><div class="objaverse-byline">Local UCI asset unavailable · ${this.escapeHtml(uci)}</div><div class="objaverse-actions"><button class="objaverse-action" data-assignment-action="remove" data-uci="${this.escapeHtml(uci)}" data-kind="${this.escapeHtml(kind)}">Remove</button></div></div></article>`;
            }
            const folder=saved.folder||{}, version=saved.version||{}, versionIndex=Math.max(0,(folder.versions||[]).indexOf(version));
            const rel=`${folder.folder}/${version.file||''}`;
            const thumb=String(version.thumbnail_file||'');
            const isImage=kind==='image';
            const thumbRel=thumb?`${folder.folder}/${thumb}`:'';
            const visual=isImage
                ? `<img class="objaverse-thumb saved-image-source" data-staged-resource="saved-file" data-staged-path="${this.escapeHtml(rel)}" alt="${this.escapeHtml(folder.name||'asset')}">`
                : `${thumbRel?`<img class="objaverse-thumb saved-asset-thumb" data-staged-resource="saved-file" data-staged-path="${this.escapeHtml(thumbRel)}" alt="${this.escapeHtml(folder.name||'asset')}">`:''}<canvas class="objaverse-canvas saved-asset-canvas"></canvas>`;
            return `<article class="objaverse-card saved-asset-card assignment-current-card${String(this.assignmentSelection?.assetUCI||'')===uci?' assignment-current-selected':''}" data-assignment-uci="${this.escapeHtml(uci)}" data-folder="${this.escapeHtml(folder.folder||'')}" data-version-index="${versionIndex}" data-product-name="${this.escapeHtml(displayName(node))}"><div class="assignment-current-main"><div class="objaverse-visual">${visual}<span class="objaverse-license">${this.escapeHtml(isDefault?'Default 3D':this.AssignmentAssetKindLabel(kind))}</span></div><div class="objaverse-card-body"><h2 class="objaverse-title">${this.escapeHtml(folder.name||uci)}</h2><div class="objaverse-byline">${this.escapeHtml(uci)}</div>${isImage?'':`<div class="asset-blockify-controls"><label>W <input class="asset-blockify-dimension" data-axis="width" type="number" min="1" max="64" value="${Math.max(1,Number(version.blockify?.width||1))}"></label><label>H <input class="asset-blockify-dimension" data-axis="height" type="number" min="1" max="64" value="${Math.max(1,Number(version.blockify?.height||1))}"></label><label>D <input class="asset-blockify-dimension" data-axis="depth" type="number" min="1" max="64" value="${Math.max(1,Number(version.blockify?.depth||1))}"></label><span class="asset-blockify-resolution-fixed">16×16×16 Bedrock voxel space</span></div>`}<div class="objaverse-actions">${isImage?`<button class="objaverse-action" data-action="assignment-image-preview">Preview</button>`:`<button class="objaverse-action" data-action="saved-preview">3D Preview</button><button class="objaverse-action" data-action="saved-block-preview">Block Preview</button><button class="objaverse-action" data-action="saved-blockify">Blockify</button><button class="objaverse-action" data-action="saved-blockbench">Blockbench</button>${this.HasBlockifyArtifact(version)?`<button class="objaverse-action" data-action="saved-block-export">Export Add-On</button>`:''}`}<button class="objaverse-action" data-assignment-action="remove" data-uci="${this.escapeHtml(uci)}" data-kind="${this.escapeHtml(kind)}">Remove</button></div></div></div></article>`;
        }).join(''):'<div class="assignment-no-results">No assets assigned.</div>';
        const resultHtml=results.length?results.map(r=>{
            const kind=String(r.kind||'asset'), label=kind==='character'?'Assign Character':kind==='animation'?'Assign Animation':'Assign';
            if(r.cloudflare){
                const sourceUrl=String(r.sourceUrl||''), path=String(r.path||''), isImage=kind==='image', is3d=kind==='3d', thumbnailUrl=String(r.thumbnailUrl||'');
                const visual=isImage?`<img class="objaverse-thumb" src="${this.escapeHtml(sourceUrl)}" alt="${this.escapeHtml(r.title)}">`:is3d?`${thumbnailUrl?`<img class="objaverse-thumb saved-asset-thumb" src="${this.escapeHtml(thumbnailUrl)}" alt="${this.escapeHtml(r.title)}">`:''}<canvas class="objaverse-canvas saved-asset-canvas"></canvas>`:'\u25a7';
                return `<article class="objaverse-card assignment-result cloudflare-assignment-result" data-direct-cloudflare-path="${this.escapeHtml(path)}" data-asset-kind="${this.escapeHtml(kind)}"${mediaProvider?` data-media-provider="${this.escapeHtml(r.provider)}" data-media-id="${this.escapeHtml(r.id)}"`:''}><div class="assignment-result-thumb">${visual}</div><div><strong>${this.escapeHtml(r.title)}</strong><small>Cloudflare \u00b7 ${this.escapeHtml(this.AssignmentAssetKindLabel(kind))}</small>${is3d?`<div class="asset-blockify-controls"><label>W <input class="asset-blockify-dimension" data-axis="width" type="number" min="1" max="64" value="1"></label><label>H <input class="asset-blockify-dimension" data-axis="height" type="number" min="1" max="64" value="1"></label><label>D <input class="asset-blockify-dimension" data-axis="depth" type="number" min="1" max="64" value="1"></label></div>`:''}<div class="objaverse-actions"><button class="objaverse-action" data-action="assignment-cloudflare-preview" ${is3d||isImage?'':'disabled'}>GLB Preview</button>${is3d?`<button class="objaverse-action" data-action="assignment-cloudflare-minecraft">Blockify</button><button class="objaverse-action" data-action="assignment-cloudflare-blockbench">Blockbench</button>`:''}<a class="objaverse-action" href="${this.escapeHtml(sourceUrl)}" target="_blank" rel="noopener">Source</a><button class="objaverse-action" data-assignment-action="add-default" data-uci="${this.escapeHtml(r.uci||'')}" data-kind="${this.escapeHtml(kind)}" ${r.uci?'':'disabled title="Cloud asset has no UCI identity"'}>Assign</button></div></div></article>`;
            }
            if(r.external){
                const sourceUrl=String(r.sourceUrl||r.row?.source_url||r.row?.viewer_url||'');
                const mediaProvider=['openclipart','heroicons','lucide','kenney','freesound','incompetech'].includes(String(r.provider||''));
                const previewAction=mediaProvider?'media-preview':kind==='3d'?'preview':kind==='character'?'character-preview':kind==='animation'?'animation-preview':'assignment-image-preview';
                const imageThumb=String(r.row?.thumbnail_url||'');
                const mediaProviderName=String(r.provider||'');
                const isSvgProvider=['openclipart','heroicons','lucide'].includes(mediaProviderName);
                const isAudioProvider=['kenney','freesound','incompetech'].includes(mediaProviderName);
                const objaverseThumb=kind==='3d'?`<img class="objaverse-thumb" data-staged-resource="objaverse-thumb" data-staged-id="${this.escapeHtml(r.id)}" alt="${this.escapeHtml(r.title)}">`:'';
                const characterThumb=kind==='character'?`<img class="objaverse-thumb" data-staged-resource="character-thumb" data-staged-id="${this.escapeHtml(r.id)}" alt="${this.escapeHtml(r.title)}">`:'';
                const svgThumb=isSvgProvider?`<img class="objaverse-thumb media-inline-thumbnail" data-media-inline-thumbnail alt="${this.escapeHtml(r.title)}">`:'';
                const audioPlay=isAudioProvider?`<button type="button" class="media-audio-play" data-action="media-preview" aria-label="Play ${this.escapeHtml(r.title)}" title="Play">\u25b6</button>`:'';
                const visual=kind==='3d'?`${objaverseThumb}<canvas class="objaverse-canvas"></canvas>`:kind==='character'?`${characterThumb}<canvas class="asset-character-canvas"></canvas>`:kind==='animation'?`<canvas class="asset-animation-canvas"></canvas>`:svgThumb||audioPlay||(imageThumb?`<img class="objaverse-thumb" data-staged-resource="image-thumb" data-staged-id="${this.escapeHtml(r.id)}" alt="${this.escapeHtml(r.title)}">`:'\u25a7');
                const previewLabel=isAudioProvider?'Play':'Preview';
                return `<article class="objaverse-card assignment-result${kind==='animation'?' asset-animation-card':''}" data-uid="${this.escapeHtml(r.id)}" data-asset-id="${this.escapeHtml(r.id)}" data-asset-kind="${this.escapeHtml(kind)}"${mediaProvider?` data-media-provider="${this.escapeHtml(mediaProviderName)}" data-media-id="${this.escapeHtml(r.id)}"`:''}><div class="assignment-result-thumb">${visual}</div><div><strong>${this.escapeHtml(r.title)}</strong><small>${this.escapeHtml(r.source)} \u00b7 ${this.escapeHtml(this.AssignmentAssetKindLabel(kind))}</small><div class="objaverse-actions"><button class="objaverse-action" data-action="${previewAction}">${previewLabel}</button>${sourceUrl?`<a class="objaverse-action" href="${this.escapeHtml(sourceUrl)}" target="_blank" rel="noopener">Source</a>`:''}${mediaProvider?(mediaProviderName==='freesound'?'':`<button class="objaverse-action" data-action="media-download">Save locally</button>`):`<button class="objaverse-action" data-assignment-action="add-external-default" data-provider="${this.escapeHtml(r.provider)}" data-source-kind="${this.escapeHtml(r.sourceKind)}" data-asset-id="${this.escapeHtml(r.id)}" data-kind="${this.escapeHtml(kind)}">${label}</button>`}</div></div></article>`;
            }
            const thumb=String(r.version?.thumbnail_file||''), meta=r.version?.metadata&&typeof r.version.metadata==='object'?r.version.metadata:{}, sourceUrl=String(meta.source_url||meta.viewer_url||''), versionIndex=Math.max(0,(r.folder.versions||[]).indexOf(r.version));
            const localVisual=kind==='image'?(thumb?`<img class="objaverse-thumb" data-staged-resource="saved-file" data-staged-path="${this.escapeHtml(`${r.folder.folder}/${thumb}`)}" alt="">`:'\u25a7'):`${`<img class="objaverse-thumb saved-asset-thumb" data-staged-resource="saved-thumb" data-staged-path="${this.escapeHtml(`${r.folder.folder}/${r.version.file}`)}" alt="">`}<canvas class="objaverse-canvas saved-asset-canvas"></canvas>`;
            return `<article class="objaverse-card saved-asset-card assignment-result" data-folder="${this.escapeHtml(r.folder.folder||'')}" data-version-index="${versionIndex}"><div class="assignment-result-thumb">${localVisual}</div><div><strong>${this.escapeHtml(r.folder.name||r.version.file)}</strong><small>${this.escapeHtml(r.version.file)} \u00b7 ${this.escapeHtml(r.version.provider||kind)} \u00b7 ${this.escapeHtml(this.AssignmentAssetKindLabel(kind))}</small>${kind!=='image'?`<div class="asset-blockify-controls"><label>W <input class="asset-blockify-dimension" data-axis="width" type="number" min="1" max="64" value="${Number(r.version.blockify?.width||1)}"></label><label>H <input class="asset-blockify-dimension" data-axis="height" type="number" min="1" max="64" value="${Number(r.version.blockify?.height||1)}"></label><label>D <input class="asset-blockify-dimension" data-axis="depth" type="number" min="1" max="64" value="${Number(r.version.blockify?.depth||1)}"></label></div>`:''}<div class="objaverse-actions"><button class="objaverse-action" data-action="${kind==='image'?'assignment-image-preview':'saved-preview'}">${kind==='image'?'Preview':'3D Preview'}</button>${kind!=='image'?`<button class="objaverse-action" data-action="saved-block-preview">Block Preview</button><button class="objaverse-action" data-action="saved-blockify">Blockify</button><button class="objaverse-action" data-action="saved-blockbench">Blockbench</button>${this.HasBlockifyArtifact(r.version)?`<button class="objaverse-action" data-action="saved-block-export">Export Add-On</button>`:''}`:''}${sourceUrl?`<a class="objaverse-action" href="${this.escapeHtml(sourceUrl)}" target="_blank" rel="noopener">Source</a>`:''}<button class="objaverse-action" data-assignment-action="add-default" data-uci="${this.escapeHtml(r.uci||r.folder?.uci||r.version?.uci||'')}" data-kind="${this.escapeHtml(kind)}">${label}</button></div></div></article>`;
        }).join(''):'<div class="assignment-no-results">Press Search to find assets for this game entity.</div>';
        const assignmentOffset=Math.max(0,Number(searchState?.offset)||0), assignmentTotal=Math.max(results.length,Number(searchState?.total)||0);
        const assignmentFirst=results.length?assignmentOffset+1:0, assignmentLast=assignmentOffset+results.length;
        const assignmentPager=searchState&&assignmentTotal>this.searchPageSize?`<nav class="asset-search-pager assignment-search-pager"><button type="button" class="objaverse-action" data-assignment-page="prev" ${assignmentOffset<=0?'disabled':''}>Previous</button><span>${assignmentFirst.toLocaleString()}–${assignmentLast.toLocaleString()} of ${assignmentTotal.toLocaleString()}</span><button type="button" class="objaverse-action" data-assignment-page="next" ${assignmentLast>=assignmentTotal?'disabled':''}>Next</button></nav>`:'';
        const assignedFolderPaths=new Set(visibleAssignedEntries.map(entry=>savedByUCI.get(String(entry.uci||''))?.folder?.folder).filter(Boolean).map(String));
        let animationSearchFolder=String(this.assignmentAnimationSearchActiveFolder||'');
        if(animationSearchFolder&&!assignedFolderPaths.has(animationSearchFolder)){this.assignmentAnimationSearchActiveFolder='';
            animationSearchFolder='';
            }
        const animationSearchState=animationSearchFolder?(this.assignmentAnimationSearch.get(animationSearchFolder)||null):null;
        const animationSearchRows=Array.isArray(animationSearchState?.results)?animationSearchState.results:[];
        const animationFolderRecord=animationSearchFolder?this.AssignmentSavedRows().find(row=>String(row?.folder?.folder||'')===animationSearchFolder):null;
        let animationSearchHtml='';
        if(animationSearchState){
            const ownerName=String(animationFolderRecord?.folder?.name||'').trim();
            const headingName=ownerName?` for ${this.escapeHtml(ownerName)}`:'';
            const countLabel=`${animationSearchRows.length} result${animationSearchRows.length===1?'':'s'}`;
            const cards=animationSearchRows.map(row=>{
                const id=this.escapeHtml(row.id||''), title=this.escapeHtml(row.title||row.file||row.id||'Animation');
                return `<article class="assignment-animation-result-row asset-animation-search-card" data-animation-folder="${this.escapeHtml(animationSearchFolder)}" data-animation-id="${id}"><div class="assignment-animation-result-name"><strong>${title}</strong><small>Mixamo \u00b7 Animation</small></div><div class="objaverse-actions"><button type="button" class="objaverse-action asset-animation-result-preview" data-animation-id="${id}">Preview</button><button type="button" class="objaverse-action asset-animation-result-add" data-animation-id="${id}">Add</button></div></article>`;
            }).join('');
            const empty=`<div class="assignment-no-results">No Mixamo animations found for \u201c${this.escapeHtml(animationSearchState.query||'')}\u201d.</div>`;
            animationSearchHtml=`<div class="assignment-animation-search-heading"><strong>Mixamo Animations${headingName}</strong><small>${countLabel}</small></div>${cards||empty}`;
        }
        const lowerResultsHtml=animationSearchState?animationSearchHtml:(resultHtml+assignmentPager);
        el.results.classList.add('assignment-workspace');
        el.results.innerHTML=`<div class="assignment-shell"><aside class="assignment-browser">${games.map(gameTree).join('')}</aside><main class="assignment-editor assignment-node" data-alias="${this.escapeHtml(selectedGame.alias)}" data-story-id="${this.escapeHtml(selectedGame.story_id||'')}" data-node-kind="${selectedKind}" data-node-id="${this.escapeHtml(node.id)}" data-key="${this.escapeHtml(key)}"><header class="assignment-editor-header"><div class="assignment-editor-title"><strong>${this.escapeHtml(displayName(node))}</strong><small>${selectedKind==='rooms'?'Room':selectedKind==='characters'?'Character':'Game object'} \u00b7 ${this.escapeHtml(selectedGame.title||selectedGame.alias)}</small></div><form class="assignment-header-search" data-assignment-search-form data-source="${this.escapeHtml(source)}"><div class="assignment-source-modes" role="group" aria-label="Asset source"><button type="button" class="assignment-source-button${source==='objaverse'?' active':''}" data-assignment-source="objaverse">Objaverse 3D</button><button type="button" class="assignment-source-button${source==='pd12m'?' active':''}" data-assignment-source="pd12m">PD12M Images</button><button type="button" class="assignment-source-button${source==='openclipart'?' active':''}" data-assignment-source="openclipart">Openclipart</button><button type="button" class="assignment-source-button${source==='heroicons'?' active':''}" data-assignment-source="heroicons">Heroicons</button><button type="button" class="assignment-source-button${source==='lucide'?' active':''}" data-assignment-source="lucide">Lucide</button><button type="button" class="assignment-source-button${source==='character'?' active':''}" data-assignment-source="character">Mixamo Characters</button><button type="button" class="assignment-source-button${source==='kenney'?' active':''}" data-assignment-source="kenney">Kenney SFX</button><button type="button" class="assignment-source-button${source==='freesound'?' active':''}" data-assignment-source="freesound">Freesound</button><button type="button" class="assignment-source-button${source==='incompetech'?' active':''}" data-assignment-source="incompetech">Incompetech Music</button><button type="button" class="assignment-source-button${source==='saved'?' active':''}" data-assignment-source="saved">Local Assets</button></div><div class="assignment-search-row"><input value="${this.escapeHtml(query)}" aria-label="Search assets for ${this.escapeHtml(displayName(node))}"><button class="objaverse-action" type="submit">Search</button></div></form></header>${pendingCloudflareHtml}<section class="assignment-current-assets-wrap${visibleAssignedEntries.length>1?' has-nav':''}">${visibleAssignedEntries.length>1?`<button type="button" class="assignment-current-nav" data-action="assignment-prev" aria-label="Previous assigned asset">\u25c0</button>`:''}<section class="assignment-current-assets">${assignedHtml}</section>${visibleAssignedEntries.length>1?`<button type="button" class="assignment-current-nav" data-action="assignment-next" aria-label="Next assigned asset">\u25b6</button>`:''}</section><div class="assignment-inline-search${animationSearchState?' assignment-animation-results-mode':''}"><div class="assignment-results">${lowerResultsHtml}</div></div></main></div>`;
        // Hydrate the complete Assign workspace in one generation.  Starting a
        // separate hydration pass for Current Assigned Assets and then Search
        // Results cancels the first pass because HydrateStagedImages owns a
        // single generation/queue.  One shared pass lets assigned thumbnails
        // and result thumbnails stage progressively without invalidating each
        // other.
        this.HydrateStagedImages(el.results,{eager:true});
        // Assigned assets are a simple horizontal queue: every card stays in
        // the rail, nothing is hidden/suppressed based on visibility, and the
        // arrows advance exactly one card at a time.  Queue missing 3D visuals
        // in DOM order so every assigned asset eventually gets rendered.
        const assignedRail=el.results.querySelector('.assignment-current-assets');
        if(assignedRail){
            assignedRail.addEventListener('wheel',event=>{
                if(Math.abs(event.deltaY)>Math.abs(event.deltaX)){
                    assignedRail.scrollLeft+=event.deltaY;
                    event.preventDefault();
                }
            },{passive:false});
            const cards=Array.from(assignedRail.querySelectorAll('.assignment-current-card:not(.assignment-missing-card)'));
            if(cards.length){
                (async()=>{
                    for(const card of cards) await this.EnsureAssignedCardVisual(card);
                })().catch(error=>console.warn('[IFPlayer Assets] assigned visual queue failed',String(error?.message||error)));
            }
        }
        Array.from(el.results.querySelectorAll('.assignment-current-card .saved-image-source')).forEach(image=>{
            const card=image.closest('.assignment-current-card');
            if (!card) {
                return;
            }
            const update=()=>this.ImageMinecraftDimensions(card).catch(()=>{});
            if (image.complete&&image.naturalWidth) {
                update();
            }
            else image.addEventListener('load',update,{once:true});
        });
        this.BindAssignmentBrowser(el,games,displayName);
        CIFAssetAssignment.HydrateAssignmentMediaThumbnails.call(this, el.results);
        const selectedAssignedUCI=String(this.assignmentSelection?.assetUCI||'');
        if(selectedAssignedUCI){
            const selectedCard=Array.from(el.results.querySelectorAll('.assignment-current-card')).find(card=>String(card.dataset.assignmentUci||'')===selectedAssignedUCI);
            if(selectedCard){
                requestAnimationFrame(()=>{if(assignedRail){const left=Math.max(0,selectedCard.offsetLeft-(assignedRail.clientWidth-selectedCard.offsetWidth)/2);
                    assignedRail.scrollTo({left,behavior:'smooth'});
                    }});
            }
        }
        el.results.querySelectorAll('[data-assignment-action]').forEach(btn=>btn.addEventListener('click',event=>{event.preventDefault();
            event.stopPropagation();
            this.HandleAssignmentAction(btn);
            }));
        el.results.querySelectorAll('.assignment-current-card').forEach(card=>card.addEventListener('dblclick',async event=>{
            if (event.target.closest('button,a,select,input')) {
                return;
            }
            event.preventDefault();
            event.stopPropagation();
            if(!card.classList.contains('viewer-active')){
                try{
                    if (card.dataset.directSavedPath) {
                        await this.LoadDirectAssignmentPreview(card);
                    }
                    else await this.LoadSavedPreview(card);
                }catch(error){this.SetProgress(card,error.message||error);
                return;
                }
            }
            if (card.classList.contains('blockified-preview-active')) {
                this.ToggleAssignmentMinecraftFullView(card);
            }
                else this.ToggleAssetFullView(card);
        }));
        el.results.querySelectorAll('.assignment-result').forEach(card=>card.addEventListener('dblclick',event=>{
            if (event.target.closest('button,a,select,input')) {
                return;
            }
            const opening=!card.classList.contains('assignment-inspect-open');
            el.results.querySelectorAll('.assignment-result.assignment-inspect-open').forEach(other=>{if(other!==card)other.classList.remove('assignment-inspect-open');
                });
            card.classList.toggle('assignment-inspect-open',opening);
            if(opening){
                const preview=card.querySelector('[data-action="preview"],[data-action="character-preview"],[data-action="animation-preview"],[data-action="saved-preview"],[data-action="assignment-image-preview"]');
                if (preview && !card.classList.contains('viewer-active')) {
                    preview.click();
                }
                card.scrollIntoView({block:'nearest',inline:'nearest'});
            }
        }));
        el.results.querySelectorAll('.asset-animation-search-form').forEach(form=>form.addEventListener('submit',event=>{event.preventDefault();
            event.stopPropagation();
            const card=form.closest('.assignment-current-card');
            this.SearchManagedAnimations(card,String(form.querySelector('input')?.value||'')).catch(error=>this.SetProgress(card,error.message||error));
            }));
        el.results.querySelectorAll('.asset-animation-manage-play').forEach(button=>button.addEventListener('click',event=>{event.preventDefault();
            event.stopPropagation();
            const card=button.closest('.assignment-current-card');
            this.PlayManagedAnimation(card,String(button.dataset.animationName||''),button).catch(error=>this.SetProgress(card,error.message||error));
            }));
        el.results.querySelectorAll('.asset-animation-embedded-toggle').forEach(button=>button.addEventListener('click',async event=>{event.preventDefault();
            event.stopPropagation();
            const card=button.closest('.assignment-current-card'),folder=this.SavedFolderForCard(card);
            if (!folder) {
                return;
            }
            button.disabled=true;
            try{await window.CIFRuntime.assetCommand({operation:'saved-embedded-animation-enabled',folder:folder.folder,animation_name:String(button.dataset.animationName||''),enabled:String(button.dataset.enabled||'')==='1'});
            await this.RefreshAssignmentSavedLibrary();
            }catch(error){button.disabled=false;
            this.SetProgress(card,error.message||error);
            }}));
        el.results.querySelectorAll('.asset-animation-remove').forEach(button=>button.addEventListener('click',async event=>{event.preventDefault();
            event.stopPropagation();
            const card=button.closest('.assignment-current-card'),folder=this.SavedFolderForCard(card);
            if (!folder) {
                return;
            }
            button.disabled=true;
            try{await window.CIFRuntime.assetCommand({operation:'saved-remove-animation',folder:folder.folder,animation_folder:String(button.dataset.animationFolder||''),animation_file:String(button.dataset.animationFile||'')});
            await this.RefreshAssignmentSavedLibrary();
            }catch(error){button.disabled=false;
            this.SetProgress(card,error.message||error);
            }}));
        const animationOwnerCard=button=>{const direct=button.closest('.assignment-current-card');
            if (direct) {
                return direct;
            }
            const folder=String(button.closest('.asset-animation-search-card')?.dataset.animationFolder||'');
            return Array.from(el.results.querySelectorAll('.assignment-current-card')).find(card=>String(card.dataset.folder||'')===folder)||null;
            };
        el.results.querySelectorAll('.asset-animation-result-add').forEach(button=>button.addEventListener('click',event=>{event.preventDefault();
            event.stopPropagation();
            const card=animationOwnerCard(button);
            if (!card) {
                return;
            }
            button.disabled=true;
            this.AddManagedAnimation(card,String(button.dataset.animationId||'')).catch(error=>{button.disabled=false;
            this.SetProgress(card,error.message||error);
            });
            }));
        el.results.querySelectorAll('.asset-animation-result-preview').forEach(button=>button.addEventListener('click',event=>{event.preventDefault();
            event.stopPropagation();
            const card=animationOwnerCard(button);
            if (!card) {
                return;
            }
            this.PreviewManagedAnimationResult(card,String(button.dataset.animationId||'')).catch(error=>this.SetProgress(card,error.message||error));
            }));
        el.results.querySelectorAll('[data-assignment-page]').forEach(button=>button.addEventListener('click',()=>{
            const state=this.assignmentSearch.get(key);
             if (!state) {
                 return;
             }
            const delta=button.dataset.assignmentPage==='prev'?-this.searchPageSize:this.searchPageSize;
            this.SearchAssignmentInline(key,state.query,state.source,Math.max(0,(Number(state.offset)||0)+delta)).catch(error=>this.SetProgress(button.closest('.assignment-node'),error.message||error));
        }));
        el.results.querySelectorAll('[data-assignment-search-form]').forEach(form=>{form.querySelectorAll('[data-assignment-source]').forEach(mode=>mode.addEventListener('click',()=>{const value=String(mode.dataset.assignmentSource||'objaverse');
            this.assignmentSource=value;
            form.dataset.source=value;
            form.querySelectorAll('[data-assignment-source]').forEach(b=>b.classList.toggle('active',b===mode));
            this.SaveAssignmentUiState();
            }));
            form.addEventListener('click',event=>event.stopPropagation());
            form.addEventListener('submit',event=>{event.preventDefault();
            event.stopPropagation();
            const card=form.closest('.assignment-node'),formKey=String(card?.dataset.key||'');
            this.SearchAssignmentInline(formKey,String(form.querySelector('input')?.value||'').trim(),String(form.dataset.source||this.assignmentSource||'objaverse')).catch(error=>this.SetProgress(form.closest('.assignment-node'),error.message||error));
            });
            });
    }

    static async HandleAssignmentAction(button){
        const card=button.closest('.assignment-node');
         if (!card) {
             return;
         }
        const action=String(button.dataset.assignmentAction||''), key=String(card.dataset.key||'');
        if(action==='toggle-search'){
            if (this.assignmentSearch.has(key)) {
                this.assignmentSearch.delete(key);
            }
                else {const q=String(card.querySelector('header strong')?.textContent||'').trim();
                this.assignmentSearch.set(key,{query:q,source:'saved',results:this.AssignmentSearchRows(q)});
                }
            this.RenderAssignments();
             return;
        }
        let uci=String(button.dataset.uci||''), kind=String(button.dataset.kind||'asset');
        let acquiredLocalAsset=false;
        const originalButtonText=button.textContent;
        button.disabled=true;
        try{
            if (action.startsWith('add-external-')) {
                const acquiredPath=await this.SaveExternalAssignment(button);
                acquiredLocalAsset=true;
                // The download has just created a canonical local Assets entry.
                // Refresh the local saved-asset catalog before the assignment is
                // rendered so the new WorldData reference resolves locally.
                // Cloudflare is not authoritative here until Push/Build.
                await this.RefreshAssignmentSavedLibrary();
                // Refreshing the catalog can re-render the workspace, but it must not
                // change the session-local availability of the asset we just acquired.
                // Reassert that fact at the assignment boundary so rendering never
                // falls through to the Cloudflare/public URL branch.
                window.CIFAsset.MarkLocalReady(acquiredPath);
                const acquired=this.AssignmentSavedRows().find(row=>String(row.path||'')===String(acquiredPath));
                uci=String(acquired?.uci||'');
                if(!uci) throw new Error(`Acquired asset has no UCI identity: ${acquiredPath}`);
            }
            const assignment = {
                uci,
                kind,
                label: String(button.closest('.assignment-result')?.querySelector('strong')?.textContent || ''),
                primary: action === 'add-default' || action === 'add-external-default'
            };
            console.info('[IFPlayer Assets] assignment applying', {
                alias: card.dataset.alias,
                storyId: card.dataset.storyId || '',
                nodeKind: card.dataset.nodeKind,
                nodeId: card.dataset.nodeId,
                uci,
                kind
            });
            const record = await window.CIFWorldStore.updateAssetAssignment(
                card.dataset.alias,
                card.dataset.nodeKind,
                card.dataset.nodeId,
                assignment,
                action === 'remove'
            );

            const game = this.assignmentGames.find(row => String(row.alias) === String(card.dataset.alias));
            const rows = card.dataset.nodeKind === 'objects'
                ? (game?.objects || [])
                : card.dataset.nodeKind === 'rooms'
                    ? (game?.rooms || [])
                    : (game?.characters || []);
            const node = rows.find(row => String(row.id) === String(card.dataset.nodeId));
            if (node) {
                Object.keys(node).forEach(name => delete node[name]);
                Object.assign(node, record);
            }
            this.pendingCloudflareAsset = null;
            if (acquiredLocalAsset) {
                window.CIFAsset.MarkLocalReady(acquiredPath);
                const acquired=this.AssignmentSavedRows().find(row=>String(row.path||'')===String(acquiredPath));
                uci=String(acquired?.uci||'');
                if(!uci) throw new Error(`Acquired asset has no UCI identity: ${acquiredPath}`);
            }
            console.info('[IFPlayer Assets] assignment applied', {alias: card.dataset.alias, nodeKind: card.dataset.nodeKind, nodeId: card.dataset.nodeId, uci});
            button.textContent='Assigned';
            this.SetProgress(card,'Assigned.');
            this.RenderAssignments();
        }catch(error){
            console.error('[IFPlayer Assets] assignment failed', {alias: card?.dataset?.alias || '', nodeKind: card?.dataset?.nodeKind || '', nodeId: card?.dataset?.nodeId || '', error: String(error?.stack || error?.message || error)});
            this.SetProgress(card,error.message||error);
            button.textContent=String(button.textContent||'').startsWith('Download failed')?'Download failed':originalButtonText;
            button.disabled=false;
        }
    }
}

window.CIFAssetAssignment = CIFAssetAssignment;

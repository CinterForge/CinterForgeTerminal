/**
 * CIFAssetMixamo ownership extracted from the legacy Asset Library controller.
 *
 * Methods intentionally execute with the controller as `this` while the refactor
 * proceeds, preserving current state without duplicating provider/preview state.
 */
class CIFAssetMixamo {
    static VERSION = 'refactor-v3';

    static AnimationCategory(row) {
        const text=`${row?.title||''} ${row?.description||''} ${row?.text||''} ${row?.file||''}`.toLowerCase();
        const has=(pattern)=>pattern.test(text);
        if (has(/\b(idle|pose|stand|standing|breath|look around|waiting|sit|sitting|kneel|crouch)\b/)) {
            return 'idle';
        }
        if (has(/\b(walk|walking|run|running|jog|jogging|sprint|jump|jumping|crawl|crawling|climb|climbing|strafe|turn|turning|swim|swimming|fly|flying|fall|falling)\b/)) {
            return 'locomotion';
        }
        if (has(/\b(punch|kick|fight|fighting|attack|combat|sword|rifle|gun|shoot|shooting|slash|stab|hit|block|dodge|death|dying|die)\b/)) {
            return 'combat';
        }
        if (has(/\b(pick|pickup|grab|carry|push|pull|open|close|throw|catch|drink|eat|use|work|working|typing|phone|talk|talking|wave|waving|point|pointing|gesture|clap|clapping)\b/)) {
            return 'actions';
        }
        if (has(/\b(dance|dancing|samba|salsa|hip hop|breakdance|rumba|macarena)\b/)) {
            return 'dance';
        }
        if (has(/\b(baseball|basketball|football|soccer|golf|tennis|boxing|sport|skate|skating|ski|skiing)\b/)) {
            return 'sports';
        }
        return 'other';
    }

    static AnimationCategories() {
        return [
            {id:'all',label:this.text('asset_animation_category_all','All')},
            {id:'idle',label:this.text('asset_animation_category_idle','Idle & Poses')},
            {id:'locomotion',label:this.text('asset_animation_category_locomotion','Locomotion')},
            {id:'combat',label:this.text('asset_animation_category_combat','Combat')},
            {id:'actions',label:this.text('asset_animation_category_actions','Actions & Gestures')},
            {id:'dance',label:this.text('asset_animation_category_dance','Dance')},
            {id:'sports',label:this.text('asset_animation_category_sports','Sports')},
            {id:'other',label:this.text('asset_animation_category_other','Other')}
        ];
    }

    static FilterAnimationsByCategory(rows) {
        const category=String(this.animationCategory||'all');
        return category==='all' ? rows : rows.filter(row=>this.AnimationCategory(row)===category);
    }

    static PopulateAnimationPicker() {
        const el=this.elements();
         if (!el.animationPicker) {
             return;
         }
        const rows=this.allMixamoAnimations.length?this.allMixamoAnimations:this.mixamoAnimations;
        const selected=String(this.selectedDefaultAnimationId||el.animationPicker.value||'');
        const category=String(this.animationCategory||'all');
        const filtered=category==='all'?rows:rows.filter(row=>this.AnimationCategory(row)===category);
        el.animationPicker.innerHTML='<option value="">'+this.escapeHtml(this.text('asset_default_animation_none','Default animation\u2026'))+'</option>'+filtered.map(row=>`<option value="${this.escapeHtml(row.id||'')}">${this.escapeHtml(row.title||row.file||row.id||'Animation')}</option>`).join('');
        if (selected && filtered.some(row=>String(row.id||'')===selected)) {
            el.animationPicker.value=selected;
        }
    }

    static RenderAnimationCategoryTabs() {
        const el=this.elements();
         if (!el.animationCategoryTabs) {
             return;
         }
        const rows=this.mixamoAnimations||[];
        el.animationCategoryTabs.innerHTML=this.AnimationCategories().map(category=>{
            const count=category.id==='all'?rows.length:rows.filter(row=>this.AnimationCategory(row)===category.id).length;
            const active=category.id===this.animationCategory?' active':'';
            return `<button class="asset-saved-tab${active}" data-animation-category="${this.escapeHtml(category.id)}" type="button">${this.escapeHtml(category.label)} <span class="asset-category-count">${count.toLocaleString()}</span></button>`;
        }).join('');
    }

    static DisposeCharacterPreviews() {
        try{this.characterObserver?.disconnect?.();
        } catch (_) {
        }
        this.characterObserver=null;
        try{this.characterThumbnailObserver?.disconnect?.();
        } catch (_) {
        }
        this.characterThumbnailObserver=null;
        if(this.characterThumbnailKickoff!==null){
            try{ if(typeof cancelIdleCallback==='function') cancelIdleCallback(this.characterThumbnailKickoff);
                else clearTimeout(this.characterThumbnailKickoff);
                } catch (_) {
                }
        }
        this.characterThumbnailKickoff=null;
        this.characterThumbnailQueue=[];
        this.characterThumbnailActive=0;
        this.DisposeCharacterThumbnailRenderer();
        for(const key of Array.from(this.engines.keys())){
            if (!String(key).startsWith('character:')) {
                continue;
            }
            const engine=this.engines.get(key), scene=this.scenes.get(key);
            try{engine?.stopRenderLoop?.();
            } catch (_) {
            }
            try{scene?.dispose?.();
            } catch (_) {
            }
            try{engine?.dispose?.();
            } catch (_) {
            }
            this.engines.delete(key);
             this.scenes.delete(key);
            if (this.activePreviewKey===key) {
                this.activePreviewKey=null;
            }
        }
    }

    static CharacterAnimationRows(characterId) {
        const id=String(characterId||'');
        const known=new Map((this.allMixamoAnimations.length?this.allMixamoAnimations:this.mixamoAnimations).map(row=>[String(row.id||''),row]));
        let ids=this.mixamoCharacterAnimations.get(id);
        if(!ids){
            const idle=String(this.selectedDefaultAnimationId||'');
            ids=idle?[idle]:[];
            this.mixamoCharacterAnimations.set(id,ids.slice());
        }
        return ids.map(animationId=>known.get(String(animationId))).filter(Boolean);
    }

    static RenderCharacters(rows) {
        const el=this.elements();
         if (!el.results) {
             return;
         }
        this.DisposeCharacterPreviews();
        if(!rows.length){el.results.innerHTML=`<div class="objaverse-empty">${this.escapeHtml(this.text('asset_characters_empty','No matching characters.'))}</div>`;
            return;
            }
        el.results.innerHTML=rows.map((row,index)=>{
            const rawId=String(row.id||''), id=this.escapeHtml(rawId), title=this.escapeHtml(row.name||row.title||'(unnamed)'), file=this.escapeHtml(row.file||''), license=this.escapeHtml(row.license||'Mixamo');
            const cachedThumb=row.has_thumbnail ? `<img class="objaverse-thumb" loading="lazy" data-staged-resource="character-thumb" data-staged-id="${id}" alt="${title}">` : `<div class="asset-character-glyph">…</div>`;
            const animations=this.CharacterAnimationRows(rawId), selectedId=String(animations[0]?.id||'');
            const animationOptions=animations.length?animations.map(animation=>`<option value="${this.escapeHtml(animation.id||'')}">${this.escapeHtml(animation.title||animation.file||animation.id||'Animation')}</option>`).join(''):'<option value="">Idle unavailable</option>';
            const query=this.escapeHtml(this.mixamoCharacterAnimationQueries.get(rawId)||'');
            const searchResults=this.mixamoCharacterAnimationResults.get(rawId)||[];
            const resultHtml=searchResults.length?searchResults.map(animation=>`<div class="mixamo-animation-result" data-animation-id="${this.escapeHtml(animation.id||'')}"><span>${this.escapeHtml(animation.title||animation.file||animation.id||'Animation')}</span><div class="objaverse-actions"><button class="objaverse-action" type="button" data-action="character-animation-preview" data-animation-id="${this.escapeHtml(animation.id||'')}">Preview</button><button class="objaverse-action" type="button" data-action="character-animation-add" data-animation-id="${this.escapeHtml(animation.id||'')}">Add</button></div></div>`).join(''):'';
            return `<article class="objaverse-card asset-character-card" data-asset-kind="3d" data-asset-id="${id}">
              <div class="objaverse-visual">${cachedThumb}<canvas class="objaverse-canvas asset-character-canvas" aria-label="Character preview for ${title}"></canvas><span class="objaverse-rank">#${index+1}</span><span class="objaverse-license">${license}</span></div>
              <div class="objaverse-card-body"><h2 class="objaverse-title">${title}</h2><div class="objaverse-byline">${this.escapeHtml(this.text('asset_mixamo_character_byline','Mixamo \u00b7 character'))}</div><div class="objaverse-stats">${this.fmt(row.bone_count)} bones \u00b7 ${file}</div>
              <div class="mixamo-character-animation-panel"><label>Animations<select class="mixamo-character-animation-picker">${animationOptions}</select></label><form class="mixamo-character-animation-search"><input type="search" value="${query}" placeholder="Search Mixamo animations\u2026"><button class="objaverse-action" type="submit">Search</button></form><div class="mixamo-character-animation-results">${resultHtml}</div></div>
              <div class="objaverse-actions"><button class="objaverse-action" data-action="character-edit" title="Edit this character in RPG Character Editor">Edit</button><button class="objaverse-action" data-action="character-preview">GLB Preview</button><button class="objaverse-action" data-action="character-minecraft-preview">Blockify</button><button class="objaverse-action" data-action="character-export">Export</button><button class="objaverse-action" data-action="character-remove">Remove</button></div></div></article>`;
        }).join('');
        el.results.querySelectorAll('.mixamo-character-animation-search').forEach(form=>form.addEventListener('submit',event=>{event.preventDefault();
            const card=form.closest('.asset-character-card');
            this.SearchCharacterAnimations(card,String(form.querySelector('input')?.value||'').trim()).catch(error=>this.SetProgress(card,error.message||error));
            }));
        this.HydrateStagedImages();
        this.SetupCharacterThumbnailGeneration();
    }

    static async SearchCharacterAnimations(card,query) {
        const characterId=String(card?.dataset?.assetId||'');
         if (!characterId) {
             return;
         }
        this.mixamoCharacterAnimationQueries.set(characterId,String(query||''));
        const payload=await CIFRuntime.assetCommand({operation:'search',provider:'library',kind:'animation',q:String(query||''),limit:48});
        this.mixamoCharacterAnimationResults.set(characterId,Array.isArray(payload.results)?payload.results:[]);
        this.RenderCharacters(this.mixamoCharacters);
    }

    static AddCharacterAnimation(card,animationId) {
        const characterId=String(card?.dataset?.assetId||''), id=String(animationId||'');
         if (!characterId||!id) {
             return;
         }
        const current=this.CharacterAnimationRows(characterId).map(row=>String(row.id||''));
        if (!current.includes(id)) {
            current.push(id);
        }
        this.mixamoCharacterAnimations.set(characterId,current);
        this.RenderCharacters(this.mixamoCharacters);
    }

    static SetupSavedCharacterPreviews() {
        try{this.savedCharacterObserver?.disconnect?.();
        } catch (_) {
        }
        if (!(this.mode==='saved'&&this.savedCategory==='Animated'&&this.savedAnimated==='Mixamo')) {
            return;
        }
        const cards=Array.from(this.elements().results?.querySelectorAll?.('.saved-asset-card')||[]);
            if (!cards.length) {
                return;
            }
        if(typeof IntersectionObserver!=='function'){cards.slice(0,8).forEach(card=>this.LoadSavedPreview(card).catch(error=>this.SetProgress(card,error.message||error)));
            return;
            }
        this.savedCharacterObserver=new IntersectionObserver(entries=>entries.forEach(entry=>{if(!entry.isIntersecting)return;
            this.savedCharacterObserver.unobserve(entry.target);
            this.LoadSavedPreview(entry.target).catch(error=>this.SetProgress(entry.target,error.message||error));
            }),{root:this.elements().results,rootMargin:'160px'});
        cards.forEach(card=>this.savedCharacterObserver.observe(card));
    }

    static RenderAnimations(rows) {
        const el = this.elements();
         if (!el.results) {
             return;
         }
        el.results.classList.remove('asset-fullview-active');
        ifGetElementById('if_root')?.classList.remove('if_asset-preview-fullview');
        if (!rows.length) { el.results.innerHTML = `<div class="objaverse-empty">${this.escapeHtml(this.text('asset_animations_empty','No matching animations.'))}</div>`;
            return;
            }
        el.results.innerHTML = rows.map((row,index) => {
            const id=this.escapeHtml(row.id||''), title=this.escapeHtml(row.title||'(untitled)'), description=this.escapeHtml(row.description||''), file=this.escapeHtml(row.file||'');
            return `<article class="objaverse-card asset-animation-card" data-asset-kind="animation" data-asset-id="${id}">
              <div class="objaverse-visual"><div class="asset-animation-glyph">\u25c6</div><canvas class="objaverse-canvas asset-animation-canvas" aria-label="Animation preview for ${title}"></canvas><span class="objaverse-rank">#${index+1}</span><span class="objaverse-license">Mixamo</span></div>
              <div class="objaverse-card-body"><h2 class="objaverse-title">${title}</h2><div class="objaverse-description">${description}</div><div class="objaverse-stats">${this.fmt(row.frames)} frames \u00b7 ${file}</div>
              <div class="objaverse-actions"><button class="objaverse-action" data-action="animation-preview">${this.escapeHtml(this.text('asset_animation_preview','Preview'))}</button><button class="objaverse-action" data-action="animation-download-rpg">${this.escapeHtml(this.text('asset_animation_download_rpg','GLB'))}</button><button class="objaverse-action" data-action="asset-download">${this.escapeHtml(this.text('asset_animation_download_fbx','FBX'))}</button>${row.source_url?`<a class="objaverse-action" href="${this.escapeHtml(row.source_url)}" target="_blank" rel="noopener">${this.escapeHtml(this.text('objaverse_source','Source'))}</a>`:''}</div></div></article>`;
        }).join('');
    }

    static async EditMixamoCharacter(card) {
        const characterId=String(card?.dataset?.assetId||'').trim();
        if (!characterId) {
            throw new Error('Mixamo character id is missing.');
        }
        const row=this.mixamoCharacters.find(candidate=>String(candidate.id||'')===characterId)||{};
        const characterLabel=String(row.name||row.title||characterId);
        this.selectedCharacterId=characterId;
        const picker=this.elements().characterPicker;
        if (picker && [...picker.options].some(option=>String(option.value||'')===characterId)) {
            picker.value=characterId;
        }
        this.SuspendRendering();
        if (typeof setStageMode==='function') {
            await setStageMode('rpg');
        }
        else if(window.CIFMapRuntime?.setStageMode) await CIFMapRuntime.setStageMode('rpg');
        else throw new Error('RPG mode is unavailable.');
        if (!window.CIFPlayerRPG) {
            throw new Error('RPG Character Editor is unavailable.');
        }
        await CIFPlayerRPG.SetMode?.('build');
        return await CIFPlayerRPG.OpenCharacterEditor?.({characterId,characterLabel});
    }

    static async SaveMixamoCharacter(card) {
        const characterId=String(card.dataset.assetId||'');
            if (!characterId) {
                throw new Error(this.text('asset_character_id_missing','Mixamo character id is missing.'));
            }
        const defaultAnimationId=String(card.querySelector('.mixamo-character-animation-picker')?.value||this.selectedDefaultAnimationId||this.elements().animationPicker?.value||'');
        const assignedRows=this.CharacterAnimationRows(characterId);
        const known=new Map((this.allMixamoAnimations.length?this.allMixamoAnimations:this.mixamoAnimations).map(row=>[String(row.id||''),row]));
        if (defaultAnimationId && known.has(defaultAnimationId) && !assignedRows.some(row=>String(row.id||'')===defaultAnimationId)) {
            assignedRows.unshift(known.get(defaultAnimationId));
        }
        this.SetProgress(card,this.text('asset_character_saving','Saving Mixamo character\u2026'));
        const animationStates=[];
        for(const animation of assignedRows){
            const animationId=String(animation?.id||'');
             if (!animationId) {
                 continue;
             }
            const animationQuery=String(animation.title||animation.file||animation.id||'animation');
            this.SetProgress(card,`Saving animation ${animationQuery}\u2026`);
            const state=await this.EnsureManagedAsset('animation',animationId,animationQuery,card);
            if(state?.relative_path) animationStates.push({row:animation,state});
        }
        const characterRow=this.mixamoCharacters.find(row=>String(row.id||'')===characterId);
        const characterQuery=String(characterRow?.name||characterRow?.title||this.mixamoQueries.character||this.query||'character');
        const characterState=await this.EnsureManagedAsset('3d',characterId,characterQuery,card);
        if(characterState?.relative_path){
            const split=path=>{const parts=String(path||'').split('/');
                return {file:parts.pop()||'',folder:parts.join('/')};
                };
            const c=split(characterState.relative_path);
            for(const entry of animationStates){
                const a=split(entry.state.relative_path);
                await CIFRuntime.assetCommand({operation:'saved-assign-animation',folder:c.folder,animation_folder:a.folder,animation_file:a.file,name:String(entry.row.title||entry.row.file||entry.row.id||'Animation')});
                if(String(entry.row.id||'')===defaultAnimationId) await this.SavedPost('/assets/saved/default-animation',{folder:c.folder,animation_folder:a.folder,animation_file:a.file});
            }
        }
        this.SetProgress(card,this.text('asset_character_saved','Character saved to CinterForge Cloud Assets.'));
        return {characterState,animationState:animationStates.find(entry=>String(entry.row?.id||'')===defaultAnimationId)?.state||animationStates[0]?.state||null,animationStates};
    }

    static async RunMixamoCharacterSavedAction(card,action) {
        const saved=await this.SaveMixamoCharacter(card), rel=String(saved?.characterState?.relative_path||'').replace(/\\/g,'/');
        if (!rel) {
            throw new Error('Mixamo character did not return a saved asset path.');
        }
        this.savedLibrary=await CIFRuntime.assetCommand({operation:'saved-list'});
        const parts=rel.split('/');
         parts.pop();
         const folderPath=parts.join('/');
        const folder=(this.savedLibrary?.groups?.Animated?.Mixamo||[]).find(row=>String(row.folder||'')===folderPath);
        if (!folder) {
            throw new Error(`Saved Mixamo folder was not found: ${folderPath}`);
        }
        const versionIndex=Math.max(0,(folder.versions||[]).findIndex(version=>rel.endsWith('/'+String(version.file||''))));
        card.dataset.folder=folderPath;
            card.dataset.versionIndex=String(versionIndex);
            card.classList.add('saved-asset-card');
        const canvas=card.querySelector('.asset-character-canvas');
            if (canvas) {
                canvas.classList.add('saved-asset-canvas');
            }
        if (!card.querySelector('.asset-blockify-dimension')) {
            card.insertAdjacentHTML('beforeend','<div hidden><input class="asset-blockify-dimension" data-axis="width" value="1"><input class="asset-blockify-dimension" data-axis="height" value="2"><input class="asset-blockify-dimension" data-axis="depth" value="1"></div>');
        }
        await this.EnsureBlockified(card);
        if (action==='export') {
            await this.ExportBlockifiedAddon(card);
        }
         else await this.LoadBlockifiedPreview(card);
    }

    static async RemoveMixamoCharacter(card) {
        const characterId=String(card?.dataset?.assetId||'');
        this.savedLibrary=await CIFRuntime.assetCommand({operation:'saved-list'});
        const folders=this.savedLibrary?.groups?.Animated?.Mixamo||[];
        const folder=folders.find(row=>(row.versions||[]).some(version=>String(version.asset_id||version.metadata?.asset_id||'')===characterId)||String(row.name||'').toLowerCase()===String(this.mixamoCharacters.find(r=>String(r.id||'')===characterId)?.name||'').toLowerCase());
        if(!folder){this.SetProgress(card,'Character is not saved locally.');
        return;
        }
        for(const version of [...(folder.versions||[])]) await this.SavedPost('/assets/saved/delete',{folder:folder.folder,file:version.file});
        this.savedLibrary=await CIFRuntime.assetCommand({operation:'saved-list'});
        this.SetProgress(card,'Removed local Mixamo character.');
    }

}

window.CIFAssetMixamo = CIFAssetMixamo;
console.info('[CIF Asset] CIFAssetMixamo loaded', CIFAssetMixamo.VERSION);

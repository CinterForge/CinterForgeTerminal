/*
 * CINTERFORGE MODULE PURPOSE
 * CinterForge Asset Browser controller: CIFAssetBrowser
 *
 * Purpose: implements one focused browser-side IFPlayer presentation/runtime concern.
 * Ownership boundary: IFPlayer presentation coordinates UI and runtime services; the
 * Open World Builder remains a supplied RPG/gameplay subsystem and CIFPlayerInput
 * remains the sole owner of physical browser input.
 *
 * Maintenance rule: preserve current behavior while refactoring. Prefer descriptive
 * CIFPlayer/CIPlayer class names, explicit ownership, and compatibility shims over
 * broad globals or duplicated application state.
 */

//========= Copyright 2025+, CinterForge, Inc. All rights reserved. ============
//
// CIFAssetBrowser
// ---------------------------------------------------------------------------
// Owns the Asset browser presentation and coordinates Asset-owned providers,
// library, assignment, preview, thumbnail, download, editing, and Blockify services.
// Objaverse is one provider; this controller is not Player-owned.
// Search uses the
// app-bundled SQLite index through the existing IFPlayer loopback HTTP server;
// model bytes are fetched only after an explicit Preview/Download action.
//
//=============================================================================
class CIFAssetBrowser extends CIFAssetEditor {
    static initialized = false;
    static query = 'lantern';
    static mode = 'assign';
    static queries = {openclipart:'', heroicons:'', lucide:'', freesound:'', kenney:'', incompetech:'', objaverse:'lantern', pd12m:'lantern', mixamo:'spell', cloudflare:'', saved:'', assign:''};
    static assignmentGames = [];
    static assignmentGame = '';
    static assignmentSearch = new Map();
    static assignmentAnimationSearch = new Map();
    static assignmentAnimationSearchActiveFolder = '';
    static assignmentSelection = null;
    static assignmentSource = 'objaverse';
    static assignmentOpenKeys = new Set();
    static assignmentBrowserScrollTop = 0;
    static assignmentStateLoaded = false;
    static assignmentStateKey = 'cinterforge.ifplayer.assign.v1';
    static mixamoSearchKind = 'character';
    static mixamoQueries = {animation:'', character:''};
    static mixamoCharacters = [];
    static mixamoAnimations = [];
    static allMixamoAnimations = [];
    static animationCategory = 'locomotion';
    static selectedDefaultAnimationId = '';
    static mixamoCharacterAnimations = new Map();
    static mixamoCharacterAnimationResults = new Map();
    static mixamoCharacterAnimationQueries = new Map();
    static savedCategory = 'Images';
    static savedAnimated = 'Mixamo';
    static savedLibrary = null;
    static cloudflareTree = null;
    static pendingCloudflareAsset = null;
    static engines = new Map();
    static scenes = new Map();
    // Blockify migration reuses one WebGL engine/context for the whole run. Safari
    // can exhaust its WebGL context/texture pool when hundreds of short-lived
    // engines are created in sequence, causing a later valid texture allocation to
    // fail with "Unable to create texture". Scenes are still disposed per asset.
    static migrationEngine = null;
    static migrationCanvas = null;
    static pollers = new Map();
    static charactersLoaded = false;
    static selectedCharacterId = '';
    static animationPreviews = new Map();
    static activePreviewKey = null;
    static animationFullViewPointer = {canvas:null, at:0};
    static characterObserver = null;
    static characterThumbnailObserver = null;
    static characterThumbnailQueue = [];
    static characterThumbnailActive = 0;
    static characterThumbnailConcurrency = 1;
    static characterThumbnailKickoff = null;
    static characterThumbnailSerial = Promise.resolve();
    static characterThumbnailPending = new Map();
    static characterThumbnailRenderer = null;
    static characterThumbnailRendererDisposeTimer = null;
    static objaverseThumbnailSerial = Promise.resolve();
    static objaverseThumbnailPending = new Map();
    static objaverseThumbnailRenderer = null;
    static objaverseThumbnailRendererDisposeTimer = null;
    static savedThumbnailPending = new Map();
    static savedCharacterObserver = null;
    static embeddedAnimationControllers = new Map();
    static savedPreviewAssetLoads = new Map();
    static stagedImageObserver = null;
    static stagedImageGeneration = 0;
    static stagedImageQueue = [];
    static stagedImageActive = 0;
    static stagedImageConcurrency = 2;
    static searchPageSize = 50;
    static searchOffsets = {objaverse:0,pd12m:0,mixamo:0,openclipart:0,heroicons:0,lucide:0,freesound:0,kenney:0,incompetech:0};
    static searchTotals = {objaverse:0,pd12m:0,mixamo:0,openclipart:0,heroicons:0,lucide:0,freesound:0,kenney:0,incompetech:0};
    static assetSyncDirty = false;
    static assetSyncGeneration = 0;

    static elements() {
        return {
            root: ifGetElementById('objaverse-browser'),
            form: ifGetElementById('objaverse-search-form'),
            input: ifGetElementById('objaverse-search-input'),
            button: ifGetElementById('objaverse-search-button'),
            status: ifGetElementById('objaverse-status'),
            results: ifGetElementById('objaverse-results'),
            characterWrap: ifGetElementById('asset-character-picker-wrap'),
            characterPicker: ifGetElementById('asset-character-picker'),
            mixamoTabs: ifGetElementById('mixamo-search-tabs'),
            animationWrap: ifGetElementById('asset-animation-picker-wrap'),
            animationPicker: ifGetElementById('asset-animation-picker'),
            animationCategoryWrap: ifGetElementById('asset-animation-category-picker-wrap'),
            animationCategoryPicker: ifGetElementById('asset-animation-category-picker'),
            animationCategoryTabs: ifGetElementById('mixamo-animation-categories'),
            savedTabs: ifGetElementById('saved-assets-tabs'),
            savedAnimatedTabs: ifGetElementById('saved-animated-tabs'),
            secondaryRow: ifGetElementById('asset-head-row-secondary'),
            tertiaryRow: ifGetElementById('asset-head-row-tertiary'),
            searchRow: ifGetElementById('asset-head-row-search'),
            syncButton: ifGetElementById('asset-sync-button'),
            worldPicker: ifGetElementById('asset-world-picker'),
            packPicker: ifGetElementById('asset-pack-picker'),
            serverButton: ifGetElementById('asset-server-button'),
            blockifyButton: ifGetElementById('asset-blockify-button')
        };
    }

    static escapeHtml(value) {
        return String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    }

    static fmt(value) {
        const n = Number(value);
        return Number.isFinite(n) ? Math.trunc(n).toLocaleString() : '\u2014';
    }

    static text(key, fallback = '', values = {}) {
        let value = typeof uiString === 'function' ? uiString(key, fallback) : String(fallback || '');
        for (const [name, replacement] of Object.entries(values || {}))
            value = value.replaceAll(`{${name}}`, String(replacement));
        return value;
    }

    static async Initialize() {
        if (this.initialized) {
            return;
        }
        this.initialized = true;
        const el = this.elements();
        if (!el.root) {
            return;
        }
        const tab = ifGetElementById('objaverse-tab');
        const title = ifGetElementById('objaverse-title');
        if (tab) {
            tab.textContent = this.text('asset_library_tab', 'Assets \u00b7 F3');
        }
        if (title) {
            title.textContent = this.text('asset_library_title', 'Asset Library');
        }
        if (el.syncButton) {
            el.syncButton.textContent = this.text('asset_sync_button', 'Sync');
        }
        if (el.serverButton) {
            el.serverButton.textContent = this.text('asset_server_start_button', 'Start Server');
        }
        if (el.blockifyButton) {
            el.blockifyButton.textContent = this.text('asset_blockify_all_button', 'Blockify All');
        }
        if (el.worldPicker) {
            el.worldPicker.innerHTML='<option value="">Scanning worlds…</option>';
            el.worldPicker.disabled=true;
        }
        if (window.CINTERFORGE_IF_ASSET_WORKSPACE) {
            if (el.worldPicker) el.worldPicker.innerHTML='<option value="">No game selected</option>';
        } else await this.RefreshMinecraftWorlds().catch(error=>{
            console.error('[IFPlayer Assets] world-scan-failed',{error:String(error?.message||error)});
            if (el.worldPicker) {
                el.worldPicker.innerHTML='<option value="">World scan failed</option>';
                el.worldPicker.disabled=true;
            }
        });
        if (el.savedTabs) {
            const labels={Images:this.text('asset_saved_images','Images'),Objects:this.text('asset_saved_objects','Objects'),Animated:this.text('asset_saved_animated','Animated'),Unassigned:this.text('asset_saved_unassigned','Unassigned')};
            el.savedTabs.querySelectorAll('[data-saved-category]').forEach(button=>button.textContent=labels[button.dataset.savedCategory]||button.dataset.savedCategory);
        }
        if (el.savedAnimatedTabs) {
            const labels={Mixamo:this.text('asset_saved_mixamo','Mixamo'),Fixed:this.text('asset_saved_fixed','Fixed')};
            el.savedAnimatedTabs.querySelectorAll('[data-saved-animated]').forEach(button=>button.textContent=labels[button.dataset.savedAnimated]||button.dataset.savedAnimated);
        }
        if (el.mixamoTabs) {
            const labels={animation:this.text('asset_mixamo_animations','Animations'),character:this.text('asset_mixamo_characters','Characters')};
            el.mixamoTabs.querySelectorAll('[data-mixamo-kind]').forEach(button=>button.textContent=labels[button.dataset.mixamoKind]||button.dataset.mixamoKind);
        }
        if (el.button) {
            el.button.textContent = this.text('objaverse_search_button', 'Search');
        }
        const characterLabel = ifGetElementById('asset-character-picker-label');
        if (characterLabel) {
            characterLabel.textContent = this.text('asset_character_picker_label', 'Character');
        }
        const animationLabel = ifGetElementById('asset-animation-picker-label');
        if (animationLabel) {
            animationLabel.textContent = this.text('asset_animation_picker_label', 'Animation');
        }
        const animationCategoryLabel = ifGetElementById('asset-animation-category-picker-label');
        if (animationCategoryLabel) {
            animationCategoryLabel.textContent = this.text('asset_animation_category_label', 'Category');
        }
        if (el.characterPicker) el.characterPicker.addEventListener('change', () => { this.selectedCharacterId = String(el.characterPicker.value || '');
            });
        if (el.animationPicker) el.animationPicker.addEventListener('change', () => { this.selectedDefaultAnimationId = String(el.animationPicker.value || '');
            if (this.mode==='mixamo'&&this.mixamoSearchKind==='character') {
                this.RenderCharacters(this.mixamoCharacters);
            }
            });
        if (el.animationCategoryPicker) el.animationCategoryPicker.addEventListener('change', () => { this.animationCategory=String(el.animationCategoryPicker.value||'all');
            this.PopulateAnimationPicker();
            });
        el.animationCategoryTabs?.addEventListener('click', event => { const button=event.target?.closest?.('[data-animation-category]');
            if (!button) {
                return;
            }
            this.animationCategory=String(button.dataset.animationCategory||'all');
            this.RenderAnimationCategoryTabs();
            this.RenderAnimations(this.FilterAnimationsByCategory(this.mixamoAnimations));
            });
        el.mixamoTabs?.addEventListener('click', event => {
            const button=event.target?.closest?.('[data-mixamo-kind]');
             if (!button) {
                 return;
             }
            this.SetMixamoKind(String(button.dataset.mixamoKind||'animation'));
        });
        ifGetElementById('asset-search-tabs')?.addEventListener('click', event => {
            const button=event.target?.closest?.('[data-asset-kind]');
             if (!button) {
                 return;
             }
            this.SetMode(String(button.dataset.assetKind||'saved'));
        });
        el.syncButton?.addEventListener('click', () => this.SyncSavedAssets());
        window.addEventListener('cinterforge-asset-modified', event => {
            this.MarkAssetSyncDirty(event?.detail?.operation || 'asset modification');
        });
        this.RefreshAssetSyncButton();
        this.StartAutomaticSync();
        el.serverButton?.addEventListener('click', () => this.ToggleAssetTestServer());
        el.blockifyButton?.addEventListener('click', () => this.BlockifyVisibleAssets());
        this.RefreshAssetTestServerState().catch(error => console.warn('[IFPlayer Assets] server-state', error));
        el.savedTabs?.addEventListener('click', event => {
            const button=event.target?.closest?.('[data-saved-category]');
             if (!button) {
                 return;
             }
            this.savedCategory=String(button.dataset.savedCategory||'Images');
            if (this.savedCategory!=='Animated') {
                this.savedAnimated='Mixamo';
            }
            this.ApplyModeText();
             this.RenderSaved();
        });
        el.savedAnimatedTabs?.addEventListener('click', event => {
            const button=event.target?.closest?.('[data-saved-animated]');
             if (!button) {
                 return;
             }
            this.savedAnimated=String(button.dataset.savedAnimated||'Mixamo');
            this.RenderSaved();
             this.ApplyModeText();
        });
        if (window.CINTERFORGE_IF_LOCAL_WORLD_ACTIVE===true) {
            this.mode='saved';
        }
        this.ApplyModeText();
        el.form && el.form.addEventListener('submit', event => {
            event.preventDefault();
            this.Search(el.input ? el.input.value : this.query);
        });
        el.results && el.results.addEventListener('click', event => this.HandleResultClick(event));
        el.results && el.results.addEventListener('change', async event => {
            const embeddedPicker=event.target?.closest?.('.embedded-animation-picker');
            if(embeddedPicker){
                this.SelectEmbeddedAnimation(String(embeddedPicker.dataset.previewKey||''),String(embeddedPicker.value||''));
                return;
            }
            const mixamoPicker=event.target?.closest?.('.mixamo-character-animation-picker');
            if(mixamoPicker){
                const card=mixamoPicker.closest('.asset-character-card');
                if(card){
                    try{await this.LoadCharacterPreview(card,String(mixamoPicker.value||''),true);
                        }catch(error){this.SetProgress(card,error.message||error);
                        }
                }
                return;
            }
            const picker=event.target?.closest?.('.saved-animation-picker');
                if (!picker) {
                    return;
                }
                const card=picker.closest('.saved-asset-card');
                const [animation_folder,animation_file]=String(picker.value||'').split('|');
                if(card&&animation_folder&&animation_file){ try{await this.SavedPost('/assets/saved/default-animation',{folder:card.dataset.folder,animation_folder,animation_file});
                await this.LoadSaved();
                }catch(error){this.SetProgress(card,error.message||error);
                } }
        });
        // Full View must not depend on the browser synthesizing `dblclick`.
        // Babylon owns the preview canvas pointer stream and can prevent enough of
        // that stream that WebKit never emits a dblclick in the embedded app. Count
        // the two primary-button releases ourselves in capture phase, before
        // Babylon, and use dblclick only as a harmless fallback.
        el.results && el.results.addEventListener('pointerup', event => {
            if (event.pointerType && event.pointerType !== 'mouse') {
                return;
            }
            if (Number(event.button) !== 0) {
                return;
            }
            const canvas = event.target?.closest?.('.asset-animation-canvas');
            const card = canvas?.closest?.('.asset-animation-card');
            if (!canvas || !card || !card.classList.contains('viewer-active')) {
                this.animationFullViewPointer = {canvas:null, at:0};
                return;
            }
            const now = Number(event.timeStamp || performance.now());
            const previous = this.animationFullViewPointer;
            if (previous.canvas === canvas && now - previous.at > 0 && now - previous.at <= 450) {
                this.animationFullViewPointer = {canvas:null, at:0};
                event.preventDefault();
                event.stopImmediatePropagation();
                this.ToggleAnimationFullView(card);
                return;
            }
            this.animationFullViewPointer = {canvas, at:now};
        }, true);
        el.results && el.results.addEventListener('dblclick', event => {
            const canvas = event.target?.closest?.('.asset-animation-canvas');
            const card = canvas?.closest?.('.asset-animation-card');
            if (!canvas || !card || !card.classList.contains('viewer-active')) {
                return;
            }
            event.preventDefault();
            event.stopImmediatePropagation();
            if (!card.classList.contains('asset-fullview')) {
                this.ToggleAnimationFullView(card, true);
            }
        }, true);
        el.results && el.results.addEventListener('dblclick', event => {const canvas=event.target?.closest?.('.saved-asset-canvas');
            const card=canvas?.closest?.('.saved-asset-card');
            if (!canvas||!card||!card.classList.contains('blockified-preview-active')) {
                return;
            }
            event.preventDefault();
            event.stopImmediatePropagation();
            if (card.closest('.assignment-editor')) {
                this.ToggleAssignmentMinecraftFullView(card);
            }
            else this.ToggleSavedMinecraftFullView(card);
            },true);
        // Number editors own Tab.  Let the browser move focus W -> H -> D, but
        // do not allow IFPlayer's window-level Tab shortcut to see the keystroke and
        // hide/show the entire UI.  Do not preventDefault: native tab order is wanted.
        el.results && el.results.addEventListener('keydown', event => {
            if (event.key !== 'Tab') {
                return;
            }
            if (!event.target?.matches?.('.asset-blockify-dimension, .image-minecraft-height')) {
                return;
            }
            event.stopPropagation();
        });
        el.results && el.results.addEventListener('input', event => {
            if (!event.target?.matches?.('.image-minecraft-height')) {
                return;
            }
            const card=event.target.closest('.saved-asset-card');
            if (!card) {
                return;
            }
            this.ImageMinecraftDimensions(card).catch(error=>{const label=card.querySelector('.image-minecraft-derived-width');
                if (label) {
                    label.textContent='W !';
                }
                });
        });
        document.addEventListener('keydown', event => {
            if (event.key !== 'Escape') {
                return;
            }
            const active = ifQuerySelector('.asset-animation-card.asset-fullview');
            if (active) {
                this.ToggleAnimationFullView(active, false);
            }
            const assetActive = ifQuerySelector('.assignment-current-card.asset-inspect-fullview');
            if (assetActive) {
                this.ToggleAssetFullView(assetActive, false);
            }
            const minecraftActive = ifQuerySelector('.saved-asset-card.asset-fullview.blockified-preview-active');
            if (minecraftActive) {
                this.ToggleSavedMinecraftFullView(minecraftActive, false);
            }
        });
    }

    static ApplyModeText() {
        const el = this.elements();
        ifQuerySelectorAll('.asset-search-tab').forEach(button => button.classList.toggle('active', button.dataset.assetKind === this.mode));
        const labels = this.mode==='mixamo'
            ? (this.mixamoSearchKind==='character'
                ? ['asset_characters_status','Mixamo character index','asset_characters_placeholder','Search Mixamo characters\u2026']
                : ['asset_animations_status','Mixamo animation index','asset_animations_placeholder','Search Mixamo animations\u2026'])
            : ({
                objaverse: ['asset_3d_status','Objaverse 3D index','asset_3d_placeholder','Search Objaverse 3D assets\u2026'],
                heroicons: ['asset_svg_status','Heroicons SVG index','asset_svg_placeholder','Search Heroicons…'],
                lucide: ['asset_svg_status','Lucide SVG index','asset_svg_placeholder','Search Lucide…'],
                freesound: ['asset_sfx_status','Freesound live search','asset_sfx_placeholder','Search Freesound…'],
                openclipart: ['asset_svg_status','Openclipart SVG index','asset_svg_placeholder','Search SVG artwork…'],
                kenney: ['asset_sfx_status','Kenney sound effects','asset_sfx_placeholder','Search sound effects…'],
                incompetech: ['asset_music_status','Incompetech music','asset_music_placeholder','Search music…'],
                pd12m: ['asset_images_status','PD12M public-domain / CC0 image index','asset_images_placeholder','Search PD12M images\u2026'],
                cloudflare: ['asset_cloudflare_status','Cloudflare Assets','asset_cloudflare_placeholder','Cloudflare Assets'],
                saved: ['asset_saved_status','Saved Assets','asset_saved_placeholder','Saved Assets'],
                assign: ['asset_assign_status','Rooms & game objects','asset_assign_placeholder','Search assets\u2026']
            }[this.mode] || []);
        if (el.status) {
            el.status.textContent = this.text(labels[0] || 'asset_library_status', labels[1] || 'Local asset index');
        }
        if (el.input) { el.input.placeholder = this.text(labels[2] || 'asset_search_placeholder', labels[3] || 'Search assets\u2026');
            el.input.value = this.mode==='mixamo' ? (this.mixamoQueries[this.mixamoSearchKind]||'') : (this.queries[this.mode] || '');
            }
        if (el.mixamoTabs) {
            el.mixamoTabs.hidden = true;
        }
        el.mixamoTabs?.querySelectorAll('[data-mixamo-kind]').forEach(button=>button.classList.toggle('active',button.dataset.mixamoKind===this.mixamoSearchKind));
        if (el.characterWrap) {
            el.characterWrap.hidden = true;
        }
        if (el.animationWrap) {
            el.animationWrap.hidden = true;
        }
        if (el.animationCategoryWrap) {
            el.animationCategoryWrap.hidden = true;
        }
        if (el.animationCategoryTabs) {
            el.animationCategoryTabs.hidden = true;
        }
        if (el.form) {
            el.form.hidden = ['cloudflare','saved','assign'].includes(this.mode);
        }
        if (el.savedTabs) {
            el.savedTabs.hidden = this.mode !== 'saved';
        }
        if (el.savedAnimatedTabs) {
            el.savedAnimatedTabs.hidden = this.mode !== 'saved' || this.savedCategory !== 'Animated';
        }
        if (el.secondaryRow) {
            el.secondaryRow.hidden = this.mode !== 'saved';
        }
        if (el.tertiaryRow) {
            el.tertiaryRow.hidden = !(this.mode==='saved'&&this.savedCategory==='Animated');
        }
        if (el.searchRow) {
            el.searchRow.hidden = ['cloudflare','saved','assign'].includes(this.mode);
        }
        el.savedTabs?.querySelectorAll('[data-saved-category]').forEach(button=>button.classList.toggle('active',button.dataset.savedCategory===this.savedCategory));
        el.savedAnimatedTabs?.querySelectorAll('[data-saved-animated]').forEach(button=>button.classList.toggle('active',button.dataset.savedAnimated===this.savedAnimated));
        if (this.mode === 'mixamo') { this.LoadCharacters();
         this.LoadAnimationOptions();
         }
    }

    static async SetMixamoKind(kind) {
        if (!['animation','character'].includes(kind)) {
            return;
        }
        const el=this.elements();
        if (el.input) {
            this.mixamoQueries[this.mixamoSearchKind]=String(el.input.value||'').trim();
        }
        this.SuspendRendering();
         this.activePreviewKey=null;
         this.mixamoSearchKind=kind;
        this.ApplyModeText();
        if (el.results) {
            el.results.dataset.loaded='false';
        }
        if(kind==='character' && !this.mixamoQueries.character){
            await this.LoadCharacters(true);
            if (el.input) {
                el.input.value='';
            }
            this.RenderCharacters(this.mixamoCharacters);
            if (el.results) {
                el.results.dataset.loaded='true';
            }
            if(el.status) el.status.textContent=this.text('asset_results_status','{count} {kind} results \u00b7 local index',{count:this.mixamoCharacters.length.toLocaleString(),kind:'Mixamo character results'});
            return;
        }
        await this.Search(this.mixamoQueries[kind] || '');
    }

    static AnimationCategory(row){
        return window.CIFAssetMixamo.AnimationCategory.call(this, row);
    }

    static AnimationCategories(){
        return window.CIFAssetMixamo.AnimationCategories.call(this);
    }

    static FilterAnimationsByCategory(rows){
        return window.CIFAssetMixamo.FilterAnimationsByCategory.call(this, rows);
    }

    static PopulateAnimationPicker(){
        return window.CIFAssetMixamo.PopulateAnimationPicker.call(this);
    }

    static RenderAnimationCategoryTabs(){
        return window.CIFAssetMixamo.RenderAnimationCategoryTabs.call(this);
    }

    static async LoadAnimationOptions() {
        const el=this.elements();
         if (!el.animationPicker) {
             return;
         }
        try {
            const payload=await CIFRuntime.assetCommand({operation:'search',provider:'library',kind:'animation',q:'',limit:10000});
            this.allMixamoAnimations=Array.isArray(payload.results)?payload.results:[];
            if (!this.mixamoAnimations.length) {
                this.mixamoAnimations=this.allMixamoAnimations.slice();
            }
            if(el.animationCategoryPicker){
                el.animationCategoryPicker.innerHTML=this.AnimationCategories().map(category=>`<option value="${this.escapeHtml(category.id)}">${this.escapeHtml(category.label)}</option>`).join('');
                el.animationCategoryPicker.value=this.animationCategory;
            }
            if(!this.selectedDefaultAnimationId){
                const idle=this.allMixamoAnimations.find(row=>/^idle(?:\s|$)/i.test(String(row.title||row.file||'')))||this.allMixamoAnimations.find(row=>this.AnimationCategory(row)==='idle');
                if (idle?.id) {
                    this.selectedDefaultAnimationId=String(idle.id);
                }
            }
            this.PopulateAnimationPicker();
            this.RenderAnimationCategoryTabs();
            // Character and animation catalogs load concurrently when Mixamo opens.
            // If character cards rendered first, their thumbnail worker had no Idle
            // animation yet and correctly refused to render a T-pose. Restart the
            // missing-thumbnail queue now that the canonical Idle animation is known.
            if(this.mode==='mixamo' && this.mixamoSearchKind==='character' && this.selectedDefaultAnimationId){
                this.SetupCharacterThumbnailGeneration();
            }
        } catch(error) {
            console.error('[IFPlayer Assets] mixamo-animation-catalog-failed',{error:String(error?.message||error)});
            el.animationPicker.innerHTML=`<option value="">${this.escapeHtml(this.text('asset_animation_picker_unavailable','Animations unavailable'))}</option>`;
        }
    }

    static async LoadCharacters(force=false) {
        if (this.charactersLoaded && !force) {
            return;
        }
        const el = this.elements();
        if (!el.characterPicker) {
            return;
        }
        try {
            const payload = await CIFRuntime.assetCommand({operation:'search',provider:'library',kind:'3d',q:'',limit:120});
            const rows = Array.isArray(payload.results) ? payload.results : [];
            this.mixamoCharacters=rows;
            el.characterPicker.innerHTML = rows.map(row => `<option value="${this.escapeHtml(row.id || '')}">${this.escapeHtml(row.name || row.title || row.id || 'Character')}</option>`).join('');
            const preferred = rows.find(row => String(row.name || row.title || '').trim().toLowerCase() === 'abe') || rows[0];
            if (preferred) { el.characterPicker.value = String(preferred.id || '');
                this.selectedCharacterId = String(preferred.id || '');
                }
            this.charactersLoaded = true;
        } catch (error) {
            el.characterPicker.innerHTML = `<option value="">${this.escapeHtml(this.text('asset_character_picker_unavailable', 'Characters unavailable'))}</option>`;
        }
    }

    static async SetMode(mode) {
        if (!['objaverse','pd12m','mixamo','openclipart','heroicons','lucide','freesound','kenney','incompetech','cloudflare','saved','assign'].includes(mode)) {
            return;
        }
        const el = this.elements();
        if(window.CINTERFORGE_IF_LOCAL_WORLD_ACTIVE===true && ['objaverse','pd12m','mixamo','openclipart','heroicons','lucide','freesound','kenney','incompetech'].includes(mode)){
            this.mode=mode;
             this.ApplyModeText();
            if (el.form) {
                el.form.hidden=true;
            }
             if (el.searchRow) {
                 el.searchRow.hidden=true;
             }
            if (el.secondaryRow) {
                el.secondaryRow.hidden=true;
            }
             if (el.tertiaryRow) {
                 el.tertiaryRow.hidden=true;
             }
            if (el.status) {
                el.status.textContent='Available in the CinterForge Desktop app';
            }
            if(el.results){el.results.dataset.loaded='true';
                el.results.innerHTML='<div class="objaverse-empty"><strong>Asset Search is available in the CinterForge Desktop app.</strong><br>Local Editor can browse and preview the shared Asset Library, but search is disabled.</div>';
                }
            return;
        }

        if (el.input) {
            this.queries[this.mode] = String(el.input.value || '').trim();
        }
        this.SuspendRendering();
        this.activePreviewKey = null;
        this.mode = mode;
        this.ApplyModeText();
        if (el.results) {
            el.results.dataset.loaded = 'false';
        }
        if (mode==='cloudflare') {
            await this.LoadCloudflare();
        }
        else if(mode==='saved') await this.LoadSaved();
        else if(mode==='assign') await this.LoadAssignments();
        else if(mode==='mixamo'&&this.mixamoSearchKind==='character'&&!this.mixamoQueries.character){ await this.LoadCharacters(true);
            if (el.input) {
                el.input.value='';
            }
            this.RenderCharacters(this.mixamoCharacters);
            if (el.results) {
                el.results.dataset.loaded='true';
            }
            }
        else { const targetQuery=mode==='mixamo'?(this.mixamoQueries[this.mixamoSearchKind]||''):(this.queries[mode]??'lantern');
            await this.Search(targetQuery);
            }
    }

    static SuspendRendering(){
        return window.CIFAssetPreview.SuspendRendering.call(this);
    }

    static DisposePreviewKey(key){
        return window.CIFAssetPreview.DisposePreviewKey.call(this, key);
    }

    static DisposeDetachedPreviews(){
        return window.CIFAssetPreview.DisposeDetachedPreviews.call(this);
    }

    static ActivatePreviewEngine(key){
        return window.CIFAssetPreview.ActivatePreviewEngine.call(this, key);
    }

    static ResumeRendering(){
        return window.CIFAssetPreview.ResumeRendering.call(this);
    }

    static async Activate() {
        await this.Initialize();
        this.ResumeRendering();
        const el = this.elements();
        if (!el.results || el.results.dataset.loaded === 'true') {
            return;
        }
        if(window.CINTERFORGE_IF_LOCAL_WORLD_ACTIVE===true){
            // Local project authoring uses the canonical local UCI Asset Library.
            // Cloudflare is only a Push/Pull transport boundary, never browse authority.
            if (['objaverse','pd12m','mixamo'].includes(this.mode)) {
                return await this.SetMode(this.mode);
            }
            if(this.mode==='assign'){
                if (el.status) {
                    el.status.textContent='Assignments are available in the CinterForge Desktop app';
                }
                el.results.innerHTML='<div class="objaverse-empty"><strong>Assignments are available in the CinterForge Desktop app.</strong></div>';
                el.results.dataset.loaded='true';
                 return;
            }
            this.mode='saved';
             this.ApplyModeText();
             await this.LoadSaved();
            if (el.status) {
                el.status.textContent='Local Assets';
            }
            if (el.results) {
                el.results.dataset.loaded='true';
            }
             return;
        }
        if (this.mode==='saved') {
            await this.LoadSaved();
        }
            else if(this.mode==='assign') await this.LoadAssignments();
            else await this.Search(el.input ? el.input.value : this.query);
    }

    static async Search(value, requestedOffset=null) {
        await this.Initialize();
        const el = this.elements();
        if(window.CINTERFORGE_IF_LOCAL_WORLD_ACTIVE===true){
            if (el.status) {
                el.status.textContent='Available in the CinterForge Desktop app';
            }
            if(el.results){el.results.dataset.loaded='true';
                el.results.innerHTML='<div class="objaverse-empty"><strong>Asset Search is available in the CinterForge Desktop app.</strong><br>Use Library to browse and preview the selected shared Assets folder.</div>';
                }
            return;
        }
        const isMedia = ['openclipart','heroicons','lucide','freesound','kenney','incompetech'].includes(this.mode);
        const fallback = this.mode === 'mixamo' || isMedia ? '' : 'lantern';
        const query = String(value ?? '').trim() || fallback;
        const previousQuery=String(this.queries[this.mode]||'');
        this.query = query;
         this.queries[this.mode] = query;
        if (requestedOffset===null && query!==previousQuery) {
            this.searchOffsets[this.mode]=0;
        }
        const offset=Math.max(0,Number(requestedOffset===null?this.searchOffsets[this.mode]:requestedOffset)||0);
        this.searchOffsets[this.mode]=offset;
        if (el.input) {
            el.input.value = query;
        }
        if (el.button) {
            el.button.disabled = true;
        }
        if (el.status) el.status.textContent = this.text('asset_searching', 'Searching local index for \u201c{query}\u201d\u2026', {query});
        if (el.results) el.results.innerHTML = `<div class="objaverse-empty">${this.escapeHtml(this.text('objaverse_searching_row', 'Searching\u2026'))}</div>`;
        try {
            let rows = [];
             let payload={};
            if (isMedia) {
                payload = await CIFRuntime.assetCommand({operation:'search',provider:this.mode,q:query,limit:this.searchPageSize,offset});
                rows = Array.isArray(payload.results) ? payload.results : [];
            } else if (this.mode === 'objaverse') {
                payload = await CIFRuntime.assetCommand({operation:'search',provider:'objaverse',q:query,limit:this.searchPageSize,offset});
                rows = (payload.results || []).map(row => ({...row, provider:'objaverse'}));
            } else {
                const searchKind=this.mode==='pd12m'?'image':(this.mixamoSearchKind==='character'?'3d':'animation');
                payload = await CIFRuntime.assetCommand({operation:'search',provider:'library',kind:searchKind,q:query,limit:this.searchPageSize,offset});
                rows = Array.isArray(payload.results) ? payload.results : [];
            }
            if(this.mode==='mixamo'){ this.mixamoQueries[this.mixamoSearchKind]=query;
                if (this.mixamoSearchKind==='character') {
                    this.mixamoCharacters=rows;
                }
                else { this.mixamoAnimations=rows;
                this.RenderAnimationCategoryTabs();
                } }
            const total=Math.max(rows.length,Number(payload.total??(offset+rows.length))||0);
                this.searchTotals[this.mode]=total;
            if (isMedia) this.RenderMedia(rows);
            else this.Render(this.mode==='mixamo'&&this.mixamoSearchKind==='animation'?this.FilterAnimationsByCategory(rows):rows);
            this.RenderSearchPager(query,offset,total,rows.length);
            if (el.results) {
                el.results.dataset.loaded = 'true';
            }
            const suffix = isMedia ? `${this.mode} results` : this.mode === 'objaverse' ? 'Objaverse 3D results' : this.mode === 'pd12m' ? 'PD12M image results' : this.mixamoSearchKind==='character' ? 'Mixamo character results' : 'Mixamo animation results';
            const first=rows.length?offset+1:0,last=offset+rows.length;
            if (el.status) el.status.textContent = `${first.toLocaleString()}–${last.toLocaleString()} of ${total.toLocaleString()} ${suffix} · local index`;
        } catch (error) {
            const message=String(error?.message||error);
            if (el.results) {
                el.results.innerHTML = `<div class="objaverse-empty">${this.escapeHtml(message)}<br><button type="button" class="asset-index-install">Download search database from Cloudflare</button></div>`;
                const installButton=el.results.querySelector('.asset-index-install');
                if(installButton){
                    installButton.addEventListener('click',async()=>{
                        installButton.disabled=true;
                        installButton.textContent='Downloading search database…';
                        try{
                            const kind=this.mode==='pd12m'?'image':(this.mode==='mixamo'?(this.mixamoSearchKind==='character'?'3d':'animation'):'');
                            await CIFRuntime.assetCommand({operation:'search-index-install',provider:this.mode==='pd12m'||this.mode==='mixamo'?'library':this.mode,kind});
                            await this.Search(query,offset);
                        }catch(installError){
                            installButton.disabled=false;
                            installButton.textContent='Download search database from Cloudflare';
                            if(el.status)el.status.textContent=`Database download failed: ${String(installError?.message||installError)}`;
                        }
                    });
                }
            }
            if (el.status) {
                el.status.textContent = this.text('asset_index_unavailable', 'Asset index unavailable');
            }
        } finally {
            if (el.button) {
                el.button.disabled = false;
            }
        }
    }

    static RenderMedia(rows) {
        const el = this.elements();
        if (!el.results) return;
        el.results.innerHTML = '';
        for (const row of rows) {
            const card = document.createElement('article');
            card.className = 'objaverse-card';
            card.dataset.mediaProvider = String(row.provider || this.mode);
            card.dataset.mediaId = String(row.id || '');
            const provider = String(row.provider || this.mode);
            const isSvgProvider = ['openclipart','heroicons','lucide'].includes(provider);
            const isAudioProvider = ['kenney','freesound','incompetech'].includes(provider);
            if (isSvgProvider) {
                const thumbnail = document.createElement('img');
                thumbnail.className = 'objaverse-thumb media-inline-thumbnail';
                thumbnail.alt = String(row.title || row.id || 'SVG preview');
                card.appendChild(thumbnail);
                this.LoadMediaThumbnail(thumbnail, provider, String(row.id || ''));
            }
            if (isAudioProvider) {
                const play = document.createElement('button');
                play.type = 'button';
                play.className = 'media-audio-play';
                play.dataset.action = 'media-preview';
                play.textContent = '\u25b6';
                play.title = 'Play';
                play.setAttribute('aria-label', `Play ${String(row.title || row.id || 'audio')}`);
                card.appendChild(play);
            }
            const title = document.createElement('strong');
            title.textContent = row.title || row.id;
            const details = document.createElement('p');
            details.textContent = `${row.creator || ''} · ${row.license || ''} · ${row.description || ''}`;
            const preview = document.createElement('button');
            preview.type = 'button';
            preview.className = 'objaverse-action';
            preview.dataset.action = 'media-preview';
            preview.textContent = isAudioProvider ? 'Play' : 'Preview';
            const download = document.createElement('button');
            download.type = 'button';
            download.className = 'objaverse-action';
            download.dataset.action = 'media-download';
            download.textContent = 'Save locally';
            if (row.provider === 'freesound') {
                download.remove();
                const source = document.createElement('a');
                source.className = 'objaverse-action';
                source.textContent = 'Source / Download';
                source.href = row.source_url || 'https://freesound.org/';
                source.target = '_blank';
                source.rel = 'noopener noreferrer';
                card.append(title, details, preview, source);
            } else {
                card.append(title, details, preview, download);
            }
            el.results.appendChild(card);
        }
        if (!rows.length) el.results.innerHTML = '<div class="objaverse-empty">No matching assets in the installed index.</div>';
    }

    static LoadMediaThumbnail(image, provider, id) {
        if (!image || !provider || !id) {
            return;
        }
        CIFRuntime.assetCommand({operation:'media-preview',provider,id})
            .then(result => {
                if (!String(result?.mime_type||'').startsWith('image/')) {
                    return;
                }
                image.src=`data:${result.mime_type};base64,${result.data_base64}`;
            })
            .catch(error => {
                image.alt=`Preview unavailable: ${String(error?.message||error)}`;
                console.warn('[IFPlayer Assets] media thumbnail failed',{provider,id,error:String(error?.message||error)});
            });
    }

    static async MediaAction(card, action) {
        const provider = String(card.dataset.mediaProvider || '');
        const id = String(card.dataset.mediaId || '');
        if (action === 'media-download') {
            const result = await CIFRuntime.assetCommand({operation:'download',provider,id});
            const status = document.createElement('p');
            status.textContent = `Saved: ${result.path || 'local asset'}`;
            card.appendChild(status);
            return;
        }
        const result = await CIFRuntime.assetCommand({operation:'media-preview',provider,id});
        const url = `data:${result.mime_type};base64,${result.data_base64}`;
        card.querySelector('.media-preview')?.remove();
        const media = document.createElement(result.mime_type === 'image/svg+xml' ? 'img' : 'audio');
        media.className = 'media-preview';
        if (media.tagName === 'AUDIO') media.controls = true;
        else { media.alt = 'SVG preview'; media.style.maxWidth = '100%'; media.style.maxHeight = '240px'; }
        media.src = url;
        card.appendChild(media);
    }

    static RenderSearchPager(query,offset,total,count){
        const el=this.elements();
         if (!el.results||total<=this.searchPageSize) {
             return;
         }
        const nav=document.createElement('nav');
         nav.className='asset-search-pager';
        const first=count?offset+1:0,last=offset+count;
        nav.innerHTML=`<button type="button" class="objaverse-action" data-search-page="prev" ${offset<=0?'disabled':''}>Previous</button><span>${first.toLocaleString()}–${last.toLocaleString()} of ${total.toLocaleString()}</span><button type="button" class="objaverse-action" data-search-page="next" ${last>=total?'disabled':''}>Next</button>`;
        nav.querySelector('[data-search-page="prev"]')?.addEventListener('click',()=>this.Search(query,Math.max(0,offset-this.searchPageSize)));
        nav.querySelector('[data-search-page="next"]')?.addEventListener('click',()=>this.Search(query,offset+this.searchPageSize));
        el.results.appendChild(nav);
    }

    static DisposeObjaverseThumbnailRenderer(){
        return window.CIFAssetThumbnail.DisposeObjaverseThumbnailRenderer.call(this);
    }

    static async ObjaverseThumbnailRenderer(){
        return await window.CIFAssetThumbnail.ObjaverseThumbnailRenderer.call(this);
    }

    static QueueObjaverseThumbnailGeneration(image,uid){
        return window.CIFAssetThumbnail.QueueObjaverseThumbnailGeneration.call(this, image, uid);
    }

    static async GenerateObjaverseSearchThumbnail(image,uid){
        return await window.CIFAssetThumbnail.GenerateObjaverseSearchThumbnail.call(this, image, uid);
    }

    static QueueSavedThumbnailGeneration(image,modelPath){
        return window.CIFAssetThumbnail.QueueSavedThumbnailGeneration.call(this, image, modelPath);
    }

    static async GenerateSavedThumbnail(image, modelPath){
        return await window.CIFAssetThumbnail.GenerateSavedThumbnail.call(this, image, modelPath);
    }

    static HydrateStagedImages(root=null, options={}){
        return window.CIFAssetThumbnail.HydrateStagedImages.call(this, root, options);
    }

    static Render(rows){
        return window.CIFAssetObjaverse.Render.call(this, rows);
    }

    static RenderImages(rows){
        return window.CIFAssetPD12M.RenderImages.call(this, rows);
    }

    static DisposeCharacterPreviews() {
        return window.CIFAssetMixamo.DisposeCharacterPreviews.apply(this, arguments);
    }

    static CharacterAnimationRows(characterId) {
        return window.CIFAssetMixamo.CharacterAnimationRows.apply(this, arguments);
    }

    static RenderCharacters(rows) {
        return window.CIFAssetMixamo.RenderCharacters.apply(this, arguments);
    }

    static SetupCharacterThumbnailGeneration() {
        return window.CIFAssetThumbnail.SetupCharacterThumbnailGeneration.apply(this, arguments);
    }

    static DisposeCharacterThumbnailRenderer() {
        return window.CIFAssetThumbnail.DisposeCharacterThumbnailRenderer.apply(this, arguments);
    }

    static async CharacterThumbnailRenderer() {
        return await window.CIFAssetThumbnail.CharacterThumbnailRenderer.apply(this, arguments);
    }

    static QueueCharacterThumbnailGeneration(card) {
        return window.CIFAssetThumbnail.QueueCharacterThumbnailGeneration.apply(this, arguments);
    }

    static async GenerateCharacterSearchThumbnail(card) {
        return await window.CIFAssetThumbnail.GenerateCharacterSearchThumbnail.apply(this, arguments);
    }

    static async SearchCharacterAnimations(card,query) {
        return await window.CIFAssetMixamo.SearchCharacterAnimations.apply(this, arguments);
    }

    static AddCharacterAnimation(card,animationId) {
        return window.CIFAssetMixamo.AddCharacterAnimation.apply(this, arguments);
    }

    static SetupSavedCharacterPreviews() {
        return window.CIFAssetMixamo.SetupSavedCharacterPreviews.apply(this, arguments);
    }

    static RenderAnimations(rows) {
        return window.CIFAssetMixamo.RenderAnimations.apply(this, arguments);
    }

    static async EditMixamoCharacter(card) {
        return await window.CIFAssetMixamo.EditMixamoCharacter.apply(this, arguments);
    }

    static async HandleResultClick(event) {
        const button = event.target && event.target.closest ? event.target.closest('button[data-action]') : null;
        if (!button) {
            return;
        }
        const action = String(button.dataset.action || '');
        const actionCard = button.closest('.objaverse-card');
        console.info('[IFPlayer Assets] action', JSON.stringify({
            action,
            folder: String(actionCard?.dataset?.folder || ''),
            versionIndex: String(actionCard?.dataset?.versionIndex || ''),
            directCloudflarePath: String(actionCard?.dataset?.directCloudflarePath || ''),
            assignmentPath: String(actionCard?.dataset?.assignmentPath || ''),
        }));
        if(window.CINTERFORGE_IF_LOCAL_WORLD_ACTIVE===true){
            const allowed=new Set(['saved-prev','saved-next','saved-preview','saved-image-minecraft-preview','saved-minecraft-preview','saved-block-preview']);
            if(!allowed.has(action)){
                this.SetProgress(button.closest('.objaverse-card'), 'This operation is available in the CinterForge Desktop app.');
                return;
            }
        }
        if(action==='assignment-prev'||action==='assignment-next'){
            button.disabled=true;
            try{await this.CycleAssignedPreview(button,action==='assignment-next'?1:-1);
                }catch(error){this.SetProgress(null,error.message||error);
                }finally{button.disabled=false;
                }
            return;
        }
        const card = button.closest('.objaverse-card');
        if (!card) {
            return;
        }
        const keepActive = action.includes('preview') || action === 'saved-export' || action === 'objaverse-export' || action === 'character-export';
        if (!keepActive) {
            button.disabled = true;
        }
        try {
            if (action === 'media-preview' || action === 'media-download') {
                await this.MediaAction(card, action);
            }
            else if (action === 'asset-download') {
                await this.StartAssetDownload(card);
            }
            else if (action === 'character-edit') await this.EditMixamoCharacter(card);
            else if (action === 'character-preview') await this.LoadCharacterPreview(card,String(card.querySelector('.mixamo-character-animation-picker')?.value||''),true);
            else if (action === 'character-animation-preview') await this.LoadCharacterPreview(card,String(button.dataset.animationId||''),true);
            else if (action === 'character-animation-add') this.AddCharacterAnimation(card,String(button.dataset.animationId||''));
            else if (action === 'character-minecraft-preview') await this.RunMixamoCharacterSavedAction(card,'preview');
            else if (action === 'character-export') await this.RunMixamoCharacterSavedAction(card,'export');
            else if (action === 'character-remove') await this.RemoveMixamoCharacter(card);
            else if (action === 'character-save') await this.SaveMixamoCharacter(card);
            else if (action === 'animation-preview') await this.LoadAnimationPreview(card);
            else if (action === 'animation-download-rpg') await this.DownloadRPGAnimation(card);
            else if (action === 'preview') await this.LoadPreview(card);
            else if (action === 'objaverse-minecraft-preview') await this.RunObjaverseSavedAction(card,'preview');
            else if (action === 'objaverse-export') await this.RunObjaverseSavedAction(card,'export');
            else if (action === 'objaverse-remove') await this.RemoveObjaverseAsset(card);
            else if (action === 'download') await this.StartTask(card, 'download');
            else if (action === 'saved-prev') await this.CycleSaved(card,-1);
            else if (action === 'saved-next') await this.CycleSaved(card,1);
            else if (action === 'saved-preview') { await this.LoadSavedPreview(card);
                if (card.classList.contains('assignment-current-card')) {
                    this.ToggleAssetFullView(card,true);
                }
                }
            else if (action === 'saved-image-minecraft-preview') await this.LoadImageMinecraftPreview(card);
            else if (action === 'saved-image-minecraft-export') await this.ExportImageMinecraftAddon(card);
            else if (action === 'saved-image-blockbench') { await this.EnsureImageMinecraft(card);
                await this.OpenBlockbench(card);
                }
            else if (action === 'saved-blockify') await this.BlockifySavedAsset(card);
            else if (action === 'saved-block-preview' || action === 'saved-minecraft-preview') { await this.LoadSavedMinecraftPreview(card);
                if (card.classList.contains('assignment-current-card')) {
                    this.ToggleAssignmentMinecraftFullView(card,true);
                }
                else this.ToggleSavedMinecraftFullView(card,true);
                }
            else if (action === 'saved-blockbench') await this.OpenBlockbench(card);
            else if (action === 'saved-block-export' || action === 'saved-export') { await this.EnsureBlockified(card);
                await this.ExportBlockifiedAddon(card);
                }
            else if (action === 'assignment-direct-preview') await this.LoadDirectAssignmentPreview(card);
            else if (action === 'assignment-cloudflare-preview') await this.LoadCloudflareAssignmentPreview(card);
            else if (action === 'assignment-cloudflare-minecraft') { await this.LoadCloudflareMinecraftPreview(card);
                if (card.classList.contains('assignment-current-card')) {
                    this.ToggleAssignmentMinecraftFullView(card,true);
                }
                }
            else if (action === 'assignment-cloudflare-blockbench') await this.OpenCloudflareBlockbench(card);
            else if (action === 'assignment-image-preview') {
                if (card.classList.contains('assignment-current-card')) {
                    this.ToggleAssetFullView(card);
                }
                else card.classList.toggle('assignment-image-preview-open');
            }
            else if (action === 'saved-delete') { const folder=this.SavedFolderForCard(card);
                if(folder?.uci&&window.confirm('Delete this entire CinterForge asset and its versions?')){await CIFRuntime.assetCommand({operation:'saved-delete-asset',uci:folder.uci});
                await this.LoadSaved();
                } }
            else if (action === 'saved-promote') { await this.TestFixedMixamo(card);
             }
        } catch (error) {
            // Safari's console frequently collapses a logged object to just
            // 'Object'. Emit the actual operation and exception as text so
            // Blockify failures can be diagnosed without object inspection.
            console.error('[IFPlayer Assets] action failed: '+action+' | '+String(error?.stack||error?.message||error));
            this.SetProgress(card, this.text('objaverse_failed','Failed: {error}', {error: error.message || error}));
        } finally {
            button.disabled = false;
        }
    }


    static async EnsureAssignedCardVisual(card){
        if (!card||card.classList.contains('viewer-active')) {
            return;
        }
        if (String(card.dataset.assetKind||'')!=='3d') {
            return;
        }
        // A published paired thumbnail is authoritative when present.  Older
        // Cloudflare assets may not have one; render that card from its public
        // Cloudflare GLB only after it becomes the active rail card.
        if (card.querySelector('.objaverse-visual > img.objaverse-thumb')) {
            return;
        }
        if (!card.dataset.directCloudflarePath) {
            return;
        }
        try{await this.LoadCloudflareAssignmentPreview(card);
            }catch(error){console.warn('[IFPlayer Assets] assigned visual render failed',{path:card.dataset.directCloudflarePath,error:String(error?.message||error)});
            }
    }

    static async CycleAssignedPreview(button,delta){
        const editor=button?.closest?.('.assignment-editor');
         if (!editor) {
             return;
         }
        const rail=editor.querySelector('.assignment-current-assets');
         if (!rail) {
             return;
         }
        const cards=Array.from(rail.querySelectorAll('.assignment-current-card:not(.assignment-missing-card)'));
            if (!cards.length) {
                return;
            }
        const first=cards[0];
        const gap=Number.parseFloat(getComputedStyle(rail).columnGap||getComputedStyle(rail).gap||'0')||0;
        const step=Math.max(1,first.getBoundingClientRect().width+gap);
        rail.scrollBy({left:(delta<0?-step:step),behavior:'smooth'});
    }

    static ToggleAnimationFullView(card, force = null) {
        if (!card) {
            return;
        }
        const results = this.elements().results;
        if (!results) {
            return;
        }
        const entering = force === null ? !card.classList.contains('asset-fullview') : Boolean(force);
        ifQuerySelectorAll('.asset-animation-card.asset-fullview').forEach(other => {
            if (other !== card) {
                other.classList.remove('asset-fullview');
            }
        });
        card.classList.toggle('asset-fullview', entering);
        results.classList.toggle('asset-fullview-active', entering);
        ifGetElementById('if_root')?.classList.toggle('if_asset-preview-fullview', entering);
        const key = String(card.dataset.animationPreviewKey || '');
        const preview = key ? this.animationPreviews.get(key) : null;
        requestAnimationFrame(() => {
            try { preview?.engine?.resize();
             } catch (_) {
             }
        });
        this.SetProgress(card, entering
            ? this.text('asset_animation_fullview_status', 'Full view \u00b7 double-click or Esc to return')
            : this.text('asset_animation_preview_ready', 'Playing on selected character'));
    }

    static ToggleAssetFullView(card, force = null) {
        if (!card) {
            return;
        }
        const editor = card.closest('.assignment-editor');
        if (!editor) {
            return;
        }
        const entering = force === null ? !card.classList.contains('asset-inspect-fullview') : Boolean(force);
        editor.querySelectorAll('.assignment-current-card.asset-inspect-fullview').forEach(other => {
            if (other !== card) {
                other.classList.remove('asset-inspect-fullview');
            }
        });
        card.classList.toggle('asset-inspect-fullview', entering);
        editor.classList.toggle('asset-inspect-fullview-active', entering);
        ifGetElementById('if_root')?.classList.toggle('if_asset-preview-fullview', entering);
        const canvas = card.querySelector('.saved-asset-canvas');
        requestAnimationFrame(()=>this.ResizeAndReframeCanvas(canvas));
        this.SetProgress(card, entering ? 'Full view \u00b7 double-click or Esc to return' : 'Preview');
    }

    static ToggleAssignmentMinecraftFullView(card,force=null){if(!card)return;
        const editor=card.closest('.assignment-editor');
        if (!editor) {
            return;
        }
        const entering=force===null?!card.classList.contains('asset-inspect-fullview'):Boolean(force);
        card.classList.toggle('asset-inspect-fullview',entering);
        editor.classList.toggle('asset-inspect-fullview-active',entering);
        ifGetElementById('if_root')?.classList.toggle('if_asset-preview-fullview',entering);
        const canvas=card.querySelector('.saved-asset-canvas');
        requestAnimationFrame(()=>this.ResizeAndReframeCanvas(canvas));
        this.SetProgress(card,entering?'Full Sized Minecraft Preview \u00b7 Blocks picker \u00b7 Left Shift place \u00b7 Right Shift remove \u00b7 Esc to return':'Minecraft Preview');
        }

    static ToggleSavedMinecraftFullView(card,force=null){if(!card)return;
        const results=this.elements().results;
        if (!results) {
            return;
        }
        const entering=force===null?!card.classList.contains('asset-fullview'):Boolean(force);
        card.classList.toggle('asset-fullview',entering);
        results.classList.toggle('asset-fullview-active',entering);
        ifGetElementById('if_root')?.classList.toggle('if_asset-preview-fullview',entering);
        const canvas=card.querySelector('.saved-asset-canvas');
        requestAnimationFrame(()=>this.ResizeAndReframeCanvas(canvas));
        this.SetProgress(card,entering?'Full Sized Minecraft Preview \u00b7 B/Blocks picker \u00b7 Left Shift place \u00b7 Right Shift remove \u00b7 Esc to return':'Minecraft Preview');
        }

    static async ConvertDownloadedAsset(state,kind,card=null) {
        const sourcePath=String(state?.relative_path||state?.saved_path||'').replace(/\\/g,'/');
        if(!sourcePath)throw new Error('Downloaded asset path was not returned.');
        if(state?.managed_path)return state;
        if(String(kind||'').toLowerCase()==='image'){
            if(card)this.SetProgress(card,'Converting image…');
            const converted=await CIFRuntime.assetCommand({operation:'asset-image-convert-local',path:sourcePath});
            return {...state,managed_path:String(converted.path||''),uci:String(converted.uci||''),relative_path:String(converted.path||'')};
        }
        if(card)this.SetProgress(card,'Converting managed asset…');
        throw new Error(
            `Downloaded 3D asset has no canonical managed UCI: ${sourcePath}. ` +
            'Publish this asset through the UCI asset catalog before assigning it.'
        );
    }

    static async EnsureManagedAsset(kind,id,query,card=null) {
        await CIFRuntime.assetCommand({operation:'download',provider:'library',kind,id,q:query});
        return await new Promise((resolve,reject)=>{
            const check=async()=>{try{const state=await CIFRuntime.assetCommand({operation:'status',provider:'library',kind,id});
                if (card) {
                    this.SetProgress(card,state.message||state.stage||'Working\u2026');
                }
                if(state.complete){clearInterval(timer);
                if(state.error)reject(new Error(state.error));
                else {
                    try{resolve(await this.ConvertDownloadedAsset(state,kind,card));}
                    catch(error){reject(error);}
                }
                }}catch(error){clearInterval(timer);
                reject(error);
                }};
            const timer=setInterval(check,650);
            check();
        });
    }

    static async SaveMixamoCharacter(card) {
        return await window.CIFAssetMixamo.SaveMixamoCharacter.apply(this, arguments);
    }

    static async CacheCharacterThumbnail(characterId, canvas) {
        return await window.CIFAssetThumbnail.CacheCharacterThumbnail.apply(this, arguments);
    }

    static async LoadCharacterPreview(card,requestedAnimationId='',playAnimation=false,{forceTPose=false}={}) {
        return await window.CIFAssetPreview.LoadCharacterPreview.apply(this, arguments);
    }

    static async RunMixamoCharacterSavedAction(card,action) {
        return await window.CIFAssetMixamo.RunMixamoCharacterSavedAction.apply(this, arguments);
    }

    static async RemoveMixamoCharacter(card) {
        return await window.CIFAssetMixamo.RemoveMixamoCharacter.apply(this, arguments);
    }

    static async StartAssetDownload(card) {
        return await window.CIFAssetDownload.StartAssetDownload.apply(this, arguments);
    }

    static async LoadCloudflare(){
        return await window.CIFAssetCloudflare.LoadCloudflare.call(this);
    }

    static CloudflareFolderTree(files){
        return window.CIFAssetCloudflare.CloudflareFolderTree.call(this, files);
    }

    static RenderCloudflare(){
        return window.CIFAssetCloudflare.RenderCloudflare.call(this);
    }

    static async LoadSaved() {
        return await window.CIFAssetLibrary.LoadSaved.call(this);
    }

    static async RefreshLocalAssignmentState() {
        return await window.CIFAssetLibrary.RefreshLocalAssignmentState.call(this);
    }

    static ActiveAssignedUCIs() {
        return window.CIFAssetLibrary.ActiveAssignedUCIs.call(this);
    }

    static SavedRows() {
        return window.CIFAssetLibrary.SavedRows.call(this);
    }

    static SavedAnimations() {
        return window.CIFAssetLibrary.SavedAnimations.call(this);
    }

    static SavedAttributionHtml(version) {
        return window.CIFAssetLibrary.SavedAttributionHtml.call(this, version);
    }

    static DisposeSavedPreviews() {
        return window.CIFAssetLibrary.DisposeSavedPreviews.call(this);
    }

    static RenderSaved() {
        return window.CIFAssetLibrary.RenderSaved.call(this);
    }

    static SavedFolderForCard(card) {
        return window.CIFAssetLibrary.SavedFolderForCard.call(this, card);
    }

    static async CycleSaved(card, delta) {
        return await window.CIFAssetLibrary.CycleSaved.call(this, card, delta);
    }

    static async SavedPost(path, body) {
        return await window.CIFAssetLibrary.SavedPost.call(this, path, body);
    }

    static async LoadAssignments(){
        return await CIFAssetAssignment.LoadAssignments.call(this);
    }

    static AssignmentSavedRows(){
        return CIFAssetAssignment.AssignmentSavedRows.call(this);
    }

    static AssignmentSearchRows(query){
        return CIFAssetAssignment.AssignmentSearchRows.call(this, query);
    }

    static async SearchAssignmentInline(key, query, source, offset=0){
        return await CIFAssetAssignment.SearchAssignmentInline.call(this, key, query, source, offset);
    }

    static async SaveExternalAssignment(button){
        return await CIFAssetAssignment.SaveExternalAssignment.call(this, button);
    }

    static async EnsureInitialMixamoIdle(characterPath,progressCard=null){
        const parts=String(characterPath||'').split('/');
            parts.pop();
            const folder=parts.join('/');
            if (!folder.startsWith('Animated/Mixamo/')) {
                return;
            }
        const payload=await CIFRuntime.assetCommand({operation:'search',provider:'library',kind:'animation',q:'Idle',limit:48}),rows=Array.isArray(payload.results)?payload.results:[];
        const idle=rows.find(row=>/^idle$/i.test(String(row.title||row.name||row.file||'').trim()))||rows.find(row=>/^idle(?:\s|$)/i.test(String(row.title||row.name||row.file||'').trim()))||rows[0];
        if (!idle) {
            throw new Error('Mixamo Idle animation is unavailable; character was not assigned without its required default animation.');
        }
        const state=await this.EnsureManagedAsset('animation',String(idle.id||''),String(idle.title||idle.file||'Idle'),progressCard),rel=String(state?.relative_path||'');
            if (!rel) {
                throw new Error('Mixamo Idle saved path was not returned.');
            }
        const animationParts=rel.split('/'),animationFile=animationParts.pop()||'',animationFolder=animationParts.join('/');
        await CIFRuntime.assetCommand({operation:'saved-assign-animation',folder,animation_folder:animationFolder,animation_file:animationFile,name:'Idle'});
        await CIFRuntime.assetCommand({operation:'saved-default-animation',folder,animation_folder:animationFolder,animation_file:animationFile});
    }

    static AssignmentAssetKindLabel(kind){
        return CIFAssetAssignment.AssignmentAssetKindLabel.call(this, kind);
    }

    static AssignmentAnimationPanelHtml(folder,version){
        return CIFAssetAssignment.AssignmentAnimationPanelHtml.call(this, folder, version);
    }


    static async RefreshAssignmentSavedLibrary(){
        return await CIFAssetAssignment.RefreshAssignmentSavedLibrary.call(this);
    }

    static SavedPreviewKey(card){
        return CIFAssetAssignment.SavedPreviewKey.call(this, card);
    }

    static async PlayManagedAnimation(card,name,button=null){
        return await CIFAssetAssignment.PlayManagedAnimation.call(this, card, name, button);
    }

    static async SearchManagedAnimations(card,query){
        return await CIFAssetAssignment.SearchManagedAnimations.call(this, card, query);
    }

    static ManagedAnimationResult(folder,id){
        return CIFAssetAssignment.ManagedAnimationResult.call(this, folder, id);
    }

    static async AddManagedAnimation(card,id){
        return await CIFAssetAssignment.AddManagedAnimation.call(this, card, id);
    }

    static async PreviewManagedAnimationResult(card,id){
        return await CIFAssetAssignment.PreviewManagedAnimationResult.call(this, card, id);
    }

    static LoadAssignmentUiState(){
        return CIFAssetAssignment.LoadAssignmentUiState.call(this);
    }

    static SaveAssignmentUiState(){
        return CIFAssetAssignment.SaveAssignmentUiState.call(this);
    }

    static BindAssignmentBrowser(el,games,displayName){
        return CIFAssetAssignment.BindAssignmentBrowser.call(this, el, games, displayName);
    }

    static RenderAssignments(){
        return CIFAssetAssignment.RenderAssignments.call(this);
    }

    static async HandleAssignmentAction(button){
        return await CIFAssetAssignment.HandleAssignmentAction.call(this, button);
    }

    static FrameScene(scene,camera){
        const meshes=scene.meshes.filter(mesh=>mesh&&mesh.getTotalVertices&&mesh.getTotalVertices()>0&&!mesh.metadata?.previewEnvironment);
        if (!meshes.length) {
            return;
        }
        let min=new BABYLON.Vector3(Number.POSITIVE_INFINITY,Number.POSITIVE_INFINITY,Number.POSITIVE_INFINITY);
        let max=new BABYLON.Vector3(Number.NEGATIVE_INFINITY,Number.NEGATIVE_INFINITY,Number.NEGATIVE_INFINITY);
        meshes.forEach(mesh=>{const box=mesh.getBoundingInfo().boundingBox;
            min=BABYLON.Vector3.Minimize(min,box.minimumWorld);
            max=BABYLON.Vector3.Maximize(max,box.maximumWorld);
            });
        const center=min.add(max).scale(.5),size=max.subtract(min);
        const width=Math.max(Number(size.x||0),.01),height=Math.max(Number(size.y||0),.01),depth=Math.max(Number(size.z||0),.01);
        // Frame the visible object to the ACTUAL canvas aspect ratio.  The old
        // diagonal*1.25 rule massively over-framed wide/flat voxel assets and
        // produced the tiny-object preview Pete is seeing.  Reset to the same
        // useful three-quarter camera every time a fresh asset is framed.
        const aspect=Math.max(.1,Number(camera.getEngine?.().getAspectRatio?.(camera)||1));
        const vfov=Math.max(.1,Number(camera.fov||.8)),hfov=2*Math.atan(Math.tan(vfov/2)*aspect);
        const vertical=(height*.5)/Math.tan(vfov*.5),horizontal=(width*.5)/Math.tan(hfov*.5);
        const radius=Math.max(vertical,horizontal)+depth*.55;
        camera.alpha=-Math.PI/4;
         camera.beta=Math.PI*0.40;
        camera.setTarget(center);
            camera.radius=Math.max(radius*1.12,.25);
            camera.lowerRadiusLimit=Math.max(camera.radius*.04,.01);
            camera.upperRadiusLimit=Math.max(camera.radius*12,10);
            camera.minZ=Math.max(camera.radius/10000,.001);
            camera.maxZ=Math.max(camera.radius*100,1000);
    }

    static ResizeAndReframeCanvas(canvas){
        if (!canvas) {
            return;
        }
        for(const [key,engine] of this.engines.entries()){
            if (engine?.getRenderingCanvas?.()!==canvas) {
                continue;
            }
            try{engine.resize();
                const scene=this.scenes.get(key),camera=scene?.activeCamera;
                if (scene&&camera) {
                    this.FrameScene(scene,camera);
                }
                } catch (_) {
                }
            break;
        }
    }

    static BlockifiedFrameZeroValue(channel){
        const rows=Object.entries(channel||{}).map(([time,value])=>[Number(time),value]).filter(row=>Number.isFinite(row[0])&&Array.isArray(row[1])).sort((a,b)=>a[0]-b[0]);
        if (!rows.length) {
            return null;
        }
        const exact=rows.find(row=>Math.abs(row[0])<1e-6);
        return (exact||rows[0])[1];
    }

    static FrameAnimatedBlockifiedIdle(camera,occupied,bones,clips){
        const B=window.BABYLON,voxels=Array.isArray(occupied)?occupied:[],rigBones=Array.isArray(bones)?bones:[];
        if(!B||!camera||!voxels.length||!rigBones.length){return;
        }
        const clipRows=Object.entries(clips||{}),idleRow=clipRows.find(([key,clip])=>/idle/i.test(`${String(clip?.label||'')} ${String(key||'')}`))||clipRows[0],idle=idleRow?.[1]||{},idleBones=idle.bones||{};
        const minX=Math.min(...voxels.map(v=>Number(v.x))),maxX=Math.max(...voxels.map(v=>Number(v.x))),minY=Math.min(...voxels.map(v=>Number(v.y))),minZ=Math.min(...voxels.map(v=>Number(v.z))),maxZ=Math.max(...voxels.map(v=>Number(v.z))),centerX=(minX+maxX)/2,centerZ=(minZ+maxZ)/2,size=.88/16,half=size/2;
        const pivots=new Map();
            for(const bone of rigBones){const p=Array.isArray(bone.pivot)?bone.pivot:[centerX,minY,centerZ];
            pivots.set(String(bone.name||''),new B.Vector3((Number(p[0])-centerX)/16,(Number(p[1])-minY)/16,(Number(p[2])-centerZ)/16));
            }
        let boundMin=new B.Vector3(Number.POSITIVE_INFINITY,Number.POSITIVE_INFINITY,Number.POSITIVE_INFINITY),boundMax=new B.Vector3(Number.NEGATIVE_INFINITY,Number.NEGATIVE_INFINITY,Number.NEGATIVE_INFINITY),count=0;
        for(const voxel of voxels){
            const name=String(voxel.bone||''),pivot=pivots.get(name);
            if (!pivot) {
                continue;
            }
            const channels=idleBones[name]||{},qv=this.BlockifiedFrameZeroValue(channels.rotation_quaternion),rv=this.BlockifiedFrameZeroValue(channels.rotation),pv=this.BlockifiedFrameZeroValue(channels.position);
            let q=B.Quaternion.Identity();
            if (qv&&qv.length>=4) {
                q=new B.Quaternion(Number(qv[0]||0),Number(qv[1]||0),Number(qv[2]||0),Number(qv[3]??1)).normalize();
            }
            else if(rv&&rv.length>=3)q=B.Quaternion.FromEulerAngles(-B.Tools.ToRadians(Number(rv[0]||0)),B.Tools.ToRadians(Number(rv[1]||0)),-B.Tools.ToRadians(Number(rv[2]||0)));
            const delta=pv&&pv.length>=3?new B.Vector3(-Number(pv[0]||0)/16,Number(pv[1]||0)/16,-Number(pv[2]||0)/16):B.Vector3.Zero(),translation=pivot.add(delta),matrix=B.Matrix.Compose(B.Vector3.One(),q,translation);
            const cx=(Number(voxel.x)-centerX)/16-pivot.x,cy=(Number(voxel.y)-minY)/16+half-pivot.y,cz=(Number(voxel.z)-centerZ)/16-pivot.z;
            for(const dx of [-half,half])for(const dy of [-half,half])for(const dz of [-half,half]){const point=B.Vector3.TransformCoordinates(new B.Vector3(cx+dx,cy+dy,cz+dz),matrix);
                boundMin=B.Vector3.Minimize(boundMin,point);
                boundMax=B.Vector3.Maximize(boundMax,point);
                count++;
                }
        }
        if (!count||!Number.isFinite(boundMin.x)||!Number.isFinite(boundMax.x)) {
            return;
        }
        const center=boundMin.add(boundMax).scale(.5),width=Math.max(.01,boundMax.x-boundMin.x),height=Math.max(.01,boundMax.y-boundMin.y),depth=Math.max(.01,boundMax.z-boundMin.z),aspect=Math.max(.1,Number(camera.getEngine?.().getAspectRatio?.(camera)||1)),vfov=Math.max(.1,Number(camera.fov||.8)),hfov=2*Math.atan(Math.tan(vfov/2)*aspect),verticalDistance=(height/2)/Math.tan(vfov/2),horizontalDistance=(width/2)/Math.tan(hfov/2),radius=Math.max(verticalDistance,horizontalDistance)+depth*.45;
        camera.setTarget(center);
            camera.radius=Math.max(radius*1.12,.25);
            camera.lowerRadiusLimit=Math.max(camera.radius*.08,.02);
            camera.upperRadiusLimit=Math.max(camera.radius*10,8);
            camera.minZ=Math.max(camera.radius/1000,.001);
            camera.maxZ=Math.max(camera.radius*100,100);
    }

    static NormalizeRigName(name) {
        // FBX2glTF can preserve hierarchy/namespace prefixes differently between
        // character and animation exports (for example Armature|mixamorig:Hips).
        // Rig identity is the terminal Mixamo bone name, not those container names.
        const rawName = String(name || '').trim();
        if (!rawName) {
            return '';
        }

        const hierarchyParts = rawName.split(/[|/\\]/);
        let boneName = hierarchyParts[hierarchyParts.length - 1] || rawName;

        const namespaceParts = boneName.split(':');
        boneName = namespaceParts[namespaceParts.length - 1] || boneName;

        boneName = boneName.replace(/^mixamorig[_-]?/i, '');
        return boneName.replace(/[^a-z0-9]/gi, '').toLowerCase();
    }

    static IsHumanoidAnimationNode(name){
        const n=this.NormalizeRigName(name);
         if (!n) {
             return false;
         }
        if (/^(hips|pelvis|root)$/.test(n)) {
            return true;
        }
        if (/^spine\d*$/.test(n)||/^(chest|upperchest|neck|head)$/.test(n)) {
            return true;
        }
        if (/^(left|right)(shoulder|arm|upperarm|forearm|lowerarm|hand|upleg|upperleg|leg|lowerleg|foot|toe|toebase)$/.test(n)) {
            return true;
        }
        // Keep this shared retarget filter identical to the proven interactive
        // Mixamo preview path: some Mixamo character rigs expose their usable
        // humanoid mapping through the hand/finger chains as well.
        if (/^(left|right)hand(thumb|index|middle|ring|pinky|little)\d*$/.test(n)) {
            return true;
        }
        return false;
    }

    static RetargetAnimation(scene, character, characterNodes, sourceNodes, sourceGroups, label='saved'){
        if (typeof BABYLON.AnimatorAvatar!=='function') {
            throw new Error('Babylon AnimatorAvatar retargeting API is unavailable.');
        }
        if (!sourceGroups?.length) {
            throw new Error(this.text('asset_animation_retarget_failed','Converted Mixamo asset contains no Babylon AnimationGroup.'));
        }
        const normalize=name=>this.NormalizeRigName(name);
        const targetRawByNormalized=new Map();
        const rememberTarget=target=>{if(!target)return;
            const raw=String(target.name||target.id||'');
            const n=normalize(raw);
            if (raw&&n&&!targetRawByNormalized.has(n)) {
                targetRawByNormalized.set(n,raw);
            }
            };
        characterNodes.forEach(rememberTarget);
        (character.skeletons||[]).forEach(skeleton=>{try{skeleton.returnToRest();
            }catch(_){};
            (skeleton.bones||[]).forEach(bone=>{rememberTarget(bone);
            try{rememberTarget(bone.getTransformNode?.());
            }catch(_){}});
            });
        const sourceRestPose=new Map();
        sourceNodes.forEach(node=>{if(!node)return;
            sourceRestPose.set(node,{position:node.position?.clone?.()||null,scaling:node.scaling?.clone?.()||null,rotation:node.rotation?.clone?.()||null,rotationQuaternion:node.rotationQuaternion?.clone?.()||null});
            });
        const restore=()=>{sourceGroups.forEach(g=>{try{g.stop();
            }catch(_){}});
            sourceRestPose.forEach((pose,node)=>{try{if(pose.position&&node.position)node.position.copyFrom(pose.position);
            }catch(_){}try{if(pose.scaling&&node.scaling)node.scaling.copyFrom(pose.scaling);
            }catch(_){}try{if(pose.rotationQuaternion){if(!node.rotationQuaternion)node.rotationQuaternion=pose.rotationQuaternion.clone();
            else node.rotationQuaternion.copyFrom(pose.rotationQuaternion);
            }else if(node.rotationQuaternion)node.rotationQuaternion=null;
            }catch(_){}try{if(pose.rotation&&node.rotation)node.rotation.copyFrom(pose.rotation);
            }catch(_){}try{node.computeWorldMatrix?.(true);
            }catch(_){}});
            };
        restore();
        const sourceGroup=sourceGroups.find(g=>g.targetedAnimations?.length)||sourceGroups[0];
        const filtered=new BABYLON.AnimationGroup(`${sourceGroup.name||'Mixamo'}-${label}-filtered`,scene);
        const mapNodeNames=new Map();
         let mappedTargets=0;
        for(const targeted of (sourceGroup.targetedAnimations||[])){
            const sourceName=String(targeted?.target?.name||targeted?.target?.id||'');
            if (!this.IsHumanoidAnimationNode(sourceName)) {
                continue;
            }
            const targetName=targetRawByNormalized.get(normalize(sourceName));
             if (!targetName) {
                 continue;
             }
            const property=String(targeted?.animation?.targetProperty||'').toLowerCase();
            const isRoot=/^(hips|pelvis|root)$/.test(normalize(sourceName));
            if (!(property==='rotationquaternion'||property==='rotation'||(isRoot&&property==='position'))) {
                continue;
            }
            filtered.addTargetedAnimation(targeted.animation,targeted.target);
                mapNodeNames.set(sourceName,targetName);
                mappedTargets++;
        }
        if(!mappedTargets||!filtered.targetedAnimations.length){try{filtered.dispose();
            }catch(_){}throw new Error(this.text('asset_animation_retarget_failed','Animation bones did not match the selected character rig.'));
            }
        restore();
         (character.skeletons||[]).forEach(s=>{try{s.returnToRest();}catch(_){}});
        const avatarRoot=(character.meshes||[])[0]||characterNodes[0];
            if (!avatarRoot) {
                throw new Error('Selected character has no Babylon avatar root.');
            }
        const avatar=new BABYLON.AnimatorAvatar(`mixamo-avatar-${label}`,avatarRoot);
         avatar.showWarnings=true;
        const playing=avatar.retargetAnimationGroup(filtered,{animationGroupName:`${sourceGroup.name||'Mixamo'}-${label}`,retargetAnimationKeys:true,fixAnimations:true,checkHierarchy:false,fixRootPosition:false,fixGroundReference:false,mapNodeNames});
        try{filtered.dispose();
        } catch (_) {
        }
        if (!playing||!playing.targetedAnimations?.length) {
            throw new Error(this.text('asset_animation_retarget_failed','Animation bones did not match the selected character rig.'));
        }
        sourceGroups.forEach(g=>{try{g.stop();
            }catch(_){}});
            sourceNodes.forEach(node=>{if('setEnabled' in node)try{node.setEnabled(false);
            }catch(_){}});
        return {playing,mappedTargets};
    }

    static DefaultSavedAnimation(){
        const folders=this.SavedAnimations();
        for(const folder of folders) for(const version of (folder.versions||[])) if(String(version.file||'').toLowerCase().endsWith('.fbx')) return {folder,version};
        return null;
    }

    static async TestFixedMixamo(card){
        const folder=this.SavedFolderForCard(card);
         if (!folder) {
             return;
         }
        const versions=folder.versions||[];
         const version=versions[Number(card.dataset.versionIndex||0)]||versions[0];
        const animation=this.DefaultSavedAnimation();
        if (!version) {
            throw new Error('No saved asset version is available.');
        }
        if(!animation){this.SetProgress(card,this.text('asset_test_requires_animation','Save a Mixamo animation first so the rig can be tested.'));
            return;
            }
        this.SetProgress(card,this.text('asset_test_running','Testing Mixamo retarget\u2026'));
        let engine=null,scene=null;
        try{
            await ensureBabylon();
             if (!window.BABYLON) {
                 throw new Error('Babylon.js is unavailable.');
             }
            const canvas=card.querySelector('.saved-asset-canvas')||document.createElement('canvas');
                engine=new BABYLON.Engine(canvas,false,{preserveDrawingBuffer:false,stencil:false});
                scene=new BABYLON.Scene(engine);
            const base='';
             const targetRel=`${folder.folder}/${version.file}`;
            const before=new Set(scene.getNodes());
                const characterUrl=await CIFRuntime.stagedAssetUrl('saved-file',{path:targetRel});
                const character=await BABYLON.SceneLoader.ImportMeshAsync('','',characterUrl,scene,undefined,'.glb');
            const characterNodes=scene.getNodes().filter(n=>!before.has(n));
                const groupsBefore=new Set(scene.animationGroups);
                const sourceBefore=new Set(scene.getNodes());
            const animationRel=`${animation.folder.folder}/${animation.version.file}`;
            const animationUrl=await CIFRuntime.stagedAssetUrl('saved-runtime-animation',{path:animationRel});
                await BABYLON.SceneLoader.ImportMeshAsync('','',animationUrl,scene,undefined,'.glb');
            const sourceNodes=scene.getNodes().filter(n=>!sourceBefore.has(n));
                const sourceGroups=scene.animationGroups.filter(g=>!groupsBefore.has(g));
            const result=this.RetargetAnimation(scene,character,characterNodes,sourceNodes,sourceGroups,`test-${folder.name||'asset'}`);
            try{result.playing.start(false,1,result.playing.from,result.playing.to,false);
                result.playing.goToFrame(Math.max(result.playing.from,1));
                result.playing.pause();
                } catch (_) {
                }
            const details={asset_file:version.file,animation_folder:animation.folder.folder,animation_file:animation.version.file,mapped_targets:result.mappedTargets,babylon_animator_avatar:true,pose_frame:1};
            const promoted=await this.SavedPost('/assets/saved/promote',{folder:folder.folder,success:true,details});
            this.SetProgress(card,promoted.message||this.text('asset_promoted','Promoted to Mixamo.'));
                await this.LoadSaved();
        }catch(error){
            try{await this.SavedPost('/assets/saved/promote',{folder:folder.folder,success:false,details:{error:String(error?.message||error),asset_file:version.file,animation_file:animation?.version?.file||''}});
                } catch (_) {
                }
            this.SetProgress(card,this.text('asset_test_failed','Mixamo test failed: {error}',{error:error.message||error}));
        }finally{try{scene?.dispose();
        }catch(_){}try{engine?.dispose();
        } catch (_) {
            }
        }
    }

    static NormalizeStaticPreviewUp(scene,nodes){
        const imported=new Set((Array.isArray(nodes)?nodes:[]).filter(Boolean)),meshes=Array.from(imported).filter(n=>n.getBoundingInfo&&n.getTotalVertices?.()>0);
        if (!meshes.length) {
            return null;
        }
        // Never infer an up-axis from the bounding-box proportions of a skinned
        // asset.  A bat, spider, dragon, etc. can legitimately be much wider and
        // deeper than it is tall.  More importantly, Babylon exposes root Bones
        // through scene.getNodes(); parenting one of those Bones under a preview
        // TransformNode inserts a non-Bone into the skeleton parent chain and
        // Skeleton.prepare() later fails at parent.getFinalMatrix().  glTF skins
        // already carry their authored coordinate system, so preserve that rig
        // hierarchy exactly.  Static unskinned imports retain the legacy Z-up
        // correction below.
        const hasSkin=meshes.some(mesh=>Boolean(mesh.skeleton))||
            Array.from(imported).some(node=>node?.getClassName?.()==='Bone');
        if (hasSkin) {
            return null;
        }
        let min=new BABYLON.Vector3(Infinity,Infinity,Infinity),max=new BABYLON.Vector3(-Infinity,-Infinity,-Infinity);
        for(const mesh of meshes){const b=mesh.getBoundingInfo().boundingBox;
            min=BABYLON.Vector3.Minimize(min,b.minimumWorld);
            max=BABYLON.Vector3.Maximize(max,b.maximumWorld);
            }
        const size=max.subtract(min),x=Math.max(.0001,size.x),y=Math.max(.0001,size.y),z=Math.max(.0001,size.z);
        /* Objaverse contains converted source models whose geometry is still Z-up even
           though the transport is glTF. Detect the characteristic sideways case only:
           Y is the thin footprint axis while Z carries the model's height. Do not use
           the previous 'longest axis is up' guess. */
        if (!(y<z*.72&&y<x*.72)) {
            return null;
        }
        const roots=Array.from(imported).filter(n=>!imported.has(n.parent)),root=new BABYLON.TransformNode('objaverse-preview-up-root',scene);
        for(const node of roots){try{node.setParent(root,true);
        }catch(_){node.parent=root;
        }}
        root.rotationQuaternion=BABYLON.Quaternion.RotationAxis(BABYLON.Axis.X,-Math.PI/2);
        root.computeWorldMatrix?.(true);
        for(const mesh of meshes)mesh.computeWorldMatrix?.(true);
        return root;
    }

    static async LoadCloudflareAssignmentPreview(card){
        const rel=String(card?.dataset.directCloudflarePath||'').replace(/^\/+/, '');
        if (!rel) {
            return;
        }
        if(String(card?.dataset.assetKind||'')==='image'){
            const image=card.querySelector('img.objaverse-thumb');
            if(image){image.src=CIFRuntime.requestAsset(rel);
                image.scrollIntoView({block:'nearest',inline:'nearest'});
                }
            return;
        }
        const canvas=card?.querySelector('.saved-asset-canvas');
         if (!canvas) {
             return;
         }
        const key=`cloudflare:${rel}`;
         let engine=this.engines.get(key),scene=this.scenes.get(key);
        if(engine&&engine.getRenderingCanvas?.()!==canvas){this.DisposePreviewKey(key);
        engine=null;
        scene=null;
        }
        if(!engine){
            this.DisposeCanvasPreview(canvas);
                await ensureBabylon();
                engine=new BABYLON.Engine(canvas,true,{preserveDrawingBuffer:true,stencil:false});
                scene=new BABYLON.Scene(engine);
                scene.clearColor=new BABYLON.Color4(.04,.045,.055,1);
            const camera=new BABYLON.ArcRotateCamera('camera',Math.PI/2,Math.PI/2.25,2.6,BABYLON.Vector3.Zero(),scene);
                camera.wheelPrecision=90;
                camera.panningSensibility=140;
                camera.attachControl(canvas,true);
            new BABYLON.HemisphericLight('light',new BABYLON.Vector3(.4,1,.2),scene);
            const before=new Set(scene.getNodes());
                await BABYLON.SceneLoader.ImportMeshAsync('','',CIFRuntime.requestAsset(rel),scene);
                await scene.whenReadyAsync?.();
                const imported=scene.getNodes().filter(n=>!before.has(n));
                this.NormalizeStaticPreviewUp(scene,imported);
                this.FrameScene(scene,camera);
            this.engines.set(key,engine);
            this.scenes.set(key,scene);
        }
        card.classList.add('viewer-active');
            this.ActivatePreviewEngine(key);
            requestAnimationFrame(()=>this.ResizeAndReframeCanvas(canvas));
    }

    static async LoadDirectAssignmentPreview(card){
        card?.classList.remove('blockified-preview-active');
        const rel=String(card?.dataset.directSavedPath||'').replace(/\\/g,'/').replace(/^<builds>\/InteractiveFiction\/Assets\//,'').replace(/^Builds\/InteractiveFiction\/Assets\//,'');
        const canvas=card?.querySelector('.saved-asset-canvas');
        if (!rel||!canvas) {
            return;
        }
        const key=`assigned:${rel}`;
         let engine=this.engines.get(key),scene=this.scenes.get(key);
        if(engine&&engine.getRenderingCanvas?.()!==canvas){this.DisposePreviewKey(key);
        engine=null;
        scene=null;
        }
        if(!engine){
            this.DisposeCanvasPreview(canvas);
            await ensureBabylon();
                engine=new BABYLON.Engine(canvas,true,{preserveDrawingBuffer:true,stencil:false});
                scene=new BABYLON.Scene(engine);
                scene.clearColor=new BABYLON.Color4(.04,.045,.055,1);
            const camera=new BABYLON.ArcRotateCamera('camera',Math.PI/2,Math.PI/2.25,2.6,BABYLON.Vector3.Zero(),scene);
                camera.wheelPrecision=90;
                camera.panningSensibility=140;
                camera.attachControl(canvas,true);
            canvas.addEventListener('wheel',event=>{event.preventDefault();event.stopPropagation();},{passive:false});
            new BABYLON.HemisphericLight('light',new BABYLON.Vector3(.4,1,.2),scene);
            const before=new Set(scene.getNodes());
                const url=await CIFRuntime.stagedAssetUrl('saved-file',{path:rel});
                await BABYLON.SceneLoader.ImportMeshAsync('','',url,scene);
                await scene.whenReadyAsync?.();
                const imported=scene.getNodes().filter(n=>!before.has(n));
                this.NormalizeStaticPreviewUp(scene,imported);
                this.FrameScene(scene,camera);
            this.engines.set(key,engine);
            this.scenes.set(key,scene);
        }
        card.classList.add('viewer-active');
            this.ActivatePreviewEngine(key);
            requestAnimationFrame(()=>this.ResizeAndReframeCanvas(canvas));
    }

    static async SavedPreviewLoadURL(remoteURL){
        const key=String(remoteURL||'');
        if (!key) {
            throw new Error('Saved preview asset URL is unavailable');
        }
        let pending=this.savedPreviewAssetLoads.get(key);
        if(!pending){
            pending=(async()=>{
                const response=await fetch(key,{cache:'no-store',mode:'cors'});
                if (!response.ok) {
                    throw new Error(`Saved preview fetch failed: HTTP ${response.status}`);
                }
                const blob=await response.blob();
                return URL.createObjectURL(blob);
            })().catch(error=>{this.savedPreviewAssetLoads.delete(key);throw error;});
            this.savedPreviewAssetLoads.set(key,pending);
        }
        return await pending;
    }

    static async LoadSavedPreview(card, representationPath=""){
        const folder=this.SavedFolderForCard(card),canvas=card?.querySelector('.saved-asset-canvas');
            if (!folder||!canvas) {
                return;
            }
        card.classList.remove('blockified-preview-active');
            card.classList.add('viewer-active');
            await new Promise(resolve=>requestAnimationFrame(resolve));
        const versions=folder.versions||[],index=Number(card.dataset.versionIndex||0),version=versions[index]||{},rel=String(representationPath||`${folder.folder}/${version.file||''}`).replace(/^\/+/, '');
        const signature=this.BlockifyBehaviorSignature(folder,version),key=`saved:${rel}:${signature}`;
            let engine=this.engines.get(key),scene=this.scenes.get(key);
        if(engine&&engine.getRenderingCanvas?.()!==canvas){this.DisposePreviewKey(key);
        engine=null;
        scene=null;
        }
        if(!engine){
            // A changed animation signature creates a new key for the same canvas.
            // Kill the previous canvas owner before allocating another engine.
            this.DisposeCanvasPreview(canvas);
            await ensureBabylon();
                engine=new BABYLON.Engine(canvas,true,{preserveDrawingBuffer:true,stencil:false});
                scene=new BABYLON.Scene(engine);
                scene.clearColor=new BABYLON.Color4(.04,.045,.055,1);
            let characterUrl='';
            try{
                const camera=new BABYLON.ArcRotateCamera('camera',Math.PI/2,Math.PI/2.25,2.6,BABYLON.Vector3.Zero(),scene);
                    camera.wheelPrecision=90;
                    camera.panningSensibility=140;
                    camera.attachControl(canvas,true);
                    canvas.addEventListener('wheel',event=>{event.preventDefault();
                    event.stopPropagation();
                    },{passive:false});
                    new BABYLON.HemisphericLight('light',new BABYLON.Vector3(.4,1,.2),scene);
                const before=new Set(scene.getNodes());
                    characterUrl=await CIFRuntime.stagedAssetUrl('saved-file',{path:rel});
                    const stagedPath=new URL(characterUrl,window.location.href).pathname;
                    const stagedExtension=stagedPath.toLowerCase().endsWith('.gltf')?'.gltf':'.glb';
                    let character;
                    if(stagedExtension==='.gltf'){
                        // GLTF is a representation package. Load it from its staged URL so
                        // model.bin/textures resolve beside the document; a blob URL would
                        // destroy that relative dependency authority.
                        const splitIndex=characterUrl.lastIndexOf('/');
                        const rootUrl=splitIndex>=0?characterUrl.slice(0,splitIndex+1):'';
                        const fileName=splitIndex>=0?characterUrl.slice(splitIndex+1):characterUrl;
                        character=await BABYLON.SceneLoader.ImportMeshAsync('',rootUrl,fileName,scene,undefined,'.gltf');
                    }else{
                        const previewLoadUrl=await this.SavedPreviewLoadURL(characterUrl);
                        character=await BABYLON.SceneLoader.ImportMeshAsync('','',previewLoadUrl,scene,undefined,'.glb');
                    }
                    const characterNodes=scene.getNodes().filter(n=>!before.has(n));
                    this.NormalizeStaticPreviewUp(scene,characterNodes);
                    this.FrameScene(scene,camera);
                const meshSet=new Set(character.meshes||[]),roots=(character.meshes||[]).filter(mesh=>mesh&&!meshSet.has(mesh.parent));
                if(folder.folder?.startsWith('Animated/Fixed/')&&(character.animationGroups||[]).length){
                    const entries=(character.animationGroups||[]).filter(group=>group?.targetedAnimations?.length).map(group=>this.AnimationBehaviorEntry(group,group.name));
                        this.SetupAnimationBehaviorCycle(card,scene,roots,entries,key,version.blockify?.behavior_profile||[]);
                }else if(folder.folder?.startsWith('Animated/Mixamo/')){
                    const refs=Array.isArray(folder.assigned_animations)?folder.assigned_animations.filter(row=>row?.folder&&row?.file):[];
                    if(!refs.length&&folder.default_animation?.folder&&folder.default_animation?.file)refs.push({...folder.default_animation,name:'Idle'});
                    const entries=[];
                    for(let i=0;i<refs.length;i++){
                        const ref=refs[i],groupsBefore=new Set(scene.animationGroups),sourceBefore=new Set(scene.getNodes());
                        const animationUrl=await CIFRuntime.stagedAssetUrl('saved-runtime-animation',{path:`${ref.folder}/${ref.file}`});
                            await BABYLON.SceneLoader.ImportMeshAsync('','',animationUrl,scene,undefined,'.glb');
                        const sourceNodes=scene.getNodes().filter(n=>!sourceBefore.has(n)),sourceGroups=scene.animationGroups.filter(g=>!groupsBefore.has(g));
                        const retarget=this.RetargetAnimation(scene,character,characterNodes,sourceNodes,sourceGroups,`saved-${folder.name||'asset'}-${i}`),playing=retarget.playing;
                        const name=String(ref.name||ref.file||playing.name||`Animation ${i+1}`);
                            playing.name=name;
                            entries.push(this.AnimationBehaviorEntry(playing,name));
                    }
                    if (entries.length) {
                        this.SetupAnimationBehaviorCycle(card,scene,roots,entries,key,version.blockify?.behavior_profile||[]);
                    }
                }
                this.engines.set(key,engine);
                this.scenes.set(key,scene);
            }catch(error){console.error('[IFPlayer Assets] saved-preview-failed',{folder:folder.folder,file:version.file,error:String(error?.message||error)});
                if (characterUrl) {
                    await CIFRuntime.diagnoseAssetFailure(characterUrl,error);
                }
                try{scene?.dispose?.();
                }catch(_){}try{engine?.dispose?.();
                }catch(_){}this.engines.delete(key);
                this.scenes.delete(key);
                throw error;
                }
        }
        card.classList.add('viewer-active');
            this.ActivatePreviewEngine(key);
            requestAnimationFrame(()=>this.ResizeAndReframeCanvas(canvas));
    }

    static SelectEmbeddedAnimation(key,name){const controller=this.embeddedAnimationControllers.get(String(key||''));
        if (controller?.select) {
            controller.select(String(name||''));
        }
        }

    static syncPollTimer = null;
    static syncSeenSignature = '';

    static ShowCollaborationNotice(changes,state={}){
        const rows=Array.isArray(changes)?changes:[];
        if (!rows.length) {
            return;
        }
        const authors=[...new Set(rows.map(r=>String(r.author||'Another collaborator').trim()).filter(Boolean))];
        const author=authors.length===1?authors[0]:(authors.length?'Multiple collaborators':'Another collaborator');
        const game=String(typeof activeGameKey==='function'?activeGameKey():'').trim();
        const title=String(typeof currentStoryTitle!=='undefined'?currentStoryTitle:'').trim();
        const story=title||game||'Shared content';
        const received=rows.some(r=>r.direction==='incoming');
        const pending=Boolean(state&&state.pending);
        const text=`${story} updated by ${author}${received?'':' \u2014 update available'}.${pending?' Local changes are pending; review before reloading.':''}`;
        let toast=document.getElementById('ifplayer-collaboration-notice');
        if(!toast){
            toast=document.createElement('div');
            toast.id='ifplayer-collaboration-notice';
            toast.style.cssText='position:fixed;right:18px;top:18px;z-index:2147483000;max-width:420px;padding:12px 14px;border-radius:8px;background:rgba(24,28,34,.96);color:#fff;box-shadow:0 8px 30px rgba(0,0,0,.35);font:13px/1.35 system-ui,-apple-system,sans-serif;display:flex;gap:12px;align-items:center;';
            const msg=document.createElement('div');
                msg.className='ifplayer-collaboration-message';
                msg.style.flex='1';
                toast.appendChild(msg);
            const review=document.createElement('button');
                review.type='button';
                review.textContent='Review';
                review.style.cssText='cursor:pointer;padding:5px 9px;';
            review.addEventListener('click',async()=>{
                const changes=Array.isArray(toast._changes)?[...toast._changes]:[];
                review.disabled=true;
                try{
                    window.dispatchEvent(new CustomEvent('cinterforge-shared-content-review-requested',{detail:{changes}}));
                    await this.ReviewCollaborationChanges(changes);
                    toast.remove();
                }catch(error){
                    review.disabled=false;
                    const message=`Review failed: ${String(error?.message||error)}`;
                    this.SetProgress(null,message);
                    const el=this.elements();
                    if (el.status) {
                        el.status.textContent=message;
                    }
                    console.error('[IFPlayer Sync] collaboration review failed',error);
                }
            });
            toast.appendChild(review);
            const dismiss=document.createElement('button');
                dismiss.type='button';
                dismiss.textContent='\u00d7';
                dismiss.title='Dismiss';
                dismiss.style.cssText='cursor:pointer;border:0;background:transparent;color:#fff;font-size:20px;line-height:1;';
                dismiss.addEventListener('click',()=>toast.remove());
                toast.appendChild(dismiss);
            document.body.appendChild(toast);
        }
        toast._changes=rows;
        const msg=toast.querySelector('.ifplayer-collaboration-message');
        if (msg) {
            msg.textContent=text;
        }
        if(typeof Notification!=='undefined'&&Notification.permission==='granted'){
            try{new Notification(`${story} updated`,{body:`${author} changed shared content in ${story}.`});
                } catch (_error) {
                }
        }
    }

    static async ReviewCollaborationChanges(changes){
        const rows=Array.isArray(changes)?changes:[];
        const paths=rows.map(row=>String(row?.path||'').replace(/\\/g,'/')).filter(Boolean);
        const lower=paths.map(path=>path.toLowerCase());
        const assignmentChanged=lower.some(path=>
            /(^|\/)data\/stories\/[^/]+\/(objects|rooms|characters)\.json$/.test(path) ||
            /(^|\/)(objects|rooms|characters)\.json$/.test(path)
        );
        const worldChanged=lower.some(path=>
            path.includes('/worlddata/') || path.endsWith('/map_shell.json') ||
            path.endsWith('/unified_scene.json') ||
            path.endsWith('/frobozz_map.json') || path.endsWith('/frobozz_map.glb')
        );
        // Dataset identity is authoritative here. Managed collaboration assets
        // (notably Castle Blockify voxel files) do not have to live under an
        // /assets/, objects/, images/, or animated/ path. The sync layer already
        // tells Review which dataset each installed file belongs to; preserve that
        // information instead of trying to infer it again from the path.
        const assetChanged=rows.some(row=>String(row?.dataset||'').toLowerCase()==='assets') ||
            lower.some(path=>path.includes('/assets/') || path.startsWith('objects/') || path.startsWith('images/') || path.startsWith('animated/'));

        this.SetProgress(null,'Applying received shared changes\u2026');
        const el=this.elements();
        if (el.status) {
            el.status.textContent='Applying received shared changes\u2026';
        }

        // The collaboration pull has already installed the authoritative files on
        // disk.  Review must consume those files into the live UI instead of merely
        // dismissing the notification.  These loaders read from disk/backend fresh.
        if (assetChanged || assignmentChanged) {
            await this.LoadSaved();
        }
        if (assignmentChanged || this.mode==='assign') {
            await this.LoadAssignments();
        }
        // Managed Terrain/Blockify assets are already instantiated in Build. Reload
        // only the affected instances from the just-installed files so remote voxel
        // edits become visible without disposing the scene, camera, floor state, or
        // unrelated local editor state.
        if(assetChanged && window.CIPlayerMapShell?.RefreshCollaborationAssets){
            await CIPlayerMapShell.RefreshCollaborationAssets(paths);
        }

        // Assignment and authored-world changes can affect the live Babylon world.
        // Review is the explicit user boundary at which replacing that live view is
        // expected; passive background polling must never do this behind the user.
        if((assignmentChanged || worldChanged) && !assetChanged && typeof CIFPlayerMap!=='undefined' && CIFPlayerMap?.reloadGltf){
            await CIFPlayerMap.reloadGltf();
        }

        const names=[...new Set(paths.map(path=>path.split('/').pop()).filter(Boolean))];
        const message=names.length?`Applied shared changes: ${names.slice(0,3).join(', ')}${names.length>3?'\u2026':''}`:'Shared changes applied.';
        this.SetProgress(null,message);
        if (el.status) {
            el.status.textContent=message;
        }
        window.dispatchEvent(new CustomEvent('cinterforge-shared-content-reviewed',{detail:{changes:rows,paths}}));
        return {assignmentChanged,worldChanged,assetChanged,paths};
    }

    static RefreshAssetSyncButton() {
        const button = ifGetElementById('asset-sync-button');
        if (!button) {
            return;
        }
        button.textContent = this.text('asset_sync_button', 'Sync') + (this.assetSyncDirty ? ' *' : '');
        button.dataset.dirty = this.assetSyncDirty ? '1' : '0';
    }

    static MarkAssetSyncDirty(reason='asset modification') {
        this.assetSyncDirty = true;
        this.assetSyncGeneration += 1;
        this.RefreshAssetSyncButton();
        console.info('[IFPlayer Assets] Sync dirty', { reason: String(reason || 'asset modification') });
    }

    static ClearAssetSyncDirty() {
        this.assetSyncDirty = false;
        this.RefreshAssetSyncButton();
    }

    static async SyncR2Assets(reason='IFPlayer') {
        if (this.r2AssetSyncPromise) {
            return this.r2AssetSyncPromise;
        }
        const label=String(reason||'IFPlayer').trim()||'IFPlayer';
        const syncGeneration = this.assetSyncGeneration;
        this.SetProgress(null,`${label} · synchronizing Assets with Cloudflare R2…`);
        this.r2AssetSyncPromise=(async()=>{
            const result=await CIFRuntime.assetCommand({
                operation:'asset-r2-sync',
                reason:label
            });
            if (result?.ok===false) {
                throw new Error(result.error||`${label} Asset Sync failed`);
            }
            console.info('[IFPlayer Assets] authenticated R2 sync complete',{reason:label,result});
            if (this.assetSyncGeneration === syncGeneration) {
                this.ClearAssetSyncDirty();
            }
            this.SetProgress(null,`${label} · Assets synchronized.`);
            return result;
        })();
        try {
            return await this.r2AssetSyncPromise;
        } finally {
            this.r2AssetSyncPromise=null;
        }
    }

    static async StartAutomaticSync(){
        if (this.initialSyncPromise) {
            return this.initialSyncPromise;
        }

        const authoringBase = String(window.CINTERFORGE_IF_AUTHORING_BASE || '').trim();
        if (window.CINTERFORGE_IF_RESOURCE_ONLY && !authoringBase) {
            window.CINTERFORGE_IF_BACKEND_MODE = 'javascript-only';
            this.SetLaunchSyncGate(true, 'Local Asset Library · read-only');
            this.initialSyncPromise = Promise.resolve({ state: 'javascript-only', static: true });
            return this.initialSyncPromise;
        }

        // Project launch is a local-authoring boundary. It must never perform an
        // implicit Cloudflare Pull: Save owns local WorldData, Pull is the explicit
        // Cloudflare -> local operation, and Push/Build are the publication paths.
        // Reload the local catalogs needed by Assets and leave remote reconciliation
        // entirely to the user's explicit lifecycle commands.
        this.initialSyncPromise = (async () => {
            await this.LoadSaved();
            // A source-repair Project has a valid Project identity and local Asset
            // library, but no newly compiled playable World.  Do not ask the World
            // assignment loader to hydrate a runtime that intentionally does not
            // exist yet.  Build will clear repair mode by replacing the session.
            if (this.mode === 'assign' && window.CINTERFORGE_IF_SOURCE_REPAIR_ONLY !== true) {
                await this.LoadAssignments();
            }

            window.CINTERFORGE_IF_BACKEND_MODE = 'full';
            this.SetLaunchSyncGate(false, 'Local authoring data ready.');
            console.info('[IFPlayer Sync] launch uses local authoring state; automatic R2 pull skipped');
            window.dispatchEvent(new CustomEvent('cinterforge-world-sync-ready', {
                detail: {
                    state: { state: 'local' },
                    phase: 'local-project-open'
                }
            }));
            return { state: 'local' };
        })().catch(error => {
            window.CINTERFORGE_IF_BACKEND_MODE = 'javascript-only';
            this.SetLaunchSyncGate(true, 'Authoring server unavailable · local authoring offline');
            console.warn('[IFPlayer Sync] local authoring initialization failed', String(error?.message || error));
            return { state: 'javascript-only', error: String(error?.message || error) };
        });

        return this.initialSyncPromise;
    }

    static SetLaunchSyncGate(locked, message=''){
        window.CINTERFORGE_IF_INITIAL_SYNC_PENDING=!!locked;
        // Startup reconciliation gates collaboration Sync only. The Assets browser is
        // a local/project view over the authoritative Assets library and remains
        // usable even while remote synchronization is unavailable or inconsistent.
        const sync=ifGetElementById('asset-sync-button');
        if(sync){
            sync.disabled=!!locked;
            if (locked) {
                sync.title='Requires successful startup world-data synchronization';
            }
            else if(sync.title==='Requires successful startup world-data synchronization')sync.removeAttribute('title');
        }
        if (message) {
            this.SetProgress(null,message);
        }
        window.dispatchEvent(new CustomEvent('cinterforge-initial-sync-gate',{detail:{pending:!!locked,message:String(message||'')}}));
    }

    static async SyncSavedAssets(){
        const el=this.elements();
        // Resource-only describes the staged browser presentation, not whether
        // this browser has a live authoring command plane.  Desktop IFPlayer
        // intentionally stages resource-only story/runtime files while exposing
        // the owned legacy standalone host through CINTERFORGE_IF_AUTHORING_BASE.  Only a truly
        // standalone presentation (no authoring base) must disable collaboration.
        if(window.CINTERFORGE_IF_RESOURCE_ONLY && !String(window.CINTERFORGE_IF_AUTHORING_BASE||'').trim()){
            if (el.status) {
                el.status.textContent='Sync requires a live CinterForge authoring host.';
            }
            return;
        }
        if (el.syncButton) {
            el.syncButton.disabled=true;
        }
        try{
            if (el.status) {
                el.status.textContent=this.text('asset_syncing','Starting world-data sync\u2026');
            }
            await this.SavedPost('/assets/sync',{});
            for(;;){
                const state=await CIFRuntime.assetCommand({operation:'sync-status'});
                const message=String(state.message||'Syncing world data\u2026');
                const percent=Number.isFinite(Number(state.percent))?Math.max(0,Math.min(100,Number(state.percent))):0;
                const shown=percent>0&&String(state.state||'')==='running'?`${message} ${Math.round(percent)}%`:message;
                if (el.status) {
                    el.status.textContent=shown;
                }
                this.SetProgress(null,shown);
                if(state.state==='complete'){
                    await this.SyncR2Assets('Sync');
                    await this.LoadSaved();
                    if (this.mode==='assign') {
                        await this.LoadAssignments();
                    }
                    if (el.status) {
                        el.status.textContent=this.text('asset_sync_complete','World-data sync complete.');
                    }
                    break;
                }
                if (state.state==='failed') {
                    throw new Error(state.error||state.message||'Asset sync failed');
                }
                await new Promise(resolve=>setTimeout(resolve,300));
            }
        }catch(error){if(el.status) el.status.textContent=this.text('asset_sync_failed','Sync failed: {error}',{error:error.message||error});
            }
        finally{if(el.syncButton) el.syncButton.disabled=false;
        }
    }

    static SetProgress(card, text) {
        const message = String(text || '');
        const footer = ifGetElementById('bottom-response');
        if (footer) {
            footer.textContent = message;
        }
        const status = ifGetElementById('objaverse-status');
        if (status) {
            status.title = message;
        }
    }

    static async RunObjaverseSavedAction(card,action) {
        const state=await this.StartTask(card,'download'), rel=String(state?.relative_path||'').replace(/\\/g,'/');
        if (!rel) {
            throw new Error('Objaverse download did not return a saved asset path.');
        }
        this.savedLibrary=await CIFRuntime.assetCommand({operation:'saved-list'});
        const parts=rel.split('/');
         parts.pop();
         const folderPath=parts.join('/');
        const all=[...(this.savedLibrary?.groups?.Objects||[]),...(this.savedLibrary?.groups?.Animated?.Fixed||[]),...(this.savedLibrary?.groups?.Animated?.Mixamo||[])];
        const folder=all.find(row=>String(row.folder||'')===folderPath);
        if (!folder) {
            throw new Error(`Saved Objaverse folder was not found: ${folderPath}`);
        }
        const versionIndex=Math.max(0,(folder.versions||[]).findIndex(version=>rel.endsWith('/'+String(version.file||''))));
        card.dataset.folder=folderPath;
            card.dataset.versionIndex=String(versionIndex);
            card.classList.add('saved-asset-card');
        const canvas=card.querySelector('.objaverse-canvas');
        if (canvas) {
            canvas.classList.add('saved-asset-canvas');
        }
        if (!card.querySelector('.asset-blockify-dimension')) {
            card.insertAdjacentHTML('beforeend','<div hidden><input class="asset-blockify-dimension" data-axis="width" value="1"><input class="asset-blockify-dimension" data-axis="height" value="1"><input class="asset-blockify-dimension" data-axis="depth" value="1"></div>');
        }
        await this.EnsureBlockified(card);
        if (action==='export') {
            await this.ExportBlockifiedAddon(card);
        }
        else await this.LoadBlockifiedPreview(card);
    }

    static async RemoveObjaverseAsset(card) {
        const uid=String(card?.dataset?.uid||'');
        this.savedLibrary=await CIFRuntime.assetCommand({operation:'saved-list'});
        const all=[...(this.savedLibrary?.groups?.Objects||[]),...(this.savedLibrary?.groups?.Animated?.Fixed||[]),...(this.savedLibrary?.groups?.Animated?.Mixamo||[])];
        const folder=all.find(row=>(row.versions||[]).some(version=>String(version.asset_id||version.metadata?.asset_id||version.metadata?.uid||'')===uid));
        if(!folder){this.SetProgress(card,'Asset is not saved locally.');
        return;
        }
        for(const version of [...(folder.versions||[])])await this.SavedPost('/assets/saved/delete',{folder:folder.folder,file:version.file});
        this.savedLibrary=await CIFRuntime.assetCommand({operation:'saved-list'});
        this.SetProgress(card,'Removed local Objaverse asset.');
    }

    static async StartTask(card, mode) {
        const uid = String(card.dataset.uid || '');
        this.SetProgress(card, mode === 'download' ? this.text('objaverse_downloading','Downloading\u2026') : this.text('objaverse_preparing_preview','Preparing 3D preview\u2026'));
        await CIFRuntime.assetCommand({operation:'download',provider:'objaverse',id:uid,mode,q:this.query});
        return this.WaitForTask(card, mode);
    }

    static WaitForTask(card, mode) {
        const uid = String(card.dataset.uid || '');
        const existing = this.pollers.get(uid);
        if (existing) {
            clearInterval(existing);
        }
        return new Promise((resolve, reject) => {
            const check = async () => {
                try {
                    const state = await CIFRuntime.assetCommand({operation:'status',provider:'objaverse',id:uid});
                    let text = state.message || state.stage || 'Working\u2026';
                    if (state.elapsed_seconds && !state.complete) text += ` \u00b7 ${Number(state.elapsed_seconds).toFixed(1)}s`;
                    if (state.saved_path) text += ` \u00b7 ${state.saved_path}`;
                    this.SetProgress(card, text);
                    if (state.error) { clearInterval(this.pollers.get(uid));
                        this.pollers.delete(uid);
                        reject(new Error(state.error));
                        return;
                        }
                    if ((mode === 'preview' && state.model_ready) || (mode === 'download' && state.saved_path)) {
                        clearInterval(this.pollers.get(uid));
                        this.pollers.delete(uid);
                        if(mode==='download'){
                            try{resolve(await this.ConvertDownloadedAsset(state,'3d',card));}
                            catch(error){reject(error);}
                        }else resolve(state);
                    }
                } catch (error) {
                    clearInterval(this.pollers.get(uid));
                     this.pollers.delete(uid);
                     reject(error);
                }
            };
            check();
            this.pollers.set(uid, setInterval(check, 600));
        });
    }

    static async LoadAnimationPreview(card) {
        const motionId=String(card.dataset.assetId||'');
        const characterId=String(this.selectedCharacterId||this.elements().characterPicker?.value||'');
        if (!motionId || !characterId) {
            throw new Error(this.text('asset_animation_character_required','Select a Mixamo character first.'));
        }
        const key=`animation:${motionId}:${characterId}`;
        if (this.engines.has(key)) {
            this.ActivatePreviewEngine(key);
            card.classList.add('viewer-active');
            const preview=this.animationPreviews.get(key);
            if(preview?.playing){
                try { preview.playing.stop();
                 } catch (_) {
                 }
                try { preview.playing.loopAnimation=false;
                 } catch (_) {
                 }
                (preview.character?.skeletons||[]).forEach(skeleton=>{ try { skeleton.returnToRest(); } catch(_){} });
                preview.playing.start(false,1.0,preview.playing.from,preview.playing.to,false);
                const previewButton=card.querySelector('[data-action="animation-preview"]');
                this.SetProgress(card,this.text('asset_animation_preview_ready','Playing on selected character'));
            }
            return;
        }
        this.SetProgress(card,this.text('asset_animation_preparing','Preparing animation preview\u2026'));
        await ensureBabylon();
        if (!window.BABYLON) {
            throw new Error('Babylon.js is unavailable.');
        }
        const canvas=card.querySelector('.asset-animation-canvas');
        if (!canvas) {
            throw new Error('Animation preview canvas is unavailable.');
        }
        card.classList.add('viewer-active');
        const engine=new BABYLON.Engine(canvas,true,{preserveDrawingBuffer:false,stencil:true,adaptToDeviceRatio:true});
        const scene=new BABYLON.Scene(engine);
         this.engines.set(key,engine);
         this.scenes.set(key,scene);
        scene.clearColor=new BABYLON.Color4(.39,.68,.93,1);
        scene.ambientColor=new BABYLON.Color3(.48,.48,.48);
        scene.imageProcessingConfiguration.exposure=1.38;
        scene.imageProcessingConfiguration.contrast=1.04;
        const camera=new BABYLON.ArcRotateCamera(`mixamo-camera-${motionId}`,-Math.PI/2,Math.PI/2.35,2.5,new BABYLON.Vector3(0,1,0),scene);
        camera.minZ=.01;
        camera.lowerRadiusLimit=.22;
         camera.upperRadiusLimit=12;
        camera.angularSensibilityX=1800;
         camera.angularSensibilityY=1800;
        camera.wheelPrecision=90;
         camera.panningSensibility=140;
        camera.attachControl(canvas,false);
        canvas.addEventListener('wheel', event => event.preventDefault(), {passive:false});
        const hemi=new BABYLON.HemisphericLight(`mixamo-hemi-${motionId}`,new BABYLON.Vector3(0,1,0),scene);
        hemi.intensity=1.9;
        hemi.diffuse=new BABYLON.Color3(1.0,1.0,1.0);
        hemi.groundColor=new BABYLON.Color3(.56,.56,.56);
        const keyLight=new BABYLON.DirectionalLight(`mixamo-key-${motionId}`,new BABYLON.Vector3(-.45,-1,.35),scene);
        keyLight.intensity=1.55;
        const fillLight=new BABYLON.PointLight(`mixamo-fill-${motionId}`,new BABYLON.Vector3(2.5,3,-2.5),scene);
        fillLight.intensity=1.05;
        const base='';
        const beforeNodes=new Set(scene.getNodes());
        const characterUrl=await CIFRuntime.stagedAssetUrl('preview-model',{kind:'3d',id:characterId});
            const character=await BABYLON.SceneLoader.ImportMeshAsync('','',characterUrl,scene,undefined,'.glb');
        const characterNodes=scene.getNodes().filter(node=>!beforeNodes.has(node));
        const targetByName=new Map();
        const normalize=name=>String(name||'').replace(/^mixamorig[:_]?/i,'').replace(/[^a-z0-9]/gi,'').toLowerCase();
        const rememberTarget=target=>{
            if (!target) {
                return;
            }
            const raw=String(target.name||target.id||'');
            if (raw) {
                targetByName.set(raw,target);
            }
            const normalized=normalize(raw);
            if (normalized) {
                targetByName.set(normalized,target);
            }
        };
        characterNodes.forEach(rememberTarget);
        // glTF skin animation frequently targets Babylon Bone objects rather than
        // TransformNodes. Include both so converted Mixamo animation groups can be
        // retargeted onto the selected character instead of reporting success while
        // the visible mesh remains in its bind pose.
        (character.skeletons||[]).forEach(skeleton=>(skeleton.bones||[]).forEach(rememberTarget));
        const characterGroups=new Set(scene.animationGroups);
        const sourceBefore=new Set(scene.getNodes());
        const motionUrl=await CIFRuntime.stagedAssetUrl('preview-model',{kind:'animation',id:motionId});
            await BABYLON.SceneLoader.ImportMeshAsync('','',motionUrl,scene,undefined,'.glb');
        const sourceNodes=scene.getNodes().filter(node=>!sourceBefore.has(node));
        const sourceGroups=scene.animationGroups.filter(group=>!characterGroups.has(group));
        if (!sourceGroups.length) {
            throw new Error(this.text('asset_animation_retarget_failed','Converted Mixamo asset contains no Babylon AnimationGroup.'));
        }

        // AnimatorAvatar samples the CURRENT source-node transforms as the source
        // reference pose.  Frame zero is not guaranteed to be the bind/rest pose
        // for Mixamo clips, so never use goToFrame(0/from) as a rest-pose reset.
        // Capture the freshly imported GLB transform state before touching any
        // AnimationGroup and restore that exact authored state immediately before
        // retargeting.  This prevents reference-pose compensation from stretching
        // limbs/fingers on characters whose proportions differ from the motion rig.
        const sourceRestPose=new Map();
        sourceNodes.forEach(node=>{
            if (!node) {
                return;
            }
            sourceRestPose.set(node,{
                position:node.position?.clone?.()||null,
                scaling:node.scaling?.clone?.()||null,
                rotation:node.rotation?.clone?.()||null,
                rotationQuaternion:node.rotationQuaternion?.clone?.()||null
            });
        });
        const restoreSourceRestPose=()=>{
            sourceGroups.forEach(group=>{ try { group.stop(); } catch(_){} });
            sourceRestPose.forEach((pose,node)=>{
                try { if(pose.position&&node.position) node.position.copyFrom(pose.position);
                 } catch (_) {
                 }
                try { if(pose.scaling&&node.scaling) node.scaling.copyFrom(pose.scaling);
                 } catch (_) {
                 }
                try {
                    if(pose.rotationQuaternion){
                        if (!node.rotationQuaternion) {
                            node.rotationQuaternion=pose.rotationQuaternion.clone();
                        }
                        else node.rotationQuaternion.copyFrom(pose.rotationQuaternion);
                    } else if(node.rotationQuaternion) node.rotationQuaternion=null;
                } catch (_) {
                }
                try { if(pose.rotation&&node.rotation) node.rotation.copyFrom(pose.rotation);
                 } catch (_) {
                 }
                try { node.computeWorldMatrix?.(true);
                 } catch (_) {
                 }
            });
        };

        // Babylon 9 has a first-class humanoid retargeter.  Use it instead of
        // cloning AnimationGroup targets by hand: glTF animation channels target
        // TransformNodes, while the destination avatar is driven by its skeleton
        // and linked transform nodes. AnimatorAvatar handles that boundary and
        // compensates the source/target rest poses.
        const targetRawByNormalized=new Map();
        const rememberTargetName=target=>{
            if (!target) {
                return;
            }
            const raw=String(target.name||target.id||'');
            const normalized=normalize(raw);
            if (raw && normalized && !targetRawByNormalized.has(normalized)) {
                targetRawByNormalized.set(normalized,raw);
            }
        };
        characterNodes.forEach(rememberTargetName);
        (character.skeletons||[]).forEach(skeleton=>{
            try { skeleton.returnToRest();
             } catch (_) {
             }
            (skeleton.bones||[]).forEach(bone=>{
                rememberTargetName(bone);
                try { rememberTargetName(bone.getTransformNode?.());
                 } catch (_) {
                 }
            });
        });

        restoreSourceRestPose();

        const sourceGroup=sourceGroups.find(group=>group.targetedAnimations?.length) || sourceGroups[0];
        // Retarget only the shared humanoid body skeleton. Character GLBs can carry
        // authored eye/eyelid/face helper bones whose names happen to match channels
        // in an animation GLB; mapping those channels lets a body motion overwrite
        // the character's facial rest pose (Abe's eyes are the visible failure case).
        // Keep AnimatorAvatar's retargeting intact and narrow only the map supplied
        // to it so character-specific facial rigging remains authored by the GLB.
        const isHumanoidAnimationNode=name=>{
            const n=normalize(name);
            if (!n) {
                return false;
            }
            if (/^(hips|pelvis|root)$/.test(n)) {
                return true;
            }
            if (/^spine\d*$/.test(n) || /^(chest|upperchest|neck|head)$/.test(n)) {
                return true;
            }
            if (/^(left|right)(shoulder|arm|upperarm|forearm|lowerarm|hand|upleg|upperleg|leg|lowerleg|foot|toe|toebase)$/.test(n)) {
                return true;
            }
            // Mixamo finger chains: LeftHandThumb1, RightHandIndex2, etc.
            if (/^(left|right)hand(thumb|index|middle|ring|pinky|little)\d*$/.test(n)) {
                return true;
            }
            return false;
        };
        const mapNodeNames=new Map();
        let mappedTargets=0;
        const filteredSourceGroup=new BABYLON.AnimationGroup(`${sourceGroup.name||'Mixamo'}-humanoid-source`,scene);
        for(const targeted of (sourceGroup.targetedAnimations||[])){
            const sourceName=String(targeted?.target?.name||targeted?.target?.id||'');
            if (!isHumanoidAnimationNode(sourceName)) {
                continue;
            }
            const targetName=targetRawByNormalized.get(normalize(sourceName));
            if (!sourceName || !targetName) {
                continue;
            }

            // Body retargeting should rotate articulated bones.  Mixamo root/hips
            // may additionally translate.  Bone-local position/scaling channels on
            // fingers/limbs are deliberately excluded: applying source proportions
            // to a differently proportioned target is exactly what creates the
            // long-finger/long-limb distortion seen in preview.
            const property=String(targeted?.animation?.targetProperty||'').toLowerCase();
            const normalizedSource=normalize(sourceName);
            const isRoot=/^(hips|pelvis|root)$/.test(normalizedSource);
            const supportedRotation=property==='rotationquaternion'||property==='rotation';
            const supportedRootPosition=isRoot && property==='position';
            if (!supportedRotation && !supportedRootPosition) {
                continue;
            }

            filteredSourceGroup.addTargetedAnimation(targeted.animation,targeted.target);
            mapNodeNames.set(sourceName,targetName);
            mappedTargets += 1;
        }
        if(!mappedTargets || !filteredSourceGroup.targetedAnimations.length) {
            try { filteredSourceGroup.dispose();
             } catch (_) {
             }
            throw new Error(this.text('asset_animation_retarget_failed','Animation bones did not match the selected character rig.'));
        }
        if (typeof BABYLON.AnimatorAvatar !== 'function') {
            throw new Error('Babylon AnimatorAvatar retargeting API is unavailable.');
        }

        restoreSourceRestPose();
        (character.skeletons||[]).forEach(skeleton=>{ try { skeleton.returnToRest(); } catch(_){} });
        const avatarRoot=(character.meshes||[])[0] || characterNodes[0];
        if (!avatarRoot) {
            throw new Error('Selected character has no Babylon avatar root.');
        }
        const avatar=new BABYLON.AnimatorAvatar(`mixamo-avatar-${characterId}`,avatarRoot);
        avatar.showWarnings=true;
        const playing=avatar.retargetAnimationGroup(filteredSourceGroup,{
            animationGroupName:`${sourceGroup.name||'Mixamo'}-${characterId}`,
            retargetAnimationKeys:true,
            fixAnimations:true,
            checkHierarchy:false,
            fixRootPosition:false,
            fixGroundReference:false,
            mapNodeNames
        });
        try { filteredSourceGroup.dispose();
         } catch (_) {
         }
        if (!playing || !playing.targetedAnimations?.length) {
            throw new Error(this.text('asset_animation_retarget_failed','Animation bones did not match the selected character rig.'));
        }

        // The source hierarchy is needed while retargeting samples its rest pose,
        // but should never be visible in the card once the destination animation
        // has been built.
        sourceGroups.forEach(group=>{ try { group.stop(); } catch(_){} });
        sourceNodes.forEach(node=>{ if ('setEnabled' in node) try { node.setEnabled(false); } catch(_){} });
        scene.animationPropertiesOverride=new BABYLON.AnimationPropertiesOverride();
        scene.animationPropertiesOverride.enableBlending=true;
        scene.animationPropertiesOverride.blendingSpeed=.12;
        const previewButton=card.querySelector('[data-action="animation-preview"]');
        playing.loopAnimation=false;
        playing.onAnimationGroupEndObservable.add(()=>{
            try { playing.loopAnimation=false;
             } catch (_) {
             }
            try { playing.stop();
             } catch (_) {
             }
            (character.skeletons||[]).forEach(skeleton=>{ try { skeleton.returnToRest(); } catch(_){} });
            if (previewButton) {
                previewButton.disabled=false;
            }
            this.SetProgress(card,this.text('asset_animation_preview_ready','Preview ready'));
        });
        playing.start(false,1.0,playing.from,playing.to,false);
        this.animationPreviews.set(key,{engine,scene,character,playing,motionId,characterId});
        card.dataset.animationPreviewKey=key;
        const meshes=character.meshes.filter(mesh=>{try{return mesh.getTotalVertices()>0;}catch(_){return false;}});
        if(meshes.length){
            let min=new BABYLON.Vector3(Infinity,Infinity,Infinity),max=new BABYLON.Vector3(-Infinity,-Infinity,-Infinity);
            meshes.forEach(mesh=>{const box=mesh.getBoundingInfo().boundingBox;
                min=BABYLON.Vector3.Minimize(min,box.minimumWorld);
                max=BABYLON.Vector3.Maximize(max,box.maximumWorld);
                });
            const center=min.add(max).scale(.5),diag=max.subtract(min).length();
            camera.setTarget(center);
             camera.radius=Math.max(diag*1.15,1.5);
            camera.lowerRadiusLimit=Math.max(diag*.16,.18);
            const groundSize=Math.max(diag*2.4,4.5);
            const ground=BABYLON.MeshBuilder.CreateGround(`mixamo-ground-${motionId}`,{width:groundSize,height:groundSize,subdivisions:1},scene);
            ground.position.y=min.y-.018;
            const groundMat=new BABYLON.StandardMaterial(`mixamo-ground-material-${motionId}`,scene);
            const grid=new BABYLON.DynamicTexture(`mixamo-ground-grid-${motionId}`,{width:512,height:512},scene,false);
            const ctx=grid.getContext();
            ctx.fillStyle='#a5a5a5';
             ctx.fillRect(0,0,512,512);
            ctx.strokeStyle='rgba(105,105,105,.42)';
             ctx.lineWidth=2;
            for(let i=0;i<=512;i+=64){ctx.beginPath();
                ctx.moveTo(i,0);
                ctx.lineTo(i,512);
                ctx.stroke();
                ctx.beginPath();
                ctx.moveTo(0,i);
                ctx.lineTo(512,i);
                ctx.stroke();
                }
            grid.update(false);
             grid.uScale=4;
             grid.vScale=4;
            groundMat.diffuseTexture=grid;
                groundMat.specularColor=new BABYLON.Color3(.08,.08,.08);
                groundMat.roughness=.9;
            ground.material=groundMat;
             ground.receiveShadows=true;
        }
        this.ActivatePreviewEngine(key);
        if (window.ResizeObserver) {
            new ResizeObserver(()=>engine.resize()).observe(canvas);
        }
        this.SetProgress(card,this.text('asset_animation_preview_ready','Playing on selected character'));
    }

    static async EnsureBabylonSerializer() {
        await ensureBabylon();
        const version=String(BABYLON?.Engine?.Version||BABYLON?.Engine?.version||'');
        if (version!=='9.26.1') {
            throw new Error(`CinterForge requires shared Babylon 9.26.1; loaded ${version||'none'}`);
        }
        if (!BABYLON.GLTF2Export) {
            throw new Error('Shared Babylon 9.26.1 serializer is unavailable.');
        }
    }

    static async DownloadRPGAnimation(card) {
        let key=String(card.dataset.animationPreviewKey||'');
        if(!key || !this.animationPreviews.has(key)){ await this.LoadAnimationPreview(card);
            key=String(card.dataset.animationPreviewKey||'');
            }
        const preview=this.animationPreviews.get(key);
        if (!preview) {
            throw new Error(this.text('asset_animation_preview_required','Preview could not be prepared for RPG export.'));
        }
        this.SetProgress(card,this.text('asset_animation_exporting_rpg','Building RPG GLB\u2026'));
        await this.EnsureBabylonSerializer();
        const safe=(String(preview.characterId)+'-'+String(preview.motionId)).replace(/[^a-zA-Z0-9._-]+/g,'-').slice(0,120);
        const glb=await BABYLON.GLTF2Export.GLBAsync(preview.scene,safe,{shouldExportNode:node=>node && node.isEnabled && node.isEnabled()});
        glb.downloadFiles();
        this.SetProgress(card,this.text('asset_animation_rpg_ready','RPG GLB downloaded \u00b7 character + animation'));
    }

    static async LoadPreview(card){
        return await window.CIFAssetPreview.LoadPreview.call(this, card);
    }
}


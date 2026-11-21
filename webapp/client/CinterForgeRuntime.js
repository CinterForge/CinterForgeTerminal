/*
 * CINTERFORGE MODULE PURPOSE
 * IFPlayer presentation controller: CIFRuntime
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
// CIFRuntime
// ---------------------------------------------------------------------------
// Browser-side transport and runtime-selection layer for IFPlayer.  It resolves
// control ports and asset roots, selects the active/viewed game in all-mode,
// performs HTTP requests to individual IFPlayer child runtimes, and maintains
// the lightweight unified-world actor markers needed by the presentation.
//
// This class is intentionally a transport adapter, not a game-state owner.  The
// Z-machine/runtime remains authoritative.  Methods here translate presentation
// intent (view this actor, post this command, fetch this asset) into the correct
// per-game endpoint without changing canonical game topology or solver state.
//
// Keeping endpoint discovery and cross-game routing here prevents panels, maps,
// and Lobby code from embedding port/path assumptions and makes all-mode and
// single-game mode share one well-defined runtime access layer.
//
//=============================================================================

// When CinterForge owns the static host, mirror browser console output into the
// launching terminal. Generic static publications have no HOST_BASE and remain
// completely browser-only. This is diagnostics transport, never gameplay state.
window.CINTERFORGE_IF_HOST_API_READY=false;
window.CINTERFORGE_IF_HOST_API_INIT=(async()=>{
    const base=String(window.CINTERFORGE_IF_HOST_BASE || '').trim().replace(/\/$/, '');
    // A standalone Pack advertises no host at all.  Resolve locally without a
    // capability probe so Play never generates speculative API traffic.
    if (!base) {
        return false;
    }
    // The identity probe is the one permitted initialization request.  Every
    // authoring/collaboration transport remains hard-off until it succeeds.
    try {
        const identity=await fetch(`${base}/runtime-identity`,{cache:'no-store'});
        const payload=await identity.json().catch(()=>({}));
        if (!identity.ok || payload.ok!==true || payload.service!=='cinterforge-ifplayer') {
            return false;
        }
        window.CINTERFORGE_IF_HOST_API_READY=true;
        return true;
    } catch (_) { return false;
     }
})();

(async function installIFPlayerTerminalConsoleMirror(){
    // Standalone Terminal Asset Browser uses the authoring command API, not
    // IFPlayer's game console/PTY. Its host has no /host/terminal-command.
    if (window.CINTERFORGE_IF_ASSET_WORKSPACE === true) return;
    if (window.__CINTERFORGE_IF_CONSOLE_MIRROR_INSTALLED) {
        return;
    }
    const base=String(window.CINTERFORGE_IF_HOST_BASE || '').trim().replace(/\/$/, '');
    if (!base) {
        return;
    }
    if (!await window.CINTERFORGE_IF_HOST_API_INIT) {
        return;
    }
    window.__CINTERFORGE_IF_CONSOLE_MIRROR_INSTALLED=true;
    const transport={stopped:false, intervalId:null, originals:{}, terminalAbort:null};
    window.__CINTERFORGE_IF_HOST_TRANSPORT=transport;
    const stopHostTransport=()=>{
        if (transport.stopped) {
            return;
        }
        transport.stopped=true;
        transport.intervalId=null;
        try {
            transport.terminalAbort?.abort();
        } catch (_) {
        }
        transport.terminalAbort=null;
        for (const [level, original] of Object.entries(transport.originals)) {
            try {
                console[level]=original;
            } catch (_) {
            }
        }
    };
    const stringify=value=>{
        if (value instanceof Error) {
            return `${value.name}: ${value.message}${value.stack ? `\n${value.stack}` : ''}`;
        }
        if (typeof value === 'string') {
            return value;
        }
        try { return JSON.stringify(value);
         } catch (_) { return String(value);
         }
    };
    for (const level of ['log','info','debug','warn','error']) {
        const original=console[level]?.bind(console);
        if (!original) {
            continue;
        }
        transport.originals[level]=original;
        console[level]=(...args)=>{
            // Full per-turn interpreter diagnostics belong in the launch
            // terminal by default. Keep Safari/Chromium DevTools readable; a
            // developer can opt back into browser debug noise explicitly.
            if (level !== 'debug' || window.CINTERFORGE_IF_VERBOSE_BROWSER_CONSOLE === true) {
                original(...args);
            }
            const message=args.map(stringify).join(' ');
            if (!transport.stopped) {
                fetch(`${base}/host/console`, {
                    method:'POST',
                    cache:'no-store',
                    headers:{'Content-Type':'application/json'},
                    body:JSON.stringify({level,message})
                }).catch(()=>stopHostTransport());
            }
        };
    }
    console.info('[ifplayer-terminal] browser console mirrored to launch terminal');
    // Mirror every authoritative browser Z-machine turn to the embedded Code PTY.
    // This event is emitted by the shared session recorder for Player and Lead, so
    // Code sees the exact Session-panel history without creating another interpreter.
    window.addEventListener('cinterforge-zmachine-session-state', event=>{
        const playerSession=event && event.detail && event.detail.player_session;
        if (!playerSession) {
            return;
        }
        if (!transport.stopped) fetch(`${base}/host/player-session`, {method:'POST', cache:'no-store', headers:{'Content-Type':'application/json'}, body:JSON.stringify({player_session:playerSession})}).catch(()=>{});
    });
    // Optional terminal prompt transport. Text is queued by the owned static host,
    // but command execution remains in this browser and therefore in the same
    // Bocfel/session authority as the Session panel. Human terminal input takes
    // control from Lead before issuing the command.
    let terminalPollBusy=false;
    const pollTerminalCommands=async()=>{
        while(!transport.stopped){
            if(terminalPollBusy) { await new Promise(resolve=>window.setTimeout(resolve,25));
             continue;
             }
            terminalPollBusy=true;
            try {
                const abort=new AbortController();
                transport.terminalAbort=abort;
                // The host GET is a long-poll: idle authoring produces at most one
                // request every 25 seconds instead of a permanent 4 Hz heartbeat.
                const response=await fetch(`${base}/host/terminal-command`, {cache:'no-store',signal:abort.signal});
                if (!response.ok) {
                    continue;
                }
                const payload=await response.json();
                const command=String(payload.command || '').trim();
                const requestId=String(payload.request_id || '').trim();
                if (!command) {
                    continue;
                }
                if (typeof window.cinterZMachineLeadStop === 'function') {
                    await window.cinterZMachineLeadStop();
                }
                console.info(`[terminal] > ${command}`);
                let result=null, terminalError='';
                try { result=await CIFRuntime.playerCommand(command);
                 }
                catch (error) { terminalError=String(error && error.message || error || 'IF command failed');
                    throw error;
                    }
                finally {
                    if (requestId) {
                        const session=(result && result.player_session) || {};
                        fetch(`${base}/host/terminal-result`, {method:'POST', cache:'no-store', headers:{'Content-Type':'application/json'}, body:JSON.stringify({request_id:requestId,ok:!terminalError,player_session:session,error:terminalError})}).catch(()=>{});
                    }
                }
                const text=String((result && result.player_session && result.player_session.last_response) || '');
                if (text) console.info(`[terminal] ${text}`);
            } catch (error) {
                if (!transport.stopped && error?.name !== 'AbortError') {
                    const originalError=transport.originals.error;
                    if (originalError) {
                        originalError('[ifplayer-terminal] host transport unavailable; stopping terminal/console transport', error);
                    }
                    stopHostTransport();
                }
            } finally {
                transport.terminalAbort=null;
                terminalPollBusy=false;
            }
        }
    };
    transport.intervalId={active:true};
    void pollTerminalCommands();
    window.CinterForgeIFPlayerStopHostTransport=stopHostTransport;
})();

class CIFRuntime {

    /**
     * runtimeControlPort()
     *
     * Implements runtime control port for runtime routing and transport; this method is part of the class-owned presentation contract.
     *
     * Side effects: Reads runtime/bootstrap state and may update presentation caches; canonical interpreter state remains external.
     *
    * @returns {*} The resolved/derived value for this presentation operation.
     */
    static runtimeControlPort() {
        // Browser HTTP traffic has one local authority: the IFPlayer host.
        // Legacy per-story/control ports may still be present in bootstrap state
        // for process bookkeeping, but browser code must never connect to them.
        const raw=String(window.CINTERFORGE_IF_AUTHORING_BASE||'').trim();
        if (!raw) {
            return 0;
        }
        try {
            const url=new URL(raw, window.location?.href||undefined);
            const port=Number(url.port||((url.protocol==='https:')?443:80));
            return Number.isInteger(port)&&port>0?port:0;
        } catch(_) { return 0;
         }
    }

    /**
     * runtimeAssetBase()
     *
     * Implements runtime asset base for runtime routing and transport; this method is part of the class-owned presentation contract.
     *
     * Side effects: Reads runtime/bootstrap state and may update presentation caches; canonical interpreter state remains external.
     *
    * @returns {*} The resolved/derived value for this presentation operation.
     */
    static runtimeAssetBase() {
        if (window.CINTERFORGE_IF_API_READY !== true) {
            return '';
        }
        if (window.CINTERFORGE_IF_RESOURCE_ONLY) {
            return '';
        }
        return String(window.CINTERFORGE_IF_AUTHORING_BASE||'').trim().replace(/\/$/, '');
    }


    static async InitializeAPI() {
        window.CINTERFORGE_IF_API_READY=false;
        if (window.CINTERFORGE_IF_RESOURCE_ONLY) {
            return false;
        }
        const base=String(window.CINTERFORGE_IF_AUTHORING_BASE||'').trim().replace(/\/$/, '');
        if (!base) {
            return false;
        }
        try {
            const response=await fetch(`${base}/runtime-identity`,{cache:'no-store'});
            const payload=await response.json().catch(()=>({}));
            if (!response.ok || payload.ok!==true || payload.service!=='cinterforge-ifplayer') {
                return false;
            }
            window.CINTERFORGE_IF_API_READY=true;
            return true;
        } catch (_) { return false;
         }
    }

    /** Relative staged asset URL. Binary/image asset references never encode a transient control port. */
    static assetRelativePath(path) {
        let value=String(path||'').trim().replace(/\\/g,'/');
        if (!value) {
            throw new Error('Shared asset path is required.');
        }
        // Managed WorldData/library references are relative.  Legacy prefixes are
        // accepted only while old projects are migrated; they are never emitted.
        const prefixes=['<builds>/Assets/','Builds/Assets/','/Assets/','Assets/'];
        for(const prefix of prefixes){ if(value.startsWith(prefix)){ value=value.slice(prefix.length);
         break;
         } }
        if (/^(?:https?:|file:|blob:|data:)/i.test(value)) {
            throw new Error(`Managed asset request must be a relative path: ${path}`);
        }
        value=value.replace(/^\/+/, '');
        if (!value || value.split('/').includes('..')) {
            throw new Error(`Invalid shared asset path: ${path}`);
        }
        return value;
    }

    static async diagnoseAssetFailure(url, error = null) {
        const requestedURL=String(url||'').trim();
        if (!requestedURL) {
            return;
        }
        const page={href:String(window.location?.href||''),origin:String(window.location?.origin||'')};
        const report={page,requestedURL,error:error instanceof Error?`${error.name}: ${error.message}`:String(error||'')};
        // Emit before either probe.  A CORS-blocked fetch must never hide the fact
        // that the exact Babylon failure path reached diagnostics.
        console.error('[asset-cors-diagnostic] start',report);
        try {
            // Match a GLB browser transaction more closely than HEAD.  Range keeps
            // the diagnostic bounded while exercising GET/Range CORS semantics.
            const response=await fetch(requestedURL,{method:'GET',cache:'no-store',headers:{Range:'bytes=0-0'}});
            const headers={};
            for(const [key,value] of response.headers.entries()) headers[key]=value;
            report.browserProbe={ok:response.ok,status:response.status,statusText:response.statusText,type:response.type,url:response.url,headers};
            try { await response.body?.cancel?.();
             } catch (_) {
             }
        } catch (probeError) {
            report.browserProbe={blocked:true,error:probeError instanceof Error?`${probeError.name}: ${probeError.message}`:String(probeError)};
        }
        console.error('[asset-cors-diagnostic]',report);
        const base=String(window.CINTERFORGE_IF_HOST_BASE||window.location?.origin||'').trim().replace(/\/$/,'');
        if(base){
            try {
                // Do not gate the independent server probe on host-init state: the
                // diagnostic endpoint itself is the evidence we need when Safari
                // rejects the canonical cross-origin transaction.
                const response=await fetch(`${base}/host/asset-cors-diagnostic`,{method:'POST',cache:'no-store',headers:{'Content-Type':'application/json'},body:JSON.stringify({url:requestedURL,origin:page.origin})});
                const payload=await response.json().catch(()=>({}));
                console.error('[asset-cors-server-diagnostic]',payload);
            } catch (serverError) {
                console.error('[asset-cors-server-diagnostic] failed',serverError);
            }
        }
    }

    static requestAsset(path) {
        const relative=this.assetRelativePath(path);
        const publicBase=String(window.CINTERFORGE_IF_ASSETS_PUBLIC_BASE_URL||'').trim().replace(/\/+$/,'');
        if(!publicBase || !/^https?:\/\//i.test(publicBase))
            throw new Error(`CinterForge public Assets base is unavailable for ${relative}`);
        return `${publicBase}/${relative.split('/').map(encodeURIComponent).join('/')}`;
    }

    // Compatibility name only. All managed asset resolution terminates at
    // requestAsset(), which is the single bucket/base-URL boundary.
    static assetUrl(path) { return this.requestAsset(path);
     }

    /** Resolve a story-owned resource from the published Cloudflare Play closure.
     * Test/F5 uses this boundary so its story presentation cannot fall back to the
     * local authoring Session while TestingGrounds itself is remote. */
    static publishedStoryResourceUrl(storyId, resource='') {
        const id=String(storyId||'').trim();
        if (!/^STORY-[A-Za-z0-9._-]+$/.test(id)) {
            throw new Error(`Invalid published story identity: ${id||'missing'}`);
        }
        const publicBase=String(window.CINTERFORGE_IF_ASSETS_PUBLIC_BASE_URL||'').trim();
        if (!/^https?:\/\//i.test(publicBase)) {
            throw new Error(`CinterForge public Assets base is unavailable for ${id}`);
        }
        const assetsUrl=new URL(publicBase.endsWith('/')?publicBase:`${publicBase}/`);
        const origin=`${assetsUrl.protocol}//${assetsUrl.host}`;
        const relative=String(resource||'').replace(/^\/+/, '');
        const encoded=relative.split('/').filter(Boolean).map(encodeURIComponent).join('/');
        return `${origin}/interactive-fiction/games/${encodeURIComponent(id)}/Story/${encoded}`;
    }

    /**
     * Assets use the already-established IFPlayer JSON command channel. The
     * command may stage a binary resource beside ifplayer.html and return a
     * relative URL; the browser therefore never fetches asset bytes from the
     * transient control port.
     */
    static async assetCommand(body = {}) {
        // Static/resource-only IFPlayer has no live Python command plane.  Build is
        // an inspector in this mode; Session/Play/Graph/Build must never probe or
        // POST to an authoring endpoint.  This guard makes legacy callers fail
        // locally instead of manufacturing /assets/command network traffic.
        if (window.CINTERFORGE_IF_RESOURCE_ONLY && !String(window.CINTERFORGE_IF_AUTHORING_BASE||'').trim()) {
            const operation=String(body?.operation||'');
            if(operation==='assignment-list' && window.CINTERFORGE_IF_LOCAL_WORLD_ACTIVE===true){
                const files=window.CINTERFORGE_IF_LOCAL_WORLD_FILES;
                const readDataset=async(kind)=>{const file=files instanceof Map?files.get(`Data/${kind}.json`):null;
                    if (!file) {
                        return {};
                    }
                    try{return JSON.parse(await file.text());
                    }catch(_){return {};
                    }};
                const [rooms,objects,characters]=await Promise.all(['rooms','objects','characters'].map(readDataset));
                const normalize=data=>Object.entries(data?.records&&typeof data.records==='object'?data.records:{}).map(([id,row])=>({...row,id:String(row?.id||id),assets:Array.isArray(row?.assets)?row.assets.filter(v=>v&&typeof v==='object').map(v=>({...v})):[]})).sort((a,b)=>String(a.name||'').localeCompare(String(b.name||'')));
                const alias=String(rooms.story_alias||objects.story_alias||characters.story_alias||window.CINTERFORGE_IF_ACTIVE_GAME||'story');
                const title=String(rooms.title||objects.title||characters.title||alias);
                const rr=normalize(rooms),oo=normalize(objects),cc=normalize(characters);
                return {games:[{alias,title,story_id:String(rooms.story_id||objects.story_id||characters.story_id||''),rooms:rr,objects:oo,characters:cc,room_count:rr.length,object_count:oo.length,character_count:cc.length}],local:true,read_only:true};
            }
            const readOnly={'saved-list':'saved_library.json','assignment-list':'assignment_library.json'};
            const resource=readOnly[operation];
            if(resource){
                const localFiles=window.CINTERFORGE_IF_LOCAL_WORLD_FILES;
                if (localFiles instanceof Map && !localFiles.has(resource)) {
                    return operation==='assignment-list'?{games:[]}:{groups:{}};
                }
                const response=await fetch(`./${resource}?t=${Date.now()}`,{cache:'no-store'});
                if (!response.ok) {
                    return operation==='assignment-list'?{games:[]}:{groups:{}};
                }
                return await response.json();
            }
            throw new Error(`This asset operation requires the CinterForge Desktop app: ${operation||'unknown'}`);
        }
        // Asset authoring has exactly one HTTP authority: the fixed IFPlayer
        // authoring host (legacy standalone in Desktop).  Gameplay/traversal control ports are
        // separate origins and must never receive asset commands.  If the owned
        // authoring host is not ready, fail loudly instead of crossing origins.
        const base = String(window.CINTERFORGE_IF_AUTHORING_BASE || '').trim().replace(/\/$/, '');
        if (!base || window.CINTERFORGE_IF_HOST_API_READY !== true) {
            throw new Error('IFPlayer asset authoring host is unavailable');
        }
        const response = await fetch(`${base}/assets/command`, {
            method:'POST', mode:'cors', cache:'no-store',
            headers:{'Content-Type':'application/json'},
            body:JSON.stringify(body || {})
        });
        const payload = await response.json().catch(()=>({}));
        if (!response.ok || payload.ok === false) {
            const operation = String(body?.operation || 'unknown');
            const detail = String(payload.error || `HTTP ${response.status}`);
            throw new Error(`Asset command ${operation} failed: ${detail}`);
        }

        // Asset writes all flow through this command plane.  Publish one semantic
        // browser event after a successful mutation so the Sync control has one
        // authoritative dirty signal instead of every editor feature maintaining
        // its own copy of that UI state.  Read/stage/sync operations are excluded.
        const assetMutationOperations = new Set([
            'cloudflare-blockbench-save',
            'local-blockbench-save',
            'cloudflare-blockify-save',
            'local-blockify-save',
            'save-objaverse-thumbnail',
            'saved-assign-animation',
            'saved-blockbench-save',
            'saved-blockify-save',
            'saved-default-animation',
            'saved-delete',
            'saved-embedded-animation-enabled',
            'saved-image-minecraft-save',
            'saved-promote',
            'saved-remove-animation',
            'testing-grounds-gltf-save'
        ]);
        const operation = String(body?.operation || '');
        if (assetMutationOperations.has(operation)) {
            window.dispatchEvent(new CustomEvent('cinterforge-asset-modified', {
                detail: { operation }
            }));
        }
        return payload;
    }

    static async streamAssetCommand(body = {}, onProgress = null) {
        const started = await this.assetCommand(body);
        const jobId = String(started?.job_id || '').trim();
        if (!jobId) {
            throw new Error('Asset command did not return a progress job');
        }

        let cursor = 0;
        while (true) {
            const status = await this.assetCommand({
                operation:'asset-command-progress',
                job_id:jobId,
                cursor
            });
            const rows = Array.isArray(status?.rows) ? status.rows : [];
            for (const row of rows) {
                cursor = Math.max(cursor, Number(row?.sequence || 0));
                if (typeof onProgress === 'function') {
                    onProgress(row);
                }
            }
            if (status?.state === 'complete') {
                return {ok:true, result:status.result};
            }
            if (status?.state === 'failed') {
                throw new Error(String(status?.error || 'Asset command failed'));
            }
            await new Promise(resolve => window.setTimeout(resolve, 100));
        }
    }

    /** Shared Code bridge used by Build/Code/Play. */
    static async codeCommand(body = {}) {
        const base = CIFRuntime.runtimeAssetBase();
        if (!base) {
            throw new Error('IFPlayer Code bridge runtime is unavailable');
        }
        const response = await fetch(`${base}/code/command`, {
            method:'POST', mode:'cors', cache:'no-store',
            headers:{'Content-Type':'application/json'},
            body:JSON.stringify(body || {})
        });
        const payload = await response.json().catch(()=>({}));
        if (!response.ok || payload.ok === false) {
            const operation = String(body?.operation || 'unknown');
            const detail = String(payload.error || `HTTP ${response.status}`);
            throw new Error(`Asset command ${operation} failed: ${detail}`);
        }
        return payload;
    }

    static async codeStat(path) { return CIFRuntime.codeCommand({operation:'stat', path:String(path || '')});
     }
    static async codeList(path='') { return CIFRuntime.codeCommand({operation:'read-directory', path:String(path || '')});
        }
    static async codeWorkspaceState() { return CIFRuntime.codeCommand({operation:'workspace-state'});
     }
    static async codeCreateDirectory(path) { return CIFRuntime.codeCommand({operation:'create-directory', path:String(path || ''), parents:true, exist_ok:true});
        }
    static async codeDelete(path, recursive=true) { return CIFRuntime.codeCommand({operation:'delete', path:String(path || ''), recursive:!!recursive});
        }
    static async codeRename(path, target, overwrite=false) { return CIFRuntime.codeCommand({operation:'rename', path:String(path || ''), target:String(target || ''), overwrite:!!overwrite});
        }

    static async codeRead(path) {
        const payload = await CIFRuntime.codeCommand({operation:'read-file', path});
        const binary = atob(String(payload.data || ''));
        const bytes = new Uint8Array(binary.length);
        for (let i=0; i<binary.length; i++) bytes[i] = binary.charCodeAt(i);
        return new TextDecoder().decode(bytes);
    }

    static async codeWrite(path, text) {
        const bytes = new TextEncoder().encode(String(text == null ? '' : text));
        let binary = '';
        for (let i=0; i<bytes.length; i+=0x8000) binary += String.fromCharCode(...bytes.subarray(i, i+0x8000));
        return CIFRuntime.codeCommand({operation:'write-file', path, data:btoa(binary), create:true, overwrite:true});
    }

    static async codeCompile(path='') {
        return CIFRuntime.codeCommand({operation:'compile', path:String(path || '')});
    }

    static async ensureBrowserZMachine() {
        if (typeof window.cinterZMachinePlayerCommand !== 'function') {
            // CIFRuntime.js is intentionally a classic script. Relative dynamic
            // imports from classic scripts are resolved against the document in some
            // browsers and against an opaque script base in others (notably local
            // serverless execution). Resolve from the IFPlayer document explicitly.
            const moduleUrl=new URL('Runtime/Engine/Session/CinterZMachineRuntime.js', document.baseURI).href;
            await import(moduleUrl);
        }
        if (typeof window.cinterZMachinePlayerCommand !== 'function') {
            throw new Error('Browser JavaScript Z-machine runtime failed to load');
        }
    }

    static async publishPlayerSession(result=null) {
        const base=String(window.CINTERFORGE_IF_HOST_BASE || '').trim().replace(/\/$/, '');
        if (!base) {
            return;
        }
        const playerSession=(result && result.player_session) || result || {};
        try {
            await fetch(`${base}/host/player-session`, {method:'POST', cache:'no-store', headers:{'Content-Type':'application/json'}, body:JSON.stringify({player_session:playerSession})});
        } catch (_) {
        }
    }

    static async playerCommand(command) {
        await CIFRuntime.ensureBrowserZMachine();
        return window.cinterZMachinePlayerCommand(String(command || ''));
    }

    static async playerSession() {
        await CIFRuntime.ensureBrowserZMachine();
        const result=await window.cinterZMachinePlayerSession();
        await CIFRuntime.publishPlayerSession(result);
        return result;
    }

    static async playerNew() {
        await CIFRuntime.ensureBrowserZMachine();
        const result=await window.cinterZMachinePlayerNew();
        await CIFRuntime.publishPlayerSession(result);
        return result;
    }


    static async playerSaves() { await CIFRuntime.ensureBrowserZMachine();
        return window.cinterZMachinePlayerSaves();
        }
    static async playerSave() { await CIFRuntime.ensureBrowserZMachine();
        return window.cinterZMachinePlayerSave();
        }
    static async playerRestore(id) { await CIFRuntime.ensureBrowserZMachine();
        return window.cinterZMachinePlayerRestore(String(id || ''));
        }
    static async playerDelete(id) { await CIFRuntime.ensureBrowserZMachine();
        return window.cinterZMachinePlayerDelete(String(id || ''));
        }

    // Session/Play has exactly one authority: the browser JavaScript Z-machine VM. This is
    // deliberately independent of whether an optional Python authoring backend
    // happens to be connected. Never use backend presence as a gameplay switch.
    static browserOwnsSession() { return true;
     }

    static browserLeadState() {
        return {
            mode:String(window.CINTERFORGE_IF_GLOBAL_RUN_MODE || 'idle').toLowerCase(),
            paused:!!window.CINTERFORGE_IF_GLOBAL_RUN_PAUSED,
        };
    }

    static async setBrowserLead(enabled) {
        await CIFRuntime.ensureBrowserZMachine();
        if (!enabled) {
            const result = typeof window.cinterZMachineLeadStop === 'function'
                ? await window.cinterZMachineLeadStop()
                : {ok:true, stopped:true};
            window.CINTERFORGE_IF_GLOBAL_RUN_MODE = 'play';
            window.CINTERFORGE_IF_GLOBAL_RUN_PAUSED = true;
            return {...result, ok:true, mode:'play', paused:true};
        }
        window.CINTERFORGE_IF_GLOBAL_RUN_MODE = 'play';
        window.CINTERFORGE_IF_GLOBAL_RUN_PAUSED = false;
        // In Frobozz, Lead is one continuous browser-owned tour.  Completing
        // CCA must cross the configured world boundary and continue with the
        // next title instead of merely stopping because one program ended.
        const rawTask = (async () => {
            let result = await window.cinterZMachineLeadStart();
            while (ifplayerFrobozzMode() && result && result.completed_program && !result.stopped) {
                const row = CIFRuntime.transitionRows().find(item => String(item.from) === String(result.game || cinterGameKey()));
                if (!row) {
                    break;
                }
                console.info('[if-lead-transition]', {from:String(row.from), to:String(row.to)});
                const moved = await CIFRuntime.transitionToGame(String(row.to), {backward:false});
                if (!moved) {
                    throw new Error(`Lead could not transition ${row.from} -> ${row.to}`);
                }
                result = await window.cinterZMachineLeadStart();
            }
            return result;
        })();
        // Lead ownership ends when the producer ends, whether it completed the
        // full tour, was stopped by Follow, or failed.  Do not leave the global
        // UI state claiming a specialist still owns the turn stream after the
        // task is gone.
        const task = Promise.resolve(rawTask).then(result => {
            window.CINTERFORGE_IF_GLOBAL_RUN_MODE = 'play';
            window.CINTERFORGE_IF_GLOBAL_RUN_PAUSED = true;
            return {...(result || {}), mode:'play', paused:true};
        }, error => {
            window.CINTERFORGE_IF_GLOBAL_RUN_MODE = 'play';
            window.CINTERFORGE_IF_GLOBAL_RUN_PAUSED = true;
            throw error;
        });
        return {ok:true, mode:'play', paused:false, task};
    }

    static async codeSessionCommand(command) {
        return CIFRuntime.playerCommand(command);
    }

    static async stagedAssetUrl(resource, parameters = {}) {
        // Saved 3D assets are persistent canonical Cloudflare objects. Resolve
        // them directly. Runtime/browser reads must never invoke a local repair,
        // republish, or provenance-reacquisition transaction.
        const kind=String(resource||'');
        const savedPath=String(parameters?.path||'').replace(/^\/+/, '');
        const authoring=String(window.CINTERFORGE_IF_AUTHORING_BASE||'').trim();
        if (authoring && savedPath && ['saved-file','saved-runtime-animation'].includes(kind)) {
            // Authoring reads the canonical local Assets tree. Push/Build own
            // publication; a newly downloaded or edited asset must preview before
            // it exists in Cloudflare.
            const payload=await CIFRuntime.assetCommand({operation:'stage',resource:kind,...(parameters||{})});
            const relative=String(payload?.url||'').trim();
            return relative?CIFRuntime.presentationAssetUrl(`SVG/${relative}`):'';
        }
        if (savedPath && ['saved-file','saved-runtime-animation'].includes(kind)) {
            // Static/Test/Play consumption resolves the published asset identity.
            const managedPath=(kind==='saved-runtime-animation' && /\.fbx$/i.test(savedPath))
                ? savedPath.replace(/\.fbx$/i,'.preview.glb') : savedPath;
            return CIFRuntime.requestAsset(managedPath);
        }
        // In a static publication the saved library itself is staged beneath
        // /Assets.  Read-only previews therefore resolve directly to immutable
        // files and never ask Python to 'stage' a resource on demand.
        // Generated search thumbnails are presentation cache, not persistent
        // managed asset identity. In desktop authoring they still come from
        // Python even when the story itself is resource-only.
        // Saved-asset thumbnails are durable managed derivatives, but resolving the
        // thumbnail for a model path still requires reading that asset version's
        // manifest. Do that through the authoring asset command even when the story
        // runtime itself is resource-only. Treating `saved-thumb` like `saved-file`
        // below incorrectly handed the GLB URL to <img>, which is why only whichever
        // card happened to have an interactive Babylon preview appeared usable.
        if (authoring && ['saved-thumb','testing-grounds-gltf'].includes(kind)) {
            // Local Test is an authoring mode. Testing Grounds is project-owned
            // source, so it must be staged from the local project even when the
            // story runtime is otherwise resource-only. Do this before the
            // resource-only branch so Local Test cannot fall through to the
            // published Assets base or reject the local master outright.
            const payload=await CIFRuntime.assetCommand({operation:'stage',resource:kind,...(parameters||{})});
            const relative=String(payload?.url||'').trim();
            return relative?CIFRuntime.presentationAssetUrl(`SVG/${relative}`):'';
        }
        if (authoring && ['character-thumb','image-thumb','objaverse-thumb','objaverse-model','preview-model'].includes(kind)) {
            const payload=await CIFRuntime.assetCommand({operation:'stage',resource:kind,...(parameters||{})});
            // Search thumbnails are local authoring presentation resources.
            // They are generated/cache files, not published managed Assets, so
            // never resolve character thumbnails through the public R2 base.
            const relative=String(payload?.url||'').trim();
            return relative?CIFRuntime.presentationAssetUrl(`SVG/${relative}`):'';
        }
        if (window.CINTERFORGE_IF_RESOURCE_ONLY) {
            const kind=String(resource||'');
            const path=String(parameters?.path||'').replace(/^\/+/, '');
            if (path && ['saved-file','saved-runtime-animation'].includes(kind)){
                const managedPath=(kind==='saved-runtime-animation' && /\.fbx$/i.test(path))
                    ? path.replace(/\.fbx$/i,'.preview.glb') : path;
                return CIFRuntime.requestAsset(managedPath);
            }
            throw new Error(`Static staged asset is unavailable: ${kind||'unknown'}`);
        }
        const payload = await CIFRuntime.assetCommand({operation:'stage', resource, ...(parameters || {})});
        const url = String(payload.url || '').trim();
        if (!url) {
            throw new Error(`IFPlayer staged asset URL missing for ${resource}`);
        }
        // stage() always returns a path beneath the active IFPlayer presentation
        // (normally SVG/_ifplayer_assets). It is not a managed Cloudflare asset
        // identity. Keep every staged preview/model/image on the presentation
        // origin; only canonical managed Assets go through assetUrl().
        return CIFRuntime.presentationAssetUrl(`SVG/${url.replace(/^\/+/, '')}`);
    }

    /**
     * cinterGameKey()
     *
     * Implements cinter game key for runtime routing and transport; this method is part of the class-owned presentation contract.
     *
     * Side effects: Reads runtime/bootstrap state and may update presentation caches; canonical interpreter state remains external.
     *
    * @returns {*} The resolved/derived value for this presentation operation.
     */
    static cinterGameKey() { return String(window.CINTERFORGE_IF_CINTER_GAME || window.CINTERFORGE_IF_SELECTED_STORY || window.CINTERFORGE_IF_ACTIVE_GAME || CIFRuntime.gameOrder()[0] || '');
        }

    /**
     * primaryAgentForGame()
     *
     * Implements primary agent for game for runtime routing and transport; this method is part of the class-owned presentation contract.
     *
     * Side effects: Reads runtime/bootstrap state and may update presentation caches; canonical interpreter state remains external.
     *
    * @param {*} game Input used by this operation; see method logic for accepted shape/semantics.
     *
    * @returns {*} The resolved/derived value for this presentation operation.
     */
    static primaryAgentForGame(game=cinterGameKey()) {
        const configured = window.CINTERFORGE_IF_PRIMARY_AGENTS || {};
        return String(configured[String(game)] || 'Agatha');
    }

    static gameOrder() {
        const ports=window.CINTERFORGE_IF_CONTROL_PORTS;
        if(ports&&typeof ports==='object'){const keys=Object.keys(ports).filter(key=>Number(ports[key]||0)>0);
            if (keys.length) {
                return keys;
            }
            }
        const stories=window.CINTERFORGE_IF_STORIES;
        if(stories&&typeof stories==='object'){const keys=Object.keys(stories);
        if (keys.length) {
            return keys;
        }
        }
        if(unifiedSpatialScene&&unifiedSpatialScene.games){const keys=Object.keys(unifiedSpatialScene.games);
            if (keys.length) {
                return keys;
            }
            }
        const active=String(window.CINTERFORGE_IF_ACTIVE_GAME||'').trim();
        return active?[active]:[];
    }

    static gameForPrimaryAgent(agent) {
        const wanted=String(agent||'').toLowerCase();
        const configured=window.CINTERFORGE_IF_PRIMARY_AGENTS||{};
        return CIFRuntime.gameOrder().find(game=>String(configured[game]||'').toLowerCase()===wanted)||'';
    }

    static transitionRows() {
        const rows = ifplayerSettings && Array.isArray(ifplayerSettings.all_mode_game_transitions)
            ? ifplayerSettings.all_mode_game_transitions : [];
        return rows.filter(row => row && row.from && row.to);
    }

    static parseProgress(value) {
        const match = String(value == null ? '' : value).match(/(-?\d+)\s*\/\s*(-?\d+)/);
        return match ? [Number(match[1]), Number(match[2])] : [0, 0];
    }

    static unifiedPlayerTotals(states=unifiedWorldStates) {
        const totals = {score_current:0,score_maximum:0,rooms_current:0,rooms_maximum:0,objects_current:0,objects_maximum:0};
        let scoreMaximumKnown = true;
        for (const game of CIFRuntime.gameOrder()) {
            const player = ((states || {})[game] || {}).player_session || {};
            const score = Number(player.score_current);
            const scoreMax = Number(player.score_maximum);
            if (Number.isFinite(score)) {
                totals.score_current += score;
            }
            if (Number.isFinite(scoreMax) && scoreMax > 0) {
                totals.score_maximum += scoreMax;
            }
            else scoreMaximumKnown = false;
            const rooms = CIFRuntime.parseProgress(player.rooms_found);
            const objects = CIFRuntime.parseProgress(player.objects_found);
            totals.rooms_current += rooms[0];
             totals.rooms_maximum += rooms[1];
            totals.objects_current += objects[0];
             totals.objects_maximum += objects[1];
        }
        totals.score = `${totals.score_current} / ${scoreMaximumKnown ? totals.score_maximum : '?'}`;
        totals.rooms = `${totals.rooms_current} / ${totals.rooms_maximum}`;
        totals.objects = `${totals.objects_current} / ${totals.objects_maximum}`;
        return totals;
    }

    static forwardTransitionForCommand(command, playerSession) {
        if (!ifplayerFrobozzMode()) {
            return null;
        }
        const game = cinterGameKey();
        const normalized = String(command || '').trim().toLowerCase();
        const canonical = ({w:'west',west:'west',e:'east',east:'east',n:'north',north:'north',s:'south',south:'south'})[normalized] || normalized;
        const foldLocation = value => String(value || '').toLowerCase().replace(/[^a-z0-9]+/g,'');
        const location = foldLocation((playerSession || {}).current_location || '');
        for (const row of CIFRuntime.transitionRows()) {
            if (String(row.from) !== game) {
                continue;
            }
            const expected = String(row.forward_command || '').trim().toLowerCase();
            if (!expected || expected !== canonical) {
                continue;
            }
            const needle = foldLocation(row.forward_location_contains || '');
            if (needle && !location.includes(needle)) {
                continue;
            }
            return {from:String(row.from), to:String(row.to), backward:false};
        }
        return null;
    }

    static backwardTransitionForCommand(command, playerSession) {
        if (!ifplayerFrobozzMode()) {
            return null;
        }
        const game = cinterGameKey();
        const normalized = String(command || '').trim().toLowerCase();
        const canonical = ({w:'west',west:'west',e:'east',east:'east',n:'north',north:'north',s:'south',south:'south'})[normalized] || normalized;
        const foldLocation = value => String(value || '').toLowerCase().replace(/[^a-z0-9]+/g,'');
        const location = foldLocation((playerSession || {}).current_location || '');
        for (const row of CIFRuntime.transitionRows()) {
            if (String(row.to) !== game) {
                continue;
            }
            if (String(row.back_command || '').toLowerCase() !== canonical) {
                continue;
            }
            const needle = foldLocation(row.back_location_contains || '');
            if (needle && !location.includes(needle)) {
                continue;
            }
            return {from:String(row.to), to:String(row.from), backward:true};
        }
        return null;
    }

    static async transitionToGame(game, {backward=false}={}) {
        if (!ifplayerFrobozzMode()) {
            return false;
        }
        game = String(game || '');
        if (!game || !CIFRuntime.gameOrder().includes(game)) {
            return false;
        }
        if (CIFRuntime._transitionInFlight) {
            return false;
        }
        CIFRuntime._transitionInFlight = true;
        try {
            // Every cross-title boundary gives the destination primary agent the
            // lead.  Its Follow checkpoint is already the destination Player's
            // exact durable save, so deterministic Play resumes from that title's
            // preserved state rather than manufacturing a synthetic combined VM.
            window.CINTERFORGE_IF_GLOBAL_RUN_MODE = 'play';
            window.CINTERFORGE_IF_GLOBAL_RUN_PAUSED = false;
            if (backward) {
                // Intermediate titles are allowed to execute their real ending
                // command so forward travel feels seamless. Restore that title's
                // separately retained pre-ending Quetzal before handing control
                // back to its primary agent; terminal VM screens are not valid
                // return destinations. Titles not yet completed simply keep their
                // current runnable Player/Follow checkpoint.
                await postRuntimeForGame(game, 'player/transition-return', {});
            }
            await setCinterWorld(game);
            // This is the one forward browser-owned boundary allowed to wake a
            // dormant destination interpreter.  Presentation switches and actor
            // inspection must never instantiate worlds.
            if (CIFRuntime.browserOwnsSession() && typeof window.cinterZMachinePlayerSession === 'function') {
                await window.cinterZMachinePlayerSession(game);
                console.info('[if-lead-runtime-transition]', {active:game, initialized:Object.keys(typeof window.cinterZMachineWorldSessions === 'function' ? window.cinterZMachineWorldSessions() : {})});
            }
            await switchUnifiedGame(game);
            const states = await loadUnifiedWorldStates();
            const target = states[game];
            if (target) {
                lastLiveState = target;
                applyLiveState(target);
            }
            const agent = primaryAgentForGame(game);
            const story = String(((target || {}).player_session || {}).story_title || ((window.CINTERFORGE_IF_STORIES||{})[game]||{}).title || game);
            const key = backward ? 'all_mode_transition_back_status' : 'all_mode_transition_status';
            let text = uiString(key, backward ? 'Returning to {story}; {agent} resumes the lead.' : 'Entering {story} with {agent} leading.');
            text = text.replace('{story}', story).replace('{agent}', agent);
            setLiveIndicator(text);
            return true;
        } finally {
            CIFRuntime._transitionInFlight = false;
        }
    }

    static async advanceCompletedStory(liveState=lastLiveState) {
        if (!ifplayerFrobozzMode() || CIFRuntime._transitionInFlight) {
            return false;
        }
        const game = cinterGameKey();
        // A viewed dormant title can publish presentation state while Cinter is
        // physically in another VM.  Never let that viewed heartbeat advance the
        // Player's world; cross-title completion belongs only to Cinter's current
        // game.
        if (activeGameKey() !== game) {
            return false;
        }
        const row = CIFRuntime.transitionRows().find(item => String(item.from) === game);
        if (!row) {
            return false;
        }
        const player = (liveState || {}).player_session || {};
        const status = (liveState || {}).status || {};
        const deterministicComplete = String((liveState || {}).run_mode || '').toLowerCase() === 'play'
            && String(status.exploration_status || '').toLowerCase() === 'complete';
        if (!deterministicComplete && !player.story_complete) {
            return false;
        }
        const signature = `${game}:${Number(player.commands_issued || 0)}:${String(player.score_current ?? '')}:${String(status.exploration_status || '')}`;
        if (CIFRuntime._lastCompletedTransitionSignature === signature) {
            return false;
        }
        CIFRuntime._lastCompletedTransitionSignature = signature;
        return CIFRuntime.transitionToGame(String(row.to), {backward:false});
    }

    static async newUnifiedGame() {
        if (!ifplayerFrobozzMode()) {
            return null;
        }
        const games = CIFRuntime.gameOrder();
        if (!games.length) {
            throw new Error('Unified IFPlayer browser stories are unavailable');
        }
        const startGame=String(window.CINTERFORGE_IF_START_GAME||games[0]);
        CIFRuntime._transitionInFlight = true;
        try {
            let results;
            if (CIFRuntime.browserOwnsSession() && typeof window.cinterZMachineResetWorld === 'function') {
                // New Frobozz session must not instantiate every title. Reset all
                // dormant world records synchronously, then create exactly the
                // starting world's VM. The Lead transition is the only operation
                // allowed to wake Zork I/II/III later.
                results = games.map(game => window.cinterZMachineResetWorld(game));
                const startIndex = games.indexOf(startGame);
                results[startIndex] = await window.cinterZMachinePlayerNew(startGame);
                console.info('[if-single-interpreter]', {active:startGame, initialized:[startGame], dormant:games.filter(game=>game!==startGame)});
            } else {
                results = await Promise.all(games.map(game => postRuntimeForGame(game, 'player/new', {})));
            }
            if (!(CIFRuntime.browserOwnsSession() && typeof window.cinterZMachinePlayerNew === 'function'))
                await Promise.all(games.map(game => postRuntimeForGame(game, 'world-active', {active:game===startGame})));
            window.CINTERFORGE_IF_CINTER_GAME = startGame;
            window.CINTERFORGE_IF_SELECTED_STORY = startGame;
            window.CINTERFORGE_IF_ACTIVE_GAME = startGame; // compatibility mirror: selected Story only
            window.CINTERFORGE_IF_GLOBAL_RUN_MODE = 'idle';
            window.CINTERFORGE_IF_GLOBAL_RUN_PAUSED = true;
            CIFRuntime._lastCompletedTransitionSignature = '';
            await switchUnifiedGame(startGame);
            const states = await loadUnifiedWorldStates();
            const initial = states[startGame];
            if (initial) {
                lastLiveState = initial;
                applyLiveState(initial);
            }
            return results;
        } finally {
            CIFRuntime._transitionInFlight = false;
        }
    }

    /**
     * viewedGameForActor()
     *
     * Implements viewed game for actor for runtime routing and transport; this method is part of the class-owned presentation contract.
     *
     * Side effects: Reads runtime/bootstrap state and may update presentation caches; canonical interpreter state remains external.
     *
    * @param {*} actor Input used by this operation; see method logic for accepted shape/semantics.
     *
    * @returns {*} The resolved/derived value for this presentation operation.
     */
    static viewedGameForActor(actor) {
        const name=String(actor||'Player');
        return IFPLAYER_ACTOR_GAMES[name] || cinterGameKey();
    }

    /**
     * agentThumbnailUrl()
     *
     * Implements agent thumbnail url for runtime routing and transport; this method is part of the class-owned presentation contract.
     *
     * Side effects: Reads runtime/bootstrap state and may update presentation caches; canonical interpreter state remains external.
     *
    * @param {*} name Input used by this operation; see method logic for accepted shape/semantics.
     *
    * @returns {*} The resolved/derived value for this presentation operation.
     */
    static agentThumbnailUrl(name) {
        const key=String(name||'player').toLowerCase();
        return runtimeAssetUrl(`agent_${key}.png`);
    }

    /**
     * runtimeThumbnailUrl()
     *
     * Implements runtime thumbnail url for runtime routing and transport; this method is part of the class-owned presentation contract.
     *
     * Side effects: Reads runtime/bootstrap state and may update presentation caches; canonical interpreter state remains external.
     *
    * @param {*} value Input used by this operation; see method logic for accepted shape/semantics.
    * @param {*} game Input used by this operation; see method logic for accepted shape/semantics.
     *
    * @returns {*} The resolved/derived value for this presentation operation.
     */
    static runtimeThumbnailUrl(value, game=viewedGameForActor(activeInventoryCharacter)) {
        const raw=String(value||'').trim();
        if (!raw) {
            return '';
        }
        if (/^https?:/i.test(raw)||raw.startsWith('data:')||raw.startsWith('blob:')) {
            return raw;
        }
        const clean=raw.replace(/^\.\.\/Thumbnails\//i,'').replace(/^Thumbnails\//i,'').replace(/^\/+/, '');
        if (window.CINTERFORGE_IF_RESOURCE_ONLY) {
            const encoded=clean.split('/').map(encodeURIComponent).join('/');
            if (ifplayerFrobozzMode()) {
                const base=runtimeBaseForGame(game);
                return base ? `${base}/Thumbnails/${encoded}` : '';
            }
            return `../Thumbnails/${encoded}`;
        }
        const base=ifplayerFrobozzMode() ? runtimeBaseForGame(game) : runtimeAssetBase();
        return base ? `${base}/thumbnail/${clean.split('/').map(encodeURIComponent).join('/')}` : raw;
    }

    static async selectViewedActor(actor) {
        const name=String(actor||'Player');
        activeInventoryCharacter=name;
        activeWorldOwner=name;
        activeSessionAgent=name;
        lastInventoryRenderSignature = "";
        const game=viewedGameForActor(name);
        if(ifplayerFrobozzMode()) {
            await switchUnifiedGame(game);
            const states = await loadUnifiedWorldStates([game]);
            // The actor just selected owns this world's presentation.  Keep the
            // selected child's published state resident even when that world is
            // spun down; otherwise a later empty/stale heartbeat can leave Session
            // and Graph looking uninitialized despite a valid startup handshake.
            if (states && states[game]) {
                lastLiveState = states[game];
            }
            updateSpecialists(lastLiveState || {});
        }
        renderInventory(lastLiveState||{});
        renderPropertyPanel(lastLiveState||{});
        renderSession();
        if (stageMode==='session') {
            await loadHumanSession();
        }
    }

    /**
     * loadUnifiedWorldStates()
     *
     * Loads unified world states needed by runtime routing and transport and updates the corresponding cached presentation state.
     *
     * Side effects: Reads runtime/bootstrap state and may update presentation caches; canonical interpreter state remains external.
     *
    * @param {*} games Input used by this operation; see method logic for accepted shape/semantics.
     *
    * @returns {Promise<*>} Resolves when the asynchronous operation completes; value is method-specific.
     */
    static async loadUnifiedWorldStates(games=null) {
        if (!ifplayerFrobozzMode()) {
            return {};
        }
        const next={};
        const wanted = Array.isArray(games) && games.length ? [...new Set(games.map(String))] : CIFRuntime.gameOrder();
        if (CIFRuntime.browserOwnsSession() && typeof window.cinterZMachineWorldSessions === 'function') {
            // Presentation inspection must never instantiate an interpreter.
            // Previously this loop used the initializing single-world accessor for every
            // Frobozz title; that accessor initializes Bocfel, so merely refreshing
            // unified UI caused CCA, Zork I, II and III VMs to come alive together.
            // Read snapshots of already-owned worlds only. The Lead transition is
            // the sole operation that is allowed to initialize the next title.
            const sessions = window.cinterZMachineWorldSessions() || {};
            for (const game of wanted) {
                const result = sessions[game];
                if (!result?.player_session) {
                    continue;
                }
                next[game]={player_session:result.player_session,current_location:result.player_session.current_location,current_room:result.player_session.current_room,last_response:result.player_session.last_response,primary_agent:CIFRuntime.primaryAgentForGame(game),run_mode:game===CIFRuntime.cinterGameKey()?String(window.CINTERFORGE_IF_GLOBAL_RUN_MODE||'idle'):'idle',paused:game===CIFRuntime.cinterGameKey()?!!window.CINTERFORGE_IF_GLOBAL_RUN_PAUSED:true};
            }
        } else {
            const ports=window.CINTERFORGE_IF_CONTROL_PORTS||{};
            await Promise.all(wanted.filter(game=>ports[game]).map(async game=>{
                const base=runtimeBaseForGame(game);
                 if (!base) {
                     return;
                 }
                try { const r=await fetch(`${base}/ui/ifplayer_live.json?t=${Date.now()}`,{cache:'no-store'});
                    if (r.ok) {
                        next[game]=await r.json();
                    }
                    } catch (_err) {
                    }
            }));
        }
        unifiedWorldStates={...unifiedWorldStates,...next};
        return unifiedWorldStates;
    }

    /**
     * unifiedActorRoom()
     *
     * Implements unified actor room for runtime routing and transport; this method is part of the class-owned presentation contract.
     *
     * Side effects: Reads runtime/bootstrap state and may update presentation caches; canonical interpreter state remains external.
     *
    * @param {*} game Input used by this operation; see method logic for accepted shape/semantics.
    * @param {*} actor Input used by this operation; see method logic for accepted shape/semantics.
    * @param {*} live Input used by this operation; see method logic for accepted shape/semantics.
     *
    * @returns {*} The resolved/derived value for this presentation operation.
     */
    static unifiedActorRoom(game, actor, live) {
        if (!live) {
            return null;
        }
        const source=actor==='Player' ? (live.player_session||{}) : live;
        const roomId=String(source.current_room||'');
        if (!roomId) {
            return null;
        }
        const prefix=IFPLAYER_GAME_STORY_PREFIX[String(game)]||String(game);
        return babylonRoomNodes.get(`${prefix}:${roomId}`)||babylonRoomNodes.get(roomId)||null;
    }

    static disposeUnifiedActors() {
        for(const rec of babylonUnifiedActors.values()){
            try{rec.mesh&&rec.mesh.dispose(false,true);
            } catch (_) {
            }
            try{rec.label&&rec.label.dispose(false,true);
            } catch (_) {
            }
        }
        babylonUnifiedActors.clear();
    }

    static ensureUnifiedActorMarker(name,color) {
        let rec=babylonUnifiedActors.get(name);
         if (rec) {
             return rec;
         }
        const mesh=BABYLON.MeshBuilder.CreateSphere(`unified-agent:${name}`,{diameter:1.25,segments:16},babylonScene);
        const mat=new BABYLON.StandardMaterial(`unified-agent-material:${name}`,babylonScene);
        mat.diffuseColor=specialistColor3(color);
            mat.emissiveColor=specialistColor3(color).scale(.32);
            mat.specularColor=BABYLON.Color3.Black();
        mesh.material=mat;
            mesh.renderingGroupId=3;
            mesh.isPickable=false;
            mesh.alwaysSelectAsActiveMesh=true;
            mesh.metadata={type:'agent',agent_name:name};
        const label=BABYLON.MeshBuilder.CreatePlane(`unified-agent-label:${name}`,{width:6,height:1.05,sideOrientation:BABYLON.Mesh.DOUBLESIDE},babylonScene);
        label.billboardMode=BABYLON.Mesh.BILLBOARDMODE_ALL;
            label.renderingGroupId=3;
            label.isPickable=false;
            label.alwaysSelectAsActiveMesh=true;
        const tex=new BABYLON.DynamicTexture(`unified-agent-label-texture:${name}`,{width:768,height:144},babylonScene,false),ctx=tex.getContext();
            tex.hasAlpha=true;
            ctx.clearRect(0,0,768,144);
            ctx.fillStyle='rgba(255,255,255,.96)';
            ctx.fillRect(6,6,756,132);
            ctx.strokeStyle=color;
            ctx.lineWidth=8;
            ctx.strokeRect(6,6,756,132);
            ctx.font='bold 66px Arial, sans-serif';
            ctx.textAlign='center';
            ctx.textBaseline='middle';
            ctx.fillStyle=color;
            ctx.fillText(name,384,74);
            tex.update();
        const lm=new BABYLON.StandardMaterial(`unified-agent-label-material:${name}`,babylonScene);
            lm.diffuseTexture=tex;
            lm.opacityTexture=tex;
            lm.emissiveColor=BABYLON.Color3.White();
            lm.disableLighting=true;
            lm.backFaceCulling=false;
            label.material=lm;
        rec={mesh,label};
         babylonUnifiedActors.set(name,rec);
         return rec;
    }

    static async refreshUnifiedActorPositions({ force = false } = {}) {
        if (!ifplayerFrobozzMode()||!babylonScene||!babylonRoomNodes.size||stageMode!=='map') {
            return;
        }
        const now = performance.now();
        if (!force && (CIFRuntime._unifiedActorRefreshInFlight || now - Number(CIFRuntime._lastUnifiedActorRefreshAt || 0) < 250))
            return;
        CIFRuntime._unifiedActorRefreshInFlight = true;
        CIFRuntime._lastUnifiedActorRefreshAt = now;
        const viewed=viewedGameForActor(activeInventoryCharacter);
        const games=unifiedWorldStatesPrimed ? [cinterGameKey(), viewed] : null;
        let states;
        try {
            states=await loadUnifiedWorldStates(games);
        } finally {
            CIFRuntime._unifiedActorRefreshInFlight = false;
        }
        unifiedWorldStatesPrimed = true;
        const rows=[['Player',cinterGameKey(),'#00a7ff']];
        const configured=window.CINTERFORGE_IF_PRIMARY_AGENTS||{};
        const palette=['#1677ff','#8b5cf6','#ff8a00','#16a34a','#d946ef','#0891b2','#ca8a04'];
        let colorIndex=0;
        for(const game of CIFRuntime.gameOrder()){
            const name=String(configured[game]||'').trim();
            if (!name||rows.some(row=>row[0]===name&&row[1]===game)) {
                continue;
            }
            rows.push([name,game,palette[colorIndex++%palette.length]]);
        }
        const actorSignature = rows.map(([name,game]) => {
            const st = states[game] || {};
            const room = unifiedActorRoom(game,name,st);
            return `${name}:${room ? String(gltfExtras(room).room_id || room.name || '') : ''}`;
        }).join('|');
        if (!force && actorSignature === CIFRuntime._lastUnifiedActorPresentationSignature)
            return;
        CIFRuntime._lastUnifiedActorPresentationSignature = actorSignature;
        for(const [name,game,color] of rows){
            const rec=ensureUnifiedActorMarker(name,color), room=unifiedActorRoom(game,name,states[game]);
            if(!room){rec.mesh.setEnabled(false);
            rec.label.setEnabled(false);
            continue;
            }
            room.computeWorldMatrix&&room.computeWorldMatrix(true);
                const p=room.getAbsolutePosition(), y=platformTopWorld(room)+.7;
            rec.mesh.setAbsolutePosition(new BABYLON.Vector3(p.x,y,p.z));
                rec.label.setAbsolutePosition(new BABYLON.Vector3(p.x,y+1.55,p.z));
                rec.mesh.setEnabled(true);
                rec.label.setEnabled(true);
        }
    }

    /**
     * runtimeBaseForGame()
     *
     * Implements runtime base for game for runtime routing and transport; this method is part of the class-owned presentation contract.
     *
     * Side effects: Reads runtime/bootstrap state and may update presentation caches; canonical interpreter state remains external.
     *
    * @param {*} game Input used by this operation; see method logic for accepted shape/semantics.
     *
    * @returns {*} The resolved/derived value for this presentation operation.
     */
    static runtimeBaseForGame(game) {
        if (window.CINTERFORGE_IF_RESOURCE_ONLY) {
            const row=(window.CINTERFORGE_IF_BROWSER_STORIES||{})[String(game)]||{};
            const raw=String(row.resource_base||'').replace(/\/$/,'');
            const presentationBase=String(window.CINTERFORGE_IFPLAYER_PRESENTATION_BASE||'').trim();
            // Single-story resource-only publications do not need a synthetic
            // CINTERFORGE_IF_BROWSER_STORIES row. Their story-owned resources live
            // beside ifplayer.html under /SVG. Embedded execution happens in the
            // outer 20380 document, so falling back to a bare relative name leaks
            // the request to CinterForge. Keep the presentation server as the one
            // authoritative resource origin in both standalone and embedded modes.
            if (!raw) {
                return presentationBase ? new URL('Story/', presentationBase).href.replace(/\/$/,'') : 'Story';
            }
            if (presentationBase) {
                try {
                    const presentationUrl=new URL(presentationBase);
                    // Absolute resource bases are IFPlayer-server-relative. Relative
                    // resource bases (./stories/cca) are presentation-relative and
                    // must remain under /SVG when IFPlayer is embedded in 20380.
                    const base=raw.startsWith('/') ? `${presentationUrl.origin}/` : presentationUrl;
                    const value=raw.startsWith('/') ? raw.replace(/^\/+/, '') : raw;
                    return new URL(value, base).href.replace(/\/$/,'');
                } catch (_) {
                }
            }
            return raw;
        }
        if (window.CINTERFORGE_IF_API_READY !== true) {
            return '';
        }
        // All live browser API traffic stays on the fixed IFPlayer host.  Story
        // identity belongs in the request payload/path, never in a transient port.
        return String(window.CINTERFORGE_IF_AUTHORING_BASE||'').trim().replace(/\/$/, '');
    }

    /** Canonical story-owned static resource resolver. Session/Play callers must
     * use this rather than manufacturing legacy Python endpoint URLs. */
    static storyResourceUrl(game, resource) {
        const base=CIFRuntime.runtimeBaseForGame(String(game||CIFRuntime.activeGameKey()));
        const name=String(resource||'').replace(/^\/+/, '');
        return base ? `${base}/${name}` : name;
    }

    static async storyResourceJson(game, resource, fallback={}) {
        const url=CIFRuntime.storyResourceUrl(game, resource);
        if (!url) {
            return fallback;
        }
        try {
            const r=await fetch(`${url}${url.includes('?')?'&':'?'}t=${Date.now()}`,{cache:'no-store'});
            if (!r.ok) {
                return fallback;
            }
            return await r.json();
        } catch(_) { return fallback;
         }
    }

    static async postRuntimeForGame(game, path, body={}) {
        const base = runtimeBaseForGame(game);
        if (!base) {
            throw new Error(`Runtime unavailable for ${game}`);
        }
        const response = await fetch(`${base}/${String(path||'').replace(/^\/+/, '')}`, {method:'POST', mode:'cors', cache:'no-store', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body||{})});
        const payload = await response.json().catch(()=>({}));
        if (!response.ok || payload.ok === false) {
            const operation = String(body?.operation || 'unknown');
            const detail = String(payload.error || `HTTP ${response.status}`);
            throw new Error(`Asset command ${operation} failed: ${detail}`);
        }
        return payload;
    }
    /**
     * shutdownBackend()
     *
     * Requests an orderly shutdown of every backend process owned by this
     * IFPlayer browser. In single-game mode there is one control server. In
     * unified all-mode every child receives the same shutdown request so the
     * supervisor can observe clean child exits instead of leaving dormant
     * interpreters behind. Browser/window lifecycle is intentionally NOT owned
     * here; CIFLobby invokes the overridable HTML close hook only after
     * this backend operation finishes.
     *
     * @returns {Promise<void>} Resolves after all reachable IFPlayer runtimes
     * have accepted the shutdown request.
     */
    static async shutdownBackend(operatorInitiated = false) {
        if (operatorInitiated !== true) {
            throw new Error('IFPlayer close requires an explicit operator Quit action');
        }
        // Browser-owned Session/Play has no gameplay backend to shut down. Quit
        // still stops any browser Lead loop, then the trusted UI handler closes
        // (or dismisses) the view. Do not manufacture a /quit gameplay API just
        // to make a static publication closable.
        if (CIFRuntime.browserOwnsSession() && typeof window.cinterZMachineLeadStop === 'function') {
            await window.cinterZMachineLeadStop();
        }
        if (window.CINTERFORGE_IF_RESOURCE_ONLY) {
            // Stop browser-owned host traffic BEFORE terminating the legacy standalone host.
            // Otherwise the embedded page survives long enough to keep polling a
            // host that has already exited, flooding DevTools with connection errors.
            if (typeof window.CinterForgeIFPlayerStopHostTransport === 'function') {
                window.CinterForgeIFPlayerStopHostTransport();
            }
            // The legacy standalone host now owns the persistent story catalog. Gameplay Quit
            // stops browser-owned execution only; it must never terminate that host.
            // The Lobby returns to the catalog in-place/navigation after this cleanup.
            return;
        }
        // Only the dedicated trusted Quit-button handler calls this method. Page
        // unload/navigation never does: leaving IFPlayer must not kill a running
        // session, while explicit Quit must terminate supervisor-owned legacy
        // runtimes before the browser view closes.
        const ports = window.CINTERFORGE_IF_CONTROL_PORTS || null;
        if (ports && typeof ports === 'object') {
            const games = Object.keys(ports).filter(game => Number(ports[game] || 0) > 0);
            if (!games.length) {
                throw new Error(uiString('quit_no_runtimes','No IFPlayer runtimes are available to shut down'));
            }
            const results = await Promise.allSettled(
                games.map(game => CIFRuntime.postRuntimeForGame(game, '/quit', {intent:'operator-quit'}))
            );
            const failures = results
                .map((result, index) => ({result, game:games[index]}))
                .filter(row => row.result.status === 'rejected');
            if (failures.length) {
                throw new Error(uiString('quit_shutdown_failed_games','IFPlayer shutdown failed for {games}').replace('{games}', failures.map(row => row.game).join(', ')));
            }
            return;
        }
        const base = CIFRuntime.runtimeAssetBase();
        if (!base) {
            throw new Error(uiString('quit_runtime_unavailable','IFPlayer runtime is unavailable'));
        }
        const response = await fetch(`${base}/quit`, {
            method:'POST', mode:'cors', cache:'no-store',
            headers:{'Content-Type':'application/json'},
            body:JSON.stringify({intent:'operator-quit'})
        });
        const payload = await response.json().catch(()=>({}));
        if (!response.ok || payload.ok === false) {
            const operation = String(body?.operation || 'unknown');
            const detail = String(payload.error || `HTTP ${response.status}`);
            throw new Error(`Asset command ${operation} failed: ${detail}`);
        }
    }

    static async setCinterWorld(game) {
        if (!ifplayerFrobozzMode()) {
            return;
        }
        game=String(game||'');
        if (!CIFRuntime.gameOrder().includes(game)) {
            throw new Error(`Unknown IF world: ${game}`);
        }
        const previous=cinterGameKey();
         if (previous===game) {
             return;
         }
        if (CIFRuntime.browserOwnsSession()) {
            window.CINTERFORGE_IF_CINTER_GAME=game;
            // Runtime/Lead identity changes here. Presentation identity changes only
            // through switchUnifiedGame(), below, at the explicit transition boundary.
            // Selecting/viewing a Frobozz world is presentation state only.  Do
            // NOT call cinterZMachineWorldSession() here: that accessor initializes
            // a Bocfel VM.  Runtime creation is reserved for New and the explicit
            // Lead transition boundary below.
            if (window.CIFLobby) CIFLobby.updateHeaderPresentation(lastLiveState || {});
            if (activeWorldOwner==='Player') {
                await switchUnifiedGame(game);
            }
            return;
        }
        const ports=window.CINTERFORGE_IF_CONTROL_PORTS||{};
        await Promise.all(Object.keys(ports).filter(alias=>alias!==game).map(alias=>postRuntimeForGame(alias,'world-active',{active:false})));
        const activated=await postRuntimeForGame(game,'world-active',{active:true});
        window.CINTERFORGE_IF_CINTER_GAME=game;
        if (window.CIFLobby) CIFLobby.updateHeaderPresentation(lastLiveState || {});
        const desiredMode=String(window.CINTERFORGE_IF_GLOBAL_RUN_MODE||'idle');
        const desiredPaused=!!window.CINTERFORGE_IF_GLOBAL_RUN_PAUSED;
        if (desiredMode!=='idle') {
            const targetMode=String(activated.mode||'idle');
            const targetPaused=!!activated.paused;
            if (targetMode!==desiredMode) {
                await postRuntimeForGame(game,'run-mode',{mode:desiredMode,action:'start'});
                if (desiredPaused) await postRuntimeForGame(game,'run-mode',{mode:desiredMode,action:'pause'});
            } else if (desiredPaused!==targetPaused) {
                await postRuntimeForGame(game,'run-mode',{mode:desiredMode,action:desiredPaused?'pause':'resume'});
            }
        }
        if (activeWorldOwner==='Player') {
            await switchUnifiedGame(game);
        }
    }

    /**
     * runtimeAssetUrl()
     *
     * Implements runtime asset url for runtime routing and transport; this method is part of the class-owned presentation contract.
     *
     * Side effects: Reads runtime/bootstrap state and may update presentation caches; canonical interpreter state remains external.
     *
    * @param {*} name Input used by this operation; see method logic for accepted shape/semantics.
     *
    * @returns {*} The resolved/derived value for this presentation operation.
     */
    static presentationOrigin() {
        const explicit=String(window.CINTERFORGE_IFPLAYER_PRESENTATION_BASE||'').trim();
        if(explicit){
            try { return new URL(explicit).origin;
             } catch (_) {
             }
        }
        // Embedded IFPlayer executes in CinterForge's 20380 document, but Lobby
        // installs a temporary <base> pointing at the real IFPlayer presentation
        // server (legacy standalone).  document.baseURI therefore remains the authoritative
        // browser-resource origin even if an embedding/global bootstrap value is
        // unavailable.  Falling straight to window.location.origin leaks /rpg,
        // /Assets, /browser, etc. into CinterForge's unrelated 20380 namespace.
        const baseURI=String(document?.baseURI||'').trim();
        if(baseURI){
            try { return new URL(baseURI).origin;
             } catch (_) {
             }
        }
        const origin=String(window.location?.origin||'').trim();
        return /^https?:\/\//i.test(origin) ? origin.replace(/\/$/,'') : '';
    }

    static presentationRootUrl(name) {
        const raw=String(name||'').replace(/^\/+/, '');
        const origin=CIFRuntime.presentationOrigin();
        if(origin){
            try { return new URL(raw, `${origin}/`).href;
             } catch (_) {
             }
        }
        return `/${raw}`;
    }

    static presentationAssetUrl(name) {
        const raw=String(name||'');
        const explicit=String(window.CINTERFORGE_IFPLAYER_PRESENTATION_BASE||'').trim();
        if(explicit && /^[a-z][a-z0-9+.-]*:/i.test(explicit)){
            try { return new URL(raw,explicit).href;
             } catch (_) {
             }
        }
        try { return new URL(raw, document.baseURI || window.location.href).href;
         } catch (_) {
         }
        return raw;
    }

    static runtimeAssetUrl(name) {
        // A World selected through File -> Load Story is outside the Editor's own
        // file:// tree. XHR-based consumers such as Babylon cannot be redirected
        // by the fetch shim, so resolve selected Story/Asset files to their owned
        // object URLs at this common resource boundary.
        const localRaw=String(name || '').replace(/^\/+/, '');
        const localKey=(localRaw.startsWith('Story/') || localRaw.startsWith('Assets/')) ? localRaw : `Story/${localRaw}`;
        // Babylon's glTF loader uses XHR rather than window.fetch, so the selected
        // World fetch shim cannot serve this one binary scene boundary. Give only
        // glTF/GLB consumers an object URL; JSON/SVG remain normal Story URLs and
        // are supplied by the selected-World fetch layer.
        if (/\.(?:gltf|glb)$/i.test(localKey) && window.CINTERFORGE_IF_LOCAL_WORLD_FILES instanceof Map && window.CINTERFORGE_IF_LOCAL_WORLD_FILES.has(localKey)) {
            const resolved=typeof window.CINTERFORGE_IF_FILE_RESOURCE_URL==='function' ? window.CINTERFORGE_IF_FILE_RESOURCE_URL(localKey) : '';
            if (resolved) {
                return resolved;
            }
        }
        // A shipped standalone Player is opened directly from file://.  It has no
        // runtime HTTP /ui endpoint, so route its static configuration through the
        // same Pack-relative namespaces embedded by CIFPlayFileResources.js.
        // In particular, input settings live under Runtime/Engine/Settings rather than
        // Story/.  Falling through to runtimeAssetBase() when API_READY is false
        // returned the bare `settings/...` path and left CIFPlayerInput unconfigured.
        if (String(window.location?.protocol || '') === 'file:') {
            const raw=String(name || '').replace(/^\/+/, '');
            if (raw.startsWith('settings/')) {
                return CIFRuntime.presentationAssetUrl(`Runtime/Engine/${raw}`);
            }
            if (raw.startsWith('data/')) {
                return CIFRuntime.presentationAssetUrl(`Runtime/Engine/Settings/${raw.slice(5)}`);
            }
            if (raw.startsWith('agent_')) {
                return CIFRuntime.presentationAssetUrl(`Media/agents/${raw.slice(6)}`);
            }
            return CIFRuntime.presentationAssetUrl(`Story/${raw}`);
        }
        const offlineEditorShell = window.CINTERFORGE_IF_PLAYER_SHELL !== true
            && window.CINTERFORGE_IF_API_READY !== true
            && window.CINTERFORGE_IF_RESOURCE_ONLY !== true;
        if (offlineEditorShell) {
            // A directly opened Editor has no /ui endpoint. Its immutable
            // presentation configuration is already packaged beside the Editor,
            // so resolve only those local static resources here. Story/world
            // products remain unloaded until File -> Load Story selects a World.
            const raw=String(name || '').replace(/^\/+/, '');
            if (raw.startsWith('settings/')) {
                return CIFRuntime.presentationAssetUrl(`Runtime/Engine/${raw}`);
            }
            if (raw.startsWith('data/')) {
                return CIFRuntime.presentationAssetUrl(`Runtime/Engine/Settings/${raw.slice(5)}`);
            }
            if (raw.startsWith('agent_')) {
                return CIFRuntime.presentationAssetUrl(`Media/agents/${raw.slice(6)}`);
            }
        }
        if (window.CINTERFORGE_IF_RESOURCE_ONLY) {
            // Embedded IFPlayer executes inside CinterForge's 20380 document, but
            // its static resources remain owned by the IFPlayer presentation
            // server (legacy standalone).  Resolve every resource-only URL against that
            // presentation origin instead of leaking root-relative requests to
            // the outer CinterForge document.
            const raw=String(name || '').replace(/^\/+/, '');
            if (raw.startsWith('settings/')) {
                return CIFRuntime.presentationAssetUrl(`Runtime/Engine/${raw}`);
            }
            if (raw.startsWith('data/')) {
                return CIFRuntime.presentationAssetUrl(`Runtime/Engine/Settings/${raw.slice(5)}`);
            }
            if (raw.startsWith('agent_')) {
                return CIFRuntime.presentationAssetUrl(`Media/agents/${raw.slice(6)}`);
            }
            return CIFRuntime.presentationAssetUrl(`Story/${raw}`);
        }
        /* Runtime presentation resources are physically staged under SVG/ by
           _stage_static_browser_runtime().  /ui was the old virtual resource
           boundary and is intentionally gone; routing generated world data back
           through it makes Map request a URL the current server cannot serve.
           Resolve the staged file through the presentation origin instead so
           standalone and embedded HTTP launches consume the same artifact. */
        const relativeName = String(name || '')
            .split('/')
            .filter(part => part.length > 0)
            .map(part => encodeURIComponent(part))
            .join('/');
        return CIFRuntime.presentationAssetUrl(`SVG/${relativeName}`);
    }

    /**
     * runtimeFetch()
     *
     * Implements runtime fetch for runtime routing and transport; this method is part of the class-owned presentation contract.
     *
     * Side effects: Reads runtime/bootstrap state and may update presentation caches; canonical interpreter state remains external.
     *
    * @param {*} path Input used by this operation; see method logic for accepted shape/semantics.
    * @param {*} options Input used by this operation; see method logic for accepted shape/semantics.
     *
    * @returns {Promise<*>} Resolves when the asynchronous operation completes; value is method-specific.
     */
    static async authoringFetch(path, options = {}) {
        const cleanPath = `/${String(path || '').replace(/^\/+/, '')}`;
        const base = String(window.CINTERFORGE_IF_AUTHORING_BASE || '').trim();
        if (!base || window.CINTERFORGE_IF_HOST_API_READY !== true) {
            throw new Error('IFPlayer authoring bridge unavailable');
        }
        // URL() gives Safari a canonical absolute URL instead of relying on
        // string concatenation of bootstrap values.  This is also where malformed
        // authoring bootstrap data now fails explicitly and diagnostically.
        let url;
        try { url = new URL(cleanPath, base.endsWith('/') ? base : `${base}/`).toString();
         }
        catch (error) { throw new Error(`Invalid IFPlayer authoring bridge URL ${JSON.stringify(base)}: ${error?.message||error}`);
            }
        return fetch(url, {cache:'no-store', ...options});
    }

    static async runtimeFetch(path, options = {}) {
        const cleanPath = `/${String(path || '').replace(/^\/+/, '')}`;
        let base = runtimeAssetBase();
        if (!base) {
            await reloadRuntimeBootstrap();
            base = runtimeAssetBase();
        }
        if (!base) {
            throw new Error('Traversal control unavailable');
        }
        try {
            return await fetch(`${base}${cleanPath}`, {cache:'no-store', ...options});
        } catch (firstError) {
            const oldPort = runtimeControlPort();
            await reloadRuntimeBootstrap();
            const newBase = runtimeAssetBase();
            if (!newBase || runtimeControlPort() === oldPort) {
                throw firstError;
            }
            return fetch(`${newBase}${cleanPath}`, {cache:'no-store', ...options});
        }
    }

    /**
     * reloadRuntimeBootstrap()
     *
     * Implements reload runtime bootstrap for runtime routing and transport; this method is part of the class-owned presentation contract.
     *
     * Side effects: Reads runtime/bootstrap state and may update presentation caches; canonical interpreter state remains external.
     *
    * @returns {*} The resolved/derived value for this presentation operation.
     */
    static reloadRuntimeBootstrap() {
        return new Promise(resolve => {
            const old = ifGetElementById('if-runtime-bootstrap-refresh');
            if (old)
                old.remove();
            const script = document.createElement('script');
            script.id = 'if-runtime-bootstrap-refresh';
            const presentationBase = String(window.CINTERFORGE_IFPLAYER_PRESENTATION_BASE || '');
            script.src = presentationBase
                ? new URL(`Runtime/Engine/Play/CIFPlayBootstrap.js?t=${Date.now()}`, presentationBase).href
                : `Runtime/Engine/Play/CIFPlayBootstrap.js?t=${Date.now()}`;
            script.onload = () => resolve(runtimeAssetBase());
            script.onerror = () => resolve('');
            document.head.appendChild(script);
        });
    }
}

// The editor shell owns Build reload acknowledgement.  Do not put this poller
// in CinterZMachineRuntime.js: that module is lazy-loaded, and a new story can
// legitimately reach Build before any Z-machine session has caused it to load.
let cinterIFRuntimeReloadRevision = 0;
async function cinterIFHandleRuntimeReload(requested, storyUrl) {
    requested = Number(requested || 0);
    if (!requested || requested <= cinterIFRuntimeReloadRevision) {
        return;
    }
    cinterIFRuntimeReloadRevision = requested;
    try {
        storyUrl = String(storyUrl || '').trim();
        if (!storyUrl) {
            throw new Error('Runtime reload request is missing the staged story URL');
        }
        // Build may change the Z-machine format (for example Library .z3 -> freshly
        // compiled .z8). Reload the server-published bootstrap before constructing
        // the replacement VM so CIFBrowserHost reads the new Story URL.
        await CIFRuntime.reloadRuntimeBootstrap();
        window.CINTERFORGE_IF_STORY_URL = storyUrl;
        console.info('[if-build-reload-url]', {revision:requested, story_url:storyUrl});
        await CIFRuntime.ensureBrowserZMachine();
        // Build reload belongs to the Project currently presented by the editor.
        // CINTERFORGE_IF_CINTER_GAME is Z-machine session state and can still name
        // the previous Project during an in-process project switch.  Using it here
        // caused a Devours Build to reload Dracula WorldData.
        const game = String(
            (typeof activeGameKey === 'function' ? activeGameKey() : '') ||
            window.CINTERFORGE_IF_SELECTED_STORY ||
            window.CINTERFORGE_IF_ACTIVE_GAME ||
            ''
        ).trim();
        if (!game) {
            throw new Error('Runtime reload has no selected Project identity');
        }
        window.CINTERFORGE_IF_CINTER_GAME = game;
        if (typeof window.cinterZMachinePlayerNew !== 'function') {
            throw new Error('Browser JavaScript Z-machine reload API unavailable');
        }
        await window.cinterZMachinePlayerNew(game);

        // Build replaces Project/Story and may initialize Project/Data. The
        // resident CIFWorld and Asset Assignment caches still describe the
        // pre-Build Project until explicitly invalidated. Refresh the same
        // authoring authority used by Map, World Browser and Assets before
        // acknowledging a successful reload.
        if (!window.CIFWorldStore?.loadWorldDataForGame) {
            throw new Error('Build reload cannot refresh canonical WorldData');
        }
        const reloadedWorldData = await window.CIFWorldStore.loadWorldDataForGame(game, true);
        if (!reloadedWorldData) {
            throw new Error(`Build reload returned no WorldData for ${game}`);
        }
        if (window.CIFAssetBrowser?.LoadAssignments) {
            await window.CIFAssetBrowser.LoadAssignments();
        }
        console.info('[if-build-reload-worlddata]', {
            game,
            rooms: Object.keys(reloadedWorldData.rooms?.records || {}).length,
            objects: Object.keys(reloadedWorldData.objects?.records || {}).length,
            characters: Object.keys(reloadedWorldData.characters?.records || {}).length
        });

        if (typeof CIFGraph !== 'undefined' && typeof CIFGraph.refreshMajorSurfaces === 'function') {
            await CIFGraph.refreshMajorSurfaces(requested, true);
        }
        await fetch('/host/runtime-reload-ack', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({revision:requested,state:'ready',game})});
        console.info('[if-build-reload-host]', {game, revision:requested, state:'ready'});
    } catch (error) {
        const message=String(error?.message||error);
        await fetch('/host/runtime-reload-ack', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({revision:requested,state:'failed',error:message})});
        console.error('[if-build-reload-host]', {revision:requested, state:'failed', error:message});
    }
}
let cinterIFRuntimeReloadPollPromise = null;
let cinterIFRuntimeReloadLastPollAt = 0;
async function cinterIFPollRuntimeReload(force = false) {
    const now = Date.now();
    // Safari can emit rapid focus/visibility churn while the embedded Code-OSS
    // iframe changes focus. Coalesce those events so they cannot hammer the
    // authoring server or compete with asset requests. BroadcastChannel remains
    // the immediate reload path; this fetch is recovery only.
    if (!force && now - cinterIFRuntimeReloadLastPollAt < 2000) {
        return;
    }
    if (cinterIFRuntimeReloadPollPromise) {
        return cinterIFRuntimeReloadPollPromise;
    }
    cinterIFRuntimeReloadLastPollAt = now;
    cinterIFRuntimeReloadPollPromise = (async () => {
        try {
            const response = await fetch('/host/runtime-reload-state', {cache:'no-store'});
            if (!response.ok) {
                return;
            }
            const state = await response.json();
            await cinterIFHandleRuntimeReload(state?.requested, state?.story_url);
        } catch (_) {
        } finally {
            cinterIFRuntimeReloadPollPromise = null;
        }
    })();
    return cinterIFRuntimeReloadPollPromise;
}
if (!window.CINTERFORGE_IF_ASSET_WORKSPACE && (!window.CINTERFORGE_IF_RESOURCE_ONLY || String(window.CINTERFORGE_IF_AUTHORING_BASE || '').trim()) && /^https?:$/.test(String(window.location?.protocol || ''))) {
    // Code-OSS Build and the editor live in different browser contexts.  The
    // same BroadcastChannel already used for editor<->Code commands gives the
    // editor an immediate reload signal; the host-state poll remains the
    // durable recovery path if that signal is missed.
    if (typeof BroadcastChannel === 'function') {
        const reloadChannel = new BroadcastChannel('cinterforge-ifplayer-code-ui');
        reloadChannel.onmessage = event => {
            if (event?.data?.type === 'runtime-reload-requested')
                void cinterIFHandleRuntimeReload(event.data.revision, event.data.story_url);
        };
        window.__cinterforgeRuntimeReloadChannel = reloadChannel;
    }
    // Recover a reload request that was already pending when this document
    // started, but do not continuously poll the authoring server. Normal
    // reload requests arrive through the BroadcastChannel above. Re-check
    // only when this browser context becomes active again in case an event
    // was missed while it was suspended.
    cinterIFPollRuntimeReload();
    window.addEventListener('focus', () => void cinterIFPollRuntimeReload());
    document.addEventListener('visibilitychange', () => {
        if (document.visibilityState === 'visible')
            void cinterIFPollRuntimeReload();
    });
}

const runtimeControlPort = CIFRuntime.runtimeControlPort.bind(CIFRuntime);
const runtimeAssetBase = CIFRuntime.runtimeAssetBase.bind(CIFRuntime);
const cinterGameKey = CIFRuntime.cinterGameKey.bind(CIFRuntime);
const primaryAgentForGame = CIFRuntime.primaryAgentForGame.bind(CIFRuntime);
const unifiedPlayerTotals = CIFRuntime.unifiedPlayerTotals.bind(CIFRuntime);
const forwardTransitionForCommand = CIFRuntime.forwardTransitionForCommand.bind(CIFRuntime);
const backwardTransitionForCommand = CIFRuntime.backwardTransitionForCommand.bind(CIFRuntime);
const advanceCompletedStory = CIFRuntime.advanceCompletedStory.bind(CIFRuntime);
const newUnifiedGame = CIFRuntime.newUnifiedGame.bind(CIFRuntime);
const viewedGameForActor = CIFRuntime.viewedGameForActor.bind(CIFRuntime);
const agentThumbnailUrl = CIFRuntime.agentThumbnailUrl.bind(CIFRuntime);
const runtimeThumbnailUrl = CIFRuntime.runtimeThumbnailUrl.bind(CIFRuntime);
const selectViewedActor = CIFRuntime.selectViewedActor.bind(CIFRuntime);
const loadUnifiedWorldStates = CIFRuntime.loadUnifiedWorldStates.bind(CIFRuntime);
const unifiedActorRoom = CIFRuntime.unifiedActorRoom.bind(CIFRuntime);
const disposeUnifiedActors = CIFRuntime.disposeUnifiedActors.bind(CIFRuntime);
const ensureUnifiedActorMarker = CIFRuntime.ensureUnifiedActorMarker.bind(CIFRuntime);
const refreshUnifiedActorPositions = CIFRuntime.refreshUnifiedActorPositions.bind(CIFRuntime);
const runtimeBaseForGame = CIFRuntime.runtimeBaseForGame.bind(CIFRuntime);
const storyResourceUrl = CIFRuntime.storyResourceUrl.bind(CIFRuntime);
const storyResourceJson = CIFRuntime.storyResourceJson.bind(CIFRuntime);
const postRuntimeForGame = CIFRuntime.postRuntimeForGame.bind(CIFRuntime);
const setCinterWorld = CIFRuntime.setCinterWorld.bind(CIFRuntime);
const presentationAssetUrl = CIFRuntime.presentationAssetUrl.bind(CIFRuntime);
const runtimeAssetUrl = CIFRuntime.runtimeAssetUrl.bind(CIFRuntime);
const runtimeFetch = CIFRuntime.runtimeFetch.bind(CIFRuntime);
const reloadRuntimeBootstrap = CIFRuntime.reloadRuntimeBootstrap.bind(CIFRuntime);

// OWB modules resolve managed assets through the presentation runtime.  A top-level
// class declared by a classic script is a global lexical binding, not a property of
// window/globalThis, so ES modules cannot discover it through globalThis.  Publish
// the canonical runtime explicitly for module consumers (Test/Build/Play).
window.CIFRuntime=CIFRuntime;
window.CINTERFORGE_DIAGNOSE_ASSET_FAILURE=(url,error)=>CIFRuntime.diagnoseAssetFailure(url,error);

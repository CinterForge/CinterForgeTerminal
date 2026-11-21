/**
 * Asset acquisition/download ownership.
 *
 * Extracted from the legacy Asset Library controller. Methods intentionally
 * execute with the active Asset Library controller as `this` while the larger
 * refactor continues, so ownership moves without duplicating runtime state.
 */
class CIFAssetDownload {
    static VERSION = 'refactor-v3';

    static async SaveExternalAssignment(button){
        const provider=String(button.dataset.provider||'library'), sourceKind=String(button.dataset.sourceKind||''), id=String(button.dataset.assetId||''), query=String(button.closest('.assignment-node')?.querySelector('[data-assignment-search-form] input')?.value||'').trim();
        const card=button.closest('.assignment-node');
        if (!id) {
            throw new Error('Asset id is missing.');
        }

        button.textContent='Downloading\u2026';
        this.SetProgress(card,'Downloading asset\u2026');
        await window.CIFRuntime.assetCommand({operation:'download',provider,kind:sourceKind,id,q:query,mode:'download'});
        // Objaverse models can legitimately take several minutes to acquire. Keep
        // the original Assign operation alive until the server reports completion
        // instead of silently abandoning it after the old 60-second polling window.
        const downloadDeadline = Date.now() + (10 * 60 * 1000);
        while (Date.now() < downloadDeadline) {
            const state=await window.CIFRuntime.assetCommand({operation:'status',provider,kind:sourceKind,id});
            if (state.complete) {
                if (state.error) {
                    button.textContent='Download failed';
                    this.SetProgress(card,`Download failed: ${state.error}`);
                    throw new Error(state.error);
                }

                // Download/import owns the local Assets commit. Assignment only
                // records that portable local asset identity in WorldData. Do not
                // run the global AssetMigration catalog or publish to Cloudflare
                // merely because the user clicked Assign. Push/Build own publish.
                let relativePath = String(state.saved_path || state.relative_path || '').replace(/\\/g, '/');
                const assetsMarker = '/Builds/Assets/';
                const assetsMarkerIndex = relativePath.lastIndexOf(assetsMarker);
                if (assetsMarkerIndex >= 0) {
                    relativePath = relativePath.slice(assetsMarkerIndex + assetsMarker.length);
                }
                relativePath = relativePath
                    .replace(/^<builds>\/InteractiveFiction\/Assets\//, '')
                    .replace(/^Builds\/InteractiveFiction\/Assets\//, '');

                if (!relativePath) {
                    throw new Error('Downloaded asset did not return its local Assets intake path.');
                }

                // Provider downloads land in the canonical Assets intake tree first.
                // A newly acquired file is not assignable until the managed conversion
                // pipeline has minted its UCI and produced the canonical asset folder.
                // Preserve the user's AssetMigration/Blockify settings: conversion uses
                // the reconciled catalog row rather than inventing assignment defaults.
                let managedState = state;
                if (state.needs_conversion) {
                    button.textContent='Converting\u2026';
                    this.SetProgress(card,'Download complete. Minting managed asset\u2026');
                    managedState = await this.ConvertDownloadedAsset(state, sourceKind, card);
                }

                let managedPath = String(
                    managedState?.managed_path ||
                    managedState?.relative_path ||
                    managedState?.saved_path ||
                    relativePath
                ).replace(/\\/g, '/');
                const managedAssetsMarkerIndex = managedPath.lastIndexOf(assetsMarker);
                if (managedAssetsMarkerIndex >= 0) {
                    managedPath = managedPath.slice(managedAssetsMarkerIndex + assetsMarker.length);
                }
                managedPath = managedPath
                    .replace(/^<builds>\/InteractiveFiction\/Assets\//, '')
                    .replace(/^Builds\/InteractiveFiction\/Assets\//, '');

                managedPath = CIFAsset.MarkLocalReady(managedPath);
                console.info('[IFPlayer Assets] assignment acquisition complete', {
                    provider,
                    sourceKind,
                    id,
                    intakePath: relativePath,
                    managedPath,
                    uci: String(managedState?.uci || ''),
                    converted: Boolean(state.needs_conversion)
                });

                button.textContent='Assigning\u2026';
                this.SetProgress(card,'Managed asset ready. Assigning\u2026');
                return managedPath;
            }
            const downloadMessage=String(state.message||'').trim();
            if (downloadMessage) {
                this.SetProgress(card,downloadMessage);
            }
            await new Promise(resolve=>setTimeout(resolve,250));
        }
        button.textContent='Download failed';
        this.SetProgress(card,'Download failed: timed out waiting for the local asset.');
        throw new Error('Asset download did not complete.');
    }

    static async StartAssetDownload(card) {
        const kind=String(card.dataset.assetKind||''), id=String(card.dataset.assetId||'');
        this.SetProgress(card, this.text('objaverse_downloading','Downloading\u2026'));
        await CIFRuntime.assetCommand({operation:'download',provider:'library',kind,id,q:this.query});
        return new Promise((resolve,reject)=>{
            const check=async()=>{try{const state=await CIFRuntime.assetCommand({operation:'status',provider:'library',kind,id});
                let text=state.message||state.stage||'Working\u2026';
                if(state.saved_path) text+=` \u00b7 ${state.saved_path}`;
                this.SetProgress(card,text);
                if(state.complete){clearInterval(timer);
                state.error?reject(new Error(state.error)):resolve(state);
                }}catch(error){clearInterval(timer);
                reject(error);
                }};
                const timer=setInterval(check,650);
                check();
        });
    }

}

window.CIFAssetDownload = CIFAssetDownload;
console.info('[CIF Asset] CIFAssetDownload loaded', CIFAssetDownload.VERSION);

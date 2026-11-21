/**
 * CIFAssetPD12M ownership extracted from the legacy Asset Library controller.
 *
 * Methods intentionally execute with the controller as `this` while the refactor
 * proceeds, preserving current state without duplicating provider/preview state.
 */
class CIFAssetPD12M {
    static VERSION = 'provider-v1';

    static RenderImages(rows) {
        const el = this.elements();
         if (!el.results) {
             return;
         }
        if (!rows.length) { el.results.innerHTML = `<div class="objaverse-empty">${this.escapeHtml(this.text('asset_images_empty','No matching images.'))}</div>`;
            return;
            }
        el.results.innerHTML = rows.map((row,index) => {
            const id=this.escapeHtml(row.id||''), title=this.escapeHtml(row.title||row.caption||'(untitled)'), thumb=this.escapeHtml(row.thumbnail_url||''), source=this.escapeHtml(row.source||'PD12M'), license=this.escapeHtml(row.license||'');
            return `<article class="objaverse-card asset-image-card" data-asset-kind="image" data-asset-id="${id}">
              <div class="objaverse-visual">${thumb?`<img class="objaverse-thumb" loading="lazy" data-staged-resource="image-thumb" data-staged-id="${id}" alt="${title}">`:''}<span class="objaverse-rank">#${index+1}</span><span class="objaverse-license">${license}</span></div>
              <div class="objaverse-card-body"><h2 class="objaverse-title">${title}</h2><div class="objaverse-byline">${source}</div><div class="objaverse-stats">${this.fmt(row.width)} \u00d7 ${this.fmt(row.height)}</div>
              <div class="objaverse-actions"><button class="objaverse-action" data-action="asset-download">${this.escapeHtml(this.text('objaverse_download','Download'))}</button>${row.source_url?`<a class="objaverse-action" href="${this.escapeHtml(row.source_url)}" target="_blank" rel="noopener">${this.escapeHtml(this.text('objaverse_source','Source'))}</a>`:''}</div></div></article>`;
        }).join('');
        this.HydrateStagedImages();
    }
}

window.CIFAssetPD12M = CIFAssetPD12M;
console.info('[CIF Asset] CIFAssetPD12M loaded', CIFAssetPD12M.VERSION);

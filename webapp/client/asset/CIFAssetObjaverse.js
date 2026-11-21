/**
 * CIFAssetObjaverse ownership extracted from the legacy Asset Library controller.
 *
 * Methods intentionally execute with the controller as `this` while the refactor
 * proceeds, preserving current state without duplicating provider/preview state.
 */
class CIFAssetObjaverse {
    static VERSION = 'provider-v1';

    static Render(rows) {
        if (this.mode === 'pd12m') {
            return this.RenderImages(rows);
        }
        if (this.mode === 'mixamo') {
            return this.mixamoSearchKind==='character' ? this.RenderCharacters(rows) : this.RenderAnimations(rows);
        }
        const el = this.elements();
        if (!el.results) {
            return;
        }
        if (!rows.length) {
            el.results.innerHTML = `<div class="objaverse-empty">${this.escapeHtml(this.text('objaverse_empty', 'No matching CC0 / CC-BY assets.'))}</div>`;
            return;
        }
        el.results.innerHTML = rows.map((row, index) => {
            if (row.provider === 'mixamo') {
                const id=this.escapeHtml(row.id||''), name=this.escapeHtml(row.name||row.title||'(unnamed)'), license=this.escapeHtml(row.license||'Mixamo');
                return `<article class="objaverse-card" data-asset-kind="3d" data-asset-id="${id}"><div class="objaverse-visual"><div class="asset-animation-glyph">3D</div><span class="objaverse-rank">#${index+1}</span><span class="objaverse-license">${license}</span></div><div class="objaverse-card-body"><h2 class="objaverse-title">${name}</h2><div class="objaverse-byline">${this.escapeHtml(this.text('asset_mixamo_character_byline','Mixamo \u00b7 character'))}</div><div class="objaverse-stats">${this.fmt(row.bone_count)} bones \u00b7 ${row.covers_animation_rig?'shared animation rig':'nonstandard rig'}</div><div class="objaverse-actions"><button class="objaverse-action" data-action="asset-download">${this.escapeHtml(this.text('objaverse_download','Download'))}</button>${row.source_url?`<a class="objaverse-action" href="${this.escapeHtml(row.source_url)}" target="_blank" rel="noopener">${this.escapeHtml(this.text('objaverse_source','Source'))}</a>`:''}</div></div></article>`;
            }
            const uid = this.escapeHtml(row.uid || '');
            const name = this.escapeHtml(row.name || '(unnamed)');
            const creator = this.escapeHtml(row.creator || this.text('objaverse_unknown_creator', 'Unknown'));
            const license = this.escapeHtml(String(row.license || 'unknown').toUpperCase());
            const thumb = this.escapeHtml(row.thumbnail_url || '');
            const description = this.escapeHtml(row.description || '');
            const viewer = this.escapeHtml(row.viewer_url || '');
            const disabled = row.is_downloadable === false || row.is_protected === true;
            return `<article class="objaverse-card" data-uid="${uid}">
                <div class="objaverse-visual">
                  <img class="objaverse-thumb" loading="lazy" data-staged-resource="objaverse-thumb" data-staged-id="${uid}" alt="${name}">
                  <canvas class="objaverse-canvas" aria-label="3D preview for ${name}"></canvas>
                  <span class="objaverse-rank">#${index + 1}</span><span class="objaverse-license">${license}</span>
                </div>
                <div class="objaverse-card-body">
                  <h2 class="objaverse-title">${name}</h2>
                  <div class="objaverse-byline">${creator}</div>
                  <div class="objaverse-stats">${this.escapeHtml(this.text('objaverse_faces','Faces'))} ${this.fmt(row.face_count)} \u00b7 ${this.escapeHtml(this.text('objaverse_vertices','Vertices'))} ${this.fmt(row.vertex_count)} \u00b7 ${this.escapeHtml(this.text('objaverse_animations','Anim'))} ${this.fmt(row.animation_count)} \u00b7 ${this.escapeHtml(this.text('objaverse_likes','Likes'))} ${this.fmt(row.like_count)}</div>
                  ${description ? `<div class="objaverse-description">${description}</div>` : ''}
                  <div class="objaverse-actions">
                    <button class="objaverse-action" data-action="preview">GLB Preview</button>
                    <button class="objaverse-action" data-action="objaverse-minecraft-preview">Blockify</button>
                    <button class="objaverse-action" data-action="objaverse-export">Export</button>
                    <button class="objaverse-action" data-action="objaverse-remove">Remove</button>
                  </div>

                </div>
              </article>`;
        }).join('');
        this.HydrateStagedImages(null,{eager:this.mode==='objaverse'});
    }
}

window.CIFAssetObjaverse = CIFAssetObjaverse;
console.info('[CIF Asset] CIFAssetObjaverse loaded', CIFAssetObjaverse.VERSION);

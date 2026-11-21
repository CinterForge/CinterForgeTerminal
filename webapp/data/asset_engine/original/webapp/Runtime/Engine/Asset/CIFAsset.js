/**
 * Canonical CinterForge asset identity, resolution, and session availability authority.
 *
 * Asset identity is currently the portable path relative to
 * Builds/Assets. Provider catalogs are discovery metadata; they
 * are not assignment identity. UCI identity will replace the path identity here,
 * without requiring asset consumers to own resolution policy themselves.
 */
class CIFAsset {
    static localReadyPaths = new Set();

    static NormalizePath(path) {
        return String(path || '')
            .trim()
            .replace(/\\/g, '/')
            .replace(/^<builds>\/InteractiveFiction\/Assets\//, '')
            .replace(/^Builds\/InteractiveFiction\/Assets\//, '')
            .replace(/^\/Assets\//, '')
            .replace(/^Assets\//, '')
            .replace(/^\/+/, '');
    }

    static ValidateManagedPath(path) {
        const normalizedPath = this.NormalizePath(path);
        if (!normalizedPath) {
            throw new Error('Shared asset path is required.');
        }
        if (/^(?:https?:|file:|blob:|data:)/i.test(normalizedPath)) {
            throw new Error(`Managed asset request must be a relative path: ${path}`);
        }
        if (normalizedPath.split('/').includes('..')) {
            throw new Error(`Invalid shared asset path: ${path}`);
        }
        return normalizedPath;
    }

    static MarkLocalReady(path) {
        const normalizedPath = this.NormalizePath(path);
        if (normalizedPath) {
            this.localReadyPaths.add(normalizedPath.toLowerCase());
        }
        return normalizedPath;
    }

    static IsLocalReady(path) {
        const normalizedPath = this.NormalizePath(path);
        return normalizedPath ? this.localReadyPaths.has(normalizedPath.toLowerCase()) : false;
    }

    static Reference(path, kind = 'asset', metadata = {}) {
        const relativePath = this.ValidateManagedPath(path);
        return {
            path: relativePath,
            kind: String(kind || 'asset'),
            ...metadata
        };
    }

    static PresentationUrl(path) {
        const resourcePath = String(path || '')
            .trim()
            .replace(/\\/g, '/')
            .replace(/^\/+/, '');
        if (!resourcePath || resourcePath.split('/').includes('..')) {
            throw new Error(`Invalid IFPlayer resource path: ${path}`);
        }

        const explicitBase = String(globalThis.CINTERFORGE_IFPLAYER_PRESENTATION_BASE || '').trim();
        if (explicitBase) {
            try {
                return new URL(resourcePath, `${new URL(explicitBase).origin}/`).href;
            } catch (_) {
                // Fall through to the active document origin.
            }
        }

        const origin = String(globalThis.location?.origin || '').replace(/\/$/, '');
        return /^https?:\/\//i.test(origin) ? `${origin}/${resourcePath}` : `/${resourcePath}`;
    }

    static Url(path) {
        const relativePath = this.ValidateManagedPath(path);
        const runtime = globalThis.CIFRuntime;
        if (!runtime || typeof runtime.requestAsset !== 'function') {
            throw new Error(`CinterForge managed asset resolver is unavailable for ${relativePath}`);
        }
        return runtime.requestAsset(relativePath);
    }

    static RelativeUrl(baseUrl, relativePath) {
        const relative = String(relativePath || '').trim().replace(/\\/g, '/');
        if (!relative) {
            throw new Error('Relative asset path is required.');
        }
        if (/^(?:https?:|blob:|data:)/i.test(relative)) {
            return relative;
        }
        return new URL(relative, String(baseUrl || globalThis.location?.href || '')).toString();
    }

    static RPGUrl(path) {
        const relativePath = String(path || '')
            .trim()
            .replace(/\\/g, '/')
            .replace(/^\/?rpg\//i, '')
            .replace(/^\/+/, '');
        if (!relativePath || relativePath.split('/').includes('..')) {
            throw new Error(`Invalid RPG resource path: ${path}`);
        }
        return this.PresentationUrl(`rpg/${relativePath}`);
    }

    static Root() {
        const probeName = '__cinterforge_asset_root__';
        const probeUrl = this.Url(probeName);
        return probeUrl.slice(0, -probeName.length);
    }
}

window.CIFAsset = CIFAsset;

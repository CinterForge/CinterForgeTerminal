/*
 * ES-module boundary for the canonical CIFAsset authority.
 *
 * Retained Open World Builder modules are ES modules while CIFAsset remains a
 * classic script shared by IFPlayer and IFEditor. This file exposes that single
 * authority without creating a second asset implementation.
 */
function assetAuthority() {
    const authority = globalThis.CIFAsset;
    if (!authority) {
        throw new Error('CIFAsset authority is unavailable.');
    }
    return authority;
}

export function presentationRootUrl(path) {
    return assetAuthority().PresentationUrl(path);
}

export function assetUrl(path) {
    return assetAuthority().Url(path);
}

export function assetRelativeUrl(baseUrl, relativePath) {
    return assetAuthority().RelativeUrl(baseUrl, relativePath);
}

export function rpgUrl(path) {
    return assetAuthority().RPGUrl(path);
}

export function assetRoot() {
    return assetAuthority().Root();
}

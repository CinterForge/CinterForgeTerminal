/*
 * CINTERFORGE SOURCE HEADER — webapp/launcher/app.js
 * PURPOSE: Implements the existing app launcher presentation and native guest desktop routing.
 * OWNERSHIP: This authored source is part of one canonical Terminal runtime.
 * STARTUP: Called or loaded by the existing lifecycle/entry point; no
 *          hidden dependency provisioning or alternate VM is allowed.
 * SHUTDOWN: Release only resources explicitly owned by this component.
 * CALLERS: Preserve existing APIs, filenames, CLI flags and data formats.
 * KEY CLASSES / METHODS:
 *   showToast: Show show toast for this module.
 * MAINTENANCE: Keep generated outputs in sibling Builds or the actual
 * Steam library; do not change Lagomorph timings, user assets or guest
 * readiness without tracing their real callers and failure boundaries.
*/
/* Shared CFOS launcher presentation. The parent Terminal owns all native or
 * guest process execution; this unprivileged iframe only emits allowlisted IDs.
 * No simulated desktop/process claims are made when the host is unavailable.
 */
'use strict';
const params = new URLSearchParams(location.search);
const platform = ['mac', 'linux', 'win'].includes(params.get('platform')) ? params.get('platform') : 'mac';
const toast = document.getElementById('toast');
const viewer = document.getElementById('remote-desktop');
const surface = document.getElementById('guest-surface');
const title = {mac: 'Mac', linux: 'Linux', win: 'Windows'}[platform];
// All three platforms share one dock and one staged-app action. Only the
// independently replaceable background PNG and execution backend differ.
const wallpaperFile = {mac: 'mac', linux: 'linux', win: 'windows'}[platform];
document.getElementById('wallpaper').src = `assets/backgrounds/${wallpaperFile}.png`;
const clock = document.getElementById('launcher-clock');
clock.textContent = new Date().toLocaleString([], {weekday:'short', hour:'numeric', minute:'2-digit'});
let toastTimer;
function showToast(message, persistent = false) {
  toast.textContent = String(message || '');
  toast.style.display = 'block';
  clearTimeout(toastTimer);
  if (!persistent) toastTimer = setTimeout(() => { toast.style.display = 'none'; }, 4500);
}
for (const button of document.querySelectorAll('[data-app]')) {
  button.addEventListener('click', () => {
    window.parent.postMessage({type: 'cinter-launcher', action: 'launch', platform,
      app: button.getAttribute('data-app')}, location.origin);
  });
}
// Only show this launcher action after Terminal has probed Cloudflare credentials.
// It uses the standard OS chooser in the parent WebView, not a guest file path.
const assetImport = document.getElementById('assets-import-launcher');
assetImport.addEventListener('click', () => {
  if (assetImport.hidden) return;
  window.parent.postMessage({type: 'cinter-launcher', action: 'assets-import', platform}, location.origin);
});
window.addEventListener('message', event => {
  if (event.origin !== location.origin || event.source !== window.parent ||
      event.data?.type !== 'cinter-launcher-assets' || event.data?.platform !== platform) return;
  assetImport.hidden = event.data.available !== true;
});
document.getElementById('tray').addEventListener('click', () => showToast(title + ' session'));
window.addEventListener('message', event => {
  if (event.origin !== location.origin || event.source !== window.parent ||
      event.data?.type !== 'cinter-launcher-session' || event.data?.platform !== platform) return;
  if (event.data.error) {
    showToast(event.data.error, true);
    return;
  }
  if (platform !== 'mac' && event.data.viewer_url) {
    const target = new URL(event.data.viewer_url);
    // The viewer is an HTTP page on the SAME hostname, but a distinct
    // port. Its RFB client connects over WS 20304/20305.
    if (target.protocol !== 'http:' || target.hostname !== location.hostname ||
        target.port !== (platform === 'linux' ? '20301' : '20302')) return;
    if (viewer.src !== target.href) viewer.src = target.href;
    surface.hidden = false;
  }
  if (event.data.message) showToast(event.data.message);
});

// Do not claim an X11 window is user-visible until the iframe's actual noVNC
// RFB client has completed its connection to the corresponding guest display.
window.addEventListener('message', event => {
  const port = platform === 'linux' ? '20301' : '20302';
  if (platform === 'mac' || event.source !== viewer.contentWindow ||
      event.origin !== `http://${location.hostname}:${port}` ||
      event.data?.type !== 'cinter-vnc-state' || event.data?.platform !== platform) return;
  showToast(event.data.message, event.data.phase !== 'connected');
  window.parent.postMessage({type:'cinter-launcher-viewer', platform,
    phase:event.data.phase, message:event.data.message}, location.origin);
});

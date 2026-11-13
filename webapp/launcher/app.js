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
function showToast(message) {
  toast.textContent = String(message || '');
  toast.style.display = 'block';
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { toast.style.display = 'none'; }, 3500);
}
for (const button of document.querySelectorAll('[data-app]')) {
  button.addEventListener('click', () => {
    window.parent.postMessage({type: 'cinter-launcher', action: 'launch', platform,
      app: button.getAttribute('data-app')}, location.origin);
  });
}
document.getElementById('tray').addEventListener('click', () => showToast(title + ' session'));
window.addEventListener('message', event => {
  if (event.origin !== location.origin || event.source !== window.parent ||
      event.data?.type !== 'cinter-launcher-session' || event.data?.platform !== platform) return;
  if (event.data.error) {
    showToast(event.data.error);
    return;
  }
  if (platform !== 'mac' && event.data.viewer_url) {
    const target = new URL(event.data.viewer_url);
    // The VNC bridge is forwarded by the owned Colima guest to loopback only.
    if (target.protocol !== 'http:' || !['127.0.0.1', 'localhost'].includes(target.hostname) ||
        !['20601', '20602'].includes(target.port)) return;
    if (viewer.src !== target.href) viewer.src = target.href;
    surface.hidden = false;
  }
  if (event.data.message) showToast(event.data.message);
});

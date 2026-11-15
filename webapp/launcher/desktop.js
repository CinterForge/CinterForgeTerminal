/* CinterForge desktop viewer — separate module required by script-src 'self'.
 * 20301/20302 serve this script; 20304/20305 carry RFB over WebSocket.
 * The native/guest backend remains owned by the existing Terminal server.
 */
'use strict';
const platform = document.body.dataset.platform;
const wsPort = document.body.dataset.wsPort;
const status = document.getElementById('state');
const ports = {linux: '20304', win: '20305'};

function report(phase, message) {
  status.textContent = message;
  status.hidden = phase === 'connected';
  if (window.parent !== window) {
    window.parent.postMessage({type: 'cinter-vnc-state', platform, phase, message},
      `http://${location.hostname}:20300`);
  }
}

if (!Object.hasOwn(ports, platform) || ports[platform] !== wsPort) {
  report('error', 'Invalid VNC viewer configuration');
} else {
  try {
    const {default: RFB} = await import('/core/rfb.js');
    const endpoint = `ws://${location.hostname}:${wsPort}/websockify`;
    const rfb = new RFB(document.getElementById('desktop'), endpoint);
    rfb.scaleViewport = true;
    rfb.clipViewport = false;
    rfb.addEventListener('connect', () => report('connected', platform + ' VNC display connected'));
    rfb.addEventListener('disconnect', event => report('disconnected', platform +
      ' VNC disconnected' + (event.detail?.clean ? '' : ' unexpectedly')));
    rfb.addEventListener('securityfailure', () => report('error',
      platform + ' VNC security negotiation failed'));
  } catch (error) {
    report('error', platform + ' VNC error: ' + error.message);
  }
}

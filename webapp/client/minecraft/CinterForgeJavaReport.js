/* Terminal-only desktop session adapter for the CraftMaster-owned report.
 * The owner report keeps its inventory, pack, conversion and Atlas controls.
 * Java and Bedrock use their existing independent visible console sessions.
 */
(() => {
  'use strict';
  const bedrock = document.getElementById('server');
  const toolbar = document.querySelector('.toolbar');
  if (!bedrock || !toolbar || document.documentElement.hasAttribute('data-hosted')) return;
  const java = document.createElement('button');
  java.id = 'java-server'; java.type = 'button'; java.disabled = true; java.textContent = 'Start Java';
  toolbar.insertBefore(java, bedrock);
  const notice = document.getElementById('notice');
  const endpoint = '/api/bedrock/report/java';
  let running = false, busy = false, checking = false;
  async function request(action) {
    const response = await fetch(endpoint, {credentials:'same-origin', cache:'no-store',
      ...(action ? {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({action})} : {})});
    const body = await response.json();
    if (!response.ok) throw new Error(body.error || 'Java request failed');
    return body;
  }
  function labelBedrock() {
    const text = bedrock.textContent;
    if (text === 'Start Server' || text === 'Start') bedrock.textContent = 'Start Bedrock';
    if (text === 'Stop Server' || text === 'Stop') bedrock.textContent = 'Stop Bedrock';
  }
  new MutationObserver(labelBedrock).observe(bedrock, {childList:true, characterData:true, subtree:true});
  labelBedrock();
  async function refresh() {
    if (checking || busy) return;
    checking = true;
    try {
      const state = await request(); running = state.running;
      java.textContent = running ? 'Stop Java' : 'Start Java';
      java.className = running ? 'stop' : 'primary';
      java.disabled = busy || (!running && !state.can_start);
    } catch (e) { java.disabled = true; java.title = e.message; }
    finally { checking = false; }
  }
  java.addEventListener('click', async () => {
    if (busy) return; busy = true; java.disabled = true;
    try {
      const result = await request(running ? 'stop' : 'start');
      notice.textContent = result.message; notice.classList.remove('error');
    } catch (e) { notice.textContent = e.message; notice.classList.add('error'); }
    finally { busy = false; void refresh(); }
  });
  void refresh(); setInterval(refresh, 2000);
})();

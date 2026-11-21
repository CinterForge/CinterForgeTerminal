/* Bedrock Report: one world inventory served by Terminal, no alternate state store. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const notice = $('notice');
  let inventory;
  let busy = false;
  const report = (text, error = false) => { notice.textContent = String(text || ''); notice.classList.toggle('error', error); };
  async function api(url, data) {
    const response = await fetch(url, {method: data === undefined ? 'GET' : 'POST', credentials:'same-origin', cache:'no-store',
      headers: data === undefined ? {} : {'Content-Type':'application/json'},
      ...(data === undefined ? {} : {body:JSON.stringify(data)})});
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || `HTTP ${response.status}`);
    return result;
  }
  const send = (action, payload = {}) => parent.postMessage({type:'cinter-bedrock-report', action, ...payload}, location.origin);
  function date(value) {return value ? new Date(value * 1000).toLocaleString() : 'Unknown';}
  function button(text, action, disabled = false) {
    const node = document.createElement('button');node.type='button';node.textContent=text;node.disabled=disabled;
    node.addEventListener('click', () => void act(action));return node;
  }
  async function operation(message, operation) {
    if (busy) return;busy=true;report(message);
    try {await operation();await refresh();} catch (error) {report(error.message,true);} finally {busy=false;}
  }
  async function convert(world) {
    const job = await api('/api/craftmaster/map/run', {name:world.command_name});
    if (!job?.map?.id) throw new Error('CraftMaster did not create a conversion tab');
    let previous = '';
    while (true) {
      await new Promise(resolve => setTimeout(resolve,1500));
      const progress=await api('/api/craftmaster/map/status?id='+encodeURIComponent(job.map.id));
      if (progress.message !== previous){previous=progress.message;report('CraftMaster: '+previous);}
      if(progress.phase==='failed')throw new Error(progress.message || 'CraftMaster conversion failed');
      if(progress.phase==='ready')return;
    }
  }
  async function runServer() {
    const current = [...inventory.worlds, ...inventory.java_worlds].find(w=>w.current);
    if(!current)throw new Error('Select a Current World first');
    let allow_stale = false;
    if(current.edition==='java' && current.conversion?.needs_update){
      if (!current.conversion?.has_conversion || confirm('Java world is out of date. Update for Bedrock now?')) {
        await convert(current);
      } else if(confirm('Run the last converted Bedrock save anyway?'))allow_stale=true;
      else return;
    }
    try { await api('/api/bedrock/run',{allow_stale});send('server-started'); }
    catch(e){
      if (/EULA|Install the official Bedrock server|Bedrock is not installed/i.test(e.message)){
        send('eula');throw new Error('Accept the official Minecraft EULA in the Terminal window, then press Start again');
      }
      throw e;
    }
  }
  async function download(world) {
    if (world.edition==='java' && world.conversion?.needs_update) {
      if(!world.conversion?.has_conversion || confirm('Update this Java world for Bedrock before downloading?'))await convert(world);
      else if(!confirm('Download the previous, out-of-date conversion instead?'))return;
    }
    // Genuine browser download: no alternate archive store in the report.
    const link=document.createElement('a');link.href='/api/bedrock/world/download?id='+encodeURIComponent(world.id);
    link.download=world.command_name+'.mcworld';document.body.append(link);link.click();link.remove();
    report('Download requested: '+world.command_name+'.mcworld');
  }
  async function act(action) {
    const [type,world] = action;
    await operation(type === 'update' ? 'Updating '+world.command_name+'…' : 'Working…', async () => {
      if(type==='select')await api('/api/bedrock/world/select',{name:world.id});
      else if(type==='update')await convert(world);
      else if(type==='map')await api('/api/craftmaster/map/open',{name:world.command_name});
      else if(type==='download')await download(world);
    });
  }
  function worldCard(world) {
    const card=document.createElement('article');if(world.current)card.classList.add('current-world');
    const thumb = world.thumbnail ? document.createElement('img') : document.createElement('div');
    if(world.thumbnail){thumb.src='/api/bedrock/world/icon?id='+encodeURIComponent(world.id);thumb.alt='Minecraft world thumbnail';thumb.className='thumb';}
    else{thumb.className='fallback';thumb.textContent='🌍';thumb.setAttribute('aria-label','No Minecraft world thumbnail');}
    card.append(thumb);
    const info=document.createElement('div');
    const title=document.createElement('h3');title.textContent=world.command_name;info.append(title);
    const state=document.createElement('div');state.className='state';
    if(world.edition==='java'){
      state.textContent=world.conversion?.ready?'Bedrock up to date':
        !world.conversion?.has_conversion?'Not converted to Bedrock':
        world.conversion?.needs_update?'Update for Bedrock required':
        'Bedrock conversion present · freshness unverified';
      if(world.conversion?.needs_update)state.classList.add('stale');
    }else state.textContent='Bedrock save ready';
    info.append(state);
    if(world.edition==='java'){
      const meta=document.createElement('div');meta.className='meta';
      meta.textContent='Last Played: '+date(world.conversion?.last_played)+'  ·  Last Updated: '+date(world.conversion?.last_updated);
      info.append(meta);
    }
    if(world.storage_error){const error=document.createElement('div');error.className='error';error.textContent=world.storage_error;info.append(error);}
    card.append(info);
    const actions=document.createElement('div');actions.className='actions';
    actions.append(button(world.current?'✓ Current World':'Set Current',['select',world],world.current));
    if(world.edition==='java'){
      actions.append(button('Update for Bedrock',['update',world],!world.conversion?.needs_update || busy));
      actions.append(button('Map',['map',world],!world.conversion?.has_conversion && !world.conversion?.ready));
    }
    if(world.downloadable)actions.append(button('Download',['download',world]));
    card.append(actions);
    return card;
  }
  async function refresh(){
    inventory=await api('/api/bedrock/report');
    $('current-world').textContent=inventory.current_world||'None';$('current-pack').textContent=inventory.current_pack||'None';
    const server=$('server');server.textContent=inventory.running?'Stop Server':'Start Server';
    server.className=inventory.running?'stop':'primary';server.disabled=!inventory.current_world && !inventory.running;
    for(const [id,worlds] of [['java',inventory.java_worlds],['bedrock',inventory.worlds]]){
      const root=$(id);root.replaceChildren(...worlds.map(worldCard));
      if(!worlds.length){const p=document.createElement('p');p.textContent=id==='java' && inventory.java_error?inventory.java_error:'No worlds found';root.append(p);}
    }
  }
  $('refresh').addEventListener('click',()=>void operation('Refreshing worlds…',async()=>{await refresh();report('World inventory refreshed');}));
  $('packs').addEventListener('click',()=>void operation('Opening packs…',async()=>{await api('/api/assets/packs/open',{});}));
  $('server').addEventListener('click',()=>void operation('Changing server state…',async()=>{
    if(inventory.running) await api('/api/bedrock/stop',{});else await runServer();
  }));
  window.addEventListener('message',event=>{
    if(event.origin===location.origin && event.source===parent &&
       event.data?.type==='cinter-bedrock-pack-changed') {
      void refresh().catch(e=>report(e.message,true));
    }
  });
  void refresh().catch(e=>report(e.message,true));
})();

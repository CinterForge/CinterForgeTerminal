/* Exercise native UI acceptance startup with a deliberately slow PTY.
 * This reproduces the HOPE-DEV race without requiring WKWebView or a Mac. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const repo = path.resolve(__dirname, '..');
const profiles = JSON.parse(fs.readFileSync(path.join(repo, 'webapp/terminal_styles.json'), 'utf8'));
const events = [];
let opens = 0, writes = 0;
const elements = new Map();
function element(id) {
  return {
    id, hidden: false, disabled: false, src: '', value: '', textContent: '',
    children: [], dataset: {}, style: {setProperty(){}},
    parentElement: {scrollTop:0,scrollHeight:0},
    clientWidth:800,clientHeight:500,
    addEventListener(){},setAttribute(){},removeAttribute(){},focus(){},
    append(item){this.children.push(item)},
    replaceChildren(...items){this.children=items},
  };
}
function get(id) {if(!elements.has(id))elements.set(id,element(id));return elements.get(id)}
get('license-dialog').hidden = true;
const document = {
  documentElement: {getAttribute(){return null}},
  getElementById: get, createElement: element,
  createTextNode: text => ({textContent:text}),
  querySelectorAll: () => [], addEventListener(){}, visibilityState:'visible'
};
const window = {
  __CPH_TOKEN__:'accept-fixture',
  addEventListener(){},
  cinterNative: async event => {events.push(event);return 'ok'}
};
const fetch = async (url) => {
  if(url === '/api/terminal/open') {
    opens++;
    await new Promise(resolve=>setTimeout(resolve,35));
    return {ok:true,json:async()=>({id:'pty-1',platform:'mac',shell:'zsh'})};
  }
  if(url === '/api/terminal/poll') return new Promise(()=>{});
  let value = {};
  if(url === '/api/status')value={platform:'mac'};
  if(url === '/terminal_styles.json')value=profiles;
  if(url === '/mac_games.json')value={games:[]};
  return {ok:true,json:async()=>value};
};
const sandbox={window,document,fetch,URL,URLSearchParams,TextDecoder,Uint8Array,
  location:{href:'http://127.0.0.1:20380/?accept-ui=1',search:'?accept-ui=1'},
  console,navigator:{}, MutationObserver:class{observe(){}},
  localStorage:{getItem(){return 'apple-ii'},setItem(){writes++}},
  setInterval(){},setTimeout,atob:()=>'',};
vm.runInNewContext(fs.readFileSync(path.join(repo,'webapp/terminal.js'),'utf8'),sandbox);
(async()=>{
  await new Promise(resolve=>setTimeout(resolve,150));
  const lifecycle = events.filter(event=>!event.event.startsWith('code-menu-'));
  assert.deepEqual(lifecycle.map(event=>event.event),['minecraft-menu','ready','machine','terminal']);
  assert.equal(lifecycle[2].id,'mac-plus');
  assert.equal(lifecycle[3].id,'vt100-terminal');
  assert.equal(opens,1,'exactly one PTY must be opened, after the museum Session');
  assert.equal(writes,0,'acceptance must not overwrite a real user profile');
  assert.equal(get('computer-view').hidden,true,'VT100 must be active after museum');
  console.log('UI acceptance smoke: real Session callbacks ordered; no PTY startup race');
})().catch(error=>{console.error(error);process.exitCode=1});

// An installed Windows user can start Zork, create another terminal, switch
// back and close the unrelated terminal without killing the interpreter.
// Hold the story download until after close to exercise late-response cleanup.
async function verifyIFTabOwnership(){
  const nodes=new Map(),intervals=[];
  let number=0,opened=0,stops=[],discordAction=null,sent=[],historyRequests=[];
  const codeMenuRequests=[];
  const codeChannels=[];
  class FakeBroadcastChannel {
    constructor(name){this.name=name;codeChannels.push(this);}
    postMessage(message){codeMenuRequests.push({channel:this.name,...message});}
    close(){}
  }
  const el=id=>{
    if(nodes.has(id))return nodes.get(id);
    const value={id,hidden:false,disabled:false,src:'',value:'',textContent:'',
      children:[],dataset:{},style:{setProperty(){}},clientWidth:800,clientHeight:500,
      parentElement:{scrollTop:0,scrollHeight:0},
      listeners:{},attributes:{},selectionStart:0,selectionEnd:0,
      setSelectionRange(start,end){this.selectionStart=start;this.selectionEnd=end},
      setRangeText(replacement,start,end,mode){
        this.value=this.value.slice(0,start)+replacement+this.value.slice(end);
        this.selectionStart=start+(mode==='end'?replacement.length:0);
        this.selectionEnd=this.selectionStart;
      },
      addEventListener(type,cb){(this.listeners[type]??=[]).push(cb)},
      setAttribute(name,value){this.attributes[name]=String(value)},
      removeAttribute(name){delete this.attributes[name]},focus(){},
      append(child){this.children.push(child);this.textContent+=child.textContent||''},
      replaceChildren(...children){this.children=children;this.textContent=children.map(item=>item.textContent||'').join('')},
    };
    nodes.set(id,value);return value;
  };
  el('license-dialog').hidden=true;
  let action={start:'zork1',launch_id:'1'.repeat(32),session_id:'a'.repeat(32)};
  const fetchIF=async (url,options={})=>{
    let data={};
    if(url==='/api/terminal/open')data={id:(opened++===0?'a'.repeat(32):'b'.repeat(32)),platform:'win',shell:'bash.exe'};
    else if(url==='/api/terminal/poll')return new Promise(()=>{});
    else if(url==='/api/if/next'){data=action||{};action=null;}
    else if(url==='/host/code-workspace')data={kind:'terminal',platform:'mac',theme:'dark'};
    else if(url==='/host/code-shell-open')data={ok:true,opened:true};
    else if(url==='/api/if/story/zork1')return new Promise((_resolve,reject)=>{
      options.signal?.addEventListener('abort',()=>reject(Object.assign(new Error('aborted'),{name:'AbortError'})));
    });
    else if(url==='/api/if/stop'){stops.push(JSON.parse(options.body));data={stopped:true};}
    else if(url==='/api/discord/next'){data=discordAction||{};discordAction=null;}
    else if(url==='/api/discord/send'){sent.push(JSON.parse(options.body));data={sent:true};}
    else if(url.startsWith('/api/discord/messages?')){
      const params=new URLSearchParams(url.split('?')[1]);
      historyRequests.push(Object.fromEntries(params));
      assert.equal(params.get('channel'),'456','Discord history follows the selected channel');
      if(params.has('before')){
        assert.equal(params.get('before'),'20');
        data={messages:[{id:'10',author:'Earlier user',content:'older channel history'}],has_more:false};
      }else if(params.has('after')){
        assert.equal(params.get('after'),'30');
        data={messages:[{id:'40',author:'Later user',content:'new live message'}],has_more:false};
      }else data={messages:[
        {id:'20',author:'First user',content:'channel history one'},
        {id:'30',author:'Second user',content:'channel history two'}],has_more:true};
    }
    else if(url==='/api/status')data={platform:'win'};
    else if(url==='/terminal_styles.json')data=profiles;
    else if(url==='/mac_games.json')data={games:[]};
    return {ok:true,json:async()=>data};
  };
  const doc={documentElement:{getAttribute(){return null}},getElementById:el,
    createElement:name=>el(name+':'+(++number)),createTextNode:text=>({textContent:text}),
    querySelectorAll(){return []},addEventListener(){},visibilityState:'visible'};
  const outerWindowListeners={};
  const codeFrameMessages=[];
  const codeFrame=el('computer-frame');
  codeFrame.contentWindow={postMessage(message,origin){codeFrameMessages.push({message,origin});}};
  const win={__CPH_TOKEN__:'fixture',location:{origin:'http://127.0.0.1:20380'},
    addEventListener(type,callback){(outerWindowListeners[type]??=[]).push(callback);}};
  const sandboxIF={window:win,document:doc,fetch:fetchIF,URL,URLSearchParams,TextDecoder,
    Uint8Array,AbortController,BroadcastChannel:FakeBroadcastChannel,
    requestAnimationFrame:callback=>callback(),
    location:{href:'http://127.0.0.1:20380/',search:''},console,
    navigator:{},MutationObserver:class{observe(){}disconnect(){}},
    localStorage:{getItem(){return null},setItem(){}},
    setInterval:cb=>{intervals.push(cb)},setTimeout,atob:()=>''};
  vm.runInNewContext(fs.readFileSync(path.join(repo,'webapp/terminal.js'),'utf8'),sandboxIF);
  await new Promise(resolve=>setTimeout(resolve,35));
  assert.equal(opened,1,'original Windows PTY opened');
  const pollIF=intervals.find(fn=>fn.toString().includes("api('/api/if/next')"));
  assert.ok(pollIF,'IF event transport is registered');
  await pollIF();
  const tabs=()=>el('terminal-tabs').children.filter(node=>(node.className||'').split(' ').includes('session-tab'));
  const closes=()=>el('terminal-tabs').children.filter(node=>node.className==='session-tab-close');
  assert.equal(tabs().length,2,'starting Zork creates its own tab');
  assert.equal(el('terminal-shell').textContent,'Interactive Fiction · zork1');
  action={start:'zork1',launch_id:'1'.repeat(32),session_id:'a'.repeat(32)};
  await pollIF();
  assert.equal(tabs().length,2,'repeated Zork command reselects existing tab');
  await win.CinterTerminalNewTab('mac');
  assert.equal(opened,2,'File menu opens another native shell while Zork runs');
  assert.equal(tabs().length,3);
  await closes()[2].listeners.click[0](); // Close the NEW shell, not Zork.
  await new Promise(resolve=>setTimeout(resolve,5));
  assert.equal(stops.length,0,'closing unrelated shell must not stop Zork');
  assert.equal(tabs().length,2);
  // Reactivate the story, then close only that story tab.
  await tabs()[1].listeners.click[0]();
  assert.equal(el('terminal-shell').textContent,'Interactive Fiction · zork1');
  await closes()[1].listeners.click[0]();
  await new Promise(resolve=>setTimeout(resolve,5));
  assert.equal(stops.length,1,'closing Zork asks the host to stop only its interpreter');
  assert.equal(stops[0].launch_id,'1'.repeat(32),'stop carries exact launch generation');
  assert.equal(tabs().length,1,'original shell remains after closing Zork');
  assert.equal(opened,2,'no stray new PTY or app window');
  console.log('IF UI acceptance smoke: separate Zork tab; File new shell; unrelated close harmless; late download cancelled');

  // Discord uses the same transparent input as IF. Drafts must be visible
  // until Enter, preserved across tab switches, and sent to Discord only.
  // Any requested channel works, not just the example #builds channel.
  discordAction={channel_id:'456',channel:'general',guild:'CinterForge Internal',bot:'Hope AI Dev'};
  const pollDiscord=intervals.find(fn=>fn.toString().includes("/api/discord/next"));
  assert.ok(pollDiscord,'Discord action transport is registered');
  await pollDiscord();
  await new Promise(resolve=>setTimeout(resolve,0));
  assert.equal(tabs().length,2,'Discord command opens its own tab');
  const input=el('terminal-input'),output=el('terminal-output');
  assert.ok(output.textContent.includes('channel history one'),'opening #general loads its own history');
  assert.ok(output.textContent.includes('channel history two'),'initial history is visible in the Terminal');
  assert.ok(output.textContent.indexOf('channel history one')<output.textContent.indexOf('channel history two'),
    'the history is chronological');
  const screen=el('terminal-screen');
  output.parentElement=screen;
  Object.defineProperty(screen,'scrollHeight',{get(){return 600+output.textContent.length}});
  screen.clientHeight=100;
  screen.scrollTop=0;
  await screen.listeners.scroll[0]();
  await new Promise(resolve=>setTimeout(resolve,0));
  assert.ok(output.textContent.includes('older channel history'),'scrolling up requests older messages');
  assert.ok(output.textContent.indexOf('older channel history')<output.textContent.indexOf('channel history one'),
    'older pages prepend above the existing history');
  assert.ok(screen.scrollTop>0,'loading earlier messages keeps the reader at their previous content');
  assert.equal(historyRequests.filter(request=>request.before).length,1);
  const draft=()=>output.children.find(node=>node.className==='discord-input-draft');
  const visible=()=>draft()?.textContent;
  const keydown=(key,options={})=>{
    let stopped=false,prevented=false;
    for(const callback of input.listeners.keydown){
      if(stopped)break;
      callback({key,isComposing:false,shiftKey:false,...options,
        preventDefault(){prevented=true},stopImmediatePropagation(){stopped=true}});
    }
    return {stopped,prevented};
  };
  input.value='draft';input.setSelectionRange(5,5);
  input.listeners.input[0]({isComposing:false});
  assert.equal(visible(),'draft▌','typed Discord draft is visible in the same DOM as history');
  assert.equal(output.attributes['data-if-echo'],undefined,'no stale CSS-only Discord echo');
  await tabs()[0].listeners.click[0]();
  assert.equal(draft(),undefined,'Discord draft is not painted over the shell');
  await tabs()[1].listeners.click[0]();
  assert.equal(input.value,'draft','switching tabs preserves the Discord draft');
  assert.equal(visible(),'draft▌','switching tabs restores precisely the same visible draft');

  // The invisible full-screen input can acquire an unintentional selection
  // after focus or a mouse click. Never lose characters while the hidden
  // textarea still holds a different string that Enter might send.
  input.value='abcd🙂ef';input.setSelectionRange(6,6);
  input.listeners.input[0]({isComposing:false});
  assert.equal(visible(),'abcd🙂▌ef','Unicode draft and mid-line caret display correctly');
  assert.equal(keydown('Backspace').prevented,true,'Backspace uses controlled single-character edit');
  assert.equal(input.value,'abcdef','Backspace deletes one whole Unicode character');
  assert.equal(visible(),'abcd▌ef','the visible draft always matches the actual textarea');
  input.setSelectionRange(0,input.value.length);
  assert.equal(keydown('Backspace').stopped,true,'unexpected select-all cannot delete everything');
  assert.equal(input.value,'abcde','only one character is removed despite a hidden selection');
  assert.equal(visible(),'abcde▌','the drawn text reflects the actual submission');
  input.setSelectionRange(1,1);
  keydown('Delete');
  assert.equal(input.value,'acde','Delete removes exactly one character at the cursor');
  assert.equal(visible(),'a▌cde');
  input.setSelectionRange(1,1);input.listeners.keyup[0]();
  assert.equal(visible(),'a▌cde','caret movement is visible without altering the draft');
  input.listeners.pointerdown[0]({preventDefault(){}});
  assert.equal(input.selectionStart,input.value.length,'clicking the transparent field selects the visible end');
  assert.equal(visible(),'acde▌');
  input.value='send me';input.setSelectionRange(7,7);
  input.listeners.input[0]({isComposing:false});
  assert.equal(visible(),'send me▌','last visible draft is exactly what Enter will submit');
  keydown('Enter');
  await new Promise(resolve=>setTimeout(resolve,0));
  assert.equal(sent.length,1,'Enter sends exactly one Discord message');
  assert.equal(sent[0].content,'send me');
  assert.equal(input.value,'','sent message clears the hidden input');
  assert.equal(visible(),'▌','sent text is no longer displayed as a draft');
  await closes()[1].listeners.click[0](); // Stop the Discord poll loop.
  console.log('Discord UI acceptance smoke: selected channel history, scrollback pagination, drafts, Enter sends once');

  // The very same outer menu must create Code-OSS integrated terminals when
  // Code is active; no extra host PTY or outer Session is allowed.
  const countBeforeCode=opened;
  await win.CinterTerminalNewTab('code');
  const outerTabs=tabs().length;
  assert.ok(el('computer-frame').src.includes('/codeoss/'),'Code-OSS workbench is active');
  await win.CinterTerminalNewTab('linux');
  await win.CinterTerminalNewTab('win');
  await win.CinterTerminalNewTab('server');
  assert.equal(opened,countBeforeCode,'Code Terminal actions must not start outer PTYs');
  assert.equal(tabs().length,outerTabs,'Code Terminal actions must not create outer tabs');
  assert.deepEqual(codeMenuRequests.filter(r=>r.type==='new-terminal').map(r=>r.backend),
    ['linux','win','server'],'the existing Code-OSS extension receives all three backend choices');
  // Only actions supplied by the real Code-OSS menu service may be run.
  // An old fabricated command ID must not be sent to the editor.
  await win.CinterMenuDispatch('code:cmd:workbench.action.showCommands');
  assert.equal(codeFrameMessages.filter(r=>r.message.type==='cinterforge-codeoss-menu-run').length,0,
    'unpublished menu commands are never dispatched');
  // Clicking the CinterForge activity logo requests full Code-OSS area;
  // only outer Terminal chrome disappears, without reloading the iframe.
  const codeFrameSrc=codeFrame.src;
  const toggleWrapper=(source=codeFrame.contentWindow,origin=win.location.origin)=>{
    for(const callback of outerWindowListeners.message||[]) callback({
      source,origin,data:{type:'cinterforge-terminal-wrapper-toggle'}
    });
  };
  toggleWrapper({},win.location.origin);
  toggleWrapper(codeFrame.contentWindow,'http://unrelated.invalid');
  assert.notEqual(el('terminal-overlay').dataset.codeWrapperHidden,'true',
    'other windows or origins cannot hide the Terminal wrapper');
  toggleWrapper();
  assert.equal(el('terminal-overlay').dataset.codeWrapperHidden,'true');
  assert.equal(codeFrameMessages.at(-1).message.type,'cinterforge-terminal-wrapper-state');
  assert.equal(codeFrameMessages.at(-1).message.hidden,true);
  assert.equal(codeFrame.src,codeFrameSrc,'full workbench must preserve the iframe');
  assert.equal(tabs().length,outerTabs,'full workbench must preserve outer sessions');
  toggleWrapper();
  assert.equal(el('terminal-overlay').dataset.codeWrapperHidden,'false');
  assert.equal(codeFrameMessages.at(-1).message.hidden,false);
  assert.ok(fs.readFileSync(path.join(repo,'webapp/terminal.css'),'utf8').includes(
    '#terminal-overlay[data-code-wrapper-hidden="true"] > #terminal-tabs'),
    'full workbench CSS must hide outer tabs as well as HTML menu/footer');
  console.log('Code-OSS full workbench: logo toggles wrapper without reloading PTYs');

  // Simulate the actual Code-OSS model message with its opaque IAction ID.
  // The outer menu must preserve its title, enabled state and action ID.
  const menuGroups=[{name:'File',items:[
    {title:'Save',action:'workbench.action.files.save',enabled:true},
    {title:'Unavailable',action:'unavailable-workbench-action',enabled:false}
  ]}];
  for(const callback of outerWindowListeners.message||[])callback({
    source:codeFrame.contentWindow,origin:win.location.origin,
    data:{type:'cinterforge-codeoss-menu-model',groups:menuGroups}
  });
  const fileMenu=el('code-menu-zone').children.find(menu=>menu.children[0]?.textContent==='File');
  assert.ok(fileMenu,'File menu comes from the Code-OSS menu model');
  assert.equal(fileMenu.cinterList.children[0].textContent,'Save');
  assert.equal(fileMenu.cinterList.children[1].disabled,true);
  await win.CinterMenuDispatch('code:workbench.action.files.save');
  assert.equal(codeFrameMessages.at(-1).message.type,'cinterforge-codeoss-menu-run');
  assert.equal(codeFrameMessages.at(-1).message.action,'workbench.action.files.save',
    'the genuine Code-OSS action ID passes to the workbench unchanged');
  await win.CinterMenuDispatch('code:unavailable-workbench-action');
  // The workbench, not Terminal, is authoritative for action enablement;
  // the UI marks disabled items, and the workbench also checks enabled.
  assert.equal(fileMenu.cinterList.children[1].disabled,true);
  const goMenu=el('code-menu-zone').children.find(menu=>menu.children[0]?.textContent==='Go');
  assert.ok(goMenu,'Go menu exists without any invented fallback items');
  goMenu.open=true;goMenu.listeners.toggle[0]();
  assert.ok(codeFrameMessages.some(r=>r.message.type==='cinterforge-codeoss-menu-request'),
    'missing menu snapshot must request the real Code-OSS model');
  // Code-OSS's worker may use file: even when hosted in this HTTP WebView.
  // Verify the already-owned outer UI can supply the host handshake and route
  // the same shell API without handing IFPlayer a Terminal connection.
  const channel=codeChannels.find(ch=>ch.name==='cinterforge-terminal-code-commands');
  assert.ok(channel?.onmessage,'Code-OSS has an outer host bridge');
  await channel.onmessage({data:{type:'host-probe',requestId:'host-123456789012345'}});
  const probe=codeMenuRequests.find(r=>r.type==='host-probe-response');
  assert.equal(probe?.payload?.kind,'terminal');
  await channel.onmessage({data:{type:'host-request',requestId:'host-123456789012346',
    path:'/host/code-shell-open',method:'POST',body:{id:'test-native'}}});
  assert.equal(codeMenuRequests.find(r=>r.type==='host-request-response')?.payload?.opened,true);
  console.log('Code menu acceptance: New Terminal creates integrated Code-OSS tabs only');
}
verifyIFTabOwnership().catch(error=>{console.error(error);process.exitCode=1});

// WebKit can evaluate the Code extension from a file: worker even though the
// surrounding editor uses the Terminal HTTP origin. Validate the REAL split
// transport and terminal backend selector, not a source-text approximation.
async function verifyCodeWorkerHostBridge(){
  class Channel {
    static all=[];
    constructor(name){this.name=name;this.onmessage=null;Channel.all.push(this);}
    postMessage(data){
      for(const other of Channel.all)if(other!==this && other.name===this.name)
        queueMicrotask(()=>other.onmessage?.({data}));
    }
    close(){Channel.all.splice(Channel.all.indexOf(this),1);}
  }
  const parent=new Channel('cinterforge-terminal-code-commands');
  const requests=[];
  parent.onmessage=({data})=>{
    if(data.type!=='host-probe' && data.type!=='host-request')return;
    requests.push(data);
    parent.postMessage({type:data.type+'-response',requestId:data.requestId,ok:true,
      payload:data.type==='host-probe'
        ? {kind:'terminal',theme:'dark',platform:'mac',cwd:'/Source'}
        : {ok:true,rows:[]}});
  };
  const context={console,BroadcastChannel:Channel,location:{protocol:'file:',origin:'null'},
    setTimeout,clearTimeout,Math,Date,
    CinterForgeCode:{vscode:{workspace:{getConfiguration:()=>({get:()=> 'http://other-if-host/'})}},
      NativeTerminal:class NativeTerminal{},JavaScriptConsoleTerminal:class JavaScriptConsoleTerminal{}}};
  context.globalThis=context;
  for(const part of ['runtime/ControlTransport.js','terminals/TerminalManager.js'])
    vm.runInNewContext(fs.readFileSync(path.join(repo,'webapp/codeoss/extension',part),'utf8'),context);
  const code=context.CinterForgeCode;
  assert.equal((await code.detectTerminalHost()).platform,'mac');
  assert.equal(code.terminalHostEnabled,true);
  assert.ok(code.createNativeTerminalPty('code-id') instanceof code.NativeTerminal,
    'file: worker must open a real Mac PTY after verified Terminal handshake');
  assert.equal((await code.request('/host/code-shell-open',{id:'code-id'})).ok,true);
  assert.equal((await code.request('/code/command',{operation:'workspace-state'})).ok,true);
  assert.deepEqual(requests.map(item=>item.type),['host-probe','host-request','host-request']);
  parent.close();
  console.log('Code-OSS worker acceptance: verified HTTP host from file: worker; real PTY and workspace transport');
}
verifyCodeWorkerHostBridge().catch(error=>{console.error(error);process.exitCode=1});

// Reproduce the Code-OSS extension-host provider validation that prevented
// all terminal contributions from activating. The Logs scheme is read-only,
// but VS Code still requires the full FS method interface.
(function verifyCodeHostFilesystemAndZMachinePty(){
  class EventEmitter {constructor(){this.event=()=>{};}fire(){} }
  class Disposable {constructor(dispose){this.dispose=dispose;}}
  const noPermissions=uri=>Object.assign(new Error('read-only'),{code:'NoPermissions',uri});
  const calls=[];
  const channelMessages=[];
  class Channel {
    constructor(name){this.name=name;}
    postMessage(value){channelMessages.push(value);}
    close(){}
  }
  const context={console,BroadcastChannel:Channel,Math,Date,
    CinterForgeCode:{vscode:{EventEmitter,Disposable,FileType:{File:1,Directory:2},
      FileChangeType:{Changed:1,Created:2,Deleted:3},FileSystemError:{NoPermissions:noPermissions}},
      request:async (endpoint,body)=>{calls.push([endpoint,body]);return {data:''};},
      encodeBase64:()=>'',decodeBase64:()=>new Uint8Array()}};
  context.globalThis=context;
  for(const part of ['filesystem/CinterForgeFileSystemProvider.js','terminals/ZMachineTerminal.js'])
    vm.runInNewContext(fs.readFileSync(path.join(repo,'webapp/codeoss/extension',part),'utf8'),context);
  const Provider=context.CinterForgeCode.HostFileSystemProvider;
  const provider=new Provider();
  for(const method of ['stat','readDirectory','readFile','writeFile','createDirectory','delete','rename','watch'])
    assert.equal(typeof provider[method],'function','read-only provider missing '+method);
  for(const method of ['writeFile','createDirectory','delete','rename'])
    assert.throws(()=>provider[method]({path:'/logs'},new Uint8Array(),{}),error=>error.code==='NoPermissions');
  assert.equal(calls.length,0,'read-only provider must not submit write calls');
  // The accepted coordinator constructs the existing ZMachineTerminal,
  // not the HostedZMachineTerminal from the rejected patch.
  const Code=context.CinterForgeCode;
  Code.terminalHostEnabled=true;
  const ZMachine=Code.ZMachineTerminal;
  assert.equal(typeof ZMachine,'function','real Z-Machine PTY must be exported');
  const pty=new ZMachine();
  pty.open();
  assert.equal(channelMessages[0].type,'subscribe');
  assert.equal(channelMessages[0].id,pty.hostSubscriptionId);
  pty.handleInput('list\r');
  assert.equal(channelMessages[1].type,'input');
  assert.equal(channelMessages[1].id,pty.hostSubscriptionId);
  assert.equal(channelMessages[1].text,'list');
  pty.close();
  assert.equal(channelMessages[2].type,'unsubscribe');
  assert.equal(channelMessages[2].id,pty.hostSubscriptionId);
  assert.equal(calls.length,0,'hosted Z-Machine PTY must not call missing HTTP endpoints');
  console.log('Code-OSS activation acceptance: read-only provider and existing Z-Machine PTY transport');
})();

// Regression: a VS Code PTY can be closed while its HTTP open is pending.
// The late open must be released instead of silently consuming a host slot.
async function verifyCodeNativeTerminalCloseDuringOpen() {
  const pending = {};
  pending.promise = new Promise(resolve => { pending.finish = resolve; });
  const calls = [];
  class EventEmitter { constructor() { this.event = () => {}; } fire() {} }
  const C = {vscode: {EventEmitter}, request: async (path, body) => {
    calls.push({path, id: body?.id});
    if (path === '/host/code-shell-open') return pending.promise;
    if (path === '/host/code-shell-output') throw new Error('closed terminal must not poll');
    return {ok: true};
  }};
  const sandbox = {CinterForgeCode:C, console, setTimeout, clearTimeout};
  sandbox.globalThis = sandbox;
  vm.runInNewContext(fs.readFileSync(path.join(repo,
    'webapp/codeoss/extension/terminals/NativeTerminal.js'), 'utf8'), sandbox);
  const terminal = new C.NativeTerminal('terminal-win-close-race', 'win');
  terminal.open();
  assert.equal(calls.filter(call => call.path === '/host/code-shell-open').length, 1);
  terminal.close();
  assert.equal(calls.filter(call => call.path === '/host/code-shell-close').length, 0,
    'close must not run before the host has allocated its session');
  pending.finish({opened:true});
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(calls.filter(call => call.path === '/host/code-shell-close').length, 1,
    'late open must release exactly its own session');
  terminal.close();
  assert.equal(calls.filter(call => call.path === '/host/code-shell-close').length, 1,
    'repeated closes must not issue duplicate host requests');
  assert.equal(calls.some(call => call.path.includes('code-shell-output')), false,
    'disposed PTY must never start a poll loop');
  console.log('Code-OSS PTY race: close-before-open releases late host session');
}
verifyCodeNativeTerminalCloseDuringOpen().catch(error => {
  console.error(error);
  process.exitCode = 1;
});

// Code-OSS sends terminal geometry before asynchronous host PTY creation.
// No HTTP resize or input may be sent to a nonexistent session.
async function verifyCodeNativeTerminalDeferredResize() {
  const pending = {};
  pending.promise = new Promise(resolve => { pending.finish = resolve; });
  const calls = [];
  class EventEmitter { constructor() { this.event = () => {}; } fire() {} }
  const C = {vscode: {EventEmitter}, request: async (endpoint, body) => {
    calls.push({endpoint, body});
    if (endpoint === '/host/code-shell-open') return pending.promise;
    if (endpoint.includes('code-shell-output')) return new Promise(() => {});
    return {ok: true};
  }};
  const sandbox = {CinterForgeCode: C, console, setTimeout, clearTimeout};
  sandbox.globalThis = sandbox;
  vm.runInNewContext(fs.readFileSync(path.join(repo,
    'webapp/codeoss/extension/terminals/NativeTerminal.js'), 'utf8'), sandbox);
  const terminal = new C.NativeTerminal('terminal-deferred-geometry', 'linux');
  terminal.setDimensions({columns: 60, rows: 12});
  assert.equal(calls.length, 0, 'resize must wait for PTY open');
  terminal.open();
  terminal.setDimensions({columns: 110, rows: 35});
  assert.equal(calls.filter(c => c.endpoint === '/host/code-shell-resize').length, 0);
  pending.finish({opened: true});
  await new Promise(resolve => setImmediate(resolve));
  const resizes = calls.filter(c => c.endpoint === '/host/code-shell-resize');
  assert.equal(resizes.length, 1, 'pending geometry should be applied exactly once');
  assert.equal(resizes[0].body.cols, 110);
  assert.equal(resizes[0].body.rows, 35);
  terminal.close();
  terminal.setDimensions({columns: 140, rows: 45});
  assert.equal(calls.filter(c => c.endpoint === '/host/code-shell-resize').length, 1,
    'disposed terminal cannot resize a closed session');
  console.log('Code-OSS deferred PTY resize: no HTTP 400 before host open');
}
verifyCodeNativeTerminalDeferredResize().catch(error => {
  console.error(error);
  process.exitCode = 1;
});

// The Code-OSS CinterForge activity icon controls the *outer* Terminal chrome
// in the embedded workbench; it must not create the old IFPlayer button menu.
(function verifyCinterActivityToggle() {
  const posts=[], listeners={}, activity={children:[],querySelector(){return null},
    insertBefore(node){this.children.unshift(node)}};
  let logo=null;
  const createElement=tag=>({tag,children:[],attributes:{},listeners:{},style:{},
    addEventListener(type,fn){(this.listeners[type]??=[]).push(fn)},
    setAttribute(key,value){this.attributes[key]=String(value)},
    appendChild(node){this.children.push(node)},
    querySelector(){return null},
    getBoundingClientRect(){return {left:0,right:36,top:0,bottom:36}}});
  const doc={
    createElement,
    getElementById:id=>id==='cinterforge-activity-menu'?logo:null,
    querySelector:selector=>selector.includes('.part.activitybar')?activity:null,
    querySelectorAll:()=>[],
    addEventListener(type,fn){(listeners[type]??=[]).push(fn)},
    body:{appendChild(){}},
  };
  const parent={document:{getElementById:id=>id==='computer-frame'?
    {contentWindow:child}:null},postMessage(message,origin){posts.push({message,origin})}};
  const child={parent,location:{origin:'http://127.0.0.1:20380',protocol:'http:'},
    addEventListener(type,fn){(this.listeners[type]??=[]).push(fn)},listeners:{}};
  const sandbox={window:child,document:doc,console,setTimeout(){}};
  vm.runInNewContext(fs.readFileSync(path.join(repo,
    'webapp/codeoss/extension/workbench_ui.js'),'utf8'),sandbox);
  listeners.DOMContentLoaded[0]();
  logo=activity.children[0];
  assert.equal(logo.id,'cinterforge-activity-menu');
  logo.listeners.click[0]({preventDefault(){},stopImmediatePropagation(){}});
  assert.equal(posts.length,1,'logo must send one outer-wrapper request');
  assert.equal(posts[0].message.type,'cinterforge-terminal-wrapper-toggle');
  assert.equal(posts[0].origin,child.location.origin);
  assert.equal(logo.attributes['aria-expanded'],'false',
    'logo must not open IFPlayer controls in the Terminal host');
  child.listeners.message[0]({source:parent,origin:child.location.origin,
    data:{type:'cinterforge-terminal-wrapper-state',hidden:true}});
  assert.equal(logo.attributes['aria-pressed'],'true',
    'logo reflects full Code-OSS mode after host acknowledgement');
  console.log('Code-OSS activity icon: real wrapper toggle, no IFPlayer menu');
})();

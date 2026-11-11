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
  assert.deepEqual(events.map(event=>event.event),['minecraft-menu','ready','machine','terminal']);
  assert.equal(events[2].id,'mac-plus');
  assert.equal(events[3].id,'vt100-terminal');
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
  const win={__CPH_TOKEN__:'fixture',addEventListener(){}};
  const sandboxIF={window:win,document:doc,fetch:fetchIF,URL,URLSearchParams,TextDecoder,
    Uint8Array,AbortController,requestAnimationFrame:callback=>callback(),
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
}
verifyIFTabOwnership().catch(error=>{console.error(error);process.exitCode=1});

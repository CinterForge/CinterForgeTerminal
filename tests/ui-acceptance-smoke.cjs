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
    addEventListener(){},setAttribute(){},focus(){},
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

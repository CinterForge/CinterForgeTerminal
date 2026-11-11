/* Four native-era HTML menus, tab selection, and hidden Lagomorph contract.
 * Actual Infinite Mac boot/render remains a separate native acceptance gate. */
const fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const assert=require('node:assert/strict');
const root=path.resolve(__dirname,'..');
const html=fs.readFileSync(path.join(root,'webapp/index.html'),'utf8');
const profiles=JSON.parse(fs.readFileSync(path.join(root,'webapp/terminal_styles.json'),'utf8'));
assert.equal(profiles.emulators.length,48);
assert.equal(profiles.emulators.filter(p=>p.lagomorph).length,1);
for(const id of ['terminal','macintosh','beos','nextstep'])
  assert.ok(html.includes(`id="${id}-menu-list"`),`missing ${id} menu`);
assert.doesNotMatch(html,/id="terminal-profile"/);
// File/Edit are Windows-only HTML controls; they are hidden for Mac.
for(const id of ['windows-file-menu','windows-edit-menu'])
  assert.match(html,new RegExp(`id="${id}" hidden`));
const listeners={};
function element(id){return {id,hidden:false,disabled:false,src:'',value:'',checked:false,textContent:'',
  dataset:{},children:[],style:{setProperty(){}},clientWidth:800,clientHeight:500,
  parentElement:{scrollTop:0,scrollHeight:0},
  addEventListener(name,cb){(listeners[id+':'+name]??=[]).push(cb)},setAttribute(){},
  focus(){},append(child){this.children.push(child)},replaceChildren(...items){this.children=items},
  get classList(){return {add(){},remove(){}}}};}
let nextId=0;
const els=new Map(),get=id=>{if(!els.has(id))els.set(id,element(id));return els.get(id)};
get('license-dialog').hidden=true;
const document={documentElement:{getAttribute(){return null}},getElementById:get,
  createElement:name=>element(name+"-"+(++nextId)),createTextNode:text=>({textContent:text}),
  querySelectorAll(){return[]},addEventListener(){},visibilityState:'visible'};
let opened=0;
const fetch=async url=>({ok:true,json:async()=>{
 if(url==='/api/status')return {platform:'mac'};
 if(url==='/terminal_styles.json')return profiles;
 if(url==='/mac_games.json')return {games:[]};
 if(url==='/api/terminal/open')return {id:'pty-'+(++opened),platform:'mac',shell:'bash'};
 if(url==='/api/bedrock/status')return {running:false,starting:false};
 if(url==='/api/terminal/poll')return new Promise(()=>{});
 return {};
}});
const window={__CPH_TOKEN__:'ui-fixture',addEventListener(){}};
const sandbox={window,document,fetch,URL,URLSearchParams,TextDecoder,Uint8Array,
  location:{href:'http://127.0.0.1:20380/',search:''},console,navigator:{},
  MutationObserver:class{observe(){}},
  localStorage:{getItem(){return null},setItem(){}},setInterval(){},atob:()=>''};
vm.runInNewContext(fs.readFileSync(path.join(root,'webapp/terminal.js'),'utf8'),sandbox);
(async()=>{
 await new Promise(resolve=>setTimeout(resolve,75));
 const kinds=['terminal','macintosh','beos','nextstep'];
 const counts=kinds.map(id=>get(id+'-menu-list').children.length);
 assert.ok(counts.every(n=>n>0),`expected four populated menus, got ${counts}`);
 const all=kinds.flatMap(id=>get(id+'-menu-list').children);
 assert.ok(all.some(item=>item.textContent.includes('Mac Plus')));
 assert.ok(all.some(item=>item.textContent.includes('VT100')));
 assert.ok(!all.some(item=>item.textContent.includes('Lagomorph')),'private game remains hidden');
 const mac=get('macintosh-menu-list').children.find(item=>item.textContent.includes('Mac Plus'));
 for(const cb of listeners[mac.id+':click']||[])cb({});
 await new Promise(resolve=>setTimeout(resolve,50));
 const openedMac=new URL(get('computer-frame').src);
 assert.equal(openedMac.origin,'https://infinitemac.org');
 assert.equal(openedMac.searchParams.get('machine'),'Mac Plus');
 assert.equal(openedMac.searchParams.has('cdrom'),false,'private media not attached to public Mac');
 console.log('Museum UI smoke: four lists, genuine Session selection, private Lagomorph hidden');
})().catch(error=>{console.error(error);process.exitCode=1});

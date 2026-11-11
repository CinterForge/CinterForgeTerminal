/* Validate the real in-Session Bedrock EULA flow, not obsolete titlebar DOM. */
const fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const assert=require('node:assert/strict');
const root=path.resolve(__dirname,'..');
const profiles=JSON.parse(fs.readFileSync(path.join(root,'webapp/terminal_styles.json'),'utf8'));
const listeners={};let unique=0;
function element(id){return {id,hidden:false,disabled:false,checked:false,value:'',src:'',textContent:'',
  children:[],dataset:{},style:{setProperty(){}},clientWidth:800,clientHeight:500,
  parentElement:{scrollTop:0,scrollHeight:0},
  setAttribute(){},removeAttribute(){},focus(){},append(item){this.children.push(item)},replaceChildren(...items){this.children=items},
  addEventListener(name,fn){(listeners[id+':'+name]??=[]).push(fn)}};}
const elements=new Map(),get=id=>{if(!elements.has(id))elements.set(id,element(id));return elements.get(id)};
get('license-dialog').hidden=true;
const document={documentElement:{getAttribute(){return null}},getElementById:get,
  createElement:name=>element(name+(++unique)),createTextNode:text=>({textContent:text}),
  querySelectorAll(){return[]},addEventListener(){},visibilityState:'visible'};
let accepted=0,installCalls=0,polls=0;
const fetch=async url=>({ok:true,json:async()=>{
 if(url==='/api/status')return {platform:'mac'};
 if(url==='/terminal_styles.json')return profiles;
 if(url==='/mac_games.json')return {games:[]};
 if(url==='/api/terminal/open')return {id:'pty',platform:'mac',shell:'bash'};
 if(url==='/api/terminal/poll'){
   if(polls++===0)return {output:'',closed:false,bedrock_eula_requested:true};
   return new Promise(()=>{});
 }
 if(url==='/api/bedrock/status')return {running:false,installed:true};
 if(url==='/api/bedrock/accept'){accepted++;return {accepted:true};}
 if(url==='/api/bedrock/install'){installCalls++;return {};}
 if(url.startsWith('/api/bedrock/events'))return {events:[]};
 return {};
}});
const window={__CPH_TOKEN__:'ui-fixture',addEventListener(){}};
const sandbox={window,document,fetch,URL,URLSearchParams,TextDecoder,Uint8Array,
  location:{href:'http://127.0.0.1:20380/',search:''},console,navigator:{},
  MutationObserver:class{observe(){}},localStorage:{getItem(){return null},setItem(){}},
  setInterval(){},atob:()=>''};
vm.runInNewContext(fs.readFileSync(path.join(root,'webapp/terminal.js'),'utf8'),sandbox);
const fire=async (id,event)=>{for(const fn of listeners[id+':'+event]||[])await fn({target:get(id)});};
(async()=>{
 await new Promise(resolve=>setTimeout(resolve,65));
 assert.equal(get('license-dialog').hidden,false,'PTY EULA request must show license');
 get('license-accept').checked=true;
 await fire('license-accept','change');
 assert.equal(get('license-confirm').disabled,false);
 await fire('license-confirm','click');
 assert.equal(accepted,1,'one explicit acceptance from the WebView');
 assert.equal(installCalls,0,'acceptance must not invoke another unconsented install route');
 console.log('UI smoke: real PTY EULA prompt, one explicit acceptance, no duplicate download');
})().catch(error=>{console.error(error);process.exitCode=1});

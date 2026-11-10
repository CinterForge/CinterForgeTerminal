// Dependency-free guest-input sequence test; not an Infinite Mac runtime test.
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const assert=require('node:assert/strict');
const shell=fs.readFileSync(path.resolve(__dirname,'../webapp/terminal.js'),'utf8');
// Evaluate the actual top-of-file configuration in the isolated guest handler.
const configBegin=shell.indexOf('  const LAGOMORPH_DELAYS_MS =');
const configEnd=shell.indexOf('  // Keep the token in the served document', configBegin);
assert.ok(configBegin>0 && configEnd>configBegin, 'find the real shipped timing constants');
const guestConfiguration=shell.slice(configBegin,configEnd);
const begin=shell.indexOf('  let lagBootSequence=0;');
const end=shell.indexOf('  const showTerminal = () => {',begin);
assert.ok(begin>0 && end>begin,'find the actual shipped emulator input handler');
const events=[];
const target={postMessage(value,origin){assert.equal(origin,'https://infinitemac.org');events.push(value);}};
const frame={contentWindow:target,src:'https://infinitemac.org/embed?machine=Quadra+650',focus(){events.push({type:'iframe_focus'});}};
const context={frame,computerView:{hidden:false},selected:'quadra-650',
  window:{addEventListener(){}},console,
  // The logic uses timed pauses in production. Collapse them in this test.
  setTimeout: callback=>{queueMicrotask(callback);return 1;},clearTimeout(){} };
vm.runInNewContext(guestConfiguration+shell.slice(begin,end)+`\nlagomorphBootControls(0,frame.contentWindow);`,context);
setTimeout(()=>{
  const sequence=events.map(item=>{
    if(item.type==='emulator_mouse_move')return `move:${item.x}:${item.y}`;
    if(item.type==='emulator_key_down'||item.type==='emulator_key_up')return `${item.type}:${item.code}`;
    return item.type;
  });
  // The Control Strip is dismissed only once, the emulator iframe is focused,
  // and both physical ADB Command-Q gestures are dispatched and released before
  // double-clicking the Lagomorph disk and game. This is a dispatch contract,
  // NOT evidence that a particular guest desktop quit the application.
  const down=sequence.filter(s=>s==='emulator_key_down:MetaLeft').length;
  const qdown=sequence.filter(s=>s==='emulator_key_down:KeyQ').length;
  const up=sequence.filter(s=>s==='emulator_key_up:MetaLeft').length;
  const qup=sequence.filter(s=>s==='emulator_key_up:KeyQ').length;
  assert.equal(down,3);
  assert.equal(qdown,2);
  assert.equal(up,3);
  assert.equal(qup,2);
  assert.equal(sequence.filter(s=>s==='iframe_focus').length,3);
  assert.equal(sequence.filter(s=>s==='emulator_key_down:KeyW').length,1);
  assert.equal(sequence.filter(s=>s==='emulator_key_up:KeyW').length,1);
  assert.ok(sequence.indexOf('emulator_key_down:MetaLeft') < sequence.indexOf('emulator_key_down:KeyQ'));
  assert.ok(sequence.lastIndexOf('emulator_key_up:KeyQ') < sequence.lastIndexOf('move:600:160'));
  // (8,469) is the Control Strip toggle. Exactly one click means it cannot
  // be accidentally reopened by the Sticky Notes focus sequence.
  assert.equal(sequence.filter(s=>s==='move:8:469').length,1);
  const diskStart=sequence.indexOf('move:600:160');
  const stripClick=sequence.indexOf('move:8:469');
  assert.equal(sequence.slice(stripClick,diskStart).filter(s=>s==='emulator_mouse_down').length,1);
  assert.ok(sequence.indexOf('emulator_key_down:KeyQ')>stripClick);
  assert.equal(sequence.filter(s=>s==='move:600:160').length,1);
  assert.equal(sequence.filter(s=>s==='move:40:142').length,1);
  assert.equal(sequence.filter(s=>s==='emulator_mouse_down').length,
               sequence.filter(s=>s==='emulator_mouse_up').length);
  console.log('Lagomorph guest input dispatch sequence: static event contract verified');
},100);

// CinterForge Terminal: text-only status events over the existing BDS console.
// No network access, commands from chat, or world edits.
import { world, system } from '@minecraft/server';

function emit(type, sender, text) {
  const message = String(text ?? '').slice(0, 240).replace(/[\r\n\x00-\x1f]/g, ' ').trim();
  const who = String(sender ?? 'World').slice(0, 32).replace(/[\r\n\x00-\x1f]/g, ' ').trim();
  if (message && who)
    console.warn('CINTERFORGE_MC_STATUS:' + JSON.stringify({type, sender: who, message}));
}

// Ordinary typed chat is not logged by the native Bedrock console.
world.afterEvents.chatSend.subscribe(event => {
  emit('chat', event.sender?.name ?? 'Player', event.message);
});

// A command block can explicitly signal the status bar using:
// /scriptevent cinterforge:status Hello from a command block
system.afterEvents.scriptEventReceive.subscribe(event => {
  if (event.id !== 'cinterforge:status') return;
  const sender = event.sourceEntity?.name ?? event.initiator?.name ??
    (event.sourceBlock ? 'CommandBlock' : 'World');
  emit('status', sender, event.message);
});
console.warn('CINTERFORGE_MC_STATUS_BRIDGE_READY');

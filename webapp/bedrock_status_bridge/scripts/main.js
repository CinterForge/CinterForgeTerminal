// Minecraft Script API → the existing Bedrock console → the existing Terminal host.
// The pack only transports events. It never interprets or executes host commands.
import { world, system } from '@minecraft/server';

function emit(type, sender, text) {
  const who = String(sender ?? 'World').slice(0, 32).replace(/[\r\n\x00-\x1f]/g, ' ').trim();
  const message = String(text ?? '').replace(/[\r\n\x00]/g, ' ').trim();
  if (!who || !message) return;
  // Send one copy of the command; the content-log transport is line-bounded.
  const payload = type === 'command'
    ? {type, sender: who, command: message.slice(0, 1800)}
    : {type, sender: who, message: message.slice(0, 240)};
  console.warn('CINTERFORGE_MC_STATUS:' + JSON.stringify(payload));
}

// /scriptevent cinter:play tetris → a single "play tetris" host Bash input.
// Only cinter:status is display-only; every other command uses the same shell.
system.afterEvents.scriptEventReceive.subscribe(event => {
  if (!event.id.startsWith('cinter:')) return;
  const sender = event.sourceEntity?.name ?? event.initiator?.name ??
    (event.sourceBlock ? 'CommandBlock' : 'World');
  if (event.id === 'cinter:status') {
    emit('status', sender, event.message);
    return;
  }
  const name = event.id.slice('cinter:'.length);
  if (!name) return;
  const command = name + (event.message ? ' ' + event.message : '');
  emit('command', sender, command);
});
// Typed chat is optional, and must never prevent the command adapter from
// initializing if this Bedrock Script API build does not expose chatSend.
if (world.afterEvents.chatSend?.subscribe) {
  world.afterEvents.chatSend.subscribe(event => {
    emit('chat', event.sender?.name ?? 'Player', event.message);
  });
}
console.warn('CINTERFORGE_MC_STATUS_BRIDGE_READY');

// Verify named guest timings against the last observed working launch.
// This test never runs Infinite Mac; it prevents accidental timer changes.
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const assert=require('node:assert/strict');
const source=fs.readFileSync(path.resolve(__dirname,'../webapp/terminal.js'),'utf8');
const declaration=source.match(/const LAGOMORPH_DELAYS_MS = Object\.freeze\((\{[\s\S]*?\})\);/);
assert.ok(declaration,'documented Lagomorph timing object must exist');
const actual=vm.runInNewContext('('+declaration[1]+')');
const expected={
  initialGuestBoot:18000, colorMenuPointerMove:150, colorMenuMouseHeld:220,
  colorMenuSelection:260, displayRedraw:1000, clickPointerSettle:140,
  clickPress:110, afterControlStripClose:350, beforeStickyNotesQuit:300,
  quitModifierDown:180, quitKeyDown:230, betweenQuitAttempts:800,
  beforeCloseWindowFallback:400, closeModifierDown:160, closeKeyDown:220,
  beforeDiskOpen:1100, doubleClickPointerSettle:140, doubleClickFirstPress:80,
  doubleClickPressGap:100, doubleClickSecondPress:80, betweenDiskAndAppOpen:1400,
};
assert.deepEqual(Object.keys(actual).sort(),Object.keys(expected).sort());
for(const [name,ms] of Object.entries(expected))assert.equal(actual[name],ms,`${name} changed`);
// Count repeated events in the guest automation: Command-Q twice, two double clicks.
const guestMs = actual.initialGuestBoot + actual.colorMenuPointerMove +
  actual.colorMenuMouseHeld + actual.colorMenuSelection + actual.displayRedraw +
  actual.clickPointerSettle + actual.clickPress +
  actual.afterControlStripClose + actual.beforeStickyNotesQuit +
  2 * (actual.quitModifierDown + actual.quitKeyDown) + actual.betweenQuitAttempts +
  actual.beforeCloseWindowFallback + actual.closeModifierDown + actual.closeKeyDown +
  actual.beforeDiskOpen + 2 * (actual.doubleClickPointerSettle +
  actual.doubleClickFirstPress + actual.doubleClickPressGap + actual.doubleClickSecondPress) +
  actual.betweenDiskAndAppOpen;
assert.equal(guestMs,26230,'the complete configured guest sequence must remain 26.23 seconds');
console.log('Lagomorph timers: all values and 26,230 ms aggregate unchanged');

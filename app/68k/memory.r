/*
 * System 7+ application memory partition. Retro68's standard APPL template
 * uses a 1 MB SIZE resource, which is insufficient for the 68K Terminal's
 * catalog, scrollback history, controls, and decoded color card artwork.
 *
 * These are Finder's Preferred Size (4 MB) and Minimum Size (2 MB).
 * Enable canBackground so catalog discovery can finish after a game takes
 * the foreground. The scan yields through WaitNextEvent between batches.
 */
#include "Processes.r"

resource 'SIZE' (-1) {
    reserved,
    ignoreSuspendResumeEvents,
    reserved,
    canBackground,
    needsActivateOnFGSwitch,
    backgroundAndForeground,
    dontGetFrontClicks,
    ignoreChildDiedEvents,
    is32BitCompatible,
    notHighLevelEventAware,
    onlyLocalHLEvents,
    notStationeryAware,
    dontUseTextEditServices,
    reserved,
    reserved,
    reserved,
    4 * 1024 * 1024,
    2 * 1024 * 1024
};

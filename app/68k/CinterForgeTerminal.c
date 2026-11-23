/*
 * CinterForge Terminal for classic Macintosh (Motorola 68000 / System 7+).
 *
 * This is a real Macintosh application, not the modern Terminal WebView.
 * It has three ordinary Mac OS windows (the last two open on demand):
 *   - Terminal: the classic Mac command window. Its existing text and commands
 *               must stay aligned with the other CinterForge terminals.
 *   - Games Report: the separate, scrollable Game Card browser.
 *   - Games Launcher: a separate Finder-style icon folder for screenshots
 *     and direct game launches (8x8 on 7.1, 6x6 on 7.6).
 *
 * Games are discovered from actual APPL files on the mounted Infinite HD.
 * Their Finder names and native icon resources are read from those files.
 * Machine-card startup opens only this Terminal. Game.txt is available only
 * through the explicit Run Game.txt command; no games auto-launch at boot.
 *
 * The System 7 Scrap Manager is polled for OS-addressed cinter: commands.
 * Setup/Build create this app using Retro68.
 */
#include <Types.h>
#include <Quickdraw.h>
#include <Windows.h>
#include <Menus.h>
#include <Events.h>
#include <Fonts.h>
#include <Files.h>
#include <Resources.h>
#include <Icons.h>
#include <Processes.h>
#include <Memory.h>
#include <Dialogs.h>
#include <OSUtils.h>
#include <TextEdit.h>
#include <string.h>
#include <ctype.h>
#include <stdio.h>

/* The OS-specific allowlist currently contains 83 APPL entries in total.
 * Avoid reserving ~200 KB for 1,200 applications in every classic Mac process;
 * 256 leaves substantial room for distinct versions and later additions. */
#define MAX_DISCOVERED_APPLICATIONS 256
#define MAX_CATALOG_FOLDERS 1200
#define MAX_CATALOG_ENTRIES 50000
/* Keep the five-card layout at the System 7.1 card proportions.
 * Wider guest screens must not stretch the card artwork horizontally. */
#define CARD_ARTWORK_WIDTH 145
#define CARD_ARTWORK_HEIGHT 225
#define CARD_HEIGHT 144
#define CARD_SPACING 7
#define REPORT_COLUMN_COUNT 5
#define LAUNCHER_ICON_SIZE 32
#define LAUNCHER_LABEL_SIZE 9
#define LAUNCHER_CONTENT_HEIGHT 51
#define REPORT_HEADER_HEIGHT 98
#define REPORT_FOOTER_HEIGHT 23
#define REPORT_SIDE_MARGIN 8
#define MAX_TERMINAL_HISTORY_LINES 1300
#define TERMINAL_OUTPUT_LINE_SIZE 110
#define TERMINAL_INPUT_SIZE 128
#define CLASSIC_SCROLLBAR_WIDTH 16
#define TERMINAL_LINE_HEIGHT 15
#define TERMINAL_PROMPT_AREA_HEIGHT 47

/* Every classic HFS volume uses directory ID 2 for its root directory.
 * Multiversal Interfaces omits Apple's fsRtDirID compatibility constant. */
#define HFS_ROOT_DIRECTORY_ID 2L

/* Apple's atNone icon alignment is zero. Use the typed value because the
 * installed Multiversal headers do not expose all newer kAlign* aliases. */
#define CLASSIC_ICON_ALIGNMENT_NONE ((IconAlignmentType)0)

/* On classic 68K Mac OS, DoubleTime is the system-wide double-click setting
 * stored at low-memory address $02F0 (in 1/60-second ticks).  Retro68's
 * Multiversal headers declare GetDblTime(), but its library does not supply
 * the corresponding GETDBLTIME symbol. Reading the documented 68K system
 * global preserves the user's Mouse control-panel preference without requiring
 * an unavailable Toolbox linker symbol. This is 68K-only source. */
#define CLASSIC_DOUBLE_CLICK_TIME_ADDRESS 0x02F0UL

static UInt32 mac_double_click_interval_in_ticks(void)
{
    const volatile UInt32 *systemDoubleClickTime =
        (const volatile UInt32 *)CLASSIC_DOUBLE_CLICK_TIME_ADDRESS;
    UInt32 configuredTicks = *systemDoubleClickTime;

    /* Defend against an unset ROM/system global during early startup. */
    return configuredTicks != 0 ? configuredTicks : 30U;
}

/* Classic Macintosh menu and resource identifiers. */
#define FILE_MENU_ID 128
#define GAMES_MENU_ID 129
#define EDIT_MENU_ID 130
#define APPLICATION_ICON_RESOURCE_ID (-16455)
#define TERMINAL_MONACO_FONT_ID 4

/* Keep the event loop responsive while enumerating large HFS volumes. */
#define CATALOG_ENTRIES_PER_EVENT_TICK 70
#define CARD_OPEN_BUTTON_OFFSET_Y 124

/* Finder location and display name for one executable found on Infinite HD. */
typedef struct {
    FSSpec spec;
    Str63 finderName;
    char shortName[32]; /* Stable, unique single-word command for this game. */
} DiscoveredApplication;

/* One level of the incremental depth-first traversal of the HFS catalog. */
typedef struct {
    long directoryId;
    short nextEntryIndex;
} CatalogFolder;

/* -------------------------------------------------------------------------
 * Application state
 * ------------------------------------------------------------------------- */

/* Catalog scanner and the applications it discovers. */
static DiscoveredApplication g_discoveredApplications[MAX_DISCOVERED_APPLICATIONS];
static short g_discoveredApplicationCount = 0;
static CatalogFolder g_catalogFolderStack[MAX_CATALOG_FOLDERS];
static short g_catalogFolderDepth = 0;
static short g_infiniteHdVolumeRef = 0;
static short g_runningGameOS = 0; /* The supported classic Mac OS family. */
static long g_catalogEntriesScanned = 0;
static Boolean g_catalogScanActive = false;
static Boolean g_catalogScanTruncated = false;
static Boolean g_autoLaunchAttempted = false;
/* Clipboard text is treated as a single, OS-targeted message. Ordinary
 * commands run once, but a failed HELLO reply is retried next poll. */
#define CINTER_CLIPBOARD_POLL_TICKS 60U
static UInt32 g_nextClipboardPollTick = 0;
static char g_lastClipboardMessage[192] = "";
/* Only report the first Scrap Manager result and transitions: the guest may
 * poll every second for hours, and the log must remain small. */
static long g_lastScrapReadStatus = 0x7fffffffL;
static char g_pendingClipboardCommand[128] = "";
/* Defer unattended Scrap Manager calls until the Finder has handed the
 * application its first update event. This avoids accessing the startup
 * clipboard while the Process Manager is still activating the new app. */
static Boolean g_terminalHasDrawn = false;
/* Wait for the first window update before scanning Infinite HD. Clipboard
 * polling begins after this initial scan, not during Process Manager startup. */
static Boolean g_startupTasksPending = true;
/* Game.txt is read at startup or by the explicit Run Game.txt menu command. */
static char g_requestedFinderName[64] = "";

/* Games Report search, selection, and scrolling state. */
static char g_applicationNameFilter[64] = "";
static char g_gamesReportStatus[128] = "Searching Infinite HD:Games...";
static short g_visibleApplicationIndices[MAX_DISCOVERED_APPLICATIONS];
static short g_visibleApplicationCount = 0;
/* Sorted independently of the Games Report search filter. */
static short g_launcherApplicationIndices[MAX_DISCOVERED_APPLICATIONS];
static short g_launcherApplicationCount = 0;
static short g_reportFirstRow = 0;
static short g_selectedApplicationIndex = -1;
static WindowPtr g_gamesReportWindow = NULL; /* Games Report, created on demand. */
static WindowPtr g_gamesLauncherWindow = NULL; /* Finder-style, created on demand. */
static ControlHandle g_gamesReportScrollBar = NULL;
/* The Terminal window and its bounded, scrollable text history. */
static WindowPtr g_terminalWindow = NULL;
static ControlHandle g_terminalScrollBar = NULL;
static char g_terminalHistoryLines[MAX_TERMINAL_HISTORY_LINES][TERMINAL_OUTPUT_LINE_SIZE];
static short g_terminalHistoryCount = 0;
static short g_terminalFirstVisibleLine = 0;
static char g_terminalInput[TERMINAL_INPUT_SIZE] = "";
static Boolean g_listGamesWhenScanCompletes = false;
static short g_terminalWindowWidth = 620;
static short g_terminalWindowHeight = 360;
/* One decoded copy of the authored GameCard.rgb image for all visible cards. */
static PixMap g_cardPixelMap;
static Ptr g_cardPixelBuffer = NULL;
/* The window's full-colour CinterForge logo is RGB555, independent of Finder's
 * classic indexed icon resources (which can render monochrome on some Macs). */
static PixMap g_logoPixelMap;
static Ptr g_logoPixelBuffer = NULL;
static Boolean g_logoArtworkReady = false;
static Boolean g_applicationIsRunning = true;
static Boolean g_cardArtworkReady = false;
static short g_gamesReportWidth = 620;
static short g_gamesReportHeight = 470;
static Rect g_searchFieldBounds;

/* Keyboard handlers are above the clipboard implementation in this file. */
static void copy_game_list_to_mac_clipboard(void);

/* -------------------------------------------------------------------------
 * String helpers: Toolbox names are Pascal strings, user commands are C text
 * ------------------------------------------------------------------------- */

static void copy_c_string_to_pascal(const char *source, Str255 destination)
{
    size_t length = strlen(source);
    if (length > 255) length = 255;
    destination[0] = (unsigned char)length;
    memcpy(destination + 1, source, length);
}

static void copy_pascal_string_to_c(const unsigned char *source, char *destination, size_t destinationSize)
{
    size_t length = source[0];
    if (length >= destinationSize) length = destinationSize - 1;
    memcpy(destination, source + 1, length);
    destination[length] = 0;
}

/* Classic Finder names are matched case-insensitively, without a path. */
static Boolean names_match_ignoring_case(const char *leftName, const char *rightName)
{
    while (*leftName && *rightName) {
        unsigned char leftCharacter = (unsigned char)*leftName++, rightCharacter = (unsigned char)*rightName++;
        if (tolower(leftCharacter) != tolower(rightCharacter)) return false;
    }
    return *leftName == 0 && *rightName == 0;
}

/* Crash-trace build 007. Every checkpoint is written and closed immediately.
 * Try guest-to-host The Outside World:Uploads first; fall back to the writable
 * CinterForge Terminal HFS disk. Logging never changes the terminal UI. */
#define TRACE68K_VERSION "010"
#define TRACE68K_FILE "\pCinterForgeTerminal.log"
static Boolean g_traceSelected = false;
static short g_traceVolume = 0;
static long g_traceDir = HFS_ROOT_DIRECTORY_ID;
static unsigned long g_traceNumber = 0;
static Boolean g_traceFirstUpdate = false;
/* Only instrument the very first history append; leave normal scrollback fast. */
static Boolean g_traceFirstAppendActive = false;
static Boolean g_traceFirstAppendDone = false;
static short find_outside_world_volume(void);
static short find_cinterforge_hd_volume(void);

static OSErr trace_write(short volume, long directory, const char *line, Boolean reset)
{
    FSSpec spec;
    OSErr result = FSMakeFSSpec(volume, directory, TRACE68K_FILE, &spec);
    short refNum = 0;
    long length;
    if (result == fnfErr) result = FSpCreate(&spec, 'ttxt', 'TEXT', 0);
    if (result != noErr) return result;
    result = FSpOpenDF(&spec, fsRdWrPerm, &refNum);
    if (result != noErr) return result;
    if (reset) {
        result = SetEOF(refNum, 0);
        if (result == noErr) result = SetFPos(refNum, fsFromStart, 0);
    } else {
        result = SetFPos(refNum, fsFromLEOF, 0);
    }
    if (result == noErr) {
        length = (long)strlen(line);
        result = FSWrite(refNum, &length, line);
    }
    FSClose(refNum);
    return result;
}

static void trace_checkpoint(const char *phase)
{
    char line[192];
    short outside, terminal;
    snprintf(line, sizeof(line), "trace=%s seq=%lu tick=%lu %s\r",
             TRACE68K_VERSION, ++g_traceNumber, (unsigned long)TickCount(), phase);
    if (g_traceSelected) {
        if (g_traceVolume) (void)trace_write(g_traceVolume, g_traceDir, line, false);
        return;
    }
    outside = find_outside_world_volume();
    if (outside) {
        CInfoPBRec folder;
        Str255 folderName = "\pUploads";
        memset(&folder, 0, sizeof(folder));
        folder.dirInfo.ioNamePtr = folderName;
        folder.dirInfo.ioVRefNum = outside;
        folder.dirInfo.ioFDirIndex = 0;
        folder.dirInfo.ioDrDirID = HFS_ROOT_DIRECTORY_ID;
        if (PBGetCatInfoSync(&folder) == noErr &&
                (folder.hFileInfo.ioFlAttrib & ioDirMask) &&
                trace_write(outside, folder.dirInfo.ioDrDirID, line, true) == noErr) {
            g_traceVolume = outside;
            g_traceDir = folder.dirInfo.ioDrDirID;
        }
    }
    if (!g_traceVolume) {
        terminal = find_cinterforge_hd_volume();
        if (terminal &&
                trace_write(terminal, HFS_ROOT_DIRECTORY_ID, line, true) == noErr) {
            g_traceVolume = terminal;
            g_traceDir = HFS_ROOT_DIRECTORY_ID;
        }
    }
    g_traceSelected = (g_traceVolume != 0); /* Retry if drives not ready yet. */
}

/* Query the application heap without allocating an image or a scratch buffer.
 * Record free memory and the largest contiguous block at startup milestones. */
static void trace_heap_checkpoint(const char *phase)
{
    char line[160];
    long freeBytes = FreeMem();
    long largestBlock = MaxBlock();
    snprintf(line, sizeof(line), "heap: %s free=%ld maxblock=%ld",
             phase, freeBytes, largestBlock);
    trace_checkpoint(line);
}

static Boolean name_contains_filter(const char *name, const char *filter)
{
    size_t startIndex, filterIndex;
    if (!*filter) return true;
    for (startIndex = 0; name[startIndex]; startIndex++) {
        for (filterIndex = 0; filter[filterIndex] && name[startIndex+filterIndex]; filterIndex++) {
            if (tolower((unsigned char)name[startIndex+filterIndex]) != tolower((unsigned char)filter[filterIndex]))
                break;
        }
        if (!filter[filterIndex]) return true;
    }
    return false;
}

/* Turn a genuine Finder name into a simple command token. Collisions are
 * disambiguated, never collapsed onto the first game with that name. */
static void assign_game_short_name(short applicationIndex)
{
    DiscoveredApplication *application = &g_discoveredApplications[applicationIndex];
    char fullName[64], baseName[28], candidate[32];
    short sourceIndex, targetIndex = 0, collisionNumber = 1, earlierIndex;
    Boolean collides;
    copy_pascal_string_to_c(application->finderName, fullName, sizeof(fullName));
    for (sourceIndex = 0; fullName[sourceIndex] && targetIndex < 23; ++sourceIndex) {
        unsigned char character = (unsigned char)fullName[sourceIndex];
        if (isalnum(character)) baseName[targetIndex++] = (char)tolower(character);
    }
    baseName[targetIndex] = 0;
    if (!targetIndex) strcpy(baseName, "game");
    do {
        if (collisionNumber == 1) {
            strcpy(candidate, baseName);
        } else {
            snprintf(candidate, sizeof(candidate), "%.23s%d", baseName, (int)collisionNumber);
        }
        collides = false;
        for (earlierIndex = 0; earlierIndex < applicationIndex; ++earlierIndex) {
            if (names_match_ignoring_case(g_discoveredApplications[earlierIndex].shortName, candidate)) {
                collides = true;
                break;
            }
        }
        ++collisionNumber;
    } while (collides);
    strcpy(application->shortName, candidate);
}

/* Finder type APPL alone does not mean "game": Infinite HD:Games contains
 * map editors, sound tools, help viewers and installers as well as games.
 * Keep a narrow, inspectable list of confirmed non-game Finder names. Do not
 * suppress game versions, same-named copies, or uncertain applications: their
 * exact FSSpec and launch behavior must be checked in a real Macintosh first.
 * The scan, the Games Report, and clipboard/export share this one filter. */
static Boolean is_non_game_application_name(const unsigned char *finderName)
{
    static const char *excludedFragments[] = {
        "manual", "register", "registration", "documentation", "basics",
        "read me", "readme"
    };
    static const char *excludedExactNames[] = {
        "Avara MicroTracker", "BSPViewer", "MacroMind Player",
        "F/A-18 Mission Chooser", "Improved Art Files.sea",
        "Urban Renewal Kit", "SnoodEdit", "Warcraft Converter"
    };
    static const char *excludedPrefixes[] = {
        "BSPSplitter", "SoundComp", "Anvil ", "Forge ",
        "Pathways Guide", "Pathways Into Cheating", "Torch ",
        "Room Editor", "EHelpEngine", "Terrain Editor", "TeachText",
        "Microsoft Help", "Warcraft II Map Editor", "Warcraft II Sound Editor"
    };
    char applicationName[64];
    size_t wordIndex;
    copy_pascal_string_to_c(finderName, applicationName, sizeof(applicationName));
    for (wordIndex = 0; wordIndex < sizeof(excludedFragments) / sizeof(excludedFragments[0]); ++wordIndex)
        if (name_contains_filter(applicationName, excludedFragments[wordIndex])) return true;
    for (wordIndex = 0; wordIndex < sizeof(excludedExactNames) / sizeof(excludedExactNames[0]); ++wordIndex)
        if (names_match_ignoring_case(applicationName, excludedExactNames[wordIndex])) return true;
    for (wordIndex = 0; wordIndex < sizeof(excludedPrefixes) / sizeof(excludedPrefixes[0]); ++wordIndex) {
        size_t length = strlen(excludedPrefixes[wordIndex]);
        size_t characterIndex;
        for (characterIndex = 0; characterIndex < length && applicationName[characterIndex]; ++characterIndex)
            if (tolower((unsigned char)applicationName[characterIndex]) !=
                tolower((unsigned char)excludedPrefixes[wordIndex][characterIndex])) break;
        if (characterIndex == length) return true;
    }
    return false;
}

/* The guest Game Catalog is intentionally per-OS, not an all-APPL list.
 * These command tokens are the 82 copied Infinite HD entries curated in
 * docs/Game Catalog.md. Both the Terminal and Games Report use this same
 * discovered catalog; unknown APPL files are not exposed automatically.
 * Games with duplicate Finder names all target the same OS. */
#define GAME_OS_SYSTEM_71 71
#define GAME_OS_MAC_76   76

static short running_game_os(void)
{
    long systemVersion = 0;
    /* gestaltSystemVersion ('sysv') returns packed BCD 0x0710, 0x0760,
     * or 0x0810, with low bits optionally indicating maintenance updates. */
    if (Gestalt('sysv', &systemVersion) != noErr) return 0;
    switch ((unsigned long)systemVersion & 0xFFF0UL) {
        case 0x0710UL: return GAME_OS_SYSTEM_71;
        case 0x0760UL: return GAME_OS_MAC_76;
        default: return 0;
    }
}

static Boolean command_in_game_os_catalog(const char *command, short gameOS)
{
    static const char *system71Commands[] = {
        "3wizcolunr", "maelstrom", "outofthisworld103", "battlechess", "bolo0997",
        "minotaur", "minotaur2", "minotaur3", "minotaur4", "minotaur5",
        "minotaur6", "minotaur7", "chainshot", "civilization", "continuum",
        "cosmicosmo", "darkcastle", "glider406", "glider409", "zero220",
        "lemmings", "fateofatlantis11", "fateofatlantisv15", "indy", "loom",
        "pipedream", "monkeyisland", "missilecommand30", "ok020", "pararena206m",
        "pipedream2", "princeofpersia", "colorrisk241", "scarabofra14", "shufflepuckcafv10",
        "simcityclassic", "sokoban242a", "solarianii", "spectre11", "strategicconquest301",
        "stuntcopter10", "stuntcopter12", "stuntcopter15", "stuntcopter16", "stuntcopter20",
        "stuntcopterpatchedforan", "tetris", "tetriscolor", "oregontrail", "wesleyantetris",
    };
    static const char *mac76Commands[] = {
        "a10attack15", "apeiron", "apeiron2", "avara", "avaracommuter",
        "barrack", "bubbletrouble", "chiral", "escapevelocity", "swoop",
        "blobbo102lite", "marathonv12", "marathon2", "pathwaysintodarkness", "hornetace10",
        "fa18hornetclassic", "hellcats", "indysdesktopadventures", "nsshaft", "simcity200012",
        "snood", "starbound", "strategicconquest401", "strategicconquestplus20", "troubledsouls",
        /* Former Mac OS 8.1 entries except the PowerPC-only Loom copy. */
        "boom10", "abuse101", "marathon15", "ultimatedoomi",
        "warcraft", "warcraftii100k", "warcraftiiv122",
    };
    const char *const *commands = NULL;
    size_t commandCount = 0, commandIndex;
    if (gameOS == GAME_OS_SYSTEM_71) {
        commands = system71Commands;
        commandCount = sizeof(system71Commands) / sizeof(system71Commands[0]);
    } else if (gameOS == GAME_OS_MAC_76) {
        commands = mac76Commands;
        commandCount = sizeof(mac76Commands) / sizeof(mac76Commands[0]);
    }
    for (commandIndex = 0; commandIndex < commandCount; ++commandIndex)
        if (names_match_ignoring_case(command, commands[commandIndex])) return true;
    return false;
}

static void set_games_report_status(const char *message)
{
    strncpy(g_gamesReportStatus, message, sizeof(g_gamesReportStatus)-1);
    g_gamesReportStatus[sizeof(g_gamesReportStatus)-1] = 0;
}

/* -------------------------------------------------------------------------
 * Terminal: plain classic Macintosh window, output history and command line
 * ------------------------------------------------------------------------- */
/* Use the live GrafPort bounds: cached width/height are unreliable during
 * GrowWindow and update events, particularly when another window overlaps. */
static Rect terminal_history_bounds(void)
{
    Rect bounds = {8, 8, 8, 8};
    if (g_terminalWindow) {
        bounds = g_terminalWindow->portRect;
        bounds.left = 8;
        bounds.top = 8;
        bounds.right -= CLASSIC_SCROLLBAR_WIDTH + 4;
        bounds.bottom -= TERMINAL_PROMPT_AREA_HEIGHT;
    }
    return bounds;
}

static short terminal_visible_line_count(void)
{
    Rect textBounds = terminal_history_bounds();
    short availableHeight = textBounds.bottom - textBounds.top - 8;
    short rows = availableHeight / TERMINAL_LINE_HEIGHT;
    return rows > 0 ? rows : 1;
}

static short terminal_maximum_scroll_position(void)
{
    short maximumPosition = g_terminalHistoryCount - terminal_visible_line_count();
    return maximumPosition > 0 ? maximumPosition : 0;
}

/* Both the scrollbar's thumb and our viewport refer to the same line index. */
static void update_terminal_scrollbar(void)
{
    short maximumPosition;
    if (g_traceFirstAppendActive) trace_checkpoint("append: update scrollbar enter");
    maximumPosition = terminal_maximum_scroll_position();
    if (g_traceFirstAppendActive) trace_checkpoint("append: scrollbar max calculated");
    if (g_terminalFirstVisibleLine > maximumPosition)
        g_terminalFirstVisibleLine = maximumPosition;
    if (g_terminalFirstVisibleLine < 0) g_terminalFirstVisibleLine = 0;
    if (g_terminalScrollBar) {
        if (g_traceFirstAppendActive) trace_checkpoint("append: before SetControlMaximum");
        SetControlMaximum(g_terminalScrollBar, maximumPosition);
        if (g_traceFirstAppendActive) trace_checkpoint("append: after SetControlMaximum");
        if (g_traceFirstAppendActive) trace_checkpoint("append: before SetControlValue");
        SetControlValue(g_terminalScrollBar, g_terminalFirstVisibleLine);
        if (g_traceFirstAppendActive) trace_checkpoint("append: after SetControlValue");
    }
}

static void position_terminal_scrollbar(void)
{
    Rect bounds;
    if (!g_terminalWindow || !g_terminalScrollBar) return;
    bounds = g_terminalWindow->portRect;
    MoveControl(g_terminalScrollBar, (short)(bounds.right - CLASSIC_SCROLLBAR_WIDTH), 1);
    SizeControl(g_terminalScrollBar, CLASSIC_SCROLLBAR_WIDTH,
                (short)(bounds.bottom - TERMINAL_PROMPT_AREA_HEIGHT - 1));
    update_terminal_scrollbar();
}

/* InvalRect affects the window of the *current* QuickDraw GrafPort, not
 * the window that supplied the rectangle. NewControl/Window Manager calls
 * can leave another port current. Always select the Terminal's port before
 * touching its update region, then restore the caller's graphics context. */
static void invalidate_terminal_window(void)
{
    GrafPtr previousPort = NULL;
    if (!g_terminalWindow) return;
    GetPort(&previousPort);
    if (g_traceFirstAppendActive)
        trace_checkpoint(previousPort == (GrafPtr)g_terminalWindow
                         ? "append: current port is Terminal"
                         : "append: current port is NOT Terminal");
    if (g_traceFirstAppendActive) trace_checkpoint("append: before SetPort Terminal");
    SetPort(g_terminalWindow);
    if (g_traceFirstAppendActive) trace_checkpoint("append: after SetPort Terminal");
    InvalRect(&g_terminalWindow->portRect);
    if (g_traceFirstAppendActive) trace_checkpoint("append: after InvalRect Terminal");
    if (previousPort && previousPort != (GrafPtr)g_terminalWindow) {
        SetPort(previousPort);
        if (g_traceFirstAppendActive) trace_checkpoint("append: prior port restored");
    }
}

/* Keyboard input changes only the fixed prompt, not the scrollback or title.
 * Invalidate that small region rather than flashing the complete Window on
 * every key press on slower System 7.1 guests.  BeginUpdate clips the redraw. */
static void invalidate_terminal_prompt(void)
{
    GrafPtr previousPort = NULL;
    Rect prompt;
    if (!g_terminalWindow) return;
    GetPort(&previousPort);
    SetPort(g_terminalWindow);
    prompt = g_terminalWindow->portRect;
    prompt.top = (short)(prompt.bottom - TERMINAL_PROMPT_AREA_HEIGHT + 5);
    prompt.right -= CLASSIC_SCROLLBAR_WIDTH;
    InvalRect(&prompt);
    if (previousPort && previousPort != (GrafPtr)g_terminalWindow)
        SetPort(previousPort);
}

/* The same QuickDraw current-port rule applies to the Games Report. */
static void invalidate_games_report_window(void)
{
    GrafPtr previousPort = NULL;
    if (!g_gamesReportWindow) return;
    GetPort(&previousPort);
    SetPort(g_gamesReportWindow);
    InvalRect(&g_gamesReportWindow->portRect);
    if (previousPort && previousPort != (GrafPtr)g_gamesReportWindow)
        SetPort(previousPort);
}

/* Never leave a different window selected as the current drawing port. */
static void invalidate_games_launcher_window(void)
{
    GrafPtr previousPort = NULL;
    if (!g_gamesLauncherWindow) return;
    GetPort(&previousPort);
    SetPort(g_gamesLauncherWindow);
    InvalRect(&g_gamesLauncherWindow->portRect);
    if (previousPort && previousPort != (GrafPtr)g_gamesLauncherWindow)
        SetPort(previousPort);
}

/* Keep a reader's place when output arrives. Follow new lines automatically
 * only when the reader was already looking at the end of the transcript. */
static void append_terminal_history_line(const char *value)
{
    Boolean followNewestLine;
    Boolean firstAppend = !g_traceFirstAppendDone;
    if (firstAppend) {
        g_traceFirstAppendActive = true;
        trace_checkpoint("append: entered first welcome line");
        trace_checkpoint("append: before terminal_maximum_scroll_position");
    }
    followNewestLine = g_terminalFirstVisibleLine >= terminal_maximum_scroll_position();
    if (firstAppend) trace_checkpoint("append: maximum scroll calculated");
    if (g_terminalHistoryCount == MAX_TERMINAL_HISTORY_LINES) {
        memmove(g_terminalHistoryLines,
                g_terminalHistoryLines + 1,
                sizeof(g_terminalHistoryLines[0]) * (MAX_TERMINAL_HISTORY_LINES - 1));
        --g_terminalHistoryCount;
        if (!followNewestLine && g_terminalFirstVisibleLine > 0)
            --g_terminalFirstVisibleLine;
    }
    if (firstAppend) trace_checkpoint("append: before history strncpy");
    strncpy(g_terminalHistoryLines[g_terminalHistoryCount], value, TERMINAL_OUTPUT_LINE_SIZE - 1);
    if (firstAppend) trace_checkpoint("append: history strncpy completed");
    g_terminalHistoryLines[g_terminalHistoryCount][TERMINAL_OUTPUT_LINE_SIZE - 1] = 0;
    ++g_terminalHistoryCount;
    if (firstAppend) trace_checkpoint("append: history count incremented");
    if (firstAppend) trace_checkpoint("append: before scroll follow calculation");
    if (followNewestLine) g_terminalFirstVisibleLine = terminal_maximum_scroll_position();
    if (firstAppend) trace_checkpoint("append: scroll follow calculated");
    if (firstAppend) trace_checkpoint("append: before update_terminal_scrollbar");
    update_terminal_scrollbar();
    if (firstAppend) trace_checkpoint("append: update_terminal_scrollbar completed");
    if (firstAppend) trace_checkpoint("append: before invalidate_terminal_window");
    invalidate_terminal_window();
    if (firstAppend) {
        trace_checkpoint("append: first welcome line complete");
        g_traceFirstAppendActive = false;
        g_traceFirstAppendDone = true;
    }
}

static void scroll_terminal_history(short rows)
{
    long newFirstLine = (long)g_terminalFirstVisibleLine + rows;
    if (newFirstLine < 0) newFirstLine = 0;
    if (newFirstLine > terminal_maximum_scroll_position())
        newFirstLine = terminal_maximum_scroll_position();
    g_terminalFirstVisibleLine = (short)newFirstLine;
    update_terminal_scrollbar();
    invalidate_terminal_window();
}

static void draw_terminal_window(void)
{
    short rowIndex;
    short visibleLineCount = terminal_visible_line_count();
    Rect windowBounds, historyBounds, divider, promptBounds;
    RgnHandle previousClip;
    if (!g_terminalWindow) return;
    if (!g_traceFirstUpdate) trace_checkpoint("draw: before SetPort");
    SetPort(g_terminalWindow);
    windowBounds = g_terminalWindow->portRect;
    historyBounds = terminal_history_bounds();
    EraseRect(&windowBounds);
    if (!g_traceFirstUpdate) trace_checkpoint("draw: EraseRect done");
    TextFont(TERMINAL_MONACO_FONT_ID);
    TextSize(10);
    TextMode(srcCopy);

    /* Do not let long output paint into the scrollbar or the fixed prompt. */
    if (!g_traceFirstUpdate) trace_checkpoint("draw: before first NewRgn");
    previousClip = NewRgn();
    if (previousClip) {
        GetClip(previousClip);
        ClipRect(&historyBounds);
    }
    for (rowIndex = 0;
         rowIndex < visibleLineCount &&
         g_terminalFirstVisibleLine + rowIndex < g_terminalHistoryCount;
         ++rowIndex) {
        Str255 pascalLine;
        copy_c_string_to_pascal(
            g_terminalHistoryLines[g_terminalFirstVisibleLine + rowIndex], pascalLine);
        MoveTo(historyBounds.left + 3,
               (short)(historyBounds.top + 13 + rowIndex * TERMINAL_LINE_HEIGHT));
        DrawString(pascalLine);
    }
    if (previousClip) {
        SetClip(previousClip);
        DisposeRgn(previousClip);
    }

    if (!g_traceFirstUpdate) trace_checkpoint("draw: history loop done");
    SetRect(&divider, 9, windowBounds.bottom - 41,
            windowBounds.right - CLASSIC_SCROLLBAR_WIDTH - 3, windowBounds.bottom - 40);
    PaintRect(&divider);
    SetRect(&promptBounds, 9, windowBounds.bottom - 39,
            windowBounds.right - CLASSIC_SCROLLBAR_WIDTH - 3, windowBounds.bottom - 2);
    previousClip = NewRgn();
    if (previousClip) {
        GetClip(previousClip);
        ClipRect(&promptBounds);
    }
    {
        Str255 prompt;
        char content[TERMINAL_INPUT_SIZE + 24];
        snprintf(content, sizeof(content), "CinterForge Terminal> %s_", g_terminalInput);
        copy_c_string_to_pascal(content, prompt);
        TextFont(TERMINAL_MONACO_FONT_ID);
        TextSize(10);
        MoveTo(10, (short)(windowBounds.bottom - 17));
        DrawString(prompt);
    }
    if (previousClip) {
        SetClip(previousClip);
        DisposeRgn(previousClip);
    }
    if (!g_traceFirstUpdate) trace_checkpoint("draw: before DrawControls");
    DrawControls(g_terminalWindow); /* EraseRect also erased the controls. */
    if (!g_traceFirstUpdate) trace_checkpoint("draw: DrawControls done");
}

/* -------------------------------------------------------------------------
 * Infinite HD: find the volume, enumerate its HFS catalog, filter applications
 * ------------------------------------------------------------------------- */

static short compare_application_names(short leftIndex, short rightIndex)
{
    char leftName[64], rightName[64];
    size_t characterIndex;
    copy_pascal_string_to_c(g_discoveredApplications[leftIndex].finderName,
                            leftName, sizeof(leftName));
    copy_pascal_string_to_c(g_discoveredApplications[rightIndex].finderName,
                            rightName, sizeof(rightName));
    for (characterIndex = 0; leftName[characterIndex] && rightName[characterIndex]; ++characterIndex) {
        unsigned char leftCharacter = (unsigned char)tolower((unsigned char)leftName[characterIndex]);
        unsigned char rightCharacter = (unsigned char)tolower((unsigned char)rightName[characterIndex]);
        if (leftCharacter != rightCharacter) return leftCharacter < rightCharacter ? -1 : 1;
    }
    return leftName[characterIndex] ? 1 : rightName[characterIndex] ? -1 : 0;
}

static void update_games_report_scrollbar(void);
static void rebuild_launcher_application_list(void);

static void rebuild_visible_application_list(void)
{
    short applicationIndex, insertionIndex;
    char applicationName[64];
    g_visibleApplicationCount = 0;
    for (applicationIndex = 0; applicationIndex < g_discoveredApplicationCount; applicationIndex++) {
        copy_pascal_string_to_c(g_discoveredApplications[applicationIndex].finderName,
                                applicationName, sizeof(applicationName));
        if (name_contains_filter(applicationName, g_applicationNameFilter)) {
            g_visibleApplicationIndices[g_visibleApplicationCount++] = applicationIndex;
        }
    }
    /* Stable, predictable ordering for the bounded list of Finder names. */
    for (applicationIndex = 1; applicationIndex < g_visibleApplicationCount; ++applicationIndex) {
        short applicationToInsert = g_visibleApplicationIndices[applicationIndex];
        insertionIndex = applicationIndex;
        while (insertionIndex > 0 &&
               compare_application_names(applicationToInsert,
                                         g_visibleApplicationIndices[insertionIndex - 1]) < 0) {
            g_visibleApplicationIndices[insertionIndex] = g_visibleApplicationIndices[insertionIndex-1];
            --insertionIndex;
        }
        g_visibleApplicationIndices[insertionIndex] = applicationToInsert;
    }
    g_reportFirstRow = 0;
    g_selectedApplicationIndex = -1;
    update_games_report_scrollbar();
    invalidate_games_report_window();
    rebuild_launcher_application_list();
}

static void rebuild_launcher_application_list(void)
{
    short i, insert;
    g_launcherApplicationCount = g_discoveredApplicationCount;
    for (i = 0; i < g_launcherApplicationCount; ++i)
        g_launcherApplicationIndices[i] = i;
    for (i = 1; i < g_launcherApplicationCount; ++i) {
        short toInsert = g_launcherApplicationIndices[i];
        insert = i;
        while (insert > 0 &&
               compare_application_names(toInsert, g_launcherApplicationIndices[insert-1]) < 0) {
            g_launcherApplicationIndices[insert] = g_launcherApplicationIndices[insert-1];
            --insert;
        }
        g_launcherApplicationIndices[insert] = toInsert;
    }
    invalidate_games_launcher_window();
}

static short find_infinite_hd_volume(void)
{
    short volumeIndex = 1;
    while (volumeIndex <= 64) {
        HParamBlockRec volumeInfo;
        Str255 volumeName;
        char volumeNameText[256];
        OSErr error;
        memset(&volumeInfo, 0, sizeof(volumeInfo));
        volumeInfo.volumeParam.ioNamePtr = volumeName;
        volumeInfo.volumeParam.ioVolIndex = volumeIndex++;
        error = PBHGetVInfoSync(&volumeInfo);
        if (error != noErr) break;
        copy_pascal_string_to_c(volumeName, volumeNameText, sizeof(volumeNameText));
        if (strncmp(volumeNameText, "Infinite HD", 11) == 0)
            return volumeInfo.volumeParam.ioVRefNum;
    }
    return 0;
}

static void start_infinite_hd_catalog_scan(void)
{
    g_discoveredApplicationCount = 0;
    g_visibleApplicationCount = 0;
    g_launcherApplicationCount = 0;
    invalidate_games_launcher_window();
    g_reportFirstRow = 0;
    g_catalogFolderDepth = 0;
    g_catalogEntriesScanned = 0;
    g_catalogScanTruncated = false;
    g_runningGameOS = running_game_os();
    g_infiniteHdVolumeRef = find_infinite_hd_volume();
    if (!g_runningGameOS) {
        g_catalogScanActive = false;
        set_games_report_status("No assigned games for this Mac OS.");
        append_terminal_history_line("This Mac OS has no assigned CinterForge games.");
    } else if (!g_infiniteHdVolumeRef) {
        g_catalogScanActive = false;
        set_games_report_status("Infinite HD is not mounted.");
        append_terminal_history_line("Infinite HD is not mounted on this Macintosh.");
    } else {
        /* Only Infinite HD:Games, including publisher and title subfolders.
         * Never scan the entire disk or infer that a folder itself is a game. */
        CInfoPBRec gamesFolder;
        Str255 folderName = "\pGames";
        memset(&gamesFolder, 0, sizeof(gamesFolder));
        gamesFolder.dirInfo.ioNamePtr = folderName;
        gamesFolder.dirInfo.ioVRefNum = g_infiniteHdVolumeRef;
        gamesFolder.dirInfo.ioFDirIndex = 0;
        gamesFolder.dirInfo.ioDrDirID = HFS_ROOT_DIRECTORY_ID;
        if (PBGetCatInfoSync(&gamesFolder) != noErr ||
                !(gamesFolder.hFileInfo.ioFlAttrib & ioDirMask)) {
            g_catalogScanActive = false;
            set_games_report_status("Infinite HD:Games folder not found.");
            append_terminal_history_line("Infinite HD:Games folder not found.");
        } else {
            g_catalogScanActive = true;
            g_catalogFolderStack[0].directoryId = gamesFolder.dirInfo.ioDrDirID;
            g_catalogFolderStack[0].nextEntryIndex = 1;
            g_catalogFolderDepth = 1;
            set_games_report_status("Finding games in Infinite HD:Games...");
        }
    }
    rebuild_visible_application_list();
}

/*
 * PBGetCatInfoSync is bounded to a small batch each event tick, rather than
 * freezing the interface while Infinite HD may contain tens of thousands of
 * files. The folder stack stores the current entry offset at every depth.
 */
static void scan_next_catalog_batch(void)
{
    short entriesRemaining = CATALOG_ENTRIES_PER_EVENT_TICK;
    while (g_catalogScanActive && g_catalogFolderDepth && entriesRemaining-- > 0) {
        CatalogFolder *currentFolder = &g_catalogFolderStack[g_catalogFolderDepth-1];
        CInfoPBRec catalogInfo;
        Str255 entryName;
        OSErr catalogError;
        memset(&catalogInfo, 0, sizeof(catalogInfo));
        catalogInfo.hFileInfo.ioNamePtr = entryName;
        catalogInfo.hFileInfo.ioVRefNum = g_infiniteHdVolumeRef;
        catalogInfo.hFileInfo.ioFDirIndex = currentFolder->nextEntryIndex++;
        catalogInfo.hFileInfo.ioDirID = currentFolder->directoryId;
        catalogError = PBGetCatInfoSync(&catalogInfo);
        if (catalogError != noErr) {
            --g_catalogFolderDepth;
            continue;
        }
        if (++g_catalogEntriesScanned >= MAX_CATALOG_ENTRIES) {
            g_catalogScanTruncated = true;
            g_catalogFolderDepth = 0;
            break;
        }
        if (catalogInfo.hFileInfo.ioFlAttrib & ioDirMask) {
            if (g_catalogFolderDepth < MAX_CATALOG_FOLDERS) {
                long childDirectoryId = catalogInfo.dirInfo.ioDrDirID;
                g_catalogFolderStack[g_catalogFolderDepth].directoryId = childDirectoryId;
                g_catalogFolderStack[g_catalogFolderDepth].nextEntryIndex = 1;
                ++g_catalogFolderDepth;
            } else g_catalogScanTruncated = true;
        } else if (catalogInfo.hFileInfo.ioFlFndrInfo.fdType == 'APPL' &&
                   !is_non_game_application_name(entryName) &&
                   g_discoveredApplicationCount < MAX_DISCOVERED_APPLICATIONS) {
            DiscoveredApplication *application =
                &g_discoveredApplications[g_discoveredApplicationCount];
            if (FSMakeFSSpec(g_infiniteHdVolumeRef, currentFolder->directoryId,
                             entryName, &application->spec) == noErr) {
                memcpy(application->finderName, entryName, (size_t)entryName[0] + 1);
                assign_game_short_name(g_discoveredApplicationCount);
                if (command_in_game_os_catalog(application->shortName, g_runningGameOS)) {
                    ++g_discoveredApplicationCount;
                    /* Avoid rebuilding/sorting every single catalog record. */
                    if (!(g_discoveredApplicationCount % 32)) rebuild_visible_application_list();
                }
            }
        } else if (g_discoveredApplicationCount >= MAX_DISCOVERED_APPLICATIONS) {
            g_catalogScanTruncated = true;
            g_catalogFolderDepth = 0;
            break;
        }
    }
    if (g_catalogScanActive && g_catalogFolderDepth == 0) {
        char message[128];
        g_catalogScanActive = false;
        rebuild_visible_application_list();
        strcpy(message, g_catalogScanTruncated
               ? "Games catalog incomplete: scan limit reached."
               : "Games catalog ready.");
        set_games_report_status(message);
        append_terminal_history_line(message);
        if (g_listGamesWhenScanCompletes) {
            short i;
            for (i = 0; i < g_discoveredApplicationCount; ++i) {
                char listed[TERMINAL_OUTPUT_LINE_SIZE];
                char finder[64];
                copy_pascal_string_to_c(g_discoveredApplications[i].finderName,
                                        finder, sizeof(finder));
                snprintf(listed, sizeof(listed), "  %-27s %s",
                         g_discoveredApplications[i].shortName, finder);
                append_terminal_history_line(listed);
            }
            g_listGamesWhenScanCompletes = false;
        }
    }
}

/* -------------------------------------------------------------------------
 * CinterForge Terminal: read the optional, user-editable automatic launch request
 * ------------------------------------------------------------------------- */

static short find_cinterforge_hd_volume(void)
{
    short volumeIndex = 1;
    for (; volumeIndex <= 64; ++volumeIndex) {
        HParamBlockRec volumeInfo;
        Str255 volumeName;
        char volumeNameText[256];
        memset(&volumeInfo, 0, sizeof(volumeInfo));
        volumeInfo.volumeParam.ioNamePtr = volumeName;
        volumeInfo.volumeParam.ioVolIndex = volumeIndex;
        if (PBHGetVInfoSync(&volumeInfo) != noErr) break;
        copy_pascal_string_to_c(volumeName, volumeNameText, sizeof(volumeNameText));
        if (names_match_ignoring_case(volumeNameText, "CinterForge Terminal")) return volumeInfo.volumeParam.ioVRefNum;
    }
    return 0;
}

/* Read only the first line; it is the exact Finder name, never a file path. */
static void read_game_name_from_config(void)
{
    FSSpec configFileSpec;
    short configFileRef;
    long bytesToRead;
    char configContents[256];
    char *firstCharacter, *lineEnd;
    short volumeRef = find_cinterforge_hd_volume();

    memset(g_requestedFinderName, 0, sizeof(g_requestedFinderName));
    if (!volumeRef || FSMakeFSSpec(volumeRef, HFS_ROOT_DIRECTORY_ID, "\pGame.txt", &configFileSpec) != noErr ||
        FSpOpenDF(&configFileSpec, fsRdPerm, &configFileRef) != noErr) return;
    bytesToRead = sizeof(configContents)-1;
    if (FSRead(configFileRef, &bytesToRead, configContents) != noErr || bytesToRead < 1) {
        FSClose(configFileRef);
        return;
    }
    FSClose(configFileRef);
    configContents[bytesToRead] = 0;
    firstCharacter = configContents;
    while (*firstCharacter == ' ' || *firstCharacter == '\t') ++firstCharacter;
    lineEnd = firstCharacter;
    while (*lineEnd && *lineEnd != '\r' && *lineEnd != '\n') ++lineEnd;
    while (lineEnd > firstCharacter && (lineEnd[-1] == ' ' || lineEnd[-1] == '\t')) --lineEnd;
    *lineEnd = 0;
    if (strlen(firstCharacter) < sizeof(g_requestedFinderName)) strcpy(g_requestedFinderName, firstCharacter);
}

/* -------------------------------------------------------------------------
 * Launching: use the real FSSpec and the classic Process Manager
 * ------------------------------------------------------------------------- */

static OSErr launch_discovered_application(short applicationIndex)
{
    LaunchParamBlockRec launchParameters;
    OSErr launchError;
    char applicationName[64], message[128];
    if (applicationIndex < 0 || applicationIndex >= g_discoveredApplicationCount) return paramErr;
    memset(&launchParameters, 0, sizeof(launchParameters));
    launchParameters.launchBlockID = extendedBlock;
    launchParameters.launchEPBLength = extendedBlockLen;
    /* launchFileFlags is zero-initialized; the available Multiversal
     * headers define launchContinue but not Apple's launchNoFileFlags. */
    launchParameters.launchControlFlags = launchContinue;
    launchParameters.launchAppSpec = &g_discoveredApplications[applicationIndex].spec;
    copy_pascal_string_to_c(g_discoveredApplications[applicationIndex].finderName,
                            applicationName, sizeof(applicationName));
    snprintf(message, sizeof(message), "launch: requested %.64s", applicationName);
    trace_checkpoint(message);
    launchError = LaunchApplication(&launchParameters);
    snprintf(message, sizeof(message), "launch: returned Mac OS error %d", (int)launchError);
    trace_checkpoint(message);
    if (launchError == noErr) {
        /* Keep the Terminal resident so it can accept the next clipboard
         * command when the separate game application eventually exits. */
        return noErr;
    }
    copy_pascal_string_to_c(g_discoveredApplications[applicationIndex].finderName, applicationName, sizeof(applicationName));
    sprintf(message, "Could not launch %.60s (Mac OS error %d)", applicationName, (int)launchError);
    set_games_report_status(message);
    invalidate_games_report_window();
    append_terminal_history_line(message);
    SysBeep(1);
    return launchError;
}

/* Wait until the catalog has finished so duplicate Finder names are detected. */
static void auto_launch_requested_game_if_ready(void)
{
    short applicationIndex, matchingIndex = -1, matchCount = 0;
    char applicationName[64];
    if (!g_requestedFinderName[0] || g_catalogScanActive || g_autoLaunchAttempted) return;
    g_autoLaunchAttempted = true;
    for (applicationIndex = 0; applicationIndex < g_discoveredApplicationCount; ++applicationIndex) {
        copy_pascal_string_to_c(g_discoveredApplications[applicationIndex].finderName,
                                applicationName, sizeof(applicationName));
        if (names_match_ignoring_case(applicationName, g_requestedFinderName)) {
            matchingIndex = applicationIndex;
            ++matchCount;
        }
    }
    if (matchCount == 1) {
        launch_discovered_application(matchingIndex);
    } else if (matchCount > 1) {
        set_games_report_status("More than one app has that name; choose its card instead.");
        append_terminal_history_line("Game.txt name is ambiguous. Try Games Report.");
    } else {
        set_games_report_status("Game.txt did not match a Finder application; choose a card.");
        append_terminal_history_line("Game.txt name not found on Infinite HD. Try Games Report.");
    }
}

/* -------------------------------------------------------------------------
 * Games Report: classic QuickDraw text, card backgrounds and Finder icons
 * ------------------------------------------------------------------------- */

static void draw_text(const char *text, short x, short y, short font, short size)
{
    Str255 pascalText;
    copy_c_string_to_pascal(text, pascalText);
    TextFont(font);
    TextSize(size);
    TextMode(srcCopy);
    ForeColor(blackColor);
    MoveTo(x, y);
    DrawString(pascalText);
}

static void draw_centered_text(const char *text, short x, short y, short width, short size)
{
    Str255 pascalText;
    short textWidth;
    copy_c_string_to_pascal(text, pascalText);
    /* Match the crisp classic Macintosh chrome instead of synthesizing bold
     * pixels at 10 points. The card artwork is already at final size here:
     * this text is painted AFTER CopyBits, never stretched/scaled. */
    TextFont(0);
    TextFace(normal);
    TextSize(size);
    TextMode(srcOr);
    ForeColor(blackColor);
    /* Never draw outside the card's embossed label panel. */
    while (pascalText[0] > 3 && StringWidth(pascalText) > width - 3) {
        pascalText[0]--;
        pascalText[pascalText[0]+1] = 0;
        pascalText[pascalText[0]-2] = '.';
        pascalText[pascalText[0]-1] = '.';
        pascalText[pascalText[0]] = '.';
    }
    textWidth = StringWidth(pascalText);
    MoveTo((short)(x + (width - textWidth)/2), y);
    DrawString(pascalText);
    TextFace(normal);
}

/*
 * Render directly from a classic 16-bit QuickDraw PixMap. Retro68's bundled
 * Multiversal Interfaces intentionally do not provide QDOffscreen.h; the
 * QuickDraw BitMap/PixMap layout and CopyBits are supported classic APIs.
 * The RGB555 asset is authored once by Build, then copied into one persistent
 * on-heap pixel buffer (not decoded per visible card or screen refresh).
 */
/* Load authored RGB555 support graphics from the mounted CinterForge disk.
 * PixMap contents persist for the application lifetime and are never decoded
 * in the update event. A missing asset leaves the normal Finder icon fallback.
 */
static Boolean load_support_rgb555_artwork(const unsigned char *filename,
                                           short requiredWidth, short requiredHeight,
                                           PixMap *pixelMap, Ptr *pixelBuffer)
{
    FSSpec artworkFileSpec;
    CInfoPBRec supportDirectory;
    Str255 supportDirectoryName = "\pSupport Files";
    short fileRef;
    short volumeRef = find_cinterforge_hd_volume();
    unsigned char imageHeader[4];
    short imageWidth, imageHeight;
    long byteCount, bytesRead;
    Ptr decodedPixels;
    Rect imageBounds;
    OSErr readError;

    if (!volumeRef) return false;
    memset(&supportDirectory, 0, sizeof(supportDirectory));
    supportDirectory.dirInfo.ioNamePtr = supportDirectoryName;
    supportDirectory.dirInfo.ioVRefNum = volumeRef;
    supportDirectory.dirInfo.ioFDirIndex = 0;
    supportDirectory.dirInfo.ioDrDirID = HFS_ROOT_DIRECTORY_ID;
    if (PBGetCatInfoSync(&supportDirectory) != noErr ||
        !(supportDirectory.hFileInfo.ioFlAttrib & ioDirMask) ||
        FSMakeFSSpec(volumeRef, supportDirectory.dirInfo.ioDrDirID,
                     filename, &artworkFileSpec) != noErr ||
        FSpOpenDF(&artworkFileSpec, fsRdPerm, &fileRef) != noErr) return false;

    bytesRead = sizeof(imageHeader);
    readError = FSRead(fileRef, &bytesRead, imageHeader);
    if (readError != noErr || bytesRead != sizeof(imageHeader)) {
        FSClose(fileRef);
        return false;
    }
    imageWidth = (short)((imageHeader[0] << 8) | imageHeader[1]);
    imageHeight = (short)((imageHeader[2] << 8) | imageHeader[3]);
    if (imageWidth != requiredWidth || imageHeight != requiredHeight) {
        FSClose(fileRef);
        return false;
    }
    byteCount = (long)imageWidth * imageHeight * 2;
    decodedPixels = NewPtr(byteCount);
    if (!decodedPixels) {
        FSClose(fileRef);
        return false;
    }
    bytesRead = byteCount;
    readError = FSRead(fileRef, &bytesRead, decodedPixels);
    FSClose(fileRef);
    if (readError != noErr || bytesRead != byteCount) {
        DisposePtr(decodedPixels);
        return false;
    }

    SetRect(&imageBounds, 0, 0, imageWidth, imageHeight);
    memset(pixelMap, 0, sizeof(*pixelMap));
    pixelMap->baseAddr = decodedPixels;
    pixelMap->rowBytes = (short)(0x8000U | (unsigned short)(imageWidth * 2));
    pixelMap->bounds = imageBounds;
    pixelMap->hRes = 72L << 16;
    pixelMap->vRes = 72L << 16;
    pixelMap->pixelType = 16;
    pixelMap->pixelSize = 16;
    pixelMap->cmpCount = 3;
    pixelMap->cmpSize = 5;
    *pixelBuffer = decodedPixels;
    return true;
}

static void load_game_card_artwork(void)
{
    if (g_cardArtworkReady) return;
    g_cardArtworkReady = load_support_rgb555_artwork("\pGameCard.rgb",
                           CARD_ARTWORK_WIDTH, CARD_ARTWORK_HEIGHT,
                           &g_cardPixelMap, &g_cardPixelBuffer);
}

static void load_cinterforge_logo_artwork(void)
{
    if (g_logoArtworkReady) return;
    g_logoArtworkReady = load_support_rgb555_artwork("\pCinterForgeLogo.rgb",
                           40, 40, &g_logoPixelMap, &g_logoPixelBuffer);
}

static void draw_game_card_background(const Rect *cardBounds)
{
    if (g_cardArtworkReady && g_cardPixelBuffer) {
        /* The classic GrafPort bit map is accepted by CopyBits as destination;
           no Color QuickDraw GWorld or GetPortPixMap API is required. */
        CopyBits((BitMap *)&g_cardPixelMap, &g_gamesReportWindow->portBits,
                 &g_cardPixelMap.bounds, cardBounds, srcCopy, NULL);
    } else {
        Rect border = *cardBounds;
        EraseRect(&border);
        PenSize(3, 3);
        FrameRoundRect(&border, 10, 10);
        PenNormal();
        InsetRect(&border, 7, 7);
        FrameRoundRect(&border, 8, 8);
    }
}

static void draw_application_finder_icon(short applicationIndex, short x, short y,
                                         short iconSize)
{
    short previousResourceFile;
    short applicationResourceFile;
    short resourceID = APPLICATION_ICON_RESOURCE_ID;
    ResType resourceType;
    Str255 resourceName;
    Handle iconResource;
    Handle iconSuite;
    Rect iconBounds;
    if (applicationIndex < 0 || applicationIndex >= g_discoveredApplicationCount) return;
    previousResourceFile = CurResFile();
    applicationResourceFile = FSpOpenResFile(&g_discoveredApplications[applicationIndex].spec, fsRdPerm);
    if (applicationResourceFile < 0) return;
    UseResFile(applicationResourceFile);
    /* Use the actual APPL resource fork, as Finder does. Prefer its color
     * icon suite at -16455; for apps using another suite ID select a real
     * color resource from that same file before monochrome fallback. */
    iconResource = Get1Resource('icl8', resourceID);
    if (!iconResource) iconResource = Get1Resource('icl4', resourceID);
    if (!iconResource) iconResource = Get1Resource('ICN#', resourceID);
    if (!iconResource) iconResource = Get1IndResource('icl8', 1);
    if (!iconResource) iconResource = Get1IndResource('icl4', 1);
    if (!iconResource) iconResource = Get1IndResource('ICN#', 1);
    if (iconResource) GetResInfo(iconResource, &resourceID, &resourceType, resourceName);
    SetRect(&iconBounds, x, y, x + iconSize, y + iconSize);
    if (iconResource && GetIconSuite(&iconSuite, resourceID, svAllAvailableData) == noErr) {
        PlotIconSuite(&iconBounds, CLASSIC_ICON_ALIGNMENT_NONE, kTransformNone, iconSuite);
        DisposeIconSuite(iconSuite, true);
    } else {
        /* Original System 6 applications sometimes have an 'ICON' resource
           without a color icon family. Draw that real icon first. */
        Handle blackWhite = Get1Resource('ICON', resourceID);
        if (!blackWhite) blackWhite = Get1IndResource('ICON', 1);
        if (blackWhite) PlotIcon(&iconBounds, blackWhite);
        else {
            Handle generic = GetIcon(0); /* Never a synthetic letter badge. */
            if (generic) PlotIcon(&iconBounds, generic);
        }
    }
    CloseResFile(applicationResourceFile);
    UseResFile(previousResourceFile);
}

/* Use the System 7.1 five-column geometry as the maximum card width.
 * That keeps 7.1 unchanged and stops 7.6 from stretching the cards when its
 * report window grows. Center the unchanged card row in the extra space.
 * Drawing and hit testing must always use these same bounds. */
static void game_card_column_bounds(short column, short *cardLeft, short *cardWidth)
{
    short availableWidth = (short)(g_gamesReportWidth - REPORT_SIDE_MARGIN * 2
                                     - CLASSIC_SCROLLBAR_WIDTH);
    /* System 7.1 is 640 pixels wide, with a 615-pixel Games Report. */
    short referenceWidth = (short)((640 - 25 - REPORT_SIDE_MARGIN * 2
                                     - CLASSIC_SCROLLBAR_WIDTH
                                     - (REPORT_COLUMN_COUNT - 1) * CARD_SPACING)
                                    / REPORT_COLUMN_COUNT);
    short maximumColumnWidth = (short)((availableWidth
                                 - (REPORT_COLUMN_COUNT - 1) * CARD_SPACING)
                                 / REPORT_COLUMN_COUNT);
    short width = referenceWidth;
    short rowWidth;
    short rowLeft;
    if (maximumColumnWidth < width) width = maximumColumnWidth;
    if (width < 1) width = 1;
    rowWidth = (short)(width * REPORT_COLUMN_COUNT
                       + (REPORT_COLUMN_COUNT - 1) * CARD_SPACING);
    rowLeft = (short)(REPORT_SIDE_MARGIN + (availableWidth - rowWidth) / 2);
    *cardLeft = (short)(rowLeft + column * (width + CARD_SPACING));
    *cardWidth = width;
}

static void draw_application_card(short visibleApplicationSlot, short cardLeft,
                                  short cardTop, short cardWidth)
{
    Rect cardBounds;
    short applicationIndex = g_visibleApplicationIndices[visibleApplicationSlot];
    char finderName[64];
    SetRect(&cardBounds, cardLeft, cardTop, cardLeft + cardWidth, cardTop + CARD_HEIGHT);
    draw_game_card_background(&cardBounds);
    /* Center real Finder icons and labels inside the unchanged 7.1-size card.
     * Keep the full Finder name in the catalog when it truncates here. */
    draw_application_finder_icon(applicationIndex,
                                 (short)(cardLeft + (cardWidth - 26) / 2),
                                 (short)(cardTop + 43), 26);
    copy_pascal_string_to_c(g_discoveredApplications[applicationIndex].finderName,
                            finderName, sizeof(finderName));
    /* 12-point Chicago uses the classic system bitmap, as the window title
     * does; synthetic 10-point bold made these five-wide labels muddy. */
    draw_centered_text(finderName, cardLeft + 4, cardTop + 109, cardWidth - 8, 12);
    draw_centered_text(g_discoveredApplications[applicationIndex].shortName,
                       cardLeft + 4, cardTop + 124, cardWidth - 8, 12);
    if (applicationIndex == g_selectedApplicationIndex) {
        PenSize(2, 2);
        FrameRect(&cardBounds);
        PenNormal();
    }
}

static short visible_game_card_row_count(void)
{
    short height = g_gamesReportWindow ? g_gamesReportWindow->portRect.bottom : g_gamesReportHeight;
    short availableHeight = height - REPORT_HEADER_HEIGHT - REPORT_FOOTER_HEIGHT - 16;
    if (availableHeight < CARD_HEIGHT) return 1;
    return (short)(availableHeight / (CARD_HEIGHT + CARD_SPACING));
}

static short report_maximum_scroll_row(void)
{
    short columns = REPORT_COLUMN_COUNT;
    short rows = (short)((g_visibleApplicationCount + columns - 1) / columns);
    short maximum = (short)(rows - visible_game_card_row_count());
    return maximum > 0 ? maximum : 0;
}

static void update_games_report_scrollbar(void)
{
    short maxRow = report_maximum_scroll_row();
    if (g_reportFirstRow > maxRow) g_reportFirstRow = maxRow;
    if (g_gamesReportScrollBar) {
        SetControlMaximum(g_gamesReportScrollBar, maxRow);
        SetControlValue(g_gamesReportScrollBar, g_reportFirstRow);
    }
}

static void position_games_report_scrollbar(void)
{
    Rect bounds;
    if (!g_gamesReportWindow || !g_gamesReportScrollBar) return;
    bounds = g_gamesReportWindow->portRect;
    MoveControl(g_gamesReportScrollBar, (short)(bounds.right - CLASSIC_SCROLLBAR_WIDTH),
                REPORT_HEADER_HEIGHT);
    SizeControl(g_gamesReportScrollBar, CLASSIC_SCROLLBAR_WIDTH,
                (short)(bounds.bottom - REPORT_HEADER_HEIGHT - REPORT_FOOTER_HEIGHT));
    update_games_report_scrollbar();
}

static void draw_games_report_window(void)
{
    short visibleSlot, columnCount, visibleRows, firstVisibleIndex, visibleSlots;
    Rect windowBounds, headerBounds, footerBounds, headerDivider, cardClip;
    RgnHandle oldClip = NULL;
    RGBColor white = {0xffff, 0xffff, 0xffff};
    RGBColor black = {0, 0, 0};
    if (!g_gamesReportWindow) return;
    SetPort(g_gamesReportWindow);
    windowBounds = g_gamesReportWindow->portRect;
    g_gamesReportWidth = windowBounds.right;
    g_gamesReportHeight = windowBounds.bottom;
    RGBBackColor(&white);
    RGBForeColor(&black);
    EraseRect(&windowBounds);
    SetRect(&headerBounds, 0, 0, g_gamesReportWidth, REPORT_HEADER_HEIGHT);
    SetRect(&footerBounds, 0, (short)(g_gamesReportHeight-REPORT_FOOTER_HEIGHT),
            g_gamesReportWidth, g_gamesReportHeight);
    SetRect(&headerDivider, 8, REPORT_HEADER_HEIGHT - 5, g_gamesReportWidth-8, REPORT_HEADER_HEIGHT-4);
    PaintRect(&headerDivider);
    draw_text("CinterForge Games", 62, 28, 1, 20);
    draw_text("Games on this Macintosh", 62, 48, 0, 12);
    draw_text("Games on Infinite HD", 62, 64, 0, 10);
    /* Full-colour authored logo. Finder's indexed icon resources can be drawn
     * as monochrome by a guest; QuickDraw RGB555 preserves blue and orange. */
    {
        Rect logoRect;
        SetRect(&logoRect, 12, 9, 52, 49);
        if (!g_logoArtworkReady) load_cinterforge_logo_artwork();
        if (g_logoArtworkReady) {
            CopyBits((BitMap *)&g_logoPixelMap, &g_gamesReportWindow->portBits,
                     &g_logoPixelMap.bounds, &logoRect, srcCopy, NULL);
        } else {
            Handle logoIconSuite;
            if (GetIconSuite(&logoIconSuite, APPLICATION_ICON_RESOURCE_ID,
                             svAllAvailableData) == noErr) {
                PlotIconSuite(&logoRect, CLASSIC_ICON_ALIGNMENT_NONE,
                              kTransformNone, logoIconSuite);
                DisposeIconSuite(logoIconSuite, true);
            }
        }
    }
    SetRect(&g_searchFieldBounds, g_gamesReportWidth-230, 11, g_gamesReportWidth-18, 35);
    FrameRect(&g_searchFieldBounds);
    draw_text(g_applicationNameFilter[0] ? g_applicationNameFilter : "Type to filter games...",
                (short)(g_searchFieldBounds.left+6), (short)(g_searchFieldBounds.top+17), 0, 11);
    /* Report deliberately omits catalog counts, like the other platforms. */
    columnCount = REPORT_COLUMN_COUNT;
    update_games_report_scrollbar();
    visibleRows = visible_game_card_row_count();
    firstVisibleIndex = (short)(g_reportFirstRow * columnCount);
    visibleSlots = (short)(visibleRows * columnCount);
    SetRect(&cardClip, 0, REPORT_HEADER_HEIGHT,
            (short)(windowBounds.right - CLASSIC_SCROLLBAR_WIDTH),
            (short)(windowBounds.bottom - REPORT_FOOTER_HEIGHT));
    oldClip = NewRgn();
    if (oldClip) { GetClip(oldClip); ClipRect(&cardClip); }
    for (visibleSlot = 0;
         visibleSlot < visibleSlots && firstVisibleIndex + visibleSlot < g_visibleApplicationCount;
         ++visibleSlot) {
        short cardLeft, cardWidth;
        short cardTop = (short)(REPORT_HEADER_HEIGHT + 5 + (visibleSlot / columnCount) * (CARD_HEIGHT + CARD_SPACING));
        game_card_column_bounds((short)(visibleSlot % columnCount), &cardLeft, &cardWidth);
        draw_application_card((short)(firstVisibleIndex+visibleSlot), cardLeft, cardTop, cardWidth);
    }
    if (oldClip) { SetClip(oldClip); DisposeRgn(oldClip); }
    if (!g_infiniteHdVolumeRef && !g_catalogScanActive)
        draw_text("Infinite HD is not mounted on this Macintosh.", 20, REPORT_HEADER_HEIGHT+38, 0, 12);
    else if (!g_visibleApplicationCount && !g_catalogScanActive)
        draw_text("No matching Macintosh games.", 20, REPORT_HEADER_HEIGHT+38, 0, 12);
    FrameRect(&footerBounds);
    draw_text(g_gamesReportStatus, 11, g_gamesReportHeight-7, 0, 10);
    DrawControls(g_gamesReportWindow);
}

static void scroll_games_report_by_rows(short rowOffset)
{
    short visibleRows = visible_game_card_row_count();
    short totalRows = (short)((g_visibleApplicationCount + REPORT_COLUMN_COUNT-1) / REPORT_COLUMN_COUNT);
    short lastPageRow = totalRows - visibleRows;
    short newFirstRow = g_reportFirstRow + rowOffset;
    if (lastPageRow < 0) lastPageRow = 0;
    if (newFirstRow < 0) newFirstRow = 0;
    if (newFirstRow > lastPageRow) newFirstRow = lastPageRow;
    if (g_reportFirstRow != newFirstRow) {
        g_reportFirstRow = newFirstRow;
        update_games_report_scrollbar();
        invalidate_games_report_window();
    }
}

/* One deliberate click anywhere on a card launches its real FSSpec.
 * Ignore the second press of a physical double-click to avoid spawning two
 * game processes before the first finishes switching to the foreground. */
static void handle_game_card_click(Point localMousePosition, UInt32 clickTime)
{
    static short lastLaunchedIndex = -1;
    static UInt32 lastLaunchClickTime = 0;
    short cardAreaY = localMousePosition.v - REPORT_HEADER_HEIGHT - 5;
    short columnIndex, rowIndex, visibleSlot, applicationIndex;
    char clickMessage[100];
    if (PtInRect(localMousePosition, &g_searchFieldBounds)) return;
    if (cardAreaY < 0 || localMousePosition.v >= g_gamesReportHeight - REPORT_FOOTER_HEIGHT) return;
    for (columnIndex = 0; columnIndex < REPORT_COLUMN_COUNT; ++columnIndex) {
        short cardLeft, cardWidth;
        game_card_column_bounds(columnIndex, &cardLeft, &cardWidth);
        if (localMousePosition.h >= cardLeft && localMousePosition.h < cardLeft + cardWidth)
            break;
    }
    if (columnIndex == REPORT_COLUMN_COUNT) return;
    rowIndex = cardAreaY / (CARD_HEIGHT + CARD_SPACING);
    if (rowIndex >= visible_game_card_row_count() ||
        cardAreaY % (CARD_HEIGHT + CARD_SPACING) >= CARD_HEIGHT) return;
    visibleSlot = (short)((g_reportFirstRow + rowIndex) * REPORT_COLUMN_COUNT + columnIndex);
    if (visibleSlot < 0 || visibleSlot >= g_visibleApplicationCount) return;
    applicationIndex = g_visibleApplicationIndices[visibleSlot];
    snprintf(clickMessage, sizeof(clickMessage),
             "report: card click x=%d y=%d col=%d row=%d app=%d",
             (int)localMousePosition.h, (int)localMousePosition.v,
             (int)columnIndex, (int)rowIndex, (int)applicationIndex);
    trace_checkpoint(clickMessage);
    if (lastLaunchedIndex == applicationIndex && clickTime >= lastLaunchClickTime &&
        clickTime - lastLaunchClickTime < mac_double_click_interval_in_ticks()) return;
    g_selectedApplicationIndex = applicationIndex;
    lastLaunchedIndex = applicationIndex;
    lastLaunchClickTime = clickTime;
    launch_discovered_application(applicationIndex);
}

static void handle_games_report_key(EventRecord *keyboardEvent)
{
    unsigned char typedCharacter =
        (unsigned char)(keyboardEvent->message & charCodeMask);

    if (keyboardEvent->modifiers & cmdKey) {
        if (typedCharacter == 'q' || typedCharacter == 'Q') {
            g_applicationIsRunning = false;
        } else if (typedCharacter == 'w' || typedCharacter == 'W') {
            HideWindow(g_gamesReportWindow);
            SelectWindow(g_terminalWindow);
        } else if (typedCharacter == 'r' || typedCharacter == 'R') {
            start_infinite_hd_catalog_scan();
        } else if (typedCharacter == 'o' || typedCharacter == 'O') {
            launch_discovered_application(g_selectedApplicationIndex);
        } else if (typedCharacter == 'c' || typedCharacter == 'C') {
            copy_game_list_to_mac_clipboard();
        }
        return;
    }

    /* Page Up/Down and arrow keys move the visible card grid. */
    if (typedCharacter == 0x0b || typedCharacter == 0x0c) {
        scroll_games_report_by_rows(typedCharacter == 0x0b ? -1 : 1);
        return;
    }
    if (typedCharacter == 0x1e) {
        scroll_games_report_by_rows(-1);
        return;
    }
    if (typedCharacter == 0x1f) {
        scroll_games_report_by_rows(1);
        return;
    }
    if (typedCharacter == 0x0d || typedCharacter == 0x03) {
        launch_discovered_application(g_selectedApplicationIndex);
        return;
    }

    /* Printable characters filter by the application's real Finder name. */
    if (typedCharacter == 0x08 || typedCharacter == 0x7f) {
        size_t filterLength = strlen(g_applicationNameFilter);
        if (filterLength) g_applicationNameFilter[filterLength - 1] = 0;
        rebuild_visible_application_list();
    } else if (typedCharacter >= 32 && typedCharacter <= 126 &&
               strlen(g_applicationNameFilter) < sizeof(g_applicationNameFilter) - 1) {
        size_t filterLength = strlen(g_applicationNameFilter);
        g_applicationNameFilter[filterLength] = typedCharacter;
        g_applicationNameFilter[filterLength + 1] = 0;
        rebuild_visible_application_list();
    }
}

static void open_games_report_window(void)
{
    Rect windowBounds, screenBounds;
    trace_checkpoint("report: opening window");
    if (!g_gamesReportWindow) {
        screenBounds = qd.screenBits.bounds;
        g_gamesReportWidth = (short)(screenBounds.right - screenBounds.left - 25);
        if (g_gamesReportWidth > 790) g_gamesReportWidth = 790;
        if (g_gamesReportWidth < 365) g_gamesReportWidth = 365;
        g_gamesReportHeight = (short)(screenBounds.bottom - screenBounds.top - 70);
        if (g_gamesReportHeight > 565) g_gamesReportHeight = 565;
        if (g_gamesReportHeight < 315) g_gamesReportHeight = 315;
        SetRect(&windowBounds, 17, 50, 17 + g_gamesReportWidth, 50 + g_gamesReportHeight);
        /* Finder icon suites need a Color QuickDraw port. NewWindow creates
         * a monochrome GrafPort even on 8-bit/16-bit Mac displays; NewCWindow
         * allows PlotIconSuite to use the application's real icl4/icl8 icons.
         * Keep the separate, already-working Terminal window unchanged. */
        g_gamesReportWindow = (WindowPtr)NewCWindow(NULL, &windowBounds,
                             "\pGames Report", false,
                             documentProc, (WindowPtr)-1, true, 0);
        if (!g_gamesReportWindow) { SysBeep(1); append_terminal_history_line("Could not open Games Report window."); return; }
        load_game_card_artwork();
        {
            Rect scrollbarBounds = g_gamesReportWindow->portRect;
            SetRect(&scrollbarBounds,
                    (short)(scrollbarBounds.right - CLASSIC_SCROLLBAR_WIDTH),
                    REPORT_HEADER_HEIGHT, scrollbarBounds.right,
                    (short)(scrollbarBounds.bottom - REPORT_FOOTER_HEIGHT));
            g_gamesReportScrollBar = NewControl(g_gamesReportWindow, &scrollbarBounds,
                                               "\p", true, 0, 0, 0, scrollBarProc, 0);
            position_games_report_scrollbar();
        }
    }
    ShowWindow(g_gamesReportWindow);
    SelectWindow(g_gamesReportWindow);
    invalidate_games_report_window();
    trace_checkpoint("report: window visible");
}

/* -------------------------------------------------------------------------
 * Games Launcher: separate Finder-style icon folder; no card images or export.
 * All icons and names come from the same real APPL entries as Games Report.
 * The exact 8x8 / 6x6 cells remain fixed even when fewer games are mounted,
 * so each screenshot can later be sliced on deterministic cell boundaries.
 * ------------------------------------------------------------------------- */

static short launcher_column_count(void)
{
    return g_runningGameOS == GAME_OS_MAC_76 ? 6 : 8;
}

static short launcher_row_count(void)
{
    return g_runningGameOS == GAME_OS_MAC_76 ? 6 : 8;
}

/* Finder-style two-line names, rather than a single card-width truncation.
 * Split on the last space that fits; long unspaced names split by width.
 * The full Finder name still stays in the unchanged catalog and FSSpec. */
static void draw_launcher_name(short applicationIndex, short left,
                               short baseline, short width)
{
    char name[64], first[64], second[64];
    Str255 measured;
    short length, fit, split = 0, i;
    copy_pascal_string_to_c(g_discoveredApplications[applicationIndex].finderName,
                            name, sizeof(name));
    TextFont(0);
    TextFace(normal);
    TextSize(LAUNCHER_LABEL_SIZE);
    length = (short)strlen(name);
    for (fit = 1; fit <= length; ++fit) {
        char saved = name[fit];
        name[fit] = 0;
        copy_c_string_to_pascal(name, measured);
        name[fit] = saved;
        if (StringWidth(measured) > width - 4) break;
    }
    if (fit > length) {
        draw_centered_text(name, left, baseline, width, LAUNCHER_LABEL_SIZE);
        return;
    }
    for (i = 1; i < fit; ++i)
        if (name[i] == ' ') split = i;
    if (!split) split = (short)(fit - 1);
    if (split < 1) split = 1;
    memcpy(first, name, (size_t)split);
    first[split] = 0;
    i = split;
    while (name[i] == ' ') ++i;
    strcpy(second, name + i);
    draw_centered_text(first, left, baseline, width, LAUNCHER_LABEL_SIZE);
    draw_centered_text(second, left, (short)(baseline + 9), width,
                       LAUNCHER_LABEL_SIZE);
}

static void draw_games_launcher_window(void)
{
    Rect bounds;
    RGBColor white = {0xffff, 0xffff, 0xffff};
    RGBColor black = {0, 0, 0};
    short columns = launcher_column_count(), rows = launcher_row_count();
    short slot, capacity = (short)(columns * rows);
    if (!g_gamesLauncherWindow) return;
    SetPort(g_gamesLauncherWindow);
    bounds = g_gamesLauncherWindow->portRect;
    RGBBackColor(&white);
    RGBForeColor(&black);
    EraseRect(&bounds);
    if (!g_launcherApplicationCount) {
        if (!g_catalogScanActive)
            draw_text("No games found on Infinite HD.", 12, 23, 0, 12);
        return;
    }
    for (slot = 0; slot < g_launcherApplicationCount && slot < capacity; ++slot) {
        short column = (short)(slot % columns);
        short row = (short)(slot / columns);
        short left = (short)((long)column * bounds.right / columns);
        short right = (short)((long)(column + 1) * bounds.right / columns);
        short top = (short)((long)row * bounds.bottom / rows);
        short bottom = (short)((long)(row + 1) * bounds.bottom / rows);
        short cellWidth = (short)(right - left);
        short cellHeight = (short)(bottom - top);
        short contentTop = (short)(top + (cellHeight > LAUNCHER_CONTENT_HEIGHT
                                   ? (cellHeight - LAUNCHER_CONTENT_HEIGHT)/2 : 0));
        short applicationIndex = g_launcherApplicationIndices[slot];
        draw_application_finder_icon(applicationIndex,
                                     (short)(left + (cellWidth - LAUNCHER_ICON_SIZE)/2),
                                     contentTop, LAUNCHER_ICON_SIZE);
        draw_launcher_name(applicationIndex, left + 2,
                           (short)(contentTop + 39), cellWidth - 4);
    }
}

/* One click opens the real application; suppress the second physical press of
 * a double-click, just as Games Report does. No extra launcher protocol. */
static void handle_games_launcher_click(Point point, UInt32 clickTime)
{
    static short previousIndex = -1;
    static UInt32 previousClick = 0;
    Rect bounds;
    short columns = launcher_column_count(), rows = launcher_row_count();
    short column, row, slot, applicationIndex;
    if (!g_gamesLauncherWindow) return;
    bounds = g_gamesLauncherWindow->portRect;
    if (point.h < 0 || point.v < 0 || point.h >= bounds.right ||
        point.v >= bounds.bottom || !bounds.right || !bounds.bottom) return;
    column = (short)((long)point.h * columns / bounds.right);
    row = (short)((long)point.v * rows / bounds.bottom);
    slot = (short)(row * columns + column);
    if (slot >= g_launcherApplicationCount) return;
    applicationIndex = g_launcherApplicationIndices[slot];
    if (applicationIndex == previousIndex && clickTime >= previousClick &&
        clickTime - previousClick < mac_double_click_interval_in_ticks()) return;
    previousIndex = applicationIndex;
    previousClick = clickTime;
    launch_discovered_application(applicationIndex);
}

static void open_games_launcher_window(void)
{
    Rect screenBounds, bounds;
    short width, height;
    if (!g_gamesLauncherWindow) {
        screenBounds = qd.screenBits.bounds;
        width = (short)(screenBounds.right - screenBounds.left - 22);
        height = (short)(screenBounds.bottom - screenBounds.top - 72);
        if (width > 1000) width = 1000;
        if (height > 730) height = 730;
        if (width < 400) width = 400;
        if (height < 350) height = 350;
        SetRect(&bounds, 11, 43, (short)(11 + width), (short)(43 + height));
        /* Color GrafPort is necessary to render the actual 'icl4'/'icl8'
         * resources through the already-used draw_application_finder_icon. */
        g_gamesLauncherWindow = (WindowPtr)NewCWindow(NULL, &bounds,
                                "\pGames Launcher", false,
                                documentProc, (WindowPtr)-1, true, 0);
        if (!g_gamesLauncherWindow) {
            SysBeep(1);
            append_terminal_history_line("Could not open Games Launcher window.");
            return;
        }
    }
    ShowWindow(g_gamesLauncherWindow);
    SelectWindow(g_gamesLauncherWindow);
    invalidate_games_launcher_window();
}

static void handle_games_launcher_key(EventRecord *event)
{
    unsigned char key = (unsigned char)(event->message & charCodeMask);
    if (event->modifiers & cmdKey) {
        if (key == 'q' || key == 'Q') g_applicationIsRunning = false;
        else if (key == 'w' || key == 'W') {
            HideWindow(g_gamesLauncherWindow);
            SelectWindow(g_terminalWindow);
        } else if (key == 'r' || key == 'R') start_infinite_hd_catalog_scan();
        else if (key == 'o' || key == 'O') open_games_report_window();
    }
}

/* -------------------------------------------------------------------------
 * Game-list exchange: classic Scrap Manager and Infinite Mac's Outside World
 * ------------------------------------------------------------------------- */

/* PutScrap and GetScrap are system traps on the System 7 guests we support.
 * Declare the trap entry points explicitly; this avoids relying on absent
 * compatibility headers or Retro68 linker aliases. */
static pascal long clear_mac_clipboard(void) M68K_INLINE(0xA9FC);
static pascal long write_mac_clipboard(long length, ResType kind,
                                     const void *source) M68K_INLINE(0xA9FE);
static pascal long read_mac_clipboard(Handle destination, ResType kind,
                                    long *offset) M68K_INLINE(0xA9FD);

/* All exports use the *actual* filtered catalog.  Tab-separated columns give
 * the short command followed by the full Finder name; MacRoman/CR line endings
 * are natural for System 7's SimpleText and the Scrap Manager. */
static Ptr make_game_list_text(long *textLength)
{
    long capacity, used = 0;
    Ptr output;
    short gameIndex;

    *textLength = 0;
    if (g_catalogScanActive || g_discoveredApplicationCount <= 0) return NULL;
    capacity = (long)g_discoveredApplicationCount * 128L + 1L;
    output = NewPtr(capacity);
    if (!output) return NULL;

    for (gameIndex = 0; gameIndex < g_discoveredApplicationCount; ++gameIndex) {
        char title[64];
        char row[128];
        size_t rowLength;
        copy_pascal_string_to_c(g_discoveredApplications[gameIndex].finderName,
                                title, sizeof(title));
        snprintf(row, sizeof(row), "%s\t%s\r",
                 g_discoveredApplications[gameIndex].shortName, title);
        rowLength = strlen(row);
        if (used + (long)rowLength > capacity) {
            DisposePtr(output);
            return NULL;
        }
        memcpy(output + used, row, rowLength);
        used += (long)rowLength;
    }
    *textLength = used;
    return output;
}

static void notify_game_list_operation(const char *message)
{
    append_terminal_history_line(message);
    set_games_report_status(message);
    invalidate_games_report_window();
}

static void copy_game_list_to_mac_clipboard(void)
{
    long length;
    Ptr text = make_game_list_text(&length);
    if (!text) {
        notify_game_list_operation(g_catalogScanActive ?
            "Games scan is running; copy after it finishes." :
            "No games available to copy, or insufficient memory.");
        return;
    }
    if (clear_mac_clipboard() < 0 ||
        write_mac_clipboard(length, 'TEXT', text) < 0) {
        notify_game_list_operation("Macintosh clipboard copy failed.");
    } else {
        notify_game_list_operation("Game list copied to Macintosh clipboard.");
    }
    DisposePtr(text);
}

/* A guest clipboard is not automatically the host macOS clipboard.  Infinite
 * Mac exposes the special 'The Outside World:Uploads' directory for genuine
 * guest-to-host downloads, so export directly there instead of claiming that
 * Command-C alone sends text across a cross-origin iframe. */
static short find_outside_world_volume(void)
{
    short volumeIndex;
    for (volumeIndex = 1; volumeIndex <= 64; ++volumeIndex) {
        HParamBlockRec volumeInfo;
        Str255 name;
        char decodedName[256];
        memset(&volumeInfo, 0, sizeof(volumeInfo));
        volumeInfo.volumeParam.ioNamePtr = name;
        volumeInfo.volumeParam.ioVolIndex = volumeIndex;
        if (PBHGetVInfoSync(&volumeInfo) != noErr) break;
        copy_pascal_string_to_c(name, decodedName, sizeof(decodedName));
        if (names_match_ignoring_case(decodedName, "The Outside World"))
            return volumeInfo.volumeParam.ioVRefNum;
    }
    return 0;
}

static void export_game_list_to_host(void)
{
    short outsideWorld = find_outside_world_volume();
    CInfoPBRec folderInfo;
    Str255 folderName = "\pUploads";
    FSSpec destination;
    OSErr error;
    short fileRef = 0;
    long bytesToWrite;
    Ptr text;

    if (!outsideWorld) {
        notify_game_list_operation("The Outside World disk is not mounted.");
        return;
    }
    memset(&folderInfo, 0, sizeof(folderInfo));
    folderInfo.dirInfo.ioNamePtr = folderName;
    folderInfo.dirInfo.ioVRefNum = outsideWorld;
    folderInfo.dirInfo.ioFDirIndex = 0;
    folderInfo.dirInfo.ioDrDirID = HFS_ROOT_DIRECTORY_ID;
    if (PBGetCatInfoSync(&folderInfo) != noErr ||
        !(folderInfo.hFileInfo.ioFlAttrib & ioDirMask)) {
        notify_game_list_operation("Outside World:Uploads folder unavailable.");
        return;
    }

    text = make_game_list_text(&bytesToWrite);
    if (!text) {
        notify_game_list_operation(g_catalogScanActive ?
            "Games scan is running; export after it finishes." :
            "No game list to export, or insufficient memory.");
        return;
    }
    error = FSMakeFSSpec(outsideWorld, folderInfo.dirInfo.ioDrDirID,
                           "\pCinterForge Games.txt", &destination);
    if (error == fnfErr) {
        error = FSpCreate(&destination, 'ttxt', 'TEXT', 0);
    }
    if (error == noErr) error = FSpOpenDF(&destination, fsRdWrPerm, &fileRef);
    if (error == noErr) {
        error = SetEOF(fileRef, 0);
        if (error == noErr) error = FSWrite(fileRef, &bytesToWrite, text);
        FSClose(fileRef);
    }
    DisposePtr(text);
    if (error == noErr)
        notify_game_list_operation("Exported to The Outside World:Uploads for host download.");
    else
        notify_game_list_operation("Could not write the game list to Outside World:Uploads.");
}

/* Paste ordinary text into the Terminal command prompt.  The Terminal accepts
 * a single command, so line endings end the paste rather than feeding an
 * arbitrary multi-line clipboard as executable commands. */
static void paste_mac_clipboard_into_terminal(void)
{
    Handle text = NewHandle(0);
    long scrapOffset = 0;
    long length;
    size_t inputLength;
    long byteIndex;
    if (!text) return;
    length = read_mac_clipboard(text, 'TEXT', &scrapOffset);
    if (length > 0) {
        HLock(text);
        inputLength = strlen(g_terminalInput);
        for (byteIndex = 0; byteIndex < length &&
             inputLength < sizeof(g_terminalInput)-1; ++byteIndex) {
            unsigned char character = (unsigned char)(*text)[byteIndex];
            if (character == '\r' || character == '\n') break;
            if (character >= 32 && character <= 126)
                g_terminalInput[inputLength++] = character;
        }
        g_terminalInput[inputLength] = 0;
        HUnlock(text);
        invalidate_terminal_prompt();
    }
    DisposeHandle(text);
}

/* -------------------------------------------------------------------------
 * Terminal commands: preserve the existing games/play/Finder-name vocabulary
 * ------------------------------------------------------------------------- */

static void list_discovered_games_in_terminal(void)
{
    short visibleIndex;
    char applicationName[64], outputLine[TERMINAL_OUTPUT_LINE_SIZE];
    if (g_catalogScanActive) {
        g_listGamesWhenScanCompletes = true;
        append_terminal_history_line("Scanning Infinite HD; listing games when ready...");
        return;
    }
    if (!g_infiniteHdVolumeRef) {
        append_terminal_history_line("Infinite HD is not mounted.");
        return;
    }
    snprintf(outputLine, sizeof(outputLine), "Games:");
    append_terminal_history_line(outputLine);
    for (visibleIndex = 0; visibleIndex < g_discoveredApplicationCount; ++visibleIndex) {
        copy_pascal_string_to_c(
            g_discoveredApplications[visibleIndex].finderName,
            applicationName, sizeof(applicationName));
        snprintf(outputLine, sizeof(outputLine), "  %-27s %s",
                 g_discoveredApplications[visibleIndex].shortName,
                 applicationName);
        append_terminal_history_line(outputLine);
    }
    append_terminal_history_line("Type 'games report' for application cards.");
}

static void launch_game_by_finder_name(const char *requestedName)
{
    short applicationIndex;
    short matchingIndex = -1;
    short matchCount = 0;
    char finderName[64];
    char errorMessage[TERMINAL_OUTPUT_LINE_SIZE];
    size_t requestedNameLength = strlen(requestedName);

    /* Accept `play "name"`, `"name"`, and an unquoted Finder name. */
    while (requestedNameLength &&
           (requestedName[requestedNameLength - 1] == ' ' ||
            requestedName[requestedNameLength - 1] == '\t')) {
        --requestedNameLength;
    }
    if (requestedNameLength >= 2 && requestedName[0] == '"' &&
        requestedName[requestedNameLength - 1] == '"') {
        ++requestedName;
        requestedNameLength -= 2;
    }

    if (!requestedNameLength || requestedNameLength > 63) {
        append_terminal_history_line("Enter an exact Finder application name.");
        return;
    }
    if (g_catalogScanActive) {
        append_terminal_history_line("Wait for the Infinite HD scan to complete.");
        return;
    }

    /* Find every matching APPL to avoid launching an ambiguous name. */
    for (applicationIndex = 0;
         applicationIndex < g_discoveredApplicationCount;
         ++applicationIndex) {
        copy_pascal_string_to_c(g_discoveredApplications[applicationIndex].finderName,
                                finderName, sizeof(finderName));
        {
            char inputName[64];
            memcpy(inputName, requestedName, requestedNameLength);
            inputName[requestedNameLength] = 0;
            if (!names_match_ignoring_case(finderName, inputName) &&
                    !names_match_ignoring_case(g_discoveredApplications[applicationIndex].shortName, inputName))
                continue;
            matchingIndex = applicationIndex;
            ++matchCount;
        }
    }

    if (matchCount == 1) {
        launch_discovered_application(matchingIndex);
    } else if (matchCount > 1) {
        append_terminal_history_line("Multiple applications match; choose a card in Games Report.");
    } else {
        snprintf(errorMessage, sizeof(errorMessage), "Not found on Infinite HD: %.*s",
                 (int)requestedNameLength, requestedName);
        append_terminal_history_line(errorMessage);
        SysBeep(1);
    }
}

static void launch_game_from_config_file(void)
{
    read_game_name_from_config();
    if (!g_requestedFinderName[0]) {
        append_terminal_history_line("Game.txt is empty. Use games report to browse.");
        return;
    }
    g_autoLaunchAttempted = false;
    auto_launch_requested_game_if_ready();
}

static void execute_terminal_command(char *commandLine)
{
    char *commandText = commandLine;

    /* Leading spaces do not change the command, but Finder names are exact. */
    while (*commandText == ' ' || *commandText == '\t') ++commandText;
    if (!*commandText) return;

    if (names_match_ignoring_case(commandText, "help")) {
        append_terminal_history_line("games                         List of playable games");
        append_terminal_history_line("play <title>                  Launch a game (play is optional)");
        append_terminal_history_line("games report                  Open Games Report");
        append_terminal_history_line("games launcher                Open Finder-style icon grid");
        append_terminal_history_line("games copy                    Copy all games to Mac clipboard");
        append_terminal_history_line("games export                  Download list via The Outside World");
        append_terminal_history_line("\"name\"            Launch a game by Finder name");
    } else if (names_match_ignoring_case(commandText, "games")) {
        list_discovered_games_in_terminal();
    } else if (names_match_ignoring_case(commandText, "games report")) {
        trace_checkpoint("report: opened by terminal command");
        open_games_report_window();
    } else if (names_match_ignoring_case(commandText, "games launcher")) {
        open_games_launcher_window();
    } else if (names_match_ignoring_case(commandText, "games copy")) {
        copy_game_list_to_mac_clipboard();
    } else if (names_match_ignoring_case(commandText, "games export")) {
        export_game_list_to_host();
    } else if (names_match_ignoring_case(commandText, "quit") ||
               names_match_ignoring_case(commandText, "exit")) {
        g_applicationIsRunning = false;
    } else if (names_match_ignoring_case(commandText, "games rescan")) {
        start_infinite_hd_catalog_scan();
    } else if (names_match_ignoring_case(commandText, "run game.txt")) {
        launch_game_from_config_file();
    } else if (strncmp(commandText, "play ", 5) == 0) {
        launch_game_by_finder_name(commandText + 5);
    } else {
        /* The existing Terminal convention allows a bare Finder name. */
        launch_game_by_finder_name(commandText);
    }
}

/* Clipboard command pipe. Each guest observes the same protocol but handles
 * only cinter:<its OS>:<command>. This is ordinary System 7 Scrap Manager
 * text, not a network listener, shell, or second app. The host serializes
 * HELLO handshakes when multiple guests boot concurrently. */
static const char *cinter_clipboard_os(void)
{
    if (g_runningGameOS == GAME_OS_SYSTEM_71) return "7.1";
    if (g_runningGameOS == GAME_OS_MAC_76) return "7.6";
    return NULL;
}

static Boolean write_cinter_clipboard_reply(const char *verb, const char *detail)
{
    const char *os = cinter_clipboard_os();
    char reply[192];
    if (!os || !detail || strlen(detail) > 125) return false;
    snprintf(reply, sizeof(reply), "cinter:%s:%s %s", os, verb, detail);
    return clear_mac_clipboard() >= 0 &&
           write_mac_clipboard((long)strlen(reply), 'TEXT', reply) >= 0;
}

/* Copy only bounded single-line ASCII from the Scrap Manager. The same traps
 * already back `games copy` and the native Paste command. */
static Boolean read_cinter_clipboard_message(char *output, size_t capacity)
{
    /* GetScrap resizes this unlocked handle. Never treat a missing TEXT
     * flavor as an empty HELLO, and preserve the result for diagnostics. */
    static Handle text = NULL;
    long length, offset = 0, i;
    long usefulLength;
    char diagnostic[96];
    if (!capacity) return false;
    if (!text) text = NewHandle(0);
    if (!text) {
        if (g_lastScrapReadStatus != -108L) {
            g_lastScrapReadStatus = -108L;
            trace_checkpoint("clipboard: GetScrap handle allocation failed");
        }
        return false;
    }
    length = read_mac_clipboard(text, 'TEXT', &offset);
    if (length != g_lastScrapReadStatus) {
        g_lastScrapReadStatus = length;
        snprintf(diagnostic, sizeof(diagnostic),
                 "clipboard: GetScrap TEXT result=%ld", length);
        trace_checkpoint(diagnostic);
    }
    if (length <= 0 || (unsigned long)length >= capacity ||
        GetHandleSize(text) < length) return false;
    HLock(text);
    usefulLength = length;
    /* Classic Mac TEXT uses CR; native clipboards may supply CRLF, LF or
     * a trailing NUL. None should prevent a one-line protocol message. */
    while (usefulLength > 0 &&
           ((*text)[usefulLength-1] == '\r' ||
            (*text)[usefulLength-1] == '\n' ||
            (*text)[usefulLength-1] == '\0'))
        --usefulLength;
    for (i = 0; i < usefulLength; ++i) {
        unsigned char byte = (unsigned char)(*text)[i];
        if (byte < 32 || byte > 126) break;
        output[i] = (char)byte;
    }
    output[i] = 0;
    HUnlock(text);
    if (i != usefulLength || !usefulLength)
        return false;
    return true;
}

/* An addressable command may arrive while the incremental Infinite HD scan
 * is running. Defer one command until discovery completes instead of claiming
 * execution when the game is not yet resolvable. */
static void dispatch_cinter_clipboard_command(const char *command)
{
    char mutableCommand[128];
    if (!command[0] || strlen(command) >= sizeof(mutableCommand)) return;
    /* Preserve the existing normal Terminal command grammar. Do not accept
     * remote quit, scrap copy/export or implicit bare-name launches, which
     * could otherwise execute accidental/untrusted clipboard text. */
    if (strncmp(command, "play ", 5) != 0 &&
        !names_match_ignoring_case(command, "help") &&
        !names_match_ignoring_case(command, "games") &&
        !names_match_ignoring_case(command, "games report") &&
        !names_match_ignoring_case(command, "games rescan")) return;
    if (g_catalogScanActive && strncmp(command, "play ", 5) == 0) {
        strcpy(g_pendingClipboardCommand, command);
        return;
    }
    if (names_match_ignoring_case(command, "games report"))
        trace_checkpoint("report: request arrived via clipboard");
    strcpy(mutableCommand, command);
    execute_terminal_command(mutableCommand);
    /* This acknowledges handling, not guaranteed success of the launched
     * game's own initialization. Never exit the Terminal on game launch. */
    write_cinter_clipboard_reply("ack", command);
}

static void poll_cinter_clipboard(void)
{
    char text[192];
    char prefix[24];
    const char *os = cinter_clipboard_os();
    char *command;
    UInt32 now = TickCount();
    size_t i;
    if (!os || !g_terminalHasDrawn ||
        (SInt32)(now - g_nextClipboardPollTick) < 0) return;
    g_nextClipboardPollTick = now + CINTER_CLIPBOARD_POLL_TICKS;
    if (!read_cinter_clipboard_message(text, sizeof(text))) return;
    snprintf(prefix, sizeof(prefix), "cinter:%s:", os);
    if (strncmp(text, prefix, strlen(prefix)) != 0) return;
    command = text + strlen(prefix);
    if (strncmp(command, "hello ", 6) == 0) {
        const char *token = command + 6;
        size_t tokenLength = strlen(token);
        if (tokenLength < 8 || tokenLength > 32) {
            trace_checkpoint("clipboard: HELLO token length rejected");
            return;
        }
        for (i = 0; i < tokenLength; ++i)
            if (!isxdigit((unsigned char)token[i])) {
                trace_checkpoint("clipboard: HELLO token contents rejected");
                return;
            }
        /* A failed PutScrap must not permanently consume HELLO. Retry on
         * the next poll until READY actually reaches the guest scrap. */
        if (write_cinter_clipboard_reply("ready", token)) {
            if (strcmp(text, g_lastClipboardMessage) != 0)
                trace_checkpoint("clipboard: READY written to guest Scrap Manager");
            strcpy(g_lastClipboardMessage, text);
        } else {
            trace_checkpoint("clipboard: READY write failed; will retry");
        }
    } else if (strcmp(text, g_lastClipboardMessage) != 0 &&
               (strncmp(command, "play ", 5) == 0 ||
                names_match_ignoring_case(command, "games") ||
                names_match_ignoring_case(command, "games report") ||
                names_match_ignoring_case(command, "games rescan") ||
                names_match_ignoring_case(command, "help"))) {
        /* Commands must not relaunch games on each subsequent poll. */
        strcpy(g_lastClipboardMessage, text);
        dispatch_cinter_clipboard_command(command);
    }
}

static void handle_terminal_key(EventRecord *keyboardEvent)
{
    unsigned char typedCharacter =
        (unsigned char)(keyboardEvent->message & charCodeMask);
    size_t inputLength = strlen(g_terminalInput);

    if (keyboardEvent->modifiers & cmdKey) {
        if (typedCharacter == 'q' || typedCharacter == 'Q') {
            g_applicationIsRunning = false;
        } else if (typedCharacter == 'w' || typedCharacter == 'W') {
            /* The Terminal is our primary window, so Close exits. */
            g_applicationIsRunning = false;
        } else if (typedCharacter == 'o' || typedCharacter == 'O') {
            trace_checkpoint("report: opened by terminal Command-O");
            open_games_report_window();
        } else if (typedCharacter == 'l' || typedCharacter == 'L') {
            open_games_launcher_window();
        } else if (typedCharacter == 'c' || typedCharacter == 'C') {
            copy_game_list_to_mac_clipboard();
        } else if (typedCharacter == 'v' || typedCharacter == 'V') {
            paste_mac_clipboard_into_terminal();
        } else if (typedCharacter == 'g' || typedCharacter == 'G') {
            launch_game_from_config_file();
        }
        return;
    }

    /* Page Up / Page Down scroll terminal history, not the input prompt. */
    if (typedCharacter == 0x0b || typedCharacter == 0x0c) {
        short rowOffset = typedCharacter == 0x0b
            ? (short)-terminal_visible_line_count() : terminal_visible_line_count();
        scroll_terminal_history(rowOffset);
        return;
    }

    if (typedCharacter == 0x0d || typedCharacter == 0x03) {
        char enteredCommandLine[TERMINAL_OUTPUT_LINE_SIZE];
        snprintf(enteredCommandLine, sizeof(enteredCommandLine),
                 "CinterForge Terminal> %.85s", g_terminalInput);
        append_terminal_history_line(enteredCommandLine);
        execute_terminal_command(g_terminalInput);
        g_terminalInput[0] = 0;
    } else if (typedCharacter == 0x08 || typedCharacter == 0x7f) {
        if (inputLength) g_terminalInput[inputLength - 1] = 0;
    } else if (typedCharacter >= 32 && typedCharacter <= 126 &&
               inputLength < sizeof(g_terminalInput) - 1) {
        g_terminalInput[inputLength] = typedCharacter;
        g_terminalInput[inputLength + 1] = 0;
    }

    invalidate_terminal_prompt();
}

static void resize_classic_window(WindowPtr window, Point mousePosition)
{
    Rect allowedSizes;
    long newSize;
    short newWidth;
    short newHeight;

    if (window == g_gamesReportWindow) {
        SetRect(&allowedSizes, 365, 315, 1100, 850);
    } else if (window == g_gamesLauncherWindow) {
        SetRect(&allowedSizes, 400, 350, 1100, 850);
    } else {
        SetRect(&allowedSizes, 360, 250, 1100, 850);
    }

    newSize = GrowWindow(window, mousePosition, &allowedSizes);
    if (!newSize) return;

    newWidth = LoWord(newSize);
    newHeight = HiWord(newSize);
    SizeWindow(window, newWidth, newHeight, true);

    if (window == g_gamesReportWindow) {
        g_gamesReportWidth = newWidth;
        g_gamesReportHeight = newHeight;
        position_games_report_scrollbar();
    } else if (window == g_terminalWindow) {
        g_terminalWindowWidth = newWidth;
        g_terminalWindowHeight = newHeight;
        position_terminal_scrollbar();
    }

    if (window == g_gamesReportWindow) invalidate_games_report_window();
    else if (window == g_gamesLauncherWindow) invalidate_games_launcher_window();
    else invalidate_terminal_window();
}

/* A menu selection is a classic Menu Manager pair: menu ID and item number. */
static void handle_menu_selection(Point mousePosition)
{
    long menuSelection = MenuSelect(mousePosition);
    short selectedMenuId = HiWord(menuSelection);
    short selectedItem = LoWord(menuSelection);

    if (selectedMenuId == FILE_MENU_ID && selectedItem == 1) {
        if (FrontWindow() == g_gamesReportWindow) {
            HideWindow(g_gamesReportWindow);
            SelectWindow(g_terminalWindow);
        } else if (FrontWindow() == g_gamesLauncherWindow) {
            HideWindow(g_gamesLauncherWindow);
            SelectWindow(g_terminalWindow);
        } else g_applicationIsRunning = false;
    } else if (selectedMenuId == FILE_MENU_ID && selectedItem == 2) {
        g_applicationIsRunning = false;
    } else if (selectedMenuId == EDIT_MENU_ID) {
        if (selectedItem == 1) copy_game_list_to_mac_clipboard();
        else if (selectedItem == 2 && FrontWindow() == g_terminalWindow)
            paste_mac_clipboard_into_terminal();
        else if (selectedItem == 3) export_game_list_to_host();
    } else if (selectedMenuId == GAMES_MENU_ID) {
        switch (selectedItem) {
            case 1: trace_checkpoint("report: opened by Games menu");
                    open_games_report_window(); break;
            case 2: open_games_launcher_window(); break;
            case 3: start_infinite_hd_catalog_scan(); break;
            case 4: launch_game_from_config_file(); break;
            default: break;
        }
    }

    HiliteMenu(0);
}

/* Native controls own their scroll thumb, arrows, and page areas. */
static Boolean handle_window_scrollbar_click(WindowPtr window, Point location)
{
    ControlHandle selectedControl = NULL;
    ControlHandle expectedControl = (window == g_gamesReportWindow)
        ? g_gamesReportScrollBar : g_terminalScrollBar;
    short part, result;
    short pageSize;
    if (!expectedControl) return false;
    part = FindControl(location, window, &selectedControl);
    if (!part || selectedControl != expectedControl) return false;
    result = TrackControl(selectedControl, location, NULL);
    pageSize = (window == g_gamesReportWindow)
        ? visible_game_card_row_count() : terminal_visible_line_count();
    if (result == inThumb) {
        if (window == g_gamesReportWindow) {
            g_reportFirstRow = GetControlValue(selectedControl);
            update_games_report_scrollbar();
            invalidate_games_report_window();
        } else {
            g_terminalFirstVisibleLine = GetControlValue(selectedControl);
            update_terminal_scrollbar();
            invalidate_terminal_window();
        }
    } else if (result) {
        short delta = (result == inUpButton) ? -1 :
                      (result == inDownButton) ? 1 :
                      (result == inPageUp) ? (short)-pageSize :
                      (result == inPageDown) ? pageSize : 0;
        if (window == g_gamesReportWindow) scroll_games_report_by_rows(delta);
        else scroll_terminal_history(delta);
    }
    return true;
}

/* Convert global mouse coordinates before hit-testing cards in the report. */
static void handle_window_content_click(WindowPtr window, EventRecord *mouseEvent)
{
    Point localMousePosition;
    if (window != FrontWindow()) SelectWindow(window);
    localMousePosition = mouseEvent->where;
    SetPort(window);
    GlobalToLocal(&localMousePosition);
    if (handle_window_scrollbar_click(window, localMousePosition)) return;
    if (window == g_gamesReportWindow) handle_game_card_click(localMousePosition, mouseEvent->when);
    else if (window == g_gamesLauncherWindow) handle_games_launcher_click(localMousePosition, mouseEvent->when);
}

static void handle_mouse_down(EventRecord *mouseEvent)
{
    WindowPtr clickedWindow = NULL;
    short windowPart = FindWindow(mouseEvent->where, &clickedWindow);
    char mouseMessage[95];
    snprintf(mouseMessage, sizeof(mouseMessage), "mouse: global x=%d y=%d part=%d",
             (int)mouseEvent->where.h, (int)mouseEvent->where.v, (int)windowPart);
    trace_checkpoint(mouseMessage);

    switch (windowPart) {
        case inMenuBar:
            handle_menu_selection(mouseEvent->where);
            break;
        case inDrag:
            if (clickedWindow) {
                DragWindow(clickedWindow, mouseEvent->where, &qd.screenBits.bounds);
            }
            break;
        case inGrow:
            if (clickedWindow) resize_classic_window(clickedWindow, mouseEvent->where);
            break;
        case inGoAway:
            if (clickedWindow && TrackGoAway(clickedWindow, mouseEvent->where)) {
                if (clickedWindow == g_gamesReportWindow) {
                    HideWindow(g_gamesReportWindow);
                } else if (clickedWindow == g_gamesLauncherWindow) {
                    HideWindow(g_gamesLauncherWindow);
                } else if (clickedWindow == g_terminalWindow) {
                    g_applicationIsRunning = false;
                }
            }
            break;
        case inContent:
            if (clickedWindow) handle_window_content_click(clickedWindow, mouseEvent);
            break;
        default:
            break;
    }
}

/* Redraw only the window that Mac OS reports as needing an update. */
static void handle_window_update(WindowPtr window)
{
    if (window != g_gamesReportWindow && window != g_gamesLauncherWindow &&
        window != g_terminalWindow) return;

    if (window == g_terminalWindow && !g_traceFirstUpdate)
        trace_checkpoint("event: first terminal redraw begin");
    BeginUpdate(window);
    if (window == g_gamesReportWindow) {
        draw_games_report_window();
    } else if (window == g_gamesLauncherWindow) {
        draw_games_launcher_window();
    } else {
        draw_terminal_window();
    }
    EndUpdate(window);
    if (window == g_terminalWindow) {
        g_terminalHasDrawn = true;
        if (!g_traceFirstUpdate) {
            g_traceFirstUpdate = true;
            trace_checkpoint("event: first terminal redraw complete");
        }
    }
}

/* The event loop remains responsive while catalog scanning progresses. */
static void handle_classic_mac_event(EventRecord *event)
{
    switch (event->what) {
        case mouseDown:
            handle_mouse_down(event);
            break;
        case keyDown:
        case autoKey:
            if (FrontWindow() == g_gamesReportWindow) {
                handle_games_report_key(event);
            } else if (FrontWindow() == g_gamesLauncherWindow) {
                handle_games_launcher_key(event);
            } else {
                handle_terminal_key(event);
            }
            break;
        case updateEvt:
            handle_window_update((WindowPtr)event->message);
            break;
        case activateEvt:
            invalidate_games_report_window();
            invalidate_games_launcher_window();
            invalidate_terminal_window();
            break;
        default:
            break;
    }
}

/* -------------------------------------------------------------------------
 * Startup and shutdown
 * ------------------------------------------------------------------------- */

static void initialize_terminal_interface(void)
{
    Rect screenBounds, windowBounds;
    MenuHandle menu;
    screenBounds = qd.screenBits.bounds;
    g_terminalWindowWidth = (short)(screenBounds.right - screenBounds.left - 30);
    if (g_terminalWindowWidth > 655) g_terminalWindowWidth = 655;
    if (g_terminalWindowWidth < 360) g_terminalWindowWidth = 360;
    g_terminalWindowHeight = (short)(screenBounds.bottom - screenBounds.top - 70);
    if (g_terminalWindowHeight > 475) g_terminalWindowHeight = 475;
    if (g_terminalWindowHeight < 250) g_terminalWindowHeight = 250;
    SetRect(&windowBounds, 10, 43, 10 + g_terminalWindowWidth, 43 + g_terminalWindowHeight);
    trace_heap_checkpoint("before NewWindow");
    trace_checkpoint("ui: before NewWindow");
    g_terminalWindow = NewWindow(NULL, &windowBounds, "\pCinterForge Terminal", true,
                           documentProc, (WindowPtr)-1, true, 0);
    if (!g_terminalWindow) { trace_checkpoint("ui: NewWindow returned NULL"); ExitToShell(); }
    trace_checkpoint("ui: NewWindow completed");
    menu = NewMenu(FILE_MENU_ID, "\pFile");
    AppendMenu(menu, "\pClose/W;Quit/Q");
    InsertMenu(menu, 0);
    menu = NewMenu(EDIT_MENU_ID, "\pEdit");
    AppendMenu(menu, "\pCopy Game List/C;Paste/V;Export Game List...");
    InsertMenu(menu, 0);
    menu = NewMenu(GAMES_MENU_ID, "\pGames");
    AppendMenu(menu, "\pGames Report/O;Games Launcher/L;Rescan/R;Run Game.txt/G");
    InsertMenu(menu, 0);
    trace_checkpoint("ui: before DrawMenuBar");
    DrawMenuBar();
    trace_checkpoint("ui: DrawMenuBar completed");
    {
        Rect scrollbarBounds = g_terminalWindow->portRect;
        SetRect(&scrollbarBounds,
                (short)(scrollbarBounds.right - CLASSIC_SCROLLBAR_WIDTH), 1,
                scrollbarBounds.right,
                (short)(scrollbarBounds.bottom - TERMINAL_PROMPT_AREA_HEIGHT));
        trace_checkpoint("ui: before NewControl scrollbar");
        g_terminalScrollBar = NewControl(g_terminalWindow, &scrollbarBounds,
                                        "\p", true, 0, 0, 0, scrollBarProc, 0);
        trace_checkpoint("ui: NewControl completed");
        position_terminal_scrollbar();
        trace_checkpoint("ui: scrollbar positioned");
    }
    trace_checkpoint("ui: before ShowWindow");
    ShowWindow(g_terminalWindow);
    SelectWindow(g_terminalWindow);
    trace_checkpoint("ui: window selected");
    trace_heap_checkpoint("after window selected");
    /* Exact established opening text; do not add an alternate greeting. */
    trace_heap_checkpoint("before welcome append");
    trace_checkpoint("ui: before first welcome append");
    append_terminal_history_line("Welcome to CinterForge. SHALL WE PLAY A GAME?");
    trace_checkpoint("ui: first welcome append returned");
    trace_heap_checkpoint("after welcome append");
    append_terminal_history_line("Commands:");
    append_terminal_history_line("  games                         List of playable games");
    append_terminal_history_line("  play <title>                  Launch a game (play is optional)");
    trace_checkpoint("ui: terminal interface complete");
}

int main(void)
{
    EventRecord eventRecord;
    Boolean tracedFirstEvent = false;
    Boolean tracedFirstBatch = false;
    Boolean tracedFirstPoll = false;
    long scannedMilestone = 0;
    trace_checkpoint("main: entered (before InitGraf)");
    InitGraf(&qd.thePort);
    trace_checkpoint("main: InitGraf done");
    InitFonts();
    trace_checkpoint("main: InitFonts done");
    InitWindows();
    trace_checkpoint("main: InitWindows done");
    InitMenus();
    trace_checkpoint("main: InitMenus done");
    TEInit();
    trace_checkpoint("main: TEInit done");
    InitDialogs(NULL);
    trace_checkpoint("main: InitDialogs done");
    InitCursor();
    trace_checkpoint("main: InitCursor done");
    MaxApplZone();
    trace_checkpoint("main: MaxApplZone done");
    trace_heap_checkpoint("after MaxApplZone");
    /* Determine the clipboard address independently of Infinite HD scanning.
     * The HELLO handshake must not depend on the game catalog. */
    g_runningGameOS = running_game_os();
    trace_checkpoint(g_runningGameOS == GAME_OS_SYSTEM_71
        ? "clipboard: guest address 7.1" :
        g_runningGameOS == GAME_OS_MAC_76
        ? "clipboard: guest address 7.6" :
          "clipboard: no supported guest OS address");
    trace_checkpoint("main: before initialize_terminal_interface");
    initialize_terminal_interface();
    trace_checkpoint("main: initialize_terminal_interface done");
    g_nextClipboardPollTick = TickCount() + 120U;
    trace_checkpoint("main: event loop started");
    while (g_applicationIsRunning) {
        if (WaitNextEvent(everyEvent, &eventRecord, 2, NULL)) {
            if (!tracedFirstEvent) {
                trace_checkpoint("event: first event received");
                tracedFirstEvent = true;
            }
            handle_classic_mac_event(&eventRecord);
        }
        if (g_startupTasksPending && g_terminalHasDrawn) {
            g_startupTasksPending = false;
            /* Machine cards start Terminal only. The host speaks its existing
             * greeting only after a matching clipboard READY arrives. */
            trace_checkpoint("catalog: before start_infinite_hd_catalog_scan");
            start_infinite_hd_catalog_scan();
            trace_checkpoint("catalog: scan setup returned");
        }
        if (g_catalogScanActive) {
            if (!tracedFirstBatch) trace_checkpoint("catalog: first batch begin");
            scan_next_catalog_batch();
            if (!tracedFirstBatch) {
                trace_checkpoint("catalog: first batch complete");
                tracedFirstBatch = true;
            }
            if (g_catalogEntriesScanned / 500L > scannedMilestone) {
                char progress[96];
                scannedMilestone = g_catalogEntriesScanned / 500L;
                snprintf(progress, sizeof(progress), "catalog: scanned %ld entries",
                         g_catalogEntriesScanned);
                trace_checkpoint(progress);
            }
            if (!g_catalogScanActive) trace_checkpoint("catalog: scan complete");
        }
        /* The optional Game.txt is explicit-only. Clipboard commands use the
         * same discovered catalog and keep Terminal running in the background. */
        if (!g_catalogScanActive && g_requestedFinderName[0]) {
            trace_checkpoint("launcher: requested game begin");
            auto_launch_requested_game_if_ready();
            trace_checkpoint("launcher: requested game returned");
        }
        if (!g_catalogScanActive && g_pendingClipboardCommand[0]) {
            char pending[sizeof(g_pendingClipboardCommand)];
            strcpy(pending, g_pendingClipboardCommand);
            g_pendingClipboardCommand[0] = 0;
            trace_checkpoint("clipboard: pending command begin");
            dispatch_cinter_clipboard_command(pending);
            trace_checkpoint("clipboard: pending command returned");
        }
        /* HELLO/READY must not wait for a potentially lengthy Infinite HD
         * scan. The scan has already initialized the OS address by this point;
         * launch commands received during discovery are deferred above.
         * A prompt READY also prevents unnecessary host Finder retries. */
        if (g_terminalHasDrawn && !g_startupTasksPending) {
            if (!tracedFirstPoll) trace_checkpoint("clipboard: first poll begin");
            poll_cinter_clipboard();
            if (!tracedFirstPoll) {
                trace_checkpoint("clipboard: first poll complete");
                tracedFirstPoll = true;
            }
        }
    }
    trace_checkpoint("main: normal shutdown");
    if (g_cardPixelBuffer) DisposePtr(g_cardPixelBuffer);
    if (g_logoPixelBuffer) DisposePtr(g_logoPixelBuffer);
    return 0;
}

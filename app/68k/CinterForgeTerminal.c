/*
 * CinterForgeTerminal for classic Macintosh (Motorola 68000 / System 7+).
 *
 * This is a real Macintosh application, not the modern Terminal WebView.
 * It has two ordinary Mac OS windows:
 *   - Terminal: the classic Mac command window. Its existing text and commands
 *               must stay aligned with the other CinterForge terminals.
 *   - Games Report: the separate, scrollable Game Card browser.
 *
 * Games are discovered from actual APPL files on the mounted Infinite HD.
 * Their Finder names and native icon resources are read from those files.
 * On startup, an optional plain-text Game.txt on CinterForge HD requests an
 * automatic launch. An empty Game.txt opens the regular Terminal interface.
 *
 * No AppleScript, networking, modern OS APIs, or host-side game catalog is
 * required at runtime. Setup/Build create this app using Retro68.
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

#define MAX_DISCOVERED_APPLICATIONS 1200
#define MAX_CATALOG_FOLDERS 1200
#define MAX_CATALOG_ENTRIES 50000
#define CARD_WIDTH 145
#define CARD_HEIGHT 225
#define CARD_SPACING 12
#define REPORT_HEADER_HEIGHT 98
#define REPORT_FOOTER_HEIGHT 23
#define REPORT_SIDE_MARGIN 14
#define MAX_TERMINAL_HISTORY_LINES 1300
#define TERMINAL_OUTPUT_LINE_SIZE 110
#define TERMINAL_INPUT_SIZE 128

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
#define APPLICATION_ICON_RESOURCE_ID (-16455)
#define TERMINAL_MONACO_FONT_ID 4

/* Keep the event loop responsive while enumerating large HFS volumes. */
#define CATALOG_ENTRIES_PER_EVENT_TICK 70
#define CARD_OPEN_BUTTON_OFFSET_Y 197

/* Finder location and display name for one executable found on Infinite HD. */
typedef struct {
    FSSpec spec;
    Str63 finderName;
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
static long g_catalogEntriesScanned = 0;
static Boolean g_catalogScanActive = false;
static Boolean g_catalogScanTruncated = false;
static Boolean g_autoLaunchAttempted = false;
/* Game.txt is read at startup or by the explicit Run Game.txt menu command. */
static char g_requestedFinderName[64] = "";

/* Games Report search, selection, and scrolling state. */
static char g_applicationNameFilter[64] = "";
static char g_gamesReportStatus[128] = "Scanning Infinite HD...";
static short g_visibleApplicationIndices[MAX_DISCOVERED_APPLICATIONS];
static short g_visibleApplicationCount = 0;
static short g_reportColumnCount = 2;
static short g_reportFirstRow = 0;
static short g_selectedApplicationIndex = -1;
static WindowPtr g_gamesReportWindow = NULL; /* Games Report, created on demand. */
/* The Terminal window and its bounded, scrollable text history. */
static WindowPtr g_terminalWindow = NULL;
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
static Boolean g_applicationIsRunning = true;
static Boolean g_cardArtworkReady = false;
static short g_gamesReportWidth = 620;
static short g_gamesReportHeight = 470;
static Rect g_searchFieldBounds;

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

static void set_games_report_status(const char *message)
{
    strncpy(g_gamesReportStatus, message, sizeof(g_gamesReportStatus)-1);
    g_gamesReportStatus[sizeof(g_gamesReportStatus)-1] = 0;
}

/* -------------------------------------------------------------------------
 * Terminal: plain classic Macintosh window, output history and command line
 * ------------------------------------------------------------------------- */
static short terminal_visible_line_count(void)
{
    short rows = (short)((g_terminalWindowHeight - 75) / 15);
    return rows > 2 ? rows : 2;
}

static void invalidate_terminal_window(void)
{
    if (g_terminalWindow) InvalRect(&g_terminalWindow->portRect);
}

/* The oldest line rolls off when the fixed history is full. */
static void append_terminal_history_line(const char *value)
{
    if (g_terminalHistoryCount == MAX_TERMINAL_HISTORY_LINES) {
        memmove(g_terminalHistoryLines,
                g_terminalHistoryLines + 1,
                sizeof(g_terminalHistoryLines[0]) * (MAX_TERMINAL_HISTORY_LINES - 1));
        --g_terminalHistoryCount;
    }
    strncpy(g_terminalHistoryLines[g_terminalHistoryCount], value, TERMINAL_OUTPUT_LINE_SIZE - 1);
    g_terminalHistoryLines[g_terminalHistoryCount][TERMINAL_OUTPUT_LINE_SIZE - 1] = 0;
    ++g_terminalHistoryCount;
    g_terminalFirstVisibleLine = g_terminalHistoryCount > terminal_visible_line_count()
        ? (short)(g_terminalHistoryCount - terminal_visible_line_count()) : 0;
    invalidate_terminal_window();
}

static void scroll_terminal_history(short rows)
{
    short lastVisibleStart = g_terminalHistoryCount > terminal_visible_line_count()
        ? (short)(g_terminalHistoryCount - terminal_visible_line_count()) : 0;
    short newFirstLine = (short)(g_terminalFirstVisibleLine + rows);

    if (newFirstLine < 0) newFirstLine = 0;
    if (newFirstLine > lastVisibleStart) newFirstLine = lastVisibleStart;
    g_terminalFirstVisibleLine = newFirstLine;
    invalidate_terminal_window();
}

static void draw_terminal_window(void)
{
    short lineIndex;
    short visibleLineCount = terminal_visible_line_count();
    Rect windowBounds, divider;
    if (!g_terminalWindow) return;
    SetPort(g_terminalWindow);
    windowBounds = g_terminalWindow->portRect;
    EraseRect(&windowBounds);
    TextFont(TERMINAL_MONACO_FONT_ID);
    TextSize(10);

    for (lineIndex = 0;
         lineIndex < visibleLineCount &&
         g_terminalFirstVisibleLine + lineIndex < g_terminalHistoryCount;
         ++lineIndex) {
        Str255 pascalLine;
        copy_c_string_to_pascal(
            g_terminalHistoryLines[g_terminalFirstVisibleLine + lineIndex], pascalLine);
        MoveTo(11, (short)(22 + lineIndex * 15));
        DrawString(pascalLine);
    }
    SetRect(&divider, 9, g_terminalWindowHeight - 41, g_terminalWindowWidth - 9, g_terminalWindowHeight - 40);
    PaintRect(&divider);
    {
        Str255 prompt;
        char content[TERMINAL_INPUT_SIZE + 24];
        snprintf(content, sizeof(content), "CinterForgeTerminal> %s_", g_terminalInput);
        copy_c_string_to_pascal(content, prompt);
        TextFont(TERMINAL_MONACO_FONT_ID);
        TextSize(10);
        MoveTo(10, g_terminalWindowHeight - 17);
        DrawString(prompt);
    }
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
    if (g_gamesReportWindow) InvalRect(&g_gamesReportWindow->portRect);
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
    g_reportFirstRow = 0;
    g_catalogFolderDepth = 0;
    g_catalogEntriesScanned = 0;
    g_catalogScanTruncated = false;
    g_infiniteHdVolumeRef = find_infinite_hd_volume();
    if (!g_infiniteHdVolumeRef) {
        g_catalogScanActive = false;
        set_games_report_status("Infinite HD not mounted. Enable Infinite HD for this machine.");
        append_terminal_history_line("Infinite HD is not mounted on this Macintosh.");
    } else {
        g_catalogScanActive = true;
        g_catalogFolderStack[0].directoryId = HFS_ROOT_DIRECTORY_ID;
        g_catalogFolderStack[0].nextEntryIndex = 1;
        g_catalogFolderDepth = 1;
        set_games_report_status("Discovering applications on Infinite HD...");
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
                   g_discoveredApplicationCount < MAX_DISCOVERED_APPLICATIONS) {
            DiscoveredApplication *application =
                &g_discoveredApplications[g_discoveredApplicationCount];
            if (FSMakeFSSpec(g_infiniteHdVolumeRef, currentFolder->directoryId,
                             entryName, &application->spec) == noErr) {
                memcpy(application->finderName, entryName, (size_t)entryName[0] + 1);
                ++g_discoveredApplicationCount;
                /* Avoid rebuilding/sorting every single catalog record. */
                if (!(g_discoveredApplicationCount % 32)) rebuild_visible_application_list();
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
        sprintf(message, "%d applications found on Infinite HD%s",
                (int)g_discoveredApplicationCount, g_catalogScanTruncated ? " (limit reached)" : "");
        set_games_report_status(message);
        append_terminal_history_line(message);
        if (g_listGamesWhenScanCompletes) {
            short i;
            for (i = 0; i < g_visibleApplicationCount; ++i) {
                char listed[TERMINAL_OUTPUT_LINE_SIZE];
                char finder[64];
                copy_pascal_string_to_c(
                    g_discoveredApplications[g_visibleApplicationIndices[i]].finderName,
                    finder, sizeof(finder));
                snprintf(listed, sizeof(listed), "%3d  %s", (int)(i + 1), finder);
                append_terminal_history_line(listed);
            }
            g_listGamesWhenScanCompletes = false;
        }
    }
}

/* -------------------------------------------------------------------------
 * CinterForge HD: read the optional, user-editable automatic launch request
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
        if (names_match_ignoring_case(volumeNameText, "CinterForge HD")) return volumeInfo.volumeParam.ioVRefNum;
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
    launchError = LaunchApplication(&launchParameters);
    if (launchError == noErr) {
        g_applicationIsRunning = false;
        return noErr;
    }
    copy_pascal_string_to_c(g_discoveredApplications[applicationIndex].finderName, applicationName, sizeof(applicationName));
    sprintf(message, "Could not launch %.60s (Mac OS error %d)", applicationName, (int)launchError);
    set_games_report_status(message);
    if (g_gamesReportWindow) InvalRect(&g_gamesReportWindow->portRect);
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
    MoveTo(x, y);
    DrawString(pascalText);
}

static void draw_centered_text(const char *text, short x, short y, short width, short size)
{
    Str255 pascalText;
    short textWidth;
    copy_c_string_to_pascal(text, pascalText);
    TextFont(0);
    TextSize(size);
    textWidth = StringWidth(pascalText);
    MoveTo((short)(x + (width - textWidth)/2), y);
    DrawString(pascalText);
}

/*
 * Render directly from a classic 16-bit QuickDraw PixMap. Retro68's bundled
 * Multiversal Interfaces intentionally do not provide QDOffscreen.h; the
 * QuickDraw BitMap/PixMap layout and CopyBits are supported classic APIs.
 * The RGB555 asset is authored once by Build, then copied into one persistent
 * on-heap pixel buffer (not decoded per visible card or screen refresh).
 */
static void load_game_card_artwork(void)
{
    FSSpec artworkFileSpec;
    short artworkFileRef;
    long headerSize = 4;
    unsigned char header[4];
    short imageWidth, imageHeight;
    OSErr readError;
    Rect bounds;
    short volumeRef = find_cinterforge_hd_volume();
    CInfoPBRec artworkDirectory;
    Str255 artworkDirectoryName = "\pSupport Files";
    Ptr pixelBuffer = NULL;
    long byteCount;
    if (g_cardArtworkReady) return;
    if (!volumeRef) return;
    memset(&artworkDirectory, 0, sizeof(artworkDirectory));
    artworkDirectory.dirInfo.ioNamePtr = artworkDirectoryName;
    artworkDirectory.dirInfo.ioVRefNum = volumeRef;
    artworkDirectory.dirInfo.ioFDirIndex = 0;
    artworkDirectory.dirInfo.ioDrDirID = HFS_ROOT_DIRECTORY_ID;
    if (PBGetCatInfoSync(&artworkDirectory) != noErr || !(artworkDirectory.hFileInfo.ioFlAttrib & ioDirMask) ||
        FSMakeFSSpec(volumeRef, artworkDirectory.dirInfo.ioDrDirID, "\pGameCard.rgb", &artworkFileSpec) != noErr ||
        FSpOpenDF(&artworkFileSpec, fsRdPerm, &artworkFileRef) != noErr) return;
    readError = FSRead(artworkFileRef, &headerSize, header);
    if (readError != noErr || headerSize != 4) { FSClose(artworkFileRef); return; }
    imageWidth = (short)((header[0] << 8) | header[1]);
    imageHeight = (short)((header[2] << 8) | header[3]);
    if (imageWidth != CARD_WIDTH || imageHeight != CARD_HEIGHT) { FSClose(artworkFileRef); return; }
    byteCount = (long)imageWidth * imageHeight * 2;
    pixelBuffer = NewPtr(byteCount);
    if (!pixelBuffer) { FSClose(artworkFileRef); return; }
    {
        long bytes = byteCount;
        readError = FSRead(artworkFileRef, &bytes, pixelBuffer);
        FSClose(artworkFileRef);
        if (readError != noErr || bytes != byteCount) { DisposePtr(pixelBuffer); return; }
    }

    SetRect(&bounds, 0, 0, imageWidth, imageHeight);
    memset(&g_cardPixelMap, 0, sizeof(g_cardPixelMap));
    g_cardPixelMap.baseAddr = pixelBuffer;
    g_cardPixelMap.rowBytes = (short)(0x8000U | (unsigned short)(imageWidth * 2));
    g_cardPixelMap.bounds = bounds;
    g_cardPixelMap.hRes = 72L << 16;
    g_cardPixelMap.vRes = 72L << 16;
    g_cardPixelMap.pixelType = 16;  /* Classic QuickDraw RGBDirect pixel data. */
    g_cardPixelMap.pixelSize = 16;
    g_cardPixelMap.cmpCount = 3;
    g_cardPixelMap.cmpSize = 5;
    g_cardPixelBuffer = pixelBuffer;
    g_cardArtworkReady = true;
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

static void draw_application_finder_icon(short applicationIndex, short x, short y)
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
    /* Application icons are traditionally in -16455, but some use IDs from
       a different BNDL. Prefer an actually present icon resource. */
    iconResource = Get1Resource('icl8', resourceID);
    if (!iconResource) iconResource = Get1Resource('ICN#', resourceID);
    if (!iconResource) iconResource = Get1IndResource('icl8', 1);
    if (!iconResource) iconResource = Get1IndResource('ICN#', 1);
    if (iconResource) GetResInfo(iconResource, &resourceID, &resourceType, resourceName);
    SetRect(&iconBounds, x, y, x + 42, y + 42);
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

static void draw_application_card(short visibleApplicationSlot, short cardLeft, short cardTop)
{
    Rect cardBounds;
    short applicationIndex = g_visibleApplicationIndices[visibleApplicationSlot];
    char finderName[64], displayName[25];
    SetRect(&cardBounds, cardLeft, cardTop, cardLeft + CARD_WIDTH, cardTop + CARD_HEIGHT);
    draw_game_card_background(&cardBounds);
    draw_application_finder_icon(applicationIndex, (short)(cardLeft + 52), (short)(cardTop + 65));
    copy_pascal_string_to_c(g_discoveredApplications[applicationIndex].finderName, finderName, sizeof(finderName));
    strncpy(displayName, finderName, 20);
    displayName[20] = 0;
    if (strlen(finderName) > 20) strcpy(displayName + 17, "...");
    draw_centered_text(displayName, cardLeft + 8, cardTop + 169, CARD_WIDTH - 16, 11);
    draw_centered_text("Infinite HD", cardLeft + 7, cardTop + 191, CARD_WIDTH - 14, 10);
    draw_centered_text("OPEN", cardLeft + 7, cardTop + 214, CARD_WIDTH - 14, 11);
    if (applicationIndex == g_selectedApplicationIndex) {
        PenSize(2, 2);
        FrameRect(&cardBounds);
        PenNormal();
    }
}

static short visible_game_card_row_count(void)
{
    short availableHeight = g_gamesReportHeight - REPORT_HEADER_HEIGHT - REPORT_FOOTER_HEIGHT - 16;
    if (availableHeight < CARD_HEIGHT) return 1;
    return (short)(availableHeight / (CARD_HEIGHT + CARD_SPACING));
}

static void draw_games_report_window(void)
{
    short visibleSlot, columnCount, visibleRows, firstVisibleIndex, visibleSlots;
    Rect windowBounds, headerBounds, footerBounds, headerDivider;
    RGBColor white = {0xffff, 0xffff, 0xffff};
    RGBColor black = {0, 0, 0};
    char countLabel[64];
    if (!g_gamesReportWindow) return;
    SetPort(g_gamesReportWindow);
    windowBounds = g_gamesReportWindow->portRect;
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
    draw_text("Applications on Infinite HD", 62, 64, 0, 10);
    /* The app's authored CinterForge icon is a native icon resource. */
    {
        Handle logoIconSuite;
        Rect logoRect;
        SetRect(&logoRect, 12, 9, 53, 50);
        if (GetIconSuite(&logoIconSuite, APPLICATION_ICON_RESOURCE_ID,
                         svAllAvailableData) == noErr) {
            PlotIconSuite(&logoRect, CLASSIC_ICON_ALIGNMENT_NONE, kTransformNone, logoIconSuite);
            DisposeIconSuite(logoIconSuite, true);
        }
    }
    SetRect(&g_searchFieldBounds, g_gamesReportWidth-230, 11, g_gamesReportWidth-18, 35);
    FrameRect(&g_searchFieldBounds);
    draw_text(g_applicationNameFilter[0] ? g_applicationNameFilter : "Type to filter apps...",
                (short)(g_searchFieldBounds.left+6), (short)(g_searchFieldBounds.top+17), 0, 11);
    sprintf(countLabel, "%d found%s", (int)g_discoveredApplicationCount, g_catalogScanActive ? " (scanning)" : "");
    draw_text(countLabel, g_gamesReportWidth-224, 53, 0, 10);
    columnCount = (short)((g_gamesReportWidth - REPORT_SIDE_MARGIN * 2 + CARD_SPACING) / (CARD_WIDTH + CARD_SPACING));
    if (columnCount < 1) columnCount = 1;
    g_reportColumnCount = columnCount;
    visibleRows = visible_game_card_row_count();
    firstVisibleIndex = (short)(g_reportFirstRow * columnCount);
    visibleSlots = (short)(visibleRows * columnCount);
    for (visibleSlot = 0;
         visibleSlot < visibleSlots && firstVisibleIndex + visibleSlot < g_visibleApplicationCount;
         ++visibleSlot) {
        short cardLeft = (short)(REPORT_SIDE_MARGIN + (visibleSlot % columnCount) * (CARD_WIDTH + CARD_SPACING));
        short cardTop = (short)(REPORT_HEADER_HEIGHT + 5 + (visibleSlot / columnCount) * (CARD_HEIGHT + CARD_SPACING));
        draw_application_card((short)(firstVisibleIndex+visibleSlot), cardLeft, cardTop);
    }
    if (!g_infiniteHdVolumeRef && !g_catalogScanActive)
        draw_text("Infinite HD is not mounted on this Macintosh.", 20, REPORT_HEADER_HEIGHT+38, 0, 12);
    else if (!g_visibleApplicationCount && !g_catalogScanActive)
        draw_text("No matching Macintosh applications.", 20, REPORT_HEADER_HEIGHT+38, 0, 12);
    FrameRect(&footerBounds);
    draw_text(g_gamesReportStatus, 11, g_gamesReportHeight-7, 0, 10);
    if (g_visibleApplicationCount > visibleSlots) {
        draw_text("Page Up / Down", (short)(g_gamesReportWidth-98),
                    (short)(REPORT_HEADER_HEIGHT+14), 0, 9);
    }
}

static void scroll_games_report_by_rows(short rowOffset)
{
    short visibleRows = visible_game_card_row_count();
    short totalRows = (short)((g_visibleApplicationCount + g_reportColumnCount-1) / g_reportColumnCount);
    short lastPageRow = totalRows - visibleRows;
    short newFirstRow = g_reportFirstRow + rowOffset;
    if (lastPageRow < 0) lastPageRow = 0;
    if (newFirstRow < 0) newFirstRow = 0;
    if (newFirstRow > lastPageRow) newFirstRow = lastPageRow;
    if (g_reportFirstRow != newFirstRow) {
        g_reportFirstRow = newFirstRow;
        InvalRect(&g_gamesReportWindow->portRect);
    }
}

static void handle_game_card_click(Point localMousePosition, UInt32 clickTime)
{
    static short lastClickedIndex = -1;
    static UInt32 lastClickTime = 0;
    short cardAreaX = localMousePosition.h - REPORT_SIDE_MARGIN;
    short cardAreaY = localMousePosition.v - REPORT_HEADER_HEIGHT - 5;
    short columnIndex, rowIndex, visibleSlot, applicationIndex;
    if (PtInRect(localMousePosition, &g_searchFieldBounds)) return;
    if (cardAreaX < 0 || cardAreaY < 0) return;
    columnIndex = cardAreaX / (CARD_WIDTH + CARD_SPACING);
    rowIndex = cardAreaY / (CARD_HEIGHT + CARD_SPACING);
    if (columnIndex >= g_reportColumnCount || columnIndex < 0 || rowIndex >= visible_game_card_row_count()) return;
    if (cardAreaX % (CARD_WIDTH + CARD_SPACING) >= CARD_WIDTH ||
        cardAreaY % (CARD_HEIGHT + CARD_SPACING) >= CARD_HEIGHT) return;
    visibleSlot = (short)((g_reportFirstRow + rowIndex) * g_reportColumnCount + columnIndex);
    if (visibleSlot < 0 || visibleSlot >= g_visibleApplicationCount) return;
    applicationIndex = g_visibleApplicationIndices[visibleSlot];
    if (g_selectedApplicationIndex == applicationIndex && lastClickedIndex == applicationIndex &&
        clickTime >= lastClickTime && clickTime - lastClickTime < mac_double_click_interval_in_ticks()) {
        launch_discovered_application(applicationIndex);
    } else if (cardAreaY % (CARD_HEIGHT + CARD_SPACING) >= CARD_OPEN_BUTTON_OFFSET_Y) {
        launch_discovered_application(applicationIndex); /* "OPEN" panel of the actual Game Card */
    } else {
        g_selectedApplicationIndex = applicationIndex;
        InvalRect(&g_gamesReportWindow->portRect);
    }
    lastClickedIndex = applicationIndex;
    lastClickTime = clickTime;
}

static void handle_games_report_key(EventRecord *keyboardEvent)
{
    unsigned char typedCharacter =
        (unsigned char)(keyboardEvent->message & charCodeMask);

    if (keyboardEvent->modifiers & cmdKey) {
        if (typedCharacter == 'q' || typedCharacter == 'Q') {
            g_applicationIsRunning = false;
        } else if (typedCharacter == 'r' || typedCharacter == 'R') {
            start_infinite_hd_catalog_scan();
        } else if (typedCharacter == 'o' || typedCharacter == 'O') {
            launch_discovered_application(g_selectedApplicationIndex);
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
    if (!g_gamesReportWindow) {
        screenBounds = qd.screenBits.bounds;
        g_gamesReportWidth = (short)(screenBounds.right - screenBounds.left - 25);
        if (g_gamesReportWidth > 790) g_gamesReportWidth = 790;
        if (g_gamesReportWidth < 365) g_gamesReportWidth = 365;
        g_gamesReportHeight = (short)(screenBounds.bottom - screenBounds.top - 70);
        if (g_gamesReportHeight > 565) g_gamesReportHeight = 565;
        if (g_gamesReportHeight < 315) g_gamesReportHeight = 315;
        SetRect(&windowBounds, 17, 50, 17 + g_gamesReportWidth, 50 + g_gamesReportHeight);
        g_gamesReportWindow = NewWindow(NULL, &windowBounds, "\pGames Report", false,
                             documentProc, (WindowPtr)-1, true, 0);
        if (!g_gamesReportWindow) { SysBeep(1); append_terminal_history_line("Could not open Games Report window."); return; }
        load_game_card_artwork();
    }
    ShowWindow(g_gamesReportWindow);
    SelectWindow(g_gamesReportWindow);
    InvalRect(&g_gamesReportWindow->portRect);
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
    snprintf(outputLine, sizeof(outputLine), "%d Macintosh applications:", (int)g_visibleApplicationCount);
    append_terminal_history_line(outputLine);
    for (visibleIndex = 0; visibleIndex < g_visibleApplicationCount; ++visibleIndex) {
        copy_pascal_string_to_c(
            g_discoveredApplications[g_visibleApplicationIndices[visibleIndex]].finderName,
            applicationName, sizeof(applicationName));
        snprintf(outputLine, sizeof(outputLine), "%3d  %s", (int)(visibleIndex+1), applicationName);
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
        if (strlen(finderName) == requestedNameLength) {
            char requestedFinderName[64];
            memcpy(requestedFinderName, requestedName, requestedNameLength);
            requestedFinderName[requestedNameLength] = 0;
            if (!names_match_ignoring_case(finderName, requestedFinderName)) continue;
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
        append_terminal_history_line("games             List applications on Infinite HD");
        append_terminal_history_line("games report      Open Games Report in another window");
        append_terminal_history_line("play \"name\"      Launch a game (play is optional)");
        append_terminal_history_line("\"name\"            Launch a game by Finder name");
    } else if (names_match_ignoring_case(commandText, "games")) {
        list_discovered_games_in_terminal();
    } else if (names_match_ignoring_case(commandText, "games report")) {
        open_games_report_window();
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

static void handle_terminal_key(EventRecord *keyboardEvent)
{
    unsigned char typedCharacter =
        (unsigned char)(keyboardEvent->message & charCodeMask);
    size_t inputLength = strlen(g_terminalInput);

    if (keyboardEvent->modifiers & cmdKey) {
        if (typedCharacter == 'q' || typedCharacter == 'Q') {
            g_applicationIsRunning = false;
        } else if (typedCharacter == 'o' || typedCharacter == 'O') {
            open_games_report_window();
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
                 "CinterForgeTerminal> %.85s", g_terminalInput);
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

    invalidate_terminal_window();
}

static void resize_classic_window(WindowPtr window, Point mousePosition)
{
    Rect allowedSizes;
    long newSize;
    short newWidth;
    short newHeight;

    if (window == g_gamesReportWindow) {
        SetRect(&allowedSizes, 365, 315, 1100, 850);
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
        g_reportFirstRow = 0;
    } else if (window == g_terminalWindow) {
        g_terminalWindowWidth = newWidth;
        g_terminalWindowHeight = newHeight;
        g_terminalFirstVisibleLine = g_terminalHistoryCount > terminal_visible_line_count()
            ? (short)(g_terminalHistoryCount - terminal_visible_line_count()) : 0;
    }

    InvalRect(&window->portRect);
}

/* A menu selection is a classic Menu Manager pair: menu ID and item number. */
static void handle_menu_selection(Point mousePosition)
{
    long menuSelection = MenuSelect(mousePosition);
    short selectedMenuId = HiWord(menuSelection);
    short selectedItem = LoWord(menuSelection);

    if (selectedMenuId == FILE_MENU_ID && selectedItem == 1) {
        g_applicationIsRunning = false;
    } else if (selectedMenuId == GAMES_MENU_ID) {
        switch (selectedItem) {
            case 1: open_games_report_window(); break;
            case 2: start_infinite_hd_catalog_scan(); break;
            case 3: launch_game_from_config_file(); break;
            default: break;
        }
    }

    HiliteMenu(0);
}

/* Convert global mouse coordinates before hit-testing cards in the report. */
static void handle_window_content_click(WindowPtr window, EventRecord *mouseEvent)
{
    Point localMousePosition;

    if (window != FrontWindow()) SelectWindow(window);
    if (window != g_gamesReportWindow) return;

    localMousePosition = mouseEvent->where;
    SetPort(window);
    GlobalToLocal(&localMousePosition);
    handle_game_card_click(localMousePosition, mouseEvent->when);
}

static void handle_mouse_down(EventRecord *mouseEvent)
{
    WindowPtr clickedWindow = NULL;
    short windowPart = FindWindow(mouseEvent->where, &clickedWindow);

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
    if (window != g_gamesReportWindow && window != g_terminalWindow) return;

    BeginUpdate(window);
    if (window == g_gamesReportWindow) {
        draw_games_report_window();
    } else {
        draw_terminal_window();
    }
    EndUpdate(window);
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
            } else {
                handle_terminal_key(event);
            }
            break;
        case updateEvt:
            handle_window_update((WindowPtr)event->message);
            break;
        case activateEvt:
            if (g_gamesReportWindow) InvalRect(&g_gamesReportWindow->portRect);
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
    g_terminalWindow = NewWindow(NULL, &windowBounds, "\pCinterForgeTerminal", true,
                           documentProc, (WindowPtr)-1, true, 0);
    if (!g_terminalWindow) ExitToShell();
    menu = NewMenu(FILE_MENU_ID, "\pFile");
    AppendMenu(menu, "\pQuit/Q");
    InsertMenu(menu, 0);
    menu = NewMenu(GAMES_MENU_ID, "\pGames");
    AppendMenu(menu, "\pGames Report/O;Rescan/R;Run Game.txt/G");
    InsertMenu(menu, 0);
    DrawMenuBar();
    ShowWindow(g_terminalWindow);
    SelectWindow(g_terminalWindow);
    append_terminal_history_line("Welcome to CinterForge.");
    append_terminal_history_line("SHALL WE PLAY A GAME?");
    append_terminal_history_line("");
    append_terminal_history_line("Type 'help' for instructions.");
    append_terminal_history_line("Type 'games' for applications on Infinite HD.");
    append_terminal_history_line("Type 'games report' for Game Cards.");
    append_terminal_history_line("");
}

int main(void)
{
    EventRecord eventRecord;
    InitGraf(&qd.thePort);
    InitFonts();
    InitWindows();
    InitMenus();
    TEInit();
    InitDialogs(NULL);
    InitCursor();
    MaxApplZone();
    initialize_terminal_interface();
    read_game_name_from_config();
    start_infinite_hd_catalog_scan();
    while (g_applicationIsRunning) {
        if (WaitNextEvent(everyEvent, &eventRecord, 2, NULL)) handle_classic_mac_event(&eventRecord);
        if (g_catalogScanActive) scan_next_catalog_batch();
        if (!g_catalogScanActive) auto_launch_requested_game_if_ready();
    }
    if (g_cardPixelBuffer) DisposePtr(g_cardPixelBuffer);
    return 0;
}

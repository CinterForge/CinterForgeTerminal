-- CinterForgeOS installer. All artwork is loaded from Contents/Resources/UI.
-- osascript steam_dialogs.applescript install-account <default> <labels...> <ui-dir>
use AppleScript version "2.5"
use framework "Foundation"
use framework "AppKit"
use scripting additions

property ca : current application
property mainWindow : missing value
property accountRows : missing value
property accountLabels : {}
property selectedIndex : 1
property resultLabel : ""
property pendingDefault : ""
property pendingLabels : {}
property pendingUIPath : ""
property uiFailure : ""

on colorRGB(r, g, b, a)
    return ca's NSColor's colorWithCalibratedRed:r green:g blue:b alpha:a
end colorRGB

on addLabel(x, y, w, h, txt, sizeValue, isBold, ink)
    set t to ca's NSTextField's alloc()'s initWithFrame:{{x, y}, {w, h}}
    t's setStringValue:(txt as text)
    t's setEditable:false
    t's setSelectable:false
    t's setBordered:false
    t's setDrawsBackground:false
    if isBold then
        t's setFont:(ca's NSFont's boldSystemFontOfSize:sizeValue)
    else
        t's setFont:(ca's NSFont's systemFontOfSize:sizeValue)
    end if
    t's setTextColor:ink
    t's setLineBreakMode:(ca's NSLineBreakByWordWrapping)
    t's setUsesSingleLineMode:false
    (my mainWindow's contentView())'s addSubview:t
    return t
end addLabel

on addImage(x, y, w, h, filePath)
    set im to ca's NSImage's alloc()'s initWithContentsOfFile:filePath
    if im is missing value then return
    set v to ca's NSImageView's alloc()'s initWithFrame:{{x, y}, {w, h}}
    v's setImage:im
    v's setImageScaling:(ca's NSImageScaleProportionallyUpOrDown)
    (my mainWindow's contentView())'s addSubview:v
end addImage

-- Fill the entire left panel without distorting the background image.
-- The artwork is 4:3, while the panel is narrower; center-crop it.
on addCroppedBackground(filePath)
    set im to ca's NSImage's alloc()'s initWithContentsOfFile:filePath
    if im is missing value then error "Installer background not found: " & filePath
    set clipView to ca's NSView's alloc()'s initWithFrame:{{0, 0}, {430, 560}}
    clipView's setWantsLayer:true
    (clipView's layer())'s setMasksToBounds:true
    -- NSImage.size returns an AppleScript record (NSSize), not an ObjC object.
    -- Sending width/height selectors to that record raises error -1728.
    set imageSize to im's |size|()
    set originalWidth to (width of imageSize) as real
    set originalHeight to (height of imageSize) as real
    set scaledWidth to 560 * originalWidth / originalHeight
    if scaledWidth < 430 then set scaledWidth to 430
    set offsetX to (430 - scaledWidth) / 2
    set imageView to ca's NSImageView's alloc()'s initWithFrame:{{offsetX, 0}, {scaledWidth, 560}}
    imageView's setImage:im
    imageView's setImageScaling:(ca's NSImageScaleAxesIndependently)
    clipView's addSubview:imageView
    (my mainWindow's contentView())'s addSubview:clipView
end addCroppedBackground

on addPanel(x, y, w, h, ink, radius)
    set v to ca's NSView's alloc()'s initWithFrame:{{x, y}, {w, h}}
    v's setWantsLayer:true
    (v's layer())'s setBackgroundColor:(ink's CGColor())
    (v's layer())'s setCornerRadius:radius
    (my mainWindow's contentView())'s addSubview:v
    return v
end addPanel

on addButton(x, y, w, h, titleText, selectorName, isPrimary)
    set b to ca's NSButton's alloc()'s initWithFrame:{{x, y}, {w, h}}
    b's setTitle:titleText
    b's setBezelStyle:(ca's NSBezelStyleRounded)
    b's setFont:(ca's NSFont's boldSystemFontOfSize:15)
    b's setTarget:me
    b's setAction:selectorName
    if isPrimary then
        b's setKeyEquivalent:(return)
        b's setBezelColor:(my colorRGB(0.02, 0.40, 0.97, 1.0))
    end if
    (my mainWindow's contentView())'s addSubview:b
    return b
end addButton

on refreshRows()
    set n to (my accountRows's |count|()) as integer
    if n = 0 then return
    repeat with i from 0 to (n - 1)
        set b to (my accountRows's objectAtIndex:i)
        if (i + 1) = my selectedIndex then
            b's setState:1
        else
            b's setState:0
        end if
    end repeat
end refreshRows

on chooseAccount:sender
    set my selectedIndex to (sender's tag()) as integer
    my refreshRows()
end chooseAccount:

on installClicked:sender
    set my resultLabel to item (my selectedIndex) of (my accountLabels)
    (ca's NSApplication's sharedApplication())'s stopModal()
end installClicked:

on cancelClicked:sender
    set my resultLabel to ""
    (ca's NSApplication's sharedApplication())'s stopModal()
end cancelClicked:

on showInstaller(defaultLabel, labelsList, uiPath)
    set my pendingDefault to defaultLabel
    set my pendingLabels to labelsList
    set my pendingUIPath to uiPath
    set my resultLabel to ""
    set my uiFailure to ""
    -- osascript runs this script on its main thread. Do not send an
    -- Objective-C selector to an AppleScript script object: it is not an
    -- NSObject and does not implement performSelectorOnMainThread:.
    my presentInstallerOnMainThread:(missing value)
    if my uiFailure is not "" then error (my uiFailure)
    return my resultLabel
end showInstaller

on presentInstallerOnMainThread:ignored
    try
        my buildInstallerWindow(my pendingDefault, my pendingLabels, my pendingUIPath)
    on error errMsg number errNum
        set my uiFailure to "Installer UI failed (" & errNum & "): " & errMsg
    end try
end presentInstallerOnMainThread:

on buildInstallerWindow(defaultLabel, labelsList, uiPath)
    set my accountLabels to labelsList
    if (count of my accountLabels) = 0 then return ""
    set my selectedIndex to 1
    repeat with i from 1 to count of my accountLabels
        if (item i of my accountLabels as text) = defaultLabel then set my selectedIndex to i
    end repeat
    set my accountRows to ca's NSMutableArray's alloc()'s init()
    set my resultLabel to ""

    set whiteInk to ca's NSColor's whiteColor()
    set mutedWhite to my colorRGB(0.84, 0.89, 1.0, 1.0)
    set ink to ca's NSColor's labelColor()
    set mutedInk to ca's NSColor's secondaryLabelColor()
    set blueInk to my colorRGB(0.04, 0.43, 1.0, 1.0)
    set cocoaApp to ca's NSApplication's sharedApplication()
    set my mainWindow to ca's NSWindow's alloc()'s initWithContentRect:{{0, 0}, {1024, 560}} styleMask:7 backing:2 defer:false
    my mainWindow's setTitle:"CinterForgeOS Installer"
    my mainWindow's |center|()
    set content to my mainWindow's contentView()
    content's setWantsLayer:true
    (content's layer())'s setBackgroundColor:((ca's NSColor's windowBackgroundColor())'s CGColor())

    -- Art direction: left artwork / dark translucent veil / independent logo.
    my addCroppedBackground(uiPath & "/InstallerBackground.png")
    my addPanel(0, 0, 430, 560, (my colorRGB(0.015, 0.025, 0.09, 0.42)), 0)
    my addImage(137, 337, 156, 174, uiPath & "/CinterForge-logo.png")
    my addLabel(47, 292, 350, 52, "CinterForgeOS", 37, true, whiteInk)
    my addLabel(58, 272, 355, 22, "C R E A T E  ·  P R E S E R V E  ·  E X P L O R E", 10, false, mutedWhite)

    -- Three steps: circles and timeline are individual native views.
    my addPanel(80, 92, 2, 158, (my colorRGB(0.75, 0.82, 0.94, 0.55)), 1)
    repeat with i from 1 to 3
        set stepY to 246 - ((i - 1) * 73)
        if i = 1 then
            set stepColor to blueInk
        else
            set stepColor to my colorRGB(0.48, 0.52, 0.62, 0.94)
        end if
        my addPanel(60, stepY - 5, 42, 42, stepColor, 21)
        my addLabel(73, stepY + 1, 30, 29, i as text, 20, true, whiteInk)
    end repeat
    my addLabel(115, 241, 270, 30, "Setup", 21, true, whiteInk)
    my addLabel(115, 221, 295, 23, "Prepares the environment", 13, false, mutedWhite)
    my addLabel(115, 168, 270, 30, "Build", 21, true, whiteInk)
    my addLabel(115, 148, 295, 23, "Compiles and configures", 13, false, mutedWhite)
    my addLabel(115, 95, 270, 30, "Stage", 21, true, whiteInk)
    my addLabel(115, 75, 295, 23, "Installs and prepares for Steam", 13, false, mutedWhite)

    -- Right panel: generous whitespace, restrained typography, clear hierarchy.
    my addLabel(468, 460, 515, 48, "Install CinterForgeOS?", 30, true, ink)
    my addLabel(470, 396, 510, 56, "This will run Setup, Build and Stage to prepare CinterForgeOS for your Steam account.", 16, false, mutedInk)
    my addLabel(470, 339, 510, 51, "Installation may take a while." & return & "Progress will appear in Terminal.", 16, false, mutedInk)
    my addPanel(470, 327, 510, 1, (my colorRGB(0.65, 0.68, 0.73, 0.28)), 0)
    my addLabel(470, 286, 510, 31, "Choose your Steam account", 18, true, ink)

    set rowHeight to 45
    set totalHeight to (count of my accountLabels) * rowHeight
    if totalHeight < 180 then set totalHeight to 180
    set scroll to ca's NSScrollView's alloc()'s initWithFrame:{{470, 102}, {510, 177}}
    scroll's setHasVerticalScroller:true
    scroll's setBorderType:(ca's NSBezelBorder)
    set documentView to ca's NSView's alloc()'s initWithFrame:{{0, 0}, {490, totalHeight}}
    repeat with i from 1 to count of my accountLabels
        set rowY to totalHeight - (i * rowHeight)
        set b to ca's NSButton's alloc()'s initWithFrame:{{10, rowY + 3}, {466, 39}}
        b's setTitle:(item i of my accountLabels as text)
        b's setButtonType:(ca's NSButtonTypeRadio)
        b's setAlignment:(ca's NSTextAlignmentLeft)
        b's setFont:(ca's NSFont's systemFontOfSize:15)
        b's setTarget:me
        b's setAction:"chooseAccount:"
        b's setTag:i
        documentView's addSubview:b
        my accountRows's addObject:b
    end repeat
    scroll's setDocumentView:documentView
    content's addSubview:scroll
    my refreshRows()
    my addPanel(470, 91, 510, 1, (my colorRGB(0.65, 0.68, 0.73, 0.28)), 0)
    my addButton(715, 31, 122, 42, "Cancel", "cancelClicked:", false)
    set installButton to my addButton(848, 31, 132, 42, "Install", "installClicked:", true)
    my mainWindow's setDefaultButtonCell:(installButton's cell())
    -- Terminal launches this dialog, but must not cover it while the user
    -- chooses an account. Floating level keeps the installer above Terminal;
    -- the window is dismissed before the build animation begins.
    my mainWindow's setLevel:3 -- NSFloatingWindowLevel
    my mainWindow's makeKeyAndOrderFront:(missing value)
    cocoaApp's activateIgnoringOtherApps:true
    cocoaApp's runModalForWindow:(my mainWindow)
    my mainWindow's orderOut:(missing value)
    return my resultLabel
end buildInstallerWindow

on run argv
    if (count of argv) = 0 then error "Missing Steam dialog action"
    set actionName to item 1 of argv as text
    if actionName is "test-ui" then
        if (count of argv) < 2 then error "Missing UI asset directory"
        return my showInstaller("Pete (petefalco)", {"Pete (petefalco)", "CreativeStudio (creativestudio)", "archive (archiveuser)"}, item 2 of argv as text)
    else if actionName is "install-account" or actionName is "choose-account" then
        if (count of argv) < 4 then error "Missing Steam account labels or UI directory"
        set uiPath to item -1 of argv as text
        set defaultLabel to item 2 of argv as text
        set localizedLabels to items 3 thru -2 of argv
        return my showInstaller(defaultLabel, localizedLabels, uiPath)
    else if actionName is "quit-steam" then
        display dialog "Please quit Steam completely, then click Continue. The installer will wait for Steam to exit." buttons {"Continue"} default button "Continue" with title "CinterForgeOS Installer"
        return "continue"
    else
        error "Unknown Steam dialog action: " & actionName
    end if
end run

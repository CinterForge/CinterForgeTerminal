on run argv
	set volumeName to item 1 of argv
	set applicationName to item 2 of argv
	-- The editable DMG is mounted under Builds, not necessarily /Volumes.
	-- Target it by its actual path instead of asking Finder for disk volumeName.
	set mountPath to item 3 of argv
	set mountedFolder to (POSIX file mountPath) as alias
	set backgroundFile to (POSIX file (mountPath & "/.background/DMGBackground.png")) as alias
	tell application "Finder"
		activate
		open mountedFolder
		delay 1
		set layoutWindow to front Finder window
		set target of layoutWindow to mountedFolder
		set current view of layoutWindow to icon view
		set toolbar visible of layoutWindow to false
		set statusbar visible of layoutWindow to false
		set bounds of layoutWindow to {200, 100, 968, 920}
		set theViewOptions to the icon view options of layoutWindow
		set arrangement of theViewOptions to not arranged
		set icon size of theViewOptions to 96
		set text size of theViewOptions to 12
		set label position of theViewOptions to bottom
		set background picture of theViewOptions to backgroundFile
		set position of item applicationName of layoutWindow to {135, 550}
		set position of item "Applications" of layoutWindow to {630, 550}
		set position of item "README.md" of layoutWindow to {385, 690}
		close layoutWindow
		open mountedFolder
		set layoutWindow to front Finder window
		update mountedFolder without registering applications
		-- Keep the finished layout on-screen briefly during Publish.
		delay 5
		close layoutWindow
	end tell
end run

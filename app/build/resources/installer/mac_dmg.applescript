on run argv
	set volumeName to item 1 of argv
	set applicationName to item 2 of argv
	tell application "Finder"
		tell disk volumeName
			open
			delay 1
			set current view of container window to icon view
			set toolbar visible of container window to false
			set statusbar visible of container window to false
			set bounds of container window to {200, 100, 968, 920}
			set theViewOptions to the icon view options of container window
			set arrangement of theViewOptions to not arranged
			set icon size of theViewOptions to 96
			set text size of theViewOptions to 12
			set label position of theViewOptions to bottom
			set background picture of theViewOptions to file ".background:DMGBackground.png"
			set position of item applicationName of container window to {135, 550}
			set position of item "Applications" of container window to {630, 550}
			set position of item "README.md" of container window to {385, 690}
			close
			open
			update without registering applications
			delay 2
			close
		end tell
	end tell
end run
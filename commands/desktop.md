---
description: Let the DualSense buttons drive the Claude desktop app (macOS permission + status)
allowed-tools: Bash(padpilot:*)
---
Desktop-app control check:

!`padpilot desktop`

Tell the user in two or three short lines whether the controller can now drive the desktop app. If Accessibility permission is missing, tell them to allow "Python" in System Settings > Privacy & Security > Accessibility (macOS may already be showing that dialog), then run `padpilot stop` and `/padpilot:desktop` again.

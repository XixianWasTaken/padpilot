---
description: Let the DualSense buttons drive the Claude desktop app (macOS permission + status)
allowed-tools: Bash("${CLAUDE_PLUGIN_ROOT}/bin/padpilot" desktop:*)
---
Desktop-app control check:

!`"${CLAUDE_PLUGIN_ROOT}/bin/padpilot" desktop`

Tell the user in two or three short lines whether the controller can now drive the desktop app. If Accessibility permission is missing, tell them to allow "Python" in System Settings > Privacy & Security > Accessibility (macOS may already be showing that dialog), then run `"${CLAUDE_PLUGIN_ROOT}/bin/padpilot" stop` (show the full path) and `/padpilot:desktop` again.

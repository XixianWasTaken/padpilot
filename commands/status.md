---
description: Show DualSense controller and session status
allowed-tools: Bash("${CLAUDE_PLUGIN_ROOT}/bin/padpilot":*)
---
Controller status:

!`"${CLAUDE_PLUGIN_ROOT}/bin/padpilot" status`

Summarize this for the user in two or three short lines (controller connected? transport, battery, which sessions and their states). If the daemon is not running, tell them to run `/padpilot:doctor`.

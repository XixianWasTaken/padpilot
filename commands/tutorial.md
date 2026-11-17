---
description: Open the DualSense tutorial page (practice mode while it is open)
allowed-tools: Bash("${CLAUDE_PLUGIN_ROOT}/bin/padpilot" tutorial:*)
---
!`"${CLAUDE_PLUGIN_ROOT}/bin/padpilot" tutorial`

The output above is the tutorial page's address; it has been opened in the browser. Tell the user in one or two lines, in their language: while that page is open the controller is in practice mode (presses only count on the page and are not sent to Claude), and the page's "Exit practice" button, or closing the page, ends it. If the output is an error instead, show it and suggest `/padpilot:doctor`.

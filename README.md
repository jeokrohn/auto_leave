# Automatically hide annoying (bot notification) spaces

Creating a tool to monitor spaces and leave annoying notification spaces based on a block list.

General idea:
* use WDM registration to open a websocket
* look for conversation activities
* if an activity is detected in a blocked space then immediately hide that space

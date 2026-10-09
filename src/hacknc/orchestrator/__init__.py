"""The discharge coordinator: a chat message in, an answer or an orchestrated discharge out.

It holds the user's token (addressed to it) but touches no patient data itself: for each
step it binds the agent to the task's patient at the EHR gateway, exchanges that token for a
narrower one addressed to one agent, and calls that agent over MCP. ``coordinator.py`` is the
Strands agent, ``discharge.py`` the checklist state, ``watch.py`` task binding and security
alerts, ``app.py`` the HTTP API.
"""

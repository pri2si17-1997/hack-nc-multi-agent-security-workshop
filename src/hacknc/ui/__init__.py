"""Hospital UI: the browser app clinicians log into.

Built as a "backend for frontend" (BFF): the browser only holds a session cookie,
while tokens stay on the server. That keeps access tokens away from JavaScript,
which is the recommended pattern for browser apps handling sensitive data.
"""

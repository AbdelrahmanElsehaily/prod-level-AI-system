"""
app/services/
=============
The services layer contains business logic — the "what the app does" — separate
from the HTTP layer (routers/) which handles "how requests come in and go out".

  chat_history.py — create conversations, store messages, load history  (Step 4)
  ai.py           — wrap the Anthropic SDK, call Claude, return replies  (Step 5)

Why a services layer?
  A router function has one job: parse the HTTP request and return a response.
  If it also contains database queries and AI calls, it becomes long, hard to
  test, and impossible to reuse. The services layer extracts that logic so:

  1. The same service function can be called from an HTTP route, a background
     task, a CLI script, or a test — without duplicating code.
  2. Service functions are tested by passing a mock DB session — no HTTP client
     needed, no request/response parsing. Tests are faster and more focused.
  3. When business logic changes (e.g. history limit increases from 20 to 50),
     you edit one function in one file, not 3 route handlers.
"""

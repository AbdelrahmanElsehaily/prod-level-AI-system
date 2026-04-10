"""
app/routers/
============
Each file in this package is one logical group of endpoints.

  health.py  — infrastructure/readiness endpoints (GET /health)
  chat.py    — core product endpoints (POST /chat)  [added in Step 5]

All routers are imported and mounted in app/main.py via:
    app.include_router(router, prefix="/...")

Keeping routers separate means:
  - Each file stays small and focused
  - Teams can work on different routers without merge conflicts
  - You can add, remove, or version a group of routes without touching main.py
"""

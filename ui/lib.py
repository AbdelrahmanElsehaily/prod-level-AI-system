"""
ui/lib.py — shared helpers for every page in the Streamlit UI
==============================================================
Every page in `ui/` and `ui/pages/` imports from here for:

  - The API base URL (read once from $CHAT_API_URL)
  - A typed-ish API client (post / get / delete with error mapping)
  - A reusable sidebar health badge widget

Why a module instead of copy-paste per page?
  Three pages need to hit `/health` for the sidebar badge, and two of them
  need an error-handling wrapper around `requests`. If those live in each
  page file, fixing a bug means editing it three times. Streamlit's multi-
  page model has no built-in shared layout, so we factor it out manually
  via this module.

This is a UI helper, NOT a backend service. It is allowed to import
`requests` and call HTTP directly. Do not import this from anywhere
inside `app/` — it is for `ui/` only.
"""

from __future__ import annotations

import os
from typing import Any

import requests
import streamlit as st

# ---------------------------------------------------------------------------
# Config — single source of truth for the API base URL
# ---------------------------------------------------------------------------
# CHAT_API_URL points at the FastAPI backend. Default is localhost:8000
# for local `streamlit run`. On Railway, set it to the staging/prod URL.
API_URL: str = os.getenv("CHAT_API_URL", "http://localhost:8000")

# Per-request timeouts (seconds). Chat endpoints can take a while when
# the model is cold; health is a tiny 200/503 ping.
TIMEOUT_HEALTH = 3
TIMEOUT_CHAT = 60
TIMEOUT_UPLOAD = 60
TIMEOUT_QUICK = 10


# ---------------------------------------------------------------------------
# API client wrappers
# ---------------------------------------------------------------------------


class ApiError(Exception):
    """
    What pages catch when they want a clean error to render to the user.

    Holds:
      status_code: HTTP status from the backend (or 0 if no response).
      detail:      Human-readable message — taken from the JSON `detail`
                   field if FastAPI returned one, else str(exception).
    """

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(f"{status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail


def _wrap(method: str, path: str, **kwargs: Any) -> dict[str, Any] | list[Any]:
    """
    Run a `requests` call against the backend and convert anything that
    goes wrong into a single ApiError.

    Why one wrapper instead of per-call try/except?
      Every page would otherwise repeat three except branches (Timeout,
      ConnectionError, HTTPError). Centralising that here means a page
      writes:

        try:
            data = lib.post("/chat", json={...})
        except lib.ApiError as exc:
            st.error(f"{exc.status_code}: {exc.detail}")

      …and gets correct error rendering on day one.

    Returns:
      The decoded JSON body. JSON is the only response shape the API
      uses today; if that ever changes, this wrapper must change too.
    """
    url = f"{API_URL}{path}"
    try:
        resp = requests.request(method, url, **kwargs)
        resp.raise_for_status()
        # 204 No Content has no body — return an empty dict so callers
        # can do `result = lib.delete(...)` and not crash on `.json()`.
        if resp.status_code == 204 or not resp.content:
            return {}
        return resp.json()  # type: ignore[no-any-return]
    except requests.exceptions.Timeout as exc:
        raise ApiError(0, "Request timed out. Try again.") from exc
    except requests.exceptions.ConnectionError as exc:
        raise ApiError(0, f"Could not reach the API at {API_URL}.") from exc
    except requests.exceptions.HTTPError as exc:
        # FastAPI returns {"detail": "..."} for errors. Fall back to the
        # raw exception string if that shape is missing.
        detail = str(exc)
        try:
            body = exc.response.json()
            if isinstance(body, dict) and "detail" in body:
                detail = str(body["detail"])
        except Exception:
            pass
        raise ApiError(exc.response.status_code, detail) from exc


def get(path: str, **kwargs: Any) -> dict[str, Any] | list[Any]:
    return _wrap("GET", path, timeout=kwargs.pop("timeout", TIMEOUT_QUICK), **kwargs)


def post(path: str, **kwargs: Any) -> dict[str, Any] | list[Any]:
    return _wrap("POST", path, timeout=kwargs.pop("timeout", TIMEOUT_CHAT), **kwargs)


def delete(path: str, **kwargs: Any) -> dict[str, Any] | list[Any]:
    return _wrap("DELETE", path, timeout=kwargs.pop("timeout", TIMEOUT_QUICK), **kwargs)


# ---------------------------------------------------------------------------
# Reusable widgets
# ---------------------------------------------------------------------------


def render_sidebar_health() -> None:
    """
    Drop a "API healthy / degraded / unreachable" badge in the sidebar.

    Call this once at the top of every page so users always see the
    backend state in the same place. No return value — the side-effect
    IS the rendered widget.
    """
    with st.sidebar:
        st.title("⚙️ Settings")
        st.markdown(f"**API:** `{API_URL}`")
        try:
            resp = requests.get(f"{API_URL}/health", timeout=TIMEOUT_HEALTH)
            if resp.status_code == 200:
                st.success("API healthy ✓")
            else:
                # Show the failing checks so an operator can act on it
                # without leaving the UI.
                body = resp.json()
                st.warning(f"API degraded — {body.get('checks', {})}")
        except requests.exceptions.RequestException:
            st.error("API unreachable ✗")
        st.divider()

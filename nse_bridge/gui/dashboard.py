"""Control Center serving: /ui renders the bundled single-page GUI.

Auth note: the GUI runs on loopback from this same app, and its API
calls must carry the mandatory local API key — but the key must never
appear in page JavaScript (any script in the page could read it). So
the /ui response sets the key as an HttpOnly + SameSite=Strict cookie
(scoped to /api/v1/); the browser then sends it automatically on
same-origin API calls, and the middleware accepts that cookie as an
alternative carrier of the SAME credential (see api/app.py). The key
is still mandatory on every /api/v1/ call — there is no
unauthenticated exemption for the GUI. The cookie is readable by any
local process that fetches /ui, which is the same exposure as the
per-user secret store the key already lives in; remote callers cannot
reach the loopback bind at all.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import Request
from fastapi.responses import HTMLResponse

_STATIC = Path(__file__).parent / "static" / "index.html"

#: Cookie carrying the local API key to the loopback GUI (see module docstring).
LOCAL_API_KEY_COOKIE = "nse_local_api_key"


def dashboard_html() -> str:
    if _STATIC.is_file():
        return _STATIC.read_text(encoding="utf-8")
    return (
        "<html><body style='font-family:sans-serif;background:#0b0f14;color:#dbe4f0'>"
        "<h2>NSE MT5 Bridge</h2><p>Control Center asset missing from the bundle.</p></body></html>"
    )


def register_dashboard(app) -> None:  # type: ignore[no-untyped-def]
    @app.get("/ui", include_in_schema=False)
    async def ui(request: Request) -> HTMLResponse:
        response = HTMLResponse(dashboard_html())
        ctx = getattr(request.app.state, "ctx", None)
        key = getattr(getattr(ctx, "settings", None), "local_api_key", "") or ""
        if key:
            # Path must cover /ws/ui too: page JS cannot set headers on a
            # WebSocket upgrade, so the cookie is the only carriage for the
            # authenticated live feed. Scoped to the loopback origin (the
            # only place this app is reachable) and still HttpOnly +
            # SameSite=Strict; the /api/v1 middleware accepts it exactly
            # as before.
            response.set_cookie(LOCAL_API_KEY_COOKIE, key, httponly=True, samesite="strict", path="/")
        return response

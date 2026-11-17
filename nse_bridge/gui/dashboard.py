"""Dashboard serving: /ui renders the bundled single-page dashboard."""

from __future__ import annotations

from pathlib import Path

from fastapi.responses import HTMLResponse

_STATIC = Path(__file__).parent / "static" / "index.html"


def dashboard_html() -> str:
    if _STATIC.is_file():
        return _STATIC.read_text(encoding="utf-8")
    return (
        "<html><body style='font-family:sans-serif;background:#0b0f14;color:#dbe4f0'>"
        "<h2>NSE MT5 Bridge</h2><p>Dashboard asset missing from the bundle.</p></body></html>"
    )


def register_dashboard(app) -> None:  # type: ignore[no-untyped-def]
    @app.get("/ui", include_in_schema=False)
    async def ui() -> HTMLResponse:
        return HTMLResponse(dashboard_html())

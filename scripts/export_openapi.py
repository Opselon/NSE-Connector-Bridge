"""Export the bridge OpenAPI schema for GitHub Pages / server-side client generation.

Usage:  .venv/Scripts/python scripts/export_openapi.py [output_path]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nse_bridge.api.app import create_app  # noqa: E402
from nse_bridge.config import BridgeSettings  # noqa: E402
from nse_bridge.engine.common import BridgeContext  # noqa: E402
from nse_bridge.mt5.driver import Mt5Driver  # noqa: E402


def export(out: Path) -> None:
    settings = BridgeSettings(log_dir=str(Path(tempdir := __import__("tempfile").mkdtemp())))
    driver = Mt5Driver(settings)  # never connects — schema generation only
    ctx = BridgeContext(settings=settings, driver=driver)
    ctx.get_or_create_device_id()
    app = create_app(ctx)
    schema = app.openapi()
    schema["servers"] = [{"url": "http://127.0.0.1:8788", "description": "Local bridge (client machine)"}]
    out.write_text(json.dumps(schema, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"exported {len(schema['paths'])} paths -> {out}")
    driver.disconnect()


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("docs/openapi.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    export(target)

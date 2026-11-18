"""Action manifest (CONTRACT v1 §4.1, [client-v1.1]).

The Connector POSTs its discovered manifest to
``POST /api/v1/connector/manifest`` so the website can show per-action
availability and broker compatibility. The server answers
``{accepted: true, manifest_hash: "…"}``; re-posting an unchanged hash
is a no-op ack.

MAPPING / OMISSIONS (documented here and in the module docstring, per
the Phase 2 spec — nothing is invented):

- The contract manifest is built from MT5 MCP discovery
  (``tools/list`` → Tier 0–3 classification, ``mcp_server_version``,
  ``terminal_build`` as a NUMBER, per-action ``tier`` /
  ``required_permission`` / ``input_schema``). This Bridge talks to MT5
  through the official Python package (C++ IPC), NOT the MCP server
  (§8 of the contract describes the Rust Connector's transport; the
  Bridge maps onto its Python driver and documents the difference).
  It therefore has NO MCP tool list to classify.
- Fields filled truthfully from this Bridge:
  ``device_id``, ``manifest_hash`` (SHA-256 over the local 90-command
  registry ``number:name`` lines — the Bridge's real local capability
  surface), ``action_count``, ``actions`` (each ``{name, number,
  category}`` — the local command identity).
- ``terminal_build``: OMITTED. The driver exposes only a terminal
  NAME string (``terminal_version``), never a numeric build — a build
  number must never be invented or parsed out of a name.
- ``mcp_server_version``: OMITTED — there is no MCP server here.
- ``tiers`` counts and per-action ``tier`` / ``required_permission`` /
  ``input_schema``: OMITTED. Tier assignments belong to the MCP tool
  classification (§8.3); inventing them from the Python registry
  would misrepresent the terminal's capabilities. Unknown actions
  default to Tier 2 server-side (fail-closed, §4.1), so omitting is
  the safe mapping.
- The contract example hash (``b3df4d9fb2a3b7e0``) is the reference
  terminal's MCP manifest hash, NOT this Bridge's — ours is computed
  over the local registry and will differ; that is expected.

The manifest is also re-sent over WS as a ``manifest.update``
envelope when it changes mid-session (§4.1) — the channel sends the
same payload produced here.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any

from ..engine.registry import all_commands

if TYPE_CHECKING:
    from ..mt5.driver import Mt5Driver

__all__ = ["build_manifest", "manifest_hash"]


def manifest_hash() -> str:
    """SHA-256 (first 16 hex chars) over the local command registry.

    The hash input is one ``<number>:<name>`` line per local command,
    sorted by command number — so any registry change (add/remove/
    rename) changes the hash, and the server can tell the manifest
    moved. The 16-char truncation matches the contract example's hash
    length (``b3df4d9fb2a3b7e0``).
    """
    lines = "".join(f"{c.number}:{c.name}\n" for c in all_commands())
    return hashlib.sha256(lines.encode("utf-8")).hexdigest()[:16]


def build_manifest(device_id: str, *, driver: Mt5Driver | None = None) -> dict[str, Any]:
    """Build the §4.1 manifest with only truthfully mappable fields.

    ``driver`` is accepted for future use (a numeric terminal build,
    if the driver ever exposes one) and is otherwise ignored — see
    the module docstring for the omission list.
    """
    _ = driver  # No truthful terminal_build is available from the driver (see docstring).
    commands = all_commands()
    return {
        "device_id": device_id,
        "manifest_hash": manifest_hash(),
        "action_count": len(commands),
        "actions": [
            {"name": c.name, "number": c.number, "category": c.category} for c in commands
        ],
    }

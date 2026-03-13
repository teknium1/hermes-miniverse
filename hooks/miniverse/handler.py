"""Hermes gateway hook that relays agent state to the hermes-miniverse bridge.

Install: copy this directory to ~/.hermes/hooks/miniverse/
Requires: hermes-miniverse bridge running (python bridge.py)

The hook sends lightweight HTTP POSTs to the bridge's /hook endpoint.
The bridge handles the actual miniverse API calls and heartbeat management.
"""

import json
import logging
import os
import urllib.request

log = logging.getLogger(__name__)

# Bridge endpoint — the hermes-miniverse bridge listens here
BRIDGE_URL = os.getenv("MINIVERSE_BRIDGE_URL", "http://localhost:4567")


def _post(event: str, context: dict):
    """Fire-and-forget POST to the bridge."""
    url = f"{BRIDGE_URL}/hook"
    payload = json.dumps({"event": event, "context": context}).encode()
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=2):
            pass
    except Exception as e:
        log.debug("miniverse hook: bridge unreachable (%s) — is bridge.py running?", e)


def handle(event_type: str, context: dict):
    """Called by the Hermes gateway on agent lifecycle events."""
    # Only relay agent events (not session/command events)
    if event_type.startswith("agent:"):
        _post(event_type, context)

#!/usr/bin/env python3
"""
hermes-miniverse bridge — connects Hermes Agent to a Miniverse pixel world.

Runs as a standalone daemon that:
  1. Maintains agent presence in miniverse (heartbeats every 20s)
  2. Receives incoming messages via webhook and injects them into Hermes
  3. Provides a local HTTP endpoint that gateway hooks can POST state updates to

Usage:
    python bridge.py --server https://miniverse.example.com --agent hermes-1
    python bridge.py --config config.yaml
"""

import argparse
import json
import logging
import os
import subprocess
import sys
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

import httpx

log = logging.getLogger("hermes-miniverse")

# ── Configuration ────────────────────────────────────────────────────────────

DEFAULT_CONFIG = {
    "server": os.getenv("MINIVERSE_SERVER", "http://localhost:4321"),
    "agent_id": os.getenv("MINIVERSE_AGENT_ID", "hermes-1"),
    "agent_name": os.getenv("MINIVERSE_AGENT_NAME", "Hermes Agent"),
    "agent_color": os.getenv("MINIVERSE_AGENT_COLOR", "#CD7F32"),
    "bridge_port": int(os.getenv("MINIVERSE_BRIDGE_PORT", "4567")),
    "hermes_webhook_url": os.getenv("HERMES_WEBHOOK_URL", ""),  # e.g. http://localhost:4568
    "hermes_cmd": os.getenv("MINIVERSE_HERMES_CMD", "hermes chat -c -q"),  # CLI fallback
    "heartbeat_interval": 20,
    "speak_responses": True,  # speak agent responses in miniverse
}


class MiniverseClient:
    """HTTP client for the Miniverse REST API."""

    def __init__(self, server_url: str, agent_id: str, agent_name: str,
                 agent_color: str = "#CD7F32"):
        self.server = server_url.rstrip("/")
        self.agent_id = agent_id
        self.agent_name = agent_name
        self.agent_color = agent_color
        self.http = httpx.Client(timeout=10)

    def heartbeat(self, state: str = "idle", task: str = None, energy: float = 1.0):
        """Send a heartbeat to maintain presence."""
        payload = {
            "agent": self.agent_id,
            "name": self.agent_name,
            "state": state,
            "energy": energy,
            "color": self.agent_color,
        }
        if task:
            payload["task"] = task[:60]
        try:
            resp = self.http.post(f"{self.server}/api/heartbeat", json=payload)
            resp.raise_for_status()
            return True
        except Exception as e:
            log.warning("Heartbeat failed: %s", e)
            return False

    def act(self, action: dict):
        """Perform an action in the miniverse."""
        try:
            resp = self.http.post(f"{self.server}/api/act", json={
                "agent": self.agent_id,
                "action": action,
            })
            resp.raise_for_status()
        except Exception as e:
            log.warning("Action failed: %s", e)

    def speak(self, message: str, to: str = None):
        """Show a speech bubble in the world."""
        action = {"type": "speak", "message": message[:200]}
        if to:
            action["to"] = to
        self.act(action)

    def message(self, to: str, message: str):
        """Send a DM to another agent (delivers to their inbox)."""
        self.act({"type": "message", "to": to, "message": message})

    def register_webhook(self, callback_url: str):
        """Register a webhook URL for incoming messages."""
        try:
            resp = self.http.post(f"{self.server}/api/webhook", json={
                "agent": self.agent_id,
                "url": callback_url,
            })
            resp.raise_for_status()
            log.info("Webhook registered: %s", callback_url)
            return True
        except Exception as e:
            log.warning("Webhook registration failed: %s", e)
            return False

    def unregister_webhook(self):
        """Remove the webhook."""
        try:
            self.http.delete(f"{self.server}/api/webhook",
                             params={"agent": self.agent_id})
        except Exception:
            pass

    def get_agents(self) -> list:
        """List all agents currently in the world."""
        try:
            resp = self.http.get(f"{self.server}/api/agents")
            return resp.json().get("agents", [])
        except Exception:
            return []

    def check_inbox(self, peek: bool = False) -> list:
        """Check for queued messages."""
        try:
            params = {"agent": self.agent_id}
            if peek:
                params["peek"] = "true"
            resp = self.http.get(f"{self.server}/api/inbox", params=params)
            return resp.json().get("messages", [])
        except Exception:
            return []

    def close(self):
        self.http.close()


# ── Bridge State ─────────────────────────────────────────────────────────────

class BridgeState:
    """Shared mutable state for the bridge."""

    def __init__(self):
        self.current_state = "idle"
        self.current_task = None
        self.lock = threading.Lock()

    def update(self, state: str, task: str = None):
        with self.lock:
            self.current_state = state
            self.current_task = task

    def get(self):
        with self.lock:
            return self.current_state, self.current_task


# ── Heartbeat Thread ─────────────────────────────────────────────────────────

def heartbeat_loop(client: MiniverseClient, state: BridgeState, interval: int = 20):
    """Background thread that sends heartbeats every N seconds."""
    log.info("Heartbeat loop started (every %ds)", interval)
    while True:
        current_state, current_task = state.get()
        client.heartbeat(state=current_state, task=current_task)
        time.sleep(interval)


# ── Incoming Message Handler ─────────────────────────────────────────────────

def handle_incoming_message(from_agent: str, message: str, config: dict,
                            client: MiniverseClient, bridge_state: BridgeState):
    """Process an incoming message from another miniverse agent.

    Routes the message to Hermes via the webhook adapter (gateway mode)
    or falls back to CLI injection. The webhook adapter maintains proper
    sessions per agent — no race conditions, no session fighting.
    """
    log.info("Message from %s: %s", from_agent, message[:80])
    bridge_state.update("thinking", f"Reading message from {from_agent}")

    hermes_url = config.get("hermes_webhook_url")
    agent_id = config["agent_id"]

    if hermes_url:
        # Gateway mode: POST to the webhook adapter (proper sessions).
        # Use agent_id:from_agent as chat_id so each sender gets their own
        # session — otherwise all conversations pile into one.
        conversation_id = f"{agent_id}:{from_agent}"
        bridge_state.update("working", f"Responding to {from_agent}")
        try:
            resp = httpx.post(
                f"{hermes_url}/message",
                json={
                    "chat_id": conversation_id,
                    "message": message,
                    "from": from_agent,
                    "user_id": from_agent,
                },
                timeout=300,
            )
            data = resp.json()
            response = data.get("response", "")
            if not response:
                response = "(No response from agent)"
        except httpx.TimeoutException:
            response = "(Sorry, I took too long thinking about that!)"
            log.warning("Hermes webhook timed out for message from %s", from_agent)
        except Exception as e:
            response = f"(Error communicating with Hermes: {e})"
            log.error("Webhook POST failed: %s", e)
    else:
        # Fallback: CLI injection (single-agent only, no concurrency)
        log.warning("No HERMES_WEBHOOK_URL set — using CLI fallback (not recommended for multi-agent)")
        hermes_input = (
            f"[Miniverse message from agent '{from_agent}']: {message}\n\n"
            f"(You are in a Miniverse pixel world. Reply naturally.)"
        )
        cmd = config["hermes_cmd"].split() + [hermes_input]
        bridge_state.update("working", f"Responding to {from_agent}")

        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=120,
                cwd=os.path.expanduser("~"),
            )
            response = result.stdout.strip()
            if not response:
                response = "(I had trouble processing that, sorry!)"
        except subprocess.TimeoutExpired:
            response = "(Sorry, I took too long thinking about that!)"
        except FileNotFoundError:
            response = "(Hermes CLI not found — is it installed?)"
        except Exception as e:
            response = f"(Error: {e})"

    # Send response back via miniverse
    if response:
        client.message(from_agent, response[:500])
        if config.get("speak_responses"):
            client.speak(response[:200], to=from_agent)

    bridge_state.update("idle")
    log.info("Replied to %s: %s", from_agent, response[:80])


# ── Webhook HTTP Server ──────────────────────────────────────────────────────

def make_webhook_handler(config: dict, client: MiniverseClient, bridge_state: BridgeState):
    """Create an HTTP request handler for miniverse webhook callbacks."""

    class WebhookHandler(BaseHTTPRequestHandler):

        def do_POST(self):
            path = urlparse(self.path).path

            # Miniverse webhook callback (incoming messages)
            if path == "/webhook":
                content_length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(content_length)
                self.send_response(200)
                self.end_headers()

                try:
                    data = json.loads(body)
                    from_agent = data.get("from", "unknown")
                    message = data.get("message", "")
                    if message:
                        # Process in background thread to not block the webhook
                        t = threading.Thread(
                            target=handle_incoming_message,
                            args=(from_agent, message, config, client, bridge_state),
                            daemon=True,
                        )
                        t.start()
                except Exception as e:
                    log.error("Webhook parse error: %s", e)
                return

            # Hook relay endpoint (gateway hook sends state updates here)
            if path == "/hook":
                content_length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(content_length)
                self.send_response(200)
                self.end_headers()

                try:
                    data = json.loads(body)
                    event = data.get("event", "")
                    ctx = data.get("context", {})

                    if event == "agent:start":
                        bridge_state.update("thinking", ctx.get("message", "")[:60])
                    elif event == "agent:step":
                        tools = ctx.get("tool_names", [])
                        task = ", ".join(tools[:3]) if tools else "working"
                        bridge_state.update("working", task)
                    elif event == "agent:end":
                        response = ctx.get("response", "")
                        if response and config.get("speak_responses"):
                            client.speak(response[:200])
                        bridge_state.update("idle")
                    else:
                        log.debug("Unknown hook event: %s", event)
                except Exception as e:
                    log.error("Hook relay error: %s", e)
                return

            self.send_response(404)
            self.end_headers()

        def do_GET(self):
            if urlparse(self.path).path == "/health":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                state, task = bridge_state.get()
                self.wfile.write(json.dumps({
                    "ok": True,
                    "agent": config["agent_id"],
                    "state": state,
                    "task": task,
                    "server": config["server"],
                }).encode())
                return
            self.send_response(404)
            self.end_headers()

        def log_message(self, format, *args):
            log.debug(format, *args)

    return WebhookHandler


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Bridge between Hermes Agent and Miniverse",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--server", default=DEFAULT_CONFIG["server"],
                        help="Miniverse server URL")
    parser.add_argument("--agent", default=DEFAULT_CONFIG["agent_id"],
                        help="Agent ID in miniverse")
    parser.add_argument("--name", default=DEFAULT_CONFIG["agent_name"],
                        help="Display name")
    parser.add_argument("--color", default=DEFAULT_CONFIG["agent_color"],
                        help="Agent color (hex)")
    parser.add_argument("--port", type=int, default=DEFAULT_CONFIG["bridge_port"],
                        help="Local port for webhook callbacks")
    parser.add_argument("--hermes-webhook", default=DEFAULT_CONFIG["hermes_webhook_url"],
                        help="Hermes webhook adapter URL (e.g. http://localhost:4568)")
    parser.add_argument("--hermes-cmd", default=DEFAULT_CONFIG["hermes_cmd"],
                        help="CLI fallback command (used when --hermes-webhook not set)")
    parser.add_argument("--no-speak", action="store_true",
                        help="Don't speak responses in the world")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    config = {
        **DEFAULT_CONFIG,
        "server": args.server,
        "agent_id": args.agent,
        "agent_name": args.name,
        "agent_color": args.color,
        "bridge_port": args.port,
        "hermes_webhook_url": args.hermes_webhook or "",
        "hermes_cmd": args.hermes_cmd,
        "speak_responses": not args.no_speak,
    }

    # Initialize miniverse client
    client = MiniverseClient(
        server_url=config["server"],
        agent_id=config["agent_id"],
        agent_name=config["agent_name"],
        agent_color=config["agent_color"],
    )

    bridge_state = BridgeState()

    # Test connection
    log.info("Connecting to miniverse at %s ...", config["server"])
    if not client.heartbeat(state="idle"):
        log.error("Could not connect to miniverse server. Is it running?")
        sys.exit(1)
    log.info("✓ Connected as '%s' (%s)", config["agent_id"], config["agent_name"])

    # Show who's in the world
    agents = client.get_agents()
    if agents:
        names = [a.get("name", a.get("agent", "?")) for a in agents]
        log.info("Agents in world: %s", ", ".join(names))

    # Start heartbeat thread
    hb_thread = threading.Thread(
        target=heartbeat_loop,
        args=(client, bridge_state, config["heartbeat_interval"]),
        daemon=True,
    )
    hb_thread.start()

    # Start webhook HTTP server
    handler = make_webhook_handler(config, client, bridge_state)
    server = HTTPServer(("0.0.0.0", config["bridge_port"]), handler)

    # Register webhook with miniverse
    webhook_url = f"http://localhost:{config['bridge_port']}/webhook"
    # For remote miniverse servers, you'd need a public URL here
    # (e.g., via ngrok or a reverse proxy)
    client.register_webhook(webhook_url)

    log.info("Bridge running on port %d", config["bridge_port"])
    log.info("  Webhook endpoint: %s", webhook_url)
    log.info("  Hook relay:       http://localhost:%d/hook", config["bridge_port"])
    log.info("  Health check:     http://localhost:%d/health", config["bridge_port"])
    log.info("")
    log.info("Press Ctrl+C to stop")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down...")
        client.heartbeat(state="offline")
        client.unregister_webhook()
        client.close()
        server.shutdown()


if __name__ == "__main__":
    main()

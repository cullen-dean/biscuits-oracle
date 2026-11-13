"""Talks to the discobiscuits.net MCP server.

The server speaks JSON-RPC 2.0 over HTTP. We send a request like
{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {...}}
and get JSON back (sometimes wrapped as a server-sent event).

The API key is read from the DISCOBISCUITS_API_KEY environment variable
(loaded from .env locally, or from a GitHub Secret in Actions).
It is never printed.
"""

import itertools
import json
import os
import time

import requests
from dotenv import load_dotenv

MCP_URL = "https://discobiscuits.net/mcp"
MIN_SECONDS_BETWEEN_CALLS = 1.1  # stays under the 60 requests/minute limit
USER_AGENT = "biscuits-oracle/0.1 (personal non-commercial fan stats)"


class MCPError(Exception):
    pass


class MCPClient:
    def __init__(self):
        load_dotenv()
        key = os.environ.get("DISCOBISCUITS_API_KEY")
        if not key:
            raise MCPError("DISCOBISCUITS_API_KEY is missing. Put it in .env.")
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "User-Agent": USER_AGENT,
        })
        self._ids = itertools.count(1)
        self._last_call = 0.0
        self._initialized = False

    # ---- low level -------------------------------------------------------

    def _wait_for_rate_limit(self):
        wait = MIN_SECONDS_BETWEEN_CALLS - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()

    @staticmethod
    def _parse_body(resp):
        """Return the JSON-RPC message from a plain JSON or SSE response."""
        ctype = resp.headers.get("Content-Type", "")
        if "text/event-stream" in ctype:
            for line in resp.text.splitlines():
                if line.startswith("data:"):
                    data = line[5:].strip()
                    if data:
                        return json.loads(data)
            raise MCPError("Empty event-stream response")
        return resp.json()

    def _post(self, method, params=None, notify=False, retries=4):
        payload = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        if not notify:
            payload["id"] = next(self._ids)

        for attempt in range(retries):
            self._wait_for_rate_limit()
            resp = self.session.post(MCP_URL, json=payload, timeout=60)
            if resp.status_code == 429 or resp.status_code >= 500:
                time.sleep(5 * (attempt + 1))  # back off, then retry
                continue
            if resp.status_code in (401, 403):
                raise MCPError(f"Not authorized (HTTP {resp.status_code}). Check the API key in .env.")
            resp.raise_for_status()
            sid = resp.headers.get("Mcp-Session-Id")
            if sid:
                self.session.headers["Mcp-Session-Id"] = sid
            if notify:
                return None
            msg = self._parse_body(resp)
            if "error" in msg:
                raise MCPError(f"{method} failed: {msg['error']}")
            return msg.get("result")
        raise MCPError(f"{method} failed after {retries} tries (HTTP {resp.status_code})")

    # ---- public ----------------------------------------------------------

    def initialize(self):
        if self._initialized:
            return
        self._post("initialize", {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "biscuits-oracle", "version": "0.1"},
        })
        self._post("notifications/initialized", notify=True)
        self._initialized = True

    def list_tools(self):
        self.initialize()
        return self._post("tools/list")["tools"]

    def call(self, tool, **arguments):
        """Call a tool and return its result as Python data (parsed JSON when possible)."""
        self.initialize()
        result = self._post("tools/call", {"name": tool, "arguments": arguments})
        if result.get("isError"):
            raise MCPError(f"{tool} returned an error: {result.get('content')}")
        if "structuredContent" in result:
            return result["structuredContent"]
        texts = [c.get("text", "") for c in result.get("content", []) if c.get("type") == "text"]
        joined = "\n".join(texts)
        try:
            return json.loads(joined)
        except json.JSONDecodeError:
            return joined

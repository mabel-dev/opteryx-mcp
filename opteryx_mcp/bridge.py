"""Bridge a stdio MCP client to the Opteryx MCP endpoint (streamable HTTP).

Access tokens from authenticate.opteryx last five minutes, so a static header
cannot carry a client session. `Tokens` holds a user and their personal access
token and exchanges them for a fresh access token a minute before each one
expires -- the same `client_credentials` call opteryx-sqlalchemy and the
Terraform provider make. `Bridge` forwards each client message to `/mcp` with
the current token, and survives the server forgetting its session.

Standard library only, so `uvx opteryx-mcp` installs nothing else.
"""

import json
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any
from typing import Dict
from typing import Iterator
from typing import List
from typing import Optional
from typing import TextIO

# Re-mint this long before expiry, so a token never lapses mid-request.
REFRESH_MARGIN = 60
# A tool call can run a query; give it as long as the server does.
TIMEOUT = 300

Message = Dict[str, Any]


def log(message: str) -> None:
    print(f"[opteryx-mcp] {message}", file=sys.stderr, flush=True)


class Tokens:
    """Mints access tokens from a personal access token, caching each until
    near expiry."""

    def __init__(self, auth_url: str, user: str, token: str):
        self._url = f"{auth_url.rstrip('/')}/token"
        self._user = user
        self._pat = token
        self._access = ""
        self._expires = 0.0
        self._lock = threading.Lock()

    def get(self, force: bool = False) -> str:
        with self._lock:
            if force or time.time() >= self._expires - REFRESH_MARGIN:
                self._mint()
            return self._access

    def _mint(self) -> None:
        body = urllib.parse.urlencode(
            {
                "grant_type": "client_credentials",
                "client_id": self._user,
                "client_secret": self._pat,
            }
        ).encode()
        request = urllib.request.Request(
            self._url, data=body, headers={"Content-Type": "application/x-www-form-urlencoded"}
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.load(response)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:300]
            raise RuntimeError(f"token request failed ({exc.code}): {detail}") from exc
        lifetime = int(payload.get("expires_in", 300))
        self._access = payload["access_token"]
        self._expires = time.time() + lifetime
        log(f"minted access token, expires in {lifetime}s")


class Bridge:
    """Forwards JSON-RPC messages to the MCP endpoint and emits every reply."""

    def __init__(self, mcp_url: str, tokens: Tokens, out: Optional[TextIO] = None):
        self.url = mcp_url
        self.tokens = tokens
        self.out = out or sys.stdout
        self.session_id: Optional[str] = None
        self.protocol_version: Optional[str] = None
        self.initialize: Optional[Message] = None  # replayed if the server forgets the session
        self._session_lock = threading.Lock()
        self._out_lock = threading.Lock()

    def emit(self, message: Message) -> None:
        with self._out_lock:
            self.out.write(json.dumps(message, separators=(",", ":")) + "\n")
            self.out.flush()

    def _post(self, message: Message, token: str):
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {token}",
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        if self.protocol_version:
            headers["MCP-Protocol-Version"] = self.protocol_version
        request = urllib.request.Request(
            self.url, data=json.dumps(message).encode(), headers=headers, method="POST"
        )
        return urllib.request.urlopen(request, timeout=TIMEOUT)

    def send(self, message: Message, emit: bool = True, retried: bool = False) -> List[Message]:
        """POST one client message; emit every server message in the reply."""
        try:
            response = self._post(message, self.tokens.get())
        except urllib.error.HTTPError as exc:
            if exc.code == 401 and not retried:
                self.tokens.get(force=True)
                return self.send(message, emit, retried=True)
            if exc.code == 404 and self.session_id and self.initialize and not retried:
                # Session lost (instance recycled); open a new one and replay.
                self._reinitialize()
                return self.send(message, emit, retried=True)
            raise

        with response:
            session = response.headers.get("Mcp-Session-Id")
            if session:
                self.session_id = session
            content_type = response.headers.get("Content-Type", "")
            replies: List[Message] = []
            if content_type.startswith("text/event-stream"):
                replies = list(sse_messages(response))
            elif content_type.startswith("application/json"):
                body = response.read()
                if body.strip():
                    parsed = json.loads(body)
                    replies = parsed if isinstance(parsed, list) else [parsed]
        if emit:
            for reply in replies:
                self.emit(reply)
        return replies

    def _reinitialize(self) -> None:
        with self._session_lock:
            log("session lost; re-initializing")
            self.session_id = None
            self.send(self.initialize, emit=False, retried=True)
            initialized = {"jsonrpc": "2.0", "method": "notifications/initialized"}
            self.send(initialized, emit=False, retried=True)

    def handle(self, message: Message) -> None:
        """Forward one message; a failure reaches the client as an error, never silence."""
        if message.get("method") == "initialize":
            self.initialize = message
        try:
            replies = self.send(message)
            if message.get("method") == "initialize":
                for reply in replies:
                    version = (reply.get("result") or {}).get("protocolVersion")
                    if version:
                        self.protocol_version = version
        except Exception as exc:  # noqa: BLE001 - every failure must reach the client
            if isinstance(exc, urllib.error.HTTPError):
                detail = f"HTTP {exc.code}: {exc.read().decode(errors='replace')[:300]}"
            else:
                detail = str(exc)
            log(f"{message.get('method')} failed: {detail}")
            if "id" in message:
                error = {"code": -32603, "message": f"opteryx-mcp: {detail}"}
                self.emit({"jsonrpc": "2.0", "id": message["id"], "error": error})

    def serve(self, stdin: TextIO) -> None:
        """Read newline-delimited JSON-RPC from stdin until it closes."""
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            message = json.loads(line)
            if message.get("method") == "initialize":
                # Everything after depends on the session this opens.
                self.handle(message)
            else:
                threading.Thread(target=self.handle, args=(message,), daemon=True).start()


def sse_messages(response) -> Iterator[Message]:
    data: List[str] = []
    for raw in response:
        line = raw.decode().rstrip("\r\n")
        if line.startswith("data:"):
            data.append(line[5:].lstrip())
        elif line == "" and data:
            yield json.loads("\n".join(data))
            data = []
    if data:
        yield json.loads("\n".join(data))

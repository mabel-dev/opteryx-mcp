"""The bridge exists because access tokens last five minutes: these pin that it
re-mints before and after expiry, survives the server forgetting its session,
and turns every failure into a JSON-RPC error rather than a hang."""

import io
import json
import urllib.error
from email.message import Message

import pytest

from opteryx_mcp import bridge as bridge_module
from opteryx_mcp.bridge import Bridge
from opteryx_mcp.bridge import Tokens

INITIALIZE = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}


class Response(io.BytesIO):
    def __init__(self, body: bytes, content_type: str = "application/json", session=None):
        super().__init__(body)
        self.headers = Message()
        self.headers["Content-Type"] = content_type
        if session:
            self.headers["Mcp-Session-Id"] = session


def http_error(code: int, body: bytes = b"") -> urllib.error.HTTPError:
    return urllib.error.HTTPError("http://x", code, "error", Message(), io.BytesIO(body))


def ok(id_, session=None):
    body = json.dumps({"jsonrpc": "2.0", "id": id_, "result": {"protocolVersion": "2025-06-18"}})
    return Response(body.encode(), session=session)


class FakeServer:
    """Stands in for both `/token` and `/mcp`, recording what it was sent."""

    def __init__(self):
        self.mints = []
        self.posts = []
        self.mcp_replies = []  # each a response or an exception, used in order

    def urlopen(self, request, timeout=None):
        if request.full_url.endswith("/token"):
            self.mints.append(dict(item.split("=") for item in request.data.decode().split("&")))
            body = {"access_token": f"jwt-{len(self.mints)}", "expires_in": 300}
            return Response(json.dumps(body).encode())
        self.posts.append(
            {
                "message": json.loads(request.data),
                "auth": request.get_header("Authorization"),
                "session": request.get_header("Mcp-session-id"),
            }
        )
        reply = self.mcp_replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


@pytest.fixture
def server(monkeypatch):
    fake = FakeServer()
    monkeypatch.setattr(bridge_module.urllib.request, "urlopen", fake.urlopen)
    return fake


@pytest.fixture
def tokens():
    return Tokens("https://auth.test", "someone", "opt_pat_01")


@pytest.fixture
def out():
    return io.StringIO()


def emitted(out):
    return [json.loads(line) for line in out.getvalue().splitlines()]


# ── tokens ─────────────────────────────────────────────────────────────────────


def test_the_user_and_token_are_exchanged_as_client_credentials(server, tokens):
    tokens.get()
    assert server.mints == [
        {"grant_type": "client_credentials", "client_id": "someone", "client_secret": "opt_pat_01"}
    ]


def test_a_token_is_reused_until_near_expiry(server, tokens, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(bridge_module.time, "time", lambda: clock[0])

    assert tokens.get() == "jwt-1"
    clock[0] += 200
    assert tokens.get() == "jwt-1"
    # Inside the refresh margin of a 300s token: mint before it can lapse mid-call.
    clock[0] += 50
    assert tokens.get() == "jwt-2"


def test_a_rejected_token_is_reported_with_the_servers_reason(monkeypatch, tokens):
    def refuse(request, timeout=None):
        raise http_error(401, b'{"detail":"authentication failed"}')

    monkeypatch.setattr(bridge_module.urllib.request, "urlopen", refuse)
    with pytest.raises(RuntimeError, match="401.*authentication failed"):
        tokens.get()


# ── forwarding ─────────────────────────────────────────────────────────────────


def test_messages_carry_the_token_and_the_session(server, tokens, out):
    bridge = Bridge("https://mcp.test/mcp/", tokens, out=out)
    server.mcp_replies = [ok(1, session="s-1"), ok(2)]

    bridge.handle(INITIALIZE)
    bridge.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})

    assert [post["auth"] for post in server.posts] == ["Bearer jwt-1", "Bearer jwt-1"]
    assert [post["session"] for post in server.posts] == [None, "s-1"]
    assert [reply["id"] for reply in emitted(out)] == [1, 2]


def test_event_stream_replies_are_unwrapped(server, tokens, out):
    bridge = Bridge("https://mcp.test/mcp/", tokens, out=out)
    stream = (
        b"event: message\r\n"
        b'data: {"jsonrpc":"2.0","method":"notifications/progress","params":{}}\r\n\r\n'
        b"event: message\r\n"
        b'data: {"jsonrpc":"2.0","id":7,"result":{}}\r\n\r\n'
    )
    server.mcp_replies = [Response(stream, content_type="text/event-stream")]

    bridge.handle({"jsonrpc": "2.0", "id": 7, "method": "tools/call"})

    assert [reply.get("id") for reply in emitted(out)] == [None, 7]


def test_an_accepted_notification_emits_nothing(server, tokens, out):
    bridge = Bridge("https://mcp.test/mcp/", tokens, out=out)
    server.mcp_replies = [Response(b"")]

    bridge.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})

    assert emitted(out) == []


def test_serve_forwards_each_line_until_stdin_closes(server, tokens, out):
    bridge = Bridge("https://mcp.test/mcp/", tokens, out=out)
    server.mcp_replies = [ok(1, session="s-1")]

    bridge.serve(io.StringIO(json.dumps(INITIALIZE) + "\n\n"))

    assert [reply["id"] for reply in emitted(out)] == [1]


# ── recovery ───────────────────────────────────────────────────────────────────


def test_a_401_re_mints_and_retries_once(server, tokens, out):
    bridge = Bridge("https://mcp.test/mcp/", tokens, out=out)
    server.mcp_replies = [http_error(401), ok(1)]

    bridge.handle(INITIALIZE)

    assert [post["auth"] for post in server.posts] == ["Bearer jwt-1", "Bearer jwt-2"]
    assert emitted(out)[0]["id"] == 1


def test_a_forgotten_session_is_reopened_and_the_call_replayed(server, tokens, out):
    bridge = Bridge("https://mcp.test/mcp/", tokens, out=out)
    server.mcp_replies = [
        ok(1, session="old"),
        http_error(404),  # the instance that held "old" is gone
        ok(1, session="new"),  # replayed initialize
        Response(b""),  # replayed notifications/initialized
        ok(2),
    ]

    bridge.handle(INITIALIZE)
    bridge.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})

    methods = [post["message"].get("method") for post in server.posts]
    assert methods == [
        "initialize",
        "tools/list",
        "initialize",
        "notifications/initialized",
        "tools/list",
    ]
    assert server.posts[-1]["session"] == "new"
    # The client sees one reply per request: the replayed initialize stays internal.
    assert [reply["id"] for reply in emitted(out)] == [1, 2]


def test_a_failure_reaches_the_client_as_an_error_not_silence(server, tokens, out):
    bridge = Bridge("https://mcp.test/mcp/", tokens, out=out)
    server.mcp_replies = [http_error(500, b"boom")]

    bridge.handle({"jsonrpc": "2.0", "id": 9, "method": "tools/call"})

    (reply,) = emitted(out)
    assert reply["id"] == 9
    assert "HTTP 500: boom" in reply["error"]["message"]

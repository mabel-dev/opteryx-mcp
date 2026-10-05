"""`opteryx-mcp` command line.

    opteryx-mcp --user U          run the bridge (what an MCP client launches)
    opteryx-mcp login --user U    store U's personal access token, then check it
    opteryx-mcp check --user U    connect once and list the tools
    opteryx-mcp config --user U   print the Claude Desktop config entry

The user comes from `--user` or `OPTERYX_USER`. The token comes from
`OPTERYX_TOKEN`, or on macOS from the Keychain entry `login` writes.
"""

import argparse
import glob
import json
import os
import platform
import shutil
import subprocess
import sys
from typing import List
from typing import Optional

from opteryx_mcp import __version__
from opteryx_mcp.bridge import Bridge
from opteryx_mcp.bridge import Tokens
from opteryx_mcp.bridge import log

MCP_URL = "https://agent.opteryx.app/mcp/"
AUTH_URL = "https://authenticate.opteryx.app"
KEYCHAIN_SERVICE = "opteryx-mcp"
DESKTOP_CONFIG = "~/Library/Application Support/Claude/claude_desktop_config.json"


class UsageError(Exception):
    pass


def _keychain_token(user: str) -> str:
    try:
        return subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", user, "-w"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return ""


def _user(args: argparse.Namespace) -> str:
    user = args.user or os.environ.get("OPTERYX_USER", "")
    if not user:
        raise UsageError("no user: pass --user or set OPTERYX_USER")
    return user


def _token(user: str) -> str:
    token = os.environ.get("OPTERYX_TOKEN") or _keychain_token(user)
    if not token:
        raise UsageError(
            f"no token for {user}: set OPTERYX_TOKEN, or store one with "
            f"`opteryx-mcp login --user {user}`"
        )
    return token


def _bridge(user: str, out=None) -> Bridge:
    tokens = Tokens(os.environ.get("OPTERYX_AUTH_URL", AUTH_URL), user, _token(user))
    return Bridge(os.environ.get("OPTERYX_MCP_URL", MCP_URL), tokens, out=out)


def serve(args: argparse.Namespace) -> int:
    user = _user(args)
    bridge = _bridge(user)
    log(f"bridging stdio to {bridge.url} as {user}")
    bridge.serve(sys.stdin)
    return 0


def check(args: argparse.Namespace) -> int:
    """Open a session and list the tools, the same way a client would."""
    user = _user(args)
    bridge = _bridge(user)
    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "opteryx-mcp-check", "version": __version__},
        },
    }
    bridge.initialize = initialize
    replies = bridge.send(initialize, emit=False)
    result = next((reply["result"] for reply in replies if "result" in reply), None)
    if result is None:
        print(f"initialize failed: {replies}", file=sys.stderr)
        return 1
    bridge.protocol_version = result.get("protocolVersion")
    bridge.send({"jsonrpc": "2.0", "method": "notifications/initialized"}, emit=False)
    listed = bridge.send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, emit=False)
    tools = [tool["name"] for reply in listed for tool in reply.get("result", {}).get("tools", [])]
    print(f"Connected to {bridge.url} as {user}.")
    print(f"{len(tools)} tools: {', '.join(tools)}")
    return 0


def login(args: argparse.Namespace) -> int:
    """Store the token in the macOS Keychain; `security` prompts, so it never
    reaches argv or shell history."""
    user = _user(args)
    if platform.system() != "Darwin":
        raise UsageError("login stores tokens in the macOS Keychain; elsewhere set OPTERYX_TOKEN")
    print(f"Paste the personal access token for {user} (created in Studio settings).")
    stored = subprocess.run(
        ["security", "add-generic-password", "-U", "-s", KEYCHAIN_SERVICE, "-a", user, "-w"]
    )
    if stored.returncode != 0:
        return stored.returncode
    return check(args)


def _real(path: Optional[str]) -> bool:
    # A pyenv shim resolves the tool from the working directory's Python
    # version, and Desktop launches servers from `/`; it can fail there.
    return bool(path) and os.sep + "shims" + os.sep not in path


def find_uvx() -> Optional[str]:
    """An absolute uvx that runs from any directory, preferring PATH order."""
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        candidate = shutil.which("uvx", path=directory) if directory else None
        if _real(candidate):
            return candidate
    for pattern in (
        "~/.local/bin/uvx",
        "/opt/homebrew/bin/uvx",
        "/usr/local/bin/uvx",
        "~/.cargo/bin/uvx",
        "~/.pyenv/versions/*/bin/uvx",
    ):
        for candidate in sorted(glob.glob(os.path.expanduser(pattern)), reverse=True):
            if os.access(candidate, os.X_OK):
                return candidate
    return None


def desktop_entry(user: str, uvx: Optional[str]) -> dict:
    # Claude Desktop starts servers with a minimal PATH, so name uvx absolutely.
    return {
        "opteryx": {
            "command": uvx or "uvx",
            "args": ["opteryx-mcp", "--user", user],
        }
    }


def config(args: argparse.Namespace) -> int:
    user = _user(args)
    uvx = find_uvx()
    print(json.dumps({"mcpServers": desktop_entry(user, uvx)}, indent=2))
    print(
        f"\nMerge this into {DESKTOP_CONFIG} with Claude Desktop closed:"
        "\nit rewrites the file when it quits, dropping edits made while it runs.",
        file=sys.stderr,
    )
    if not uvx:
        print(
            "uvx was not found; install uv (https://docs.astral.sh/uv/) and replace"
            " `uvx` with its absolute path.",
            file=sys.stderr,
        )
    return 0


COMMANDS = {"serve": serve, "check": check, "login": login, "config": config}


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="opteryx-mcp", description="Connect MCP clients such as Claude to Opteryx."
    )
    parser.add_argument("command", nargs="?", default="serve", choices=COMMANDS)
    parser.add_argument("--user", help="the user the token belongs to (or OPTERYX_USER)")
    parser.add_argument("--version", action="version", version=f"opteryx-mcp {__version__}")
    args = parser.parse_args(argv)
    try:
        return COMMANDS[args.command](args)
    except UsageError as exc:
        print(f"opteryx-mcp: {exc}", file=sys.stderr)
        return 2
    except RuntimeError as exc:
        print(f"opteryx-mcp: {exc}", file=sys.stderr)
        return 1

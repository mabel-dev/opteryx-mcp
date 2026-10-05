import json

import pytest

from opteryx_mcp import cli


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ("OPTERYX_USER", "OPTERYX_TOKEN", "OPTERYX_MCP_URL", "OPTERYX_AUTH_URL"):
        monkeypatch.delenv(name, raising=False)
    # Never read the developer's real Keychain from a test.
    monkeypatch.setattr(cli, "_keychain_token", lambda user: "")


def test_no_user_is_a_usage_error(capsys):
    assert cli.main([]) == 2
    assert "no user" in capsys.readouterr().err


def test_no_token_says_how_to_store_one(capsys):
    assert cli.main(["--user", "someone"]) == 2
    assert "opteryx-mcp login --user someone" in capsys.readouterr().err


def test_the_token_env_var_wins_over_the_keychain(monkeypatch):
    monkeypatch.setenv("OPTERYX_TOKEN", "opt_env_01")
    monkeypatch.setattr(cli, "_keychain_token", lambda user: "opt_keychain_01")
    assert cli._token("someone") == "opt_env_01"


def test_the_keychain_is_read_under_the_user(monkeypatch):
    monkeypatch.setattr(cli, "_keychain_token", lambda user: f"opt_{user}_01")
    assert cli._token("someone") == "opt_someone_01"


def test_endpoints_can_be_pointed_elsewhere(monkeypatch):
    monkeypatch.setenv("OPTERYX_TOKEN", "opt_env_01")
    monkeypatch.setenv("OPTERYX_MCP_URL", "http://localhost:8080/mcp/")
    assert cli._bridge("someone").url == "http://localhost:8080/mcp/"


def test_config_names_uvx_absolutely_and_passes_the_user(monkeypatch, capsys):
    monkeypatch.setattr(cli, "find_uvx", lambda: "/opt/homebrew/bin/uvx")
    assert cli.main(["config", "--user", "someone"]) == 0
    captured = capsys.readouterr()
    entry = json.loads(captured.out)["mcpServers"]["opteryx"]
    assert entry == {
        "command": "/opt/homebrew/bin/uvx",
        "args": ["opteryx-mcp", "--user", "someone"],
    }
    # The trap that cost a restart cycle: edits made while Desktop runs are lost.
    assert "Claude Desktop closed" in captured.err


def test_login_refuses_off_macos(monkeypatch, capsys):
    monkeypatch.setattr(cli.platform, "system", lambda: "Linux")
    assert cli.main(["login", "--user", "someone"]) == 2
    assert "OPTERYX_TOKEN" in capsys.readouterr().err


def test_a_pyenv_shim_is_passed_over_for_the_real_uvx(monkeypatch, tmp_path):
    shims, real = tmp_path / ".pyenv" / "shims", tmp_path / "bin"
    for directory in (shims, real):
        directory.mkdir(parents=True)
        (directory / "uvx").write_text("#!/bin/sh\n")
        (directory / "uvx").chmod(0o755)
    monkeypatch.setenv("PATH", f"{shims}:{real}")
    assert cli.find_uvx() == str(real / "uvx")

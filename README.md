# opteryx-mcp

Connect Claude Desktop, Claude Code and other MCP clients to
[Opteryx](https://opteryx.app). Ask questions about your data in plain English;
the assistant finds datasets, reads schemas, queries, and checks Opteryx SQL,
all as you and against only the data you can see.

```bash
uvx opteryx-mcp login --user <user>
```

## Why a local bridge

The Opteryx MCP endpoint (`https://agent.opteryx.app/mcp/`) takes a bearer
token, and access tokens last five minutes. A token pasted into a client's
config stops working almost immediately. `opteryx-mcp` holds a **user** and a
**personal access token** instead, exchanges them for a fresh access token a
minute before each one expires, and forwards everything the client sends.
It also re-mints if a token is rejected, and re-opens the MCP session if the
server forgets it.

It has no dependencies beyond the Python standard library.

## Setup

### 1. Create a personal access token

In Opteryx Studio, open **Settings** and create a personal access token. It
looks like `opt_..._01`, lasts 90 days by default, and is shown once.

### 2. Store it

On macOS, `login` stores the token in the Keychain and checks it works.
`security` prompts for the token, so it never appears in shell history:

```bash
uvx opteryx-mcp login --user <user>
```

```
Connected to https://agent.opteryx.app/mcp/ as <user>.
7 tools: search_datasets, get_dataset_schema, query_dataset, profile_column, lookup_sql_syntax, search_docs, validate_sql
```

Run it again to replace an expired token. On other platforms, set
`OPTERYX_TOKEN` in the client's config instead.

### 3. Add it to your client

**Claude Desktop.** Print the config entry:

```bash
uvx opteryx-mcp config --user <user>
```

Then **quit Claude Desktop (Cmd-Q)** and merge the entry into
`~/Library/Application Support/Claude/claude_desktop_config.json`, keeping the
file's other keys. Desktop rewrites that file when it quits, so an entry added
while it is running is silently dropped. The entry looks like:

```json
{
  "mcpServers": {
    "opteryx": {
      "command": "/opt/homebrew/bin/uvx",
      "args": ["opteryx-mcp", "--user", "<user>"]
    }
  }
}
```

`uvx` is named by absolute path because Desktop starts servers with a minimal
`PATH`. Reopen Desktop and the server appears in a chat's **+** / tools menu,
under **Settings → Developer**, and in Code-tab sessions.

**Claude Code.**

```bash
claude mcp add opteryx -- uvx opteryx-mcp --user <user>
```

**Other clients.** Any client that launches stdio servers takes the same
command: `uvx opteryx-mcp --user <user>`.

## Tools

| Tool | What it does |
| --- | --- |
| `search_datasets` | Find datasets by name or column name |
| `get_dataset_schema` | Columns, types, statistics and sample rows |
| `query_dataset` | Run an OData v4 query; reads are capped at 200 rows |
| `profile_column` | The values a column actually holds, before you filter on one |
| `lookup_sql_syntax` | Confirm a function or statement exists in Opteryx SQL |
| `validate_sql` | Check SQL against the dialect and your schemas without running it |
| `search_docs` | Search the Opteryx documentation |

Every call runs as the user the token belongs to.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `OPTERYX_USER` | — | The user, if `--user` is not given |
| `OPTERYX_TOKEN` | Keychain entry `opteryx-mcp` / user | The personal access token |
| `OPTERYX_MCP_URL` | `https://agent.opteryx.app/mcp/` | MCP endpoint |
| `OPTERYX_AUTH_URL` | `https://authenticate.opteryx.app` | Where access tokens are minted |

## Troubleshooting

- `uvx opteryx-mcp check --user <user>` connects once and lists the tools, the
  same way a client would.
- Claude Desktop writes the bridge's log to
  `~/Library/Logs/Claude/mcp-server-opteryx.log`. It records each token minted
  and every request that fails.
- `token request failed (401)` means the user and token don't match, or the
  token has expired or been revoked. Create a new one and run `login` again.
- If the server never appears in Desktop, check that the entry is still in the
  config file. If you edited it while Desktop was running, it was overwritten.

## Development

```bash
pip install -e . pytest ruff
make test
make lint
```

Releases are published to PyPI from `v*` tags.

# Azure SQL MCP Server

A local [MCP](https://modelcontextprotocol.io) server that exposes a read-only
SQL surface over an Azure SQL Database, authenticating via your existing
`az login` session (Entra ID). Single-file Python, stdio transport.

## Tools

| Tool | Purpose |
|---|---|
| `list_tables(schema?)` | List base tables, excluding system schemas. |
| `describe_table(schema, table)` | Columns + primary key for a table. |
| `query(sql, max_rows=1000)` | Run a `SELECT` / `WITH` query, get rows as JSON. Hard cap 10 000 rows. |

`query` rejects writes/DDL/EXEC keywords as defense-in-depth — the real
boundary is the database role of the authenticated principal.

## Prerequisites

- Python 3.10+
- [Microsoft ODBC Driver 18 for SQL Server](https://learn.microsoft.com/sql/connect/odbc/download-odbc-driver-for-sql-server)
  (`brew install msodbcsql18` on macOS)
- Azure CLI signed in: `az login`

## Setup

```bash
git clone https://github.com/SChervonyi/mcp-azure-sql.git
cd mcp-azure-sql

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

cp .env.example .env
# edit .env: set MCP_DB_SERVER and MCP_DB_DATABASE
```

Smoke-test from the venv:

```bash
.venv/bin/python -c "import server; print(server.SERVER, server.DATABASE)"
```

## Register with your MCP client

### Claude Code

Add an entry under `mcpServers` in `~/.claude.json` (use absolute paths):

```json
{
  "mcpServers": {
    "azure-sql": {
      "command": "/absolute/path/to/mcp-azure-sql/.venv/bin/python",
      "args": ["/absolute/path/to/mcp-azure-sql/server.py"]
    }
  }
}
```

Then restart Claude Code (or any open session) and verify:

```bash
claude mcp list
# azure-sql: ... - ✓ Connected
```

#### Multiple databases (e.g. dev + staging)

Register one entry per database. Reuse the same `command`/`args` and pass
the connection details via the entry's `env` block — values supplied here
take precedence over `.env`, so each entry connects to its own database:

```json
{
  "mcpServers": {
    "azure-sql-dev": {
      "command": "/absolute/path/to/mcp-azure-sql/.venv/bin/python",
      "args": ["/absolute/path/to/mcp-azure-sql/server.py"],
      "env": {
        "MCP_DB_SERVER": "your-dev-server.database.windows.net",
        "MCP_DB_DATABASE": "your-dev-db"
      }
    },
    "azure-sql-stg": {
      "command": "/absolute/path/to/mcp-azure-sql/.venv/bin/python",
      "args": ["/absolute/path/to/mcp-azure-sql/server.py"],
      "env": {
        "MCP_DB_SERVER": "your-stg-server.database.windows.net",
        "MCP_DB_DATABASE": "your-stg-db"
      }
    }
  }
}
```

Each entry runs as an independent process and shows up as a separate tool
set in the client. Drop the per-entry `env` block to fall back to the
project-local `.env`.

If it shows `✗ Failed to connect`, run with debug logging:

```bash
claude --debug=mcp --debug-file=/tmp/mcp.log mcp list
grep azure-sql /tmp/mcp.log
```

### Other MCP clients

The server speaks JSON-RPC over stdio. Anything that can launch a stdio MCP
server works — point its config at `.venv/bin/python server.py` and pass
`MCP_DB_SERVER` / `MCP_DB_DATABASE` either via `.env` or the client's `env`
block (process env wins; `.env` is a fallback).

## Configuration

| Variable | Required | Default |
|---|---|---|
| `MCP_DB_SERVER` | yes | — (e.g. `your-server.database.windows.net`) |
| `MCP_DB_DATABASE` | yes | — |
| `MCP_DB_DRIVER` | no | `ODBC Driver 18 for SQL Server` |
| `MCP_DB_LOG_LEVEL` | no | `INFO` |

`.env` (gitignored) is loaded at server startup; values already in the process
environment take precedence.

## Auth

Tokens are fetched via `azure-identity`'s `AzureCliCredential` and refreshed
~5 min before expiry. Run `az login` once per device; no secrets are stored
in this repo.

## Design notes

See [`docs/design.md`](docs/design.md) for the read-only enforcement rules,
token plumbing, and JSON serialization details.

# Azure SQL MCP Server — Design

## Goal

A local MCP server that exposes a read-only SQL surface over an Azure SQL
Database, authenticating via the user's existing `az login` session.

## Stack

| Concern | Choice |
|---|---|
| MCP framework | Official `mcp` SDK, `FastMCP` high-level API |
| DB driver | `pyodbc` + Microsoft ODBC Driver 18 for SQL Server |
| Auth | `azure-identity` → `AzureCliCredential` |
| Sync/Async | Sync tools (pyodbc is sync; single-user stdio server) |
| Token plumbing | `attrs_before={1256: <packed token>}`; UTF-16-LE bytes prefixed with 4-byte length |

## Files

- `server.py` — MCP server, single file
- `requirements.txt` — pinned deps
- `.env.example` — template for required env vars; copy to `.env` (gitignored)
- `.venv/` — virtualenv created by the user; the MCP client config points at `.venv/bin/python`

## System prerequisite

Microsoft ODBC Driver 18 for SQL Server (e.g. installed via Homebrew on macOS).

## Configuration

All connection details come from environment variables. `server.py` loads a
project-local `.env` file at startup (without overriding values already set in
the process environment), so settings can come from either source.

| Variable | Required | Description |
|---|---|---|
| `MCP_DB_SERVER` | yes | Azure SQL server hostname (e.g. `<name>.database.windows.net`) |
| `MCP_DB_DATABASE` | yes | Database name |
| `MCP_DB_DRIVER` | no | ODBC driver name (default: `ODBC Driver 18 for SQL Server`) |
| `MCP_DB_LOG_LEVEL` | no | Python log level (default: `INFO`) |

The connection string omits `UID/PWD/Authentication` (required for token auth)
and uses `Encrypt=yes; TrustServerCertificate=no; Connection Timeout=30`.

## Token handling

- Scope: `https://database.windows.net/.default`
- Cache token in process; refresh when within 5 minutes of expiry
- On `pyodbc` auth failure: invalidate cache, reconnect once

## Tools

### `list_tables(schema?)`

Lists base tables, excluding `sys` and `INFORMATION_SCHEMA`.
Returns `{"tables": [{"schema": str, "table": str}, ...]}`.

### `describe_table(schema, table)`

Returns columns and primary key for a table.
Returns `{"schema", "table", "columns": [...], "primary_key": [...]}`.
Each column: `name, data_type, max_length, precision, scale, is_nullable, default, ordinal`.

### `query(sql, max_rows=1000)`

Executes a read-only query and returns rows as JSON.

**Read-only enforcement (defense-in-depth, not a security boundary):**
1. Strip `/* ... */` and `--` comments.
2. First keyword must be `SELECT` or `WITH`.
3. Reject any occurrence of: `INSERT|UPDATE|DELETE|MERGE|TRUNCATE|DROP|CREATE|ALTER|GRANT|REVOKE|EXEC(UTE)?|sp_*|xp_*|BACKUP|RESTORE|SHUTDOWN|BULK|OPENROWSET|OPENQUERY`.
4. Reject `SELECT ... INTO <target>` (write side-effect).

Real protection comes from the database role of the authenticated principal.

**Truncation:** `max_rows` defaults to 1000, hard-capped at 10000. Fetches `max_rows + 1` to detect truncation.

Returns `{"columns", "rows", "row_count", "truncated"}`.

## JSON serialization

Custom coercion for non-JSON-native types:

| Python type | JSON output |
|---|---|
| `datetime`, `date`, `time` | ISO 8601 string |
| `Decimal` | string (preserves precision) |
| `bytes`, `bytearray`, `memoryview` | base64 string |
| `UUID` | string |
| anything else | `str(v)` fallback |

## Logging

`stderr` only — `stdout` is reserved for JSON-RPC framing.

## MCP client registration

Example `mcpServers` entry (e.g. `~/.claude.json`):

```json
"azure-sql": {
  "command": "/path/to/repo/.venv/bin/python",
  "args": ["/path/to/repo/server.py"]
}
```

Env vars can be supplied either via the project-local `.env` file or via the
client's `env` block; values already in the process environment win.

## Verification plan

1. `pip install -r requirements.txt` succeeds in venv.
2. `python -c "import server"` (in venv) succeeds with `.env` configured.
3. JSON-RPC `initialize` + `notifications/initialized` + `tools/list` over stdio returns the three tools.
4. Live DB query is verified against the configured database after the ODBC driver is installed.

## Out of scope

- Schema / database introspection beyond `INFORMATION_SCHEMA`.
- Stored procedure execution.
- Multi-database support per server instance.
- Connection pooling (single connection is sufficient for one stdio client).

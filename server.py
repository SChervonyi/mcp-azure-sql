"""Azure SQL MCP server.

Exposes read-only access to an Azure SQL Database via Entra ID authentication
using the user's local `az login` session.

Tools:
  - list_tables(schema?)        list user tables (excludes system schemas)
  - describe_table(schema,table) columns + primary key
  - query(sql, max_rows=1000)   execute a SELECT/WITH query, return JSON

Run: python server.py   (stdio transport; speaks MCP JSON-RPC over stdin/stdout)
"""
from __future__ import annotations

import base64
import logging
import os
import re
import struct
import sys
import time as _time
from dataclasses import dataclass, field
from datetime import date, datetime, time as dt_time
from decimal import Decimal
from threading import Lock
from typing import Any, Optional
from uuid import UUID

import pyodbc
from azure.identity import AzureCliCredential
from mcp.server.fastmcp import FastMCP


# ---- Logging --------------------------------------------------------------
# stdout is reserved for JSON-RPC framing; never log to it.
logging.basicConfig(
    level=os.environ.get("MCP_DB_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("azure-sql-mcp")


# ---- Configuration --------------------------------------------------------
DEFAULT_DRIVER = "ODBC Driver 18 for SQL Server"


def _load_env_file(path: str) -> None:
    """Load KEY=value pairs from a .env file into os.environ (without overriding)."""
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)


_load_env_file(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(
            f"Required environment variable {name} is not set. "
            f"Set it in .env (see .env.example) or in your MCP client's env config."
        )
    return value


SERVER = _require_env("MCP_DB_SERVER")
DATABASE = _require_env("MCP_DB_DATABASE")
DRIVER = os.environ.get("MCP_DB_DRIVER", DEFAULT_DRIVER)

SQL_COPT_SS_ACCESS_TOKEN = 1256
TOKEN_SCOPE = "https://database.windows.net/.default"
TOKEN_REFRESH_BUFFER_SECONDS = 300

DEFAULT_MAX_ROWS = 1000
HARD_MAX_ROWS = 10_000


# ---- Read-only enforcement ------------------------------------------------
# Defense-in-depth, NOT a security boundary. Real protection comes from the
# database role of the authenticated principal.

_BAD_KEYWORDS = re.compile(
    r"\b(?:"
    r"INSERT|UPDATE|DELETE|MERGE|TRUNCATE|DROP|CREATE|ALTER|"
    r"GRANT|REVOKE|DENY|EXEC(?:UTE)?|BACKUP|RESTORE|SHUTDOWN|"
    r"BULK|OPENROWSET|OPENQUERY|"
    r"sp_\w+|xp_\w+"
    r")\b",
    re.IGNORECASE,
)
# `SELECT ... INTO new_table` creates a table; reject. Allow `INTO @var` (table var).
_INTO_TARGET = re.compile(r"\bINTO\s+(?!@)\w", re.IGNORECASE)


def _strip_sql_comments(sql: str) -> str:
    no_block = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    no_line = re.sub(r"--[^\n]*", " ", no_block)
    return no_line


def _validate_readonly(sql: str) -> None:
    stripped = _strip_sql_comments(sql).strip().rstrip(";").strip()
    if not stripped:
        raise ValueError("Empty SQL")

    first_kw = stripped.split(None, 1)[0].upper()
    if first_kw not in ("SELECT", "WITH"):
        raise ValueError(
            f"Read-only mode: query must begin with SELECT or WITH (got '{first_kw}')"
        )

    bad = _BAD_KEYWORDS.search(stripped)
    if bad:
        raise ValueError(
            f"Read-only mode: rejected keyword '{bad.group(0)}'"
        )

    if _INTO_TARGET.search(stripped):
        raise ValueError(
            "Read-only mode: 'SELECT ... INTO <target>' is not allowed"
        )


# ---- Connection management ------------------------------------------------
@dataclass
class _TokenCache:
    token: Optional[str] = None
    expires_on: float = 0.0


class _DbConnection:
    def __init__(self) -> None:
        self._credential = AzureCliCredential()
        self._token = _TokenCache()
        self._conn: Optional[pyodbc.Connection] = None
        self._lock = Lock()

    def _fetch_token(self) -> str:
        now = _time.time()
        if (
            self._token.token is None
            or now >= self._token.expires_on - TOKEN_REFRESH_BUFFER_SECONDS
        ):
            log.info("Fetching Azure access token via AzureCliCredential")
            tk = self._credential.get_token(TOKEN_SCOPE)
            self._token.token = tk.token
            self._token.expires_on = float(tk.expires_on)
        return self._token.token

    @staticmethod
    def _pack_token(token: str) -> bytes:
        token_bytes = token.encode("utf-16-le")
        return struct.pack("=i", len(token_bytes)) + token_bytes

    def _open(self) -> pyodbc.Connection:
        token = self._fetch_token()
        token_struct = self._pack_token(token)
        conn_str = (
            f"Driver={{{DRIVER}}};"
            f"Server=tcp:{SERVER},1433;"
            f"Database={DATABASE};"
            "Encrypt=yes;TrustServerCertificate=no;Connection Timeout=30;"
        )
        log.info("Connecting to %s / %s", SERVER, DATABASE)
        return pyodbc.connect(
            conn_str,
            attrs_before={SQL_COPT_SS_ACCESS_TOKEN: token_struct},
        )

    def get(self) -> pyodbc.Connection:
        with self._lock:
            if self._conn is None:
                self._conn = self._open()
                return self._conn

            try:
                self._conn.cursor().execute("SELECT 1").fetchone()
                return self._conn
            except pyodbc.Error as e:
                log.warning("Existing connection failed liveness check (%s); reconnecting", e)
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._token = _TokenCache()
                self._conn = self._open()
                return self._conn


_db = _DbConnection()


# ---- JSON-friendly value coercion -----------------------------------------
def _coerce(v: Any) -> Any:
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, dt_time):
        return v.isoformat()
    if isinstance(v, (bytes, bytearray, memoryview)):
        return base64.b64encode(bytes(v)).decode("ascii")
    if isinstance(v, UUID):
        return str(v)
    return str(v)


# ---- MCP server -----------------------------------------------------------
mcp = FastMCP("azure-sql-mcp")


@mcp.tool()
def list_tables(schema: Optional[str] = None) -> dict:
    """List user tables in the database.

    Excludes system schemas (sys, INFORMATION_SCHEMA).

    Args:
        schema: Optional schema name to filter by (e.g. "dbo").

    Returns:
        {"tables": [{"schema": ..., "table": ...}, ...]}
    """
    conn = _db.get()
    cur = conn.cursor()
    sql = (
        "SELECT TABLE_SCHEMA, TABLE_NAME "
        "FROM INFORMATION_SCHEMA.TABLES "
        "WHERE TABLE_TYPE = 'BASE TABLE' "
        "AND TABLE_SCHEMA NOT IN ('sys', 'INFORMATION_SCHEMA')"
    )
    params: list[Any] = []
    if schema:
        sql += " AND TABLE_SCHEMA = ?"
        params.append(schema)
    sql += " ORDER BY TABLE_SCHEMA, TABLE_NAME"

    cur.execute(sql, *params)
    rows = cur.fetchall()
    return {"tables": [{"schema": r[0], "table": r[1]} for r in rows]}


@mcp.tool()
def describe_table(schema: str, table: str) -> dict:
    """Describe a table's columns and primary key.

    Args:
        schema: Schema name (e.g. "dbo").
        table: Table name.

    Returns:
        {
          "schema", "table",
          "columns": [{name, data_type, max_length, precision, scale,
                       is_nullable, default, ordinal}, ...],
          "primary_key": [col, ...]
        }
    """
    conn = _db.get()
    cur = conn.cursor()

    cur.execute(
        "SELECT COLUMN_NAME, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH, "
        "       NUMERIC_PRECISION, NUMERIC_SCALE, IS_NULLABLE, "
        "       COLUMN_DEFAULT, ORDINAL_POSITION "
        "FROM INFORMATION_SCHEMA.COLUMNS "
        "WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ? "
        "ORDER BY ORDINAL_POSITION",
        schema,
        table,
    )
    cols: list[dict] = []
    for r in cur.fetchall():
        cols.append({
            "name": r[0],
            "data_type": r[1],
            "max_length": r[2],
            "precision": r[3],
            "scale": r[4],
            "is_nullable": (str(r[5]).upper() == "YES"),
            "default": r[6],
            "ordinal": r[7],
        })

    if not cols:
        raise ValueError(f"Table not found: {schema}.{table}")

    cur.execute(
        "SELECT k.COLUMN_NAME "
        "FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS t "
        "JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE k "
        "  ON t.CONSTRAINT_NAME = k.CONSTRAINT_NAME "
        " AND t.TABLE_SCHEMA = k.TABLE_SCHEMA "
        " AND t.TABLE_NAME   = k.TABLE_NAME "
        "WHERE t.CONSTRAINT_TYPE = 'PRIMARY KEY' "
        "  AND t.TABLE_SCHEMA = ? AND t.TABLE_NAME = ? "
        "ORDER BY k.ORDINAL_POSITION",
        schema,
        table,
    )
    pk = [r[0] for r in cur.fetchall()]

    return {
        "schema": schema,
        "table": table,
        "columns": cols,
        "primary_key": pk,
    }


@mcp.tool()
def query(sql: str, max_rows: int = DEFAULT_MAX_ROWS) -> dict:
    """Execute a read-only SQL query and return rows as JSON.

    Only SELECT and WITH (CTE) statements are allowed. Write/DDL/EXEC
    keywords are rejected before execution. This is a defense-in-depth
    sanity check, not a security boundary — true protection should come
    from the database role of the authenticated user.

    Args:
        sql: A SELECT or WITH ... SELECT statement.
        max_rows: Maximum rows to return (default 1000, hard max 10000).

    Returns:
        {
          "columns": [name, ...],
          "rows": [[v, ...], ...],
          "row_count": N,
          "truncated": bool
        }
    """
    _validate_readonly(sql)
    capped = max(1, min(int(max_rows), HARD_MAX_ROWS))

    conn = _db.get()
    cur = conn.cursor()
    cur.execute(sql)

    if cur.description is None:
        return {"columns": [], "rows": [], "row_count": 0, "truncated": False}

    fetched = cur.fetchmany(capped + 1)
    truncated = len(fetched) > capped
    rows = fetched[:capped]
    columns = [c[0] for c in cur.description]
    out_rows = [[_coerce(v) for v in row] for row in rows]

    return {
        "columns": columns,
        "rows": out_rows,
        "row_count": len(out_rows),
        "truncated": truncated,
    }


def main() -> None:
    log.info("Starting Azure SQL MCP server (server=%s, db=%s)", SERVER, DATABASE)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()

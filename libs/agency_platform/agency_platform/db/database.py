"""MySQL access layer (local MySQL 8.4, OCI HeatWave MySQL in prod).

A thin async wrapper over an aiomysql pool, so repositories write plain SQL
and never deal with driver details:

  * rows are dicts; JSON columns come back decoded; DATETIMEs come back as
    timezone-aware UTC (the session time zone is pinned to UTC)
  * parameters use DB-API placeholders: positional ``%s`` with a sequence, or
    named ``%(name)s`` with a mapping. UUIDs, dicts/lists (-> JSON) and aware
    datetimes (-> naive UTC) are adapted automatically
  * ``execute`` returns the number of *matched* rows (CLIENT.FOUND_ROWS), so
    "UPDATE ... affected 0" really means "not found", like other databases
  * ``transaction()`` gives a connection with the same methods inside
    BEGIN/COMMIT (ROLLBACK on any exception)

Schema is owned by migrations/ (applied by the migrate job), never by app
startup -- N replicas booting at once must not race on DDL.
"""
import asyncio
import json
import ssl
import uuid
from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

import aiomysql
from pymysql.constants import CLIENT, FIELD_TYPE

Params = Sequence[Any] | Mapping[str, Any] | None

_SCHEMES = {"mysql", "mysql+aiomysql", "mysql+asyncmy", "mysql+pymysql"}
SSL_MODES = {"DISABLED", "PREFERRED", "REQUIRED", "VERIFY_CA", "VERIFY_IDENTITY"}


# --------------------------------------------------------------------------- connection URL
@dataclass(frozen=True)
class ConnectionInfo:
    host: str
    port: int
    user: str
    password: str
    database: str
    ssl_mode: str = "DISABLED"
    ssl_ca: str | None = None

    def ssl_context(self) -> ssl.SSLContext | None:
        if self.ssl_mode in {"DISABLED", "PREFERRED"}:
            return None
        ctx = ssl.create_default_context(cafile=self.ssl_ca) if self.ssl_ca else ssl.create_default_context()
        if self.ssl_mode == "REQUIRED":  # encrypted, server cert not verified (HeatWave default certs)
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        elif self.ssl_mode == "VERIFY_CA":
            ctx.check_hostname = False
        return ctx


def parse_url(url: str) -> ConnectionInfo:
    """``mysql://user:pass@host:3306/db?ssl-mode=REQUIRED&ssl-ca=/etc/ssl/heatwave-ca.pem``

    ssl-mode follows the MySQL client names: DISABLED | REQUIRED | VERIFY_CA | VERIFY_IDENTITY.
    """
    parts = urlsplit(url)
    if parts.scheme not in _SCHEMES:
        raise ValueError(f"DATABASE_URL must be a mysql:// URL (got scheme '{parts.scheme}')")
    if not parts.hostname or not parts.path.strip("/"):
        raise ValueError("DATABASE_URL must include a host and a database name")
    query = {k.replace("_", "-").lower(): v[-1] for k, v in parse_qs(parts.query).items()}
    mode = query.get("ssl-mode", "DISABLED").upper()
    if mode not in SSL_MODES:
        raise ValueError(f"ssl-mode must be one of {sorted(SSL_MODES)}")
    return ConnectionInfo(
        host=parts.hostname,
        port=parts.port or 3306,
        user=unquote(parts.username or ""),
        password=unquote(parts.password or ""),
        database=parts.path.strip("/"),
        ssl_mode=mode,
        ssl_ca=query.get("ssl-ca"),
    )


# --------------------------------------------------------------------------- value adaptation
def _param(value: Any) -> Any:
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime) and value.tzinfo is not None:
        return value.astimezone(UTC).replace(tzinfo=None)
    if isinstance(value, dict | list):
        return json.dumps(value, default=str)
    return value


def adapt_params(params: Params) -> Any:
    if params is None:
        return None
    if isinstance(params, Mapping):
        return {k: _param(v) for k, v in params.items()}
    return tuple(_param(v) for v in params)


def _decode_rows(description: Sequence[Sequence[Any]] | None, rows: Iterable[Sequence[Any]]) -> list[dict[str, Any]]:
    if not description:
        return []
    names = [d[0] for d in description]
    json_cols = {i for i, d in enumerate(description) if d[1] == FIELD_TYPE.JSON}
    out: list[dict[str, Any]] = []
    for row in rows:
        rec: dict[str, Any] = {}
        for i, value in enumerate(row):
            if value is not None:
                if i in json_cols:
                    value = json.loads(value.decode() if isinstance(value, bytes | bytearray) else value)
                elif isinstance(value, datetime) and value.tzinfo is None:
                    value = value.replace(tzinfo=UTC)
            rec[names[i]] = value
        out.append(rec)
    return out


def placeholders(n: int) -> str:
    """'%s, %s, %s' for an IN (...) list of n values (n >= 1)."""
    if n < 1:
        raise ValueError("IN list needs at least one value")
    return ", ".join(["%s"] * n)


# --------------------------------------------------------------------------- query API
class _Queries:
    """fetch / fetchrow / fetchval / execute / insert / executemany."""

    async def _run(self, sql: str, params: Any, mode: str, many: bool = False) -> Any:  # pragma: no cover
        raise NotImplementedError

    async def fetch(self, sql: str, params: Params = None) -> list[dict[str, Any]]:
        return await self._run(sql, adapt_params(params), "all")

    async def fetchrow(self, sql: str, params: Params = None) -> dict[str, Any] | None:
        rows = await self._run(sql, adapt_params(params), "all")
        return rows[0] if rows else None

    async def fetchval(self, sql: str, params: Params = None) -> Any:
        row = await self.fetchrow(sql, params)
        return next(iter(row.values())) if row else None

    async def execute(self, sql: str, params: Params = None) -> int:
        """Returns the number of matched rows."""
        return await self._run(sql, adapt_params(params), "count")

    async def insert(self, sql: str, params: Params = None) -> int:
        """Returns LAST_INSERT_ID() (MySQL has no RETURNING)."""
        return await self._run(sql, adapt_params(params), "lastid")

    async def executemany(self, sql: str, seq: Iterable[Params]) -> int:
        rows = [adapt_params(p) for p in seq]
        if not rows:
            return 0
        return await self._run(sql, rows, "count", many=True)


async def _on_cursor(raw: aiomysql.Connection, sql: str, params: Any, mode: str, many: bool) -> Any:
    async with raw.cursor() as cur:
        if many:
            await cur.executemany(sql, params)
        else:
            await cur.execute(sql, params)
        if mode == "all":
            return _decode_rows(cur.description, await cur.fetchall())
        if mode == "lastid":
            return cur.lastrowid
        return cur.rowcount


class Connection(_Queries):
    """One connection inside a transaction."""

    def __init__(self, raw: aiomysql.Connection) -> None:
        self._raw = raw

    async def _run(self, sql: str, params: Any, mode: str, many: bool = False) -> Any:
        return await _on_cursor(self._raw, sql, params, mode, many)


class Database(_Queries):
    def __init__(self, pool: aiomysql.Pool) -> None:
        self._pool = pool

    @asynccontextmanager
    async def _acquire(self) -> AsyncIterator[aiomysql.Connection]:
        async with self._pool.acquire() as raw:
            try:
                yield raw
            except (asyncio.CancelledError, TimeoutError):
                # A query interrupted mid-flight leaves the protocol in an unknown
                # state: close so the pool never hands this connection out again.
                raw.close()
                raise

    async def _run(self, sql: str, params: Any, mode: str, many: bool = False) -> Any:
        async with self._acquire() as raw:
            return await _on_cursor(raw, sql, params, mode, many)

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[Connection]:
        """Pin one autocommit connection (session state such as GET_LOCK, temp tables)."""
        async with self._acquire() as raw:
            yield Connection(raw)

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[Connection]:
        async with self._acquire() as raw:
            await raw.begin()
            try:
                yield Connection(raw)
            except BaseException:
                if not raw.closed:
                    await raw.rollback()
                raise
            await raw.commit()

    async def close(self) -> None:
        self._pool.close()
        await self._pool.wait_closed()


async def connect(url: str, *, min_size: int = 1, max_size: int = 10, statement_timeout_ms: int = 30_000) -> Database:
    info = parse_url(url)
    pool = await aiomysql.create_pool(
        host=info.host,
        port=info.port,
        user=info.user,
        password=info.password,
        db=info.database,
        minsize=min_size,
        maxsize=max_size,
        autocommit=True,
        charset="utf8mb4",
        client_flag=CLIENT.FOUND_ROWS,
        ssl=info.ssl_context(),
        connect_timeout=10,
        pool_recycle=1800,  # below HeatWave/NAT idle timeouts
        # UTC everywhere; cap runaway SELECTs (MySQL's max_execution_time applies to SELECT only).
        init_command=f"SET time_zone = '+00:00', max_execution_time = {int(statement_timeout_ms)}",
    )
    return Database(pool)

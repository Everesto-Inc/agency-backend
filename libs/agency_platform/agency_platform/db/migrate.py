"""Forward-only SQL migration runner for MySQL / HeatWave.

    python -m agency_platform.db.migrate [migrations_dir]

Applies migrations/NNNN_*.sql in order and records them in schema_migrations.
A named lock (GET_LOCK) makes concurrent runs safe (two deploys racing). Run as
a one-shot job before app rollout: the docker-compose `migrate` service
locally, an Argo CD PreSync Job in Kubernetes.

MySQL DDL is NOT transactional (each statement commits implicitly), so a
migration that fails halfway can't be rolled back. Therefore:
  * migrations are written to be re-runnable (CREATE TABLE IF NOT EXISTS ...);
  * "already exists" errors (duplicate column/index/table) are tolerated, which
    makes ADD COLUMN / ADD INDEX effectively idempotent on a re-run;
  * a version is recorded only after all its statements succeeded -- fix
    forward and re-run.
"""
import asyncio
import hashlib
import logging
import re
import sys
from pathlib import Path

import pymysql

from agency_platform.config.settings import get_settings
from agency_platform.db.database import Connection, connect
from agency_platform.observability.logging import configure_logging

logger = logging.getLogger("migrate")

_LOCK = "agency_schema_migrations"
_LOCK_TIMEOUT_S = 120
# ER_TABLE_EXISTS_ERROR, ER_DUP_FIELDNAME, ER_DUP_KEYNAME, ER_CANT_DROP_FIELD_OR_KEY, ER_FK_DUP_NAME
_IDEMPOTENT_ERRORS = {1050, 1060, 1061, 1091, 1826}


def split_statements(sql: str) -> list[str]:
    """Strip `-- ` comments, then split on ';' at end of line.
    (Migrations contain no procedures/triggers and no '--' inside string literals.)"""
    no_comments = re.sub(r"(^|\s)--(\s.*)?$", r"\1", sql, flags=re.MULTILINE)
    out = []
    for chunk in re.split(r";\s*$", no_comments, flags=re.MULTILINE):
        body = "\n".join(line.rstrip() for line in chunk.splitlines() if line.strip()).strip()
        if body:
            out.append(body)
    return out


async def _apply(conn: Connection, version: str, statements: list[str]) -> None:
    for stmt in statements:
        try:
            await conn.execute(stmt)
        except pymysql.err.MySQLError as exc:
            code = exc.args[0] if exc.args else None
            if code in _IDEMPOTENT_ERRORS:
                logger.warning("migration statement already applied", extra={"version": version, "code": code})
                continue
            raise RuntimeError(f"migration {version} failed on: {stmt[:200]}... ({exc})") from exc


async def migrate(migrations_dir: Path, database_url: str | None = None) -> list[str]:
    url = database_url or get_settings().database_url.get_secret_value()
    db = await connect(url, min_size=1, max_size=1)
    applied_now: list[str] = []
    try:
        async with db.connection() as conn:  # one session: GET_LOCK is per-connection
            if await conn.fetchval("SELECT GET_LOCK(%s, %s) AS ok", (_LOCK, _LOCK_TIMEOUT_S)) != 1:
                raise RuntimeError("could not acquire the migration lock (another migration running?)")
            try:
                await conn.execute(
                    """CREATE TABLE IF NOT EXISTS schema_migrations (
                           version VARCHAR(255) NOT NULL PRIMARY KEY,
                           checksum CHAR(64) NOT NULL,
                           applied_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
                       ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"""
                )
                rows = await conn.fetch("SELECT version, checksum FROM schema_migrations")
                applied = {r["version"]: r["checksum"] for r in rows}
                for path in sorted(migrations_dir.glob("[0-9][0-9][0-9][0-9]_*.sql")):  # noqa: ASYNC240 - one-shot CLI
                    sql = path.read_text(encoding="utf-8")
                    checksum = hashlib.sha256(sql.encode()).hexdigest()
                    version = path.stem
                    if version in applied:
                        if applied[version] != checksum:
                            raise RuntimeError(f"Applied migration {version} was modified; add a new migration instead")
                        continue
                    await _apply(conn, version, split_statements(sql))
                    await conn.execute(
                        "INSERT INTO schema_migrations (version, checksum) VALUES (%s, %s)", (version, checksum)
                    )
                    logger.info("applied migration", extra={"version": version})
                    applied_now.append(version)
            finally:
                await conn.fetchval("SELECT RELEASE_LOCK(%s) AS released", (_LOCK,))
    finally:
        await db.close()
    return applied_now


def main() -> None:
    settings = get_settings()
    configure_logging("migrate", settings.log_level, settings.log_json)
    directory = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("migrations")
    applied = asyncio.run(migrate(directory))
    logger.info("migrations complete", extra={"applied": len(applied)})


if __name__ == "__main__":
    main()

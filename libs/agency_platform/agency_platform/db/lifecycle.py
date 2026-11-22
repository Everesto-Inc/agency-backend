"""Process-wide Database handle, opened in the app lifespan / job entrypoint."""
from typing import TYPE_CHECKING

from agency_platform.db.database import Database, connect

if TYPE_CHECKING:  # settings imports db.database (URL validation): avoid the cycle
    from agency_platform.config.settings import PlatformSettings

_db: Database | None = None


async def init_db(settings: "PlatformSettings") -> Database:
    global _db
    if _db is None:
        _db = await connect(
            settings.database_url.get_secret_value(),
            min_size=settings.db_pool_min_size,
            max_size=settings.db_pool_max_size,
            statement_timeout_ms=settings.db_statement_timeout_ms,
        )
    return _db


def get_db() -> Database:
    if _db is None:
        raise RuntimeError("Database not initialised; call init_db() in the app lifespan")
    return _db


async def close_db() -> None:
    global _db
    if _db is not None:
        await _db.close()
        _db = None

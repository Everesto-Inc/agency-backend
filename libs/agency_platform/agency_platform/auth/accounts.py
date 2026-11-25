"""User accounts: repository (MySQL) + cached directory lookups (Redis).

Every authenticated request resolves the account through `AccountDirectory`
(Redis cache, short TTL, explicit invalidation on change) so subscription state
is current without hitting the database on every call.
"""
import hashlib
import json
import secrets
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pymysql
from redis.asyncio import Redis

from agency_platform.auth.passwords import hash_password, verify_password
from agency_platform.cache import rkey
from agency_platform.config.registry import plans
from agency_platform.db import Database
from agency_platform.errors import ConflictError, UnauthorizedError

_CACHE_TTL_S = 60
_ER_DUP_ENTRY = 1062


@dataclass(frozen=True)
class Account:
    id: str
    email: str
    role: str
    plan: str
    plan_expires_at: datetime | None
    is_active: bool

    @property
    def effective_plan(self) -> str:
        return plans().effective_plan(self.plan, self.plan_expires_at)


def _row_to_account(row: dict[str, Any]) -> Account:
    return Account(
        id=row["id"],
        email=row["email"],
        role=row["role"],
        plan=row["plan"],
        plan_expires_at=row["plan_expires_at"],
        is_active=bool(row["is_active"]),
    )


def canonical_uuid(value: str) -> str:
    """Validate + normalise a UUID string (raises ValueError if malformed)."""
    return str(uuid.UUID(value))


_ACCOUNT_COLS = "id, email, role, plan, plan_expires_at, is_active"


class AccountRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def create(self, email: str, password: str, plan: str, role: str = "user") -> Account:
        hashed = await hash_password(password)
        user_id = str(uuid.uuid4())
        try:
            await self._db.execute(
                "INSERT INTO users (id, email, hashed_password, plan, role) VALUES (%s, %s, %s, %s, %s)",
                (user_id, email.lower(), hashed, plan, role),
            )
        except pymysql.err.IntegrityError as exc:
            if exc.args and exc.args[0] == _ER_DUP_ENTRY:
                raise ConflictError("Email already registered") from None
            raise
        return Account(id=user_id, email=email.lower(), role=role, plan=plan, plan_expires_at=None, is_active=True)

    async def authenticate(self, email: str, password: str) -> Account:
        row = await self._db.fetchrow(
            f"SELECT {_ACCOUNT_COLS}, hashed_password FROM users WHERE email = %s", (email.lower(),)
        )
        ok = await verify_password(password, row["hashed_password"] if row else None)
        if not ok or row is None or not row["is_active"]:
            raise UnauthorizedError("Invalid email or password")
        return _row_to_account(row)

    async def get(self, user_id: str) -> Account | None:
        row = await self._db.fetchrow(f"SELECT {_ACCOUNT_COLS} FROM users WHERE id = %s", (canonical_uuid(user_id),))
        return _row_to_account(row) if row else None

    async def get_by_email(self, email: str) -> Account | None:
        row = await self._db.fetchrow(f"SELECT {_ACCOUNT_COLS} FROM users WHERE email = %s", (email.lower(),))
        return _row_to_account(row) if row else None

    async def set_plan(self, user_id: str, plan: str, expires_at: datetime | None) -> Account | None:
        uid = canonical_uuid(user_id)
        matched = await self._db.execute(
            "UPDATE users SET plan = %s, plan_expires_at = %s WHERE id = %s", (plan, expires_at, uid)
        )
        return await self.get(uid) if matched else None

    async def set_password(self, user_id: str, password: str) -> bool:
        """New password (hashed). False = no such user. Callers revoke the user's sessions."""
        hashed = await hash_password(password)
        matched = await self._db.execute(
            "UPDATE users SET hashed_password = %s WHERE id = %s", (hashed, canonical_uuid(user_id))
        )
        return bool(matched)

    async def list_accounts(self, plan: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        """Newest first; plan='none' = accounts waiting for activation."""
        where, args = ("WHERE plan = %s", (plan,)) if plan else ("", ())
        return await self._db.fetch(
            f"SELECT {_ACCOUNT_COLS}, created_at FROM users {where} ORDER BY created_at DESC LIMIT %s", (*args, limit)
        )

    async def set_role(self, user_id: str, role: str) -> None:
        await self._db.execute("UPDATE users SET role = %s WHERE id = %s", (role, canonical_uuid(user_id)))

    async def set_active(self, user_id: str, active: bool) -> None:
        await self._db.execute("UPDATE users SET is_active = %s WHERE id = %s", (active, canonical_uuid(user_id)))

    # ---- data subject rights (GDPR-style) ----------------------------------
    async def export(self, user_id: str) -> dict:
        uid = canonical_uuid(user_id)
        account = await self._db.fetchrow(
            "SELECT id, email, role, plan, plan_expires_at, created_at FROM users WHERE id = %s", (uid,)
        )
        runs = await self._db.fetch(
            """SELECT request_id, topic, session_id, prompt, answer, status, prompt_tokens, completion_tokens,
                      cost_usd, feedback_score, feedback_comment, created_at
               FROM agent_runs WHERE user_id = %s ORDER BY created_at""",
            (uid,),
        )
        usage = await self._db.fetch(
            """SELECT day, topic, runs, prompt_tokens, completion_tokens, cost_usd
               FROM usage_daily WHERE user_id = %s ORDER BY day, topic""",
            (uid,),
        )
        return {"account": account, "runs": runs, "usage_daily": usage}

    async def erase(self, user_id: str) -> None:
        """Delete the account and personal data. Per-call LLM ledger rows are kept
        anonymised (user_id nulled) so aggregate cost accounting stays correct."""
        uid = canonical_uuid(user_id)
        async with self._db.transaction() as conn:
            await conn.execute(
                """DELETE c FROM eval_cases c JOIN agent_runs r ON r.request_id = c.source_request_id
                   WHERE r.user_id = %s""",
                (uid,),
            )
            await conn.execute(
                """DELETE e FROM eval_results e JOIN agent_runs r ON r.request_id = e.request_id
                   WHERE r.user_id = %s""",
                (uid,),
            )
            await conn.execute("UPDATE llm_calls SET user_id = NULL WHERE user_id = %s", (uid,))
            await conn.execute("DELETE FROM users WHERE id = %s", (uid,))  # cascades runs, usage, tokens, audit


class AccountDirectory:
    """Read-through cache in front of AccountRepository."""

    def __init__(self, repo: AccountRepository, redis: Redis) -> None:
        self._repo = repo
        self._redis = redis

    @staticmethod
    def _key(user_id: str) -> str:
        return rkey(f"acct:{user_id}")

    async def get(self, user_id: str) -> Account | None:
        cached = await self._redis.get(self._key(user_id))
        if cached:
            data = json.loads(cached)
            exp = data.get("plan_expires_at")
            data["plan_expires_at"] = datetime.fromisoformat(exp) if exp else None
            return Account(**data)
        try:
            account = await self._repo.get(user_id)
        except ValueError:  # malformed UUID in token subject
            return None
        if account is not None:
            await self._redis.set(self._key(user_id), json.dumps(asdict(account), default=str), ex=_CACHE_TTL_S)
        return account

    async def invalidate(self, user_id: str) -> None:
        await self._redis.delete(self._key(user_id))


# --------------------------------------------------------------------------
# Refresh tokens: opaque, stored hashed, rotated on every use. Presenting an
# already-rotated token means it leaked -> the whole token family is revoked.
# --------------------------------------------------------------------------


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class RefreshTokenStore:
    def __init__(self, db: Database, ttl_days: int) -> None:
        self._db = db
        self._ttl = timedelta(days=ttl_days)

    async def issue(self, user_id: str, family_id: str | None = None) -> str:
        token = secrets.token_urlsafe(48)
        await self._db.execute(
            """INSERT INTO refresh_tokens (user_id, token_hash, family_id, expires_at)
               VALUES (%s, %s, %s, %s)""",
            (canonical_uuid(user_id), _hash_token(token), family_id or str(uuid.uuid4()), datetime.now(UTC) + self._ttl),
        )
        return token

    async def rotate(self, token: str) -> tuple[str, str]:
        """Consume a refresh token; return (user_id, new_refresh_token).

        Presenting an already-rotated token revokes its whole family. The
        revocation is committed *before* the error is raised (raising inside
        the transaction would roll it back)."""
        reuse = False
        async with self._db.transaction() as conn:
            row = await conn.fetchrow(
                """SELECT id, user_id, family_id, expires_at, revoked_at
                   FROM refresh_tokens WHERE token_hash = %s FOR UPDATE""",
                (_hash_token(token),),
            )
            if row is None:
                raise UnauthorizedError("Invalid refresh token")
            if row["revoked_at"] is not None:
                await conn.execute(
                    "UPDATE refresh_tokens SET revoked_at = UTC_TIMESTAMP(6) WHERE family_id = %s AND revoked_at IS NULL",
                    (row["family_id"],),
                )
                reuse = True
            elif row["expires_at"] <= datetime.now(UTC):
                raise UnauthorizedError("Refresh token expired")
            else:
                await conn.execute("UPDATE refresh_tokens SET revoked_at = UTC_TIMESTAMP(6) WHERE id = %s", (row["id"],))
        if reuse:
            raise UnauthorizedError("Refresh token reuse detected; session revoked")
        return row["user_id"], await self.issue(row["user_id"], family_id=row["family_id"])

    async def revoke(self, token: str) -> None:
        family = await self._db.fetchval(
            "SELECT family_id FROM refresh_tokens WHERE token_hash = %s", (_hash_token(token),)
        )
        if family:
            await self._db.execute(
                "UPDATE refresh_tokens SET revoked_at = UTC_TIMESTAMP(6) WHERE family_id = %s AND revoked_at IS NULL",
                (family,),
            )

    async def revoke_all(self, user_id: str) -> None:
        await self._db.execute(
            "UPDATE refresh_tokens SET revoked_at = UTC_TIMESTAMP(6) WHERE user_id = %s AND revoked_at IS NULL",
            (canonical_uuid(user_id),),
        )

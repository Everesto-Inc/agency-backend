"""Account repository: password change + account listing (fake DB, real bcrypt)."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
os.environ.setdefault("CONFIG_DIR", str(ROOT / "config"))

from agency_platform.auth.accounts import AccountRepository
from agency_platform.auth.passwords import verify_password

UID = "6f1c2c1e-3b9d-4a51-9a52-0b5a4c1d2e3f"


class FakeDb:
    def __init__(self, matched: int = 1) -> None:
        self.calls: list[tuple[str, tuple]] = []
        self.matched = matched

    async def execute(self, sql, params=None):
        self.calls.append((sql, params))
        return self.matched

    async def fetch(self, sql, params=None):
        self.calls.append((sql, params))
        return [{"id": UID, "email": "a@b.c", "role": "user", "plan": "none"}]


async def test_set_password_stores_a_hash_not_the_password():
    db = FakeDb()
    assert await AccountRepository(db).set_password(UID, "new-secret-password") is True
    sql, (hashed, uid) = db.calls[0]
    assert sql.startswith("UPDATE users SET hashed_password") and uid == UID
    assert hashed != "new-secret-password" and await verify_password("new-secret-password", hashed)


async def test_set_password_unknown_user():
    assert await AccountRepository(FakeDb(matched=0)).set_password(UID, "new-secret-password") is False


async def test_list_accounts_filters_by_plan():
    db = FakeDb()
    repo = AccountRepository(db)
    assert (await repo.list_accounts("none", 10))[0]["plan"] == "none"
    assert "WHERE plan = %s" in db.calls[0][0] and db.calls[0][1] == ("none", 10)
    await repo.list_accounts()
    assert "WHERE" not in db.calls[1][0] and db.calls[1][1] == (100,)

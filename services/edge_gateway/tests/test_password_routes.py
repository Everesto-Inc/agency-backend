"""Password change (user) and reset / account list (admin) — handlers called with fakes, no DB."""
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from agency_platform.errors import NotFoundError, UnauthorizedError
from edge_gateway import routes_admin
from edge_gateway.routes_auth import PasswordChange, change_password

UID = uuid.UUID("6f1c2c1e-3b9d-4a51-9a52-0b5a4c1d2e3f")


class Repo:
    def __init__(self, password_ok=True, exists=True):
        self.password_ok, self.exists, self.new = password_ok, exists, None

    async def authenticate(self, email, password):
        if not self.password_ok:
            raise UnauthorizedError("Invalid email or password")

    async def set_password(self, user_id, password):
        self.new = password
        return self.exists

    async def list_accounts(self, plan=None, limit=100):
        return [{"id": str(UID), "email": "new@x.io", "role": "user", "plan": plan or "none",
                 "plan_expires_at": None, "is_active": 1, "created_at": datetime.now(UTC)}]


class Refresh:
    revoked: list[str] = []

    def __init__(self, *_a):
        pass

    async def revoke_all(self, user_id):
        Refresh.revoked.append(user_id)


class Limiter:
    async def hit(self, *_a):
        return None


USER = SimpleNamespace(id=str(UID), email="me@x.io")


async def test_change_password_needs_the_current_one_and_logs_out_everywhere():
    Refresh.revoked = []
    repo = Repo()
    await change_password(PasswordChange(current_password="old-password", new_password="brand-new-pass"),
                          USER, repo, Refresh(), Limiter())
    assert repo.new == "brand-new-pass" and Refresh.revoked == [str(UID)]

    repo = Repo(password_ok=False)
    with pytest.raises(UnauthorizedError):
        await change_password(PasswordChange(current_password="wrong", new_password="brand-new-pass"),
                              USER, repo, Refresh(), Limiter())
    assert repo.new is None


def test_new_password_rules():
    with pytest.raises(ValueError):
        PasswordChange(current_password="x", new_password="short")
    with pytest.raises(ValueError):
        routes_admin.PasswordReset(new_password="é" * 40)  # 80 bytes > bcrypt's 72


@pytest.fixture
def admin_fakes(monkeypatch):
    repo = Repo()
    Refresh.revoked = []
    monkeypatch.setattr(routes_admin, "AccountRepository", lambda _db: repo)
    monkeypatch.setattr(routes_admin, "RefreshTokenStore", Refresh)
    monkeypatch.setattr(routes_admin, "get_db", lambda: None)
    return repo


async def test_admin_reset_password(admin_fakes):
    await routes_admin.reset_password(UID, routes_admin.PasswordReset(new_password="temporary-pass"), None)
    assert admin_fakes.new == "temporary-pass" and Refresh.revoked == [str(UID)]
    admin_fakes.exists = False
    with pytest.raises(NotFoundError):
        await routes_admin.reset_password(UID, routes_admin.PasswordReset(new_password="temporary-pass"), None)


async def test_admin_lists_accounts_waiting_for_activation(admin_fakes):
    rows = await routes_admin.list_users(None, plan="none", limit=10)
    assert rows[0].plan == "none" and rows[0].is_active is True

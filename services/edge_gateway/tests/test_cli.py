"""Ops CLI: create-admin / create-service-user are idempotent (they run as Argo CD hooks on every Sync)."""
import pytest

from agency_platform.errors import ConflictError
from edge_gateway import cli


class _Account:
    id = "u-1"


class _Repo:
    existing: set[str] = set()
    passwords: dict[str, str] = {}

    def __init__(self, _db) -> None:
        pass

    async def create(self, email, password, plan, role=None):
        if email in self.existing:
            raise ConflictError("exists")
        self.existing.add(email)
        return _Account()

    async def get_by_email(self, email):
        return _Account() if email in self.existing else None

    async def set_password(self, user_id, password):
        self.passwords[user_id] = password
        return True


class _Refresh:
    revoked: list[str] = []

    def __init__(self, *_a) -> None:
        pass

    async def revoke_all(self, user_id):
        self.revoked.append(user_id)


@pytest.fixture(autouse=True)
def _fake_db(monkeypatch):
    _Repo.existing = set()
    async def _init(_settings):
        return object()
    async def _close():
        return None
    monkeypatch.setattr(cli, "AccountRepository", _Repo)
    monkeypatch.setattr(cli, "RefreshTokenStore", _Refresh)
    _Repo.passwords, _Refresh.revoked = {}, []
    monkeypatch.setenv("NEW_PASSWORD", "another-long-password")
    monkeypatch.setattr(cli, "init_db", _init)
    monkeypatch.setattr(cli, "close_db", _close)
    monkeypatch.setenv("ADMIN_PASSWORD", "a-long-password")
    monkeypatch.setenv("SERVICE_USER_PASSWORD", "a-long-password")


async def test_create_admin_twice_is_fine(capsys):
    await cli._create_admin("admin@example.com")
    await cli._create_admin("admin@example.com")
    out = capsys.readouterr().out
    assert "admin created: u-1" in out and "admin admin@example.com already exists" in out


async def test_create_service_user_twice_is_fine(capsys):
    await cli._create_service_user("evals@agency.internal")
    await cli._create_service_user("evals@agency.internal")
    assert "already exists" in capsys.readouterr().out


def test_short_password_is_refused(monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "short")
    with pytest.raises(SystemExit):
        cli._password("ADMIN_PASSWORD", "")


async def test_set_password_changes_it_and_logs_the_user_out(capsys):
    await cli._create_admin("admin@example.com")
    await cli._set_password("admin@example.com")
    assert _Repo.passwords == {"u-1": "another-long-password"} and _Refresh.revoked == ["u-1"]
    assert "password changed" in capsys.readouterr().out


async def test_set_password_unknown_user():
    with pytest.raises(SystemExit):
        await cli._set_password("nobody@example.com")

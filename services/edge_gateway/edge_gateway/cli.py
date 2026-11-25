"""Ops CLI (run inside the edge container: docker compose run --rm -it edge-gateway python -m edge_gateway.cli ...).

    create-admin admin@example.com              # password from ADMIN_PASSWORD or prompt (idempotent)
    set-plan <user-email> pro
    set-password <user-email>                   # new password from NEW_PASSWORD or prompt; logs the user out
    create-service-user evals@agency.internal   # plan 'internal', for eval/smoke jobs (idempotent)
"""
import asyncio
import getpass
import os
import sys

from agency_platform.auth.accounts import AccountRepository, RefreshTokenStore
from agency_platform.config.settings import get_settings
from agency_platform.db import close_db, init_db
from agency_platform.errors import ConflictError


def _password(env: str, prompt: str) -> str:
    password = os.environ.get(env) or getpass.getpass(prompt)
    if len(password) < 10:
        sys.exit("password too short")
    return password


async def _create_admin(email: str) -> None:
    password = _password("ADMIN_PASSWORD", "Admin password (min 10 chars): ")
    try:
        repo = AccountRepository(await init_db(get_settings()))
        try:
            account = await repo.create(email, password, plan="pro", role="admin")
        except ConflictError:  # re-run (e.g. the Argo CD hook on every Sync): keep the account and its password
            print(f"admin {email} already exists")
            return
        print(f"admin created: {account.id}")
    finally:
        await close_db()


async def _create_service_user(email: str) -> None:
    password = _password("SERVICE_USER_PASSWORD", "Service user password (min 10 chars): ")
    try:
        repo = AccountRepository(await init_db(get_settings()))
        try:
            account = await repo.create(email, password, plan="internal", role="service")
        except ConflictError:
            print(f"service user {email} already exists")
            return
        print(f"service user created: {account.id} (plan=internal)")
    finally:
        await close_db()


async def _set_plan(email: str, plan: str) -> None:
    try:
        repo = AccountRepository(await init_db(get_settings()))
        account = await repo.get_by_email(email)
        if account is None:
            sys.exit(f"no user {email}")
        await repo.set_plan(account.id, plan, None)
        print(f"{email}: plan={plan}")
    finally:
        await close_db()


async def _set_password(email: str) -> None:
    password = _password("NEW_PASSWORD", "New password (min 10 chars): ")
    try:
        db = await init_db(get_settings())
        repo = AccountRepository(db)
        account = await repo.get_by_email(email)
        if account is None:
            sys.exit(f"no user {email}")
        await repo.set_password(account.id, password)
        await RefreshTokenStore(db, get_settings().refresh_token_ttl_days).revoke_all(account.id)
        print(f"{email}: password changed, sessions revoked")
    finally:
        await close_db()


def main() -> None:
    if len(sys.argv) >= 3 and sys.argv[1] == "create-admin":
        asyncio.run(_create_admin(sys.argv[2]))
    elif len(sys.argv) >= 3 and sys.argv[1] == "create-service-user":
        asyncio.run(_create_service_user(sys.argv[2]))
    elif len(sys.argv) >= 3 and sys.argv[1] == "set-password":
        asyncio.run(_set_password(sys.argv[2]))
    elif len(sys.argv) >= 4 and sys.argv[1] == "set-plan":
        asyncio.run(_set_plan(sys.argv[2], sys.argv[3]))
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()

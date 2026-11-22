"""Ops CLI (run inside the edge container: docker compose run --rm -it edge-gateway python -m edge_gateway.cli ...).

    create-admin admin@example.com              # password from ADMIN_PASSWORD or prompt
    set-plan <user-email> pro
    create-service-user evals@agency.internal   # plan 'internal', for eval/smoke jobs (idempotent)
"""
import asyncio
import getpass
import os
import sys

from agency_platform.auth.accounts import AccountRepository
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
        account = await AccountRepository(await init_db(get_settings())).create(email, password, plan="pro", role="admin")
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


def main() -> None:
    if len(sys.argv) >= 3 and sys.argv[1] == "create-admin":
        asyncio.run(_create_admin(sys.argv[2]))
    elif len(sys.argv) >= 3 and sys.argv[1] == "create-service-user":
        asyncio.run(_create_service_user(sys.argv[2]))
    elif len(sys.argv) >= 4 and sys.argv[1] == "set-plan":
        asyncio.run(_set_plan(sys.argv[2], sys.argv[3]))
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()

"""Password hashing (bcrypt). bcrypt only uses the first 72 bytes, so longer
passwords are rejected at the API boundary instead of silently truncated."""
import asyncio

import bcrypt

MAX_PASSWORD_BYTES = 72
_ROUNDS = 12

# Verified against when the user doesn't exist, so a login attempt costs the
# same either way and response timing doesn't reveal which emails are registered.
_DUMMY_HASH = bcrypt.hashpw(b"timing-equaliser", bcrypt.gensalt(_ROUNDS))


async def hash_password(password: str) -> str:
    # bcrypt is CPU-bound (~250ms); keep it off the event loop.
    hashed = await asyncio.to_thread(bcrypt.hashpw, password.encode(), bcrypt.gensalt(_ROUNDS))
    return hashed.decode()


async def verify_password(password: str, hashed: str | None) -> bool:
    target = hashed.encode() if hashed else _DUMMY_HASH
    ok = await asyncio.to_thread(bcrypt.checkpw, password.encode(), target)
    return ok and hashed is not None

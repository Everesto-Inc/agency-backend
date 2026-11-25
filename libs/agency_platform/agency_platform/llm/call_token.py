"""Per-run call tokens: how the model gateway knows who an LLM call is for.

A topic service mints one short-lived JWT per agent run, signed with its own
client secret (`kid` = client id), and hands it to the agent framework *as the
OpenAI API key*. Every framework (LangGraph, CrewAI, AutoGen, crawl4ai, ...)
supports a custom api_key + base_url, while few support custom headers -- so
this gives uniform attribution (user, plan, topic, run) for every call without
any framework-specific patching.

Security properties:
  * Provider API keys live only in the model gateway, never in topic services.
  * Each client has its own secret; a token can't claim another client's topic.
  * Tokens expire with the run timeout, limiting replay if one leaks.
"""
import time
import uuid
from dataclasses import dataclass

import jwt

AUDIENCE = "model-gateway"
_ALG = "HS256"
_LEEWAY_S = 10


@dataclass(frozen=True)
class CallClaims:
    client_id: str  # calling service; also the topic it may bill to
    user_id: str
    plan: str
    request_id: str
    session_id: str | None = None
    config_draft: int | None = None   # Admin → Experiments: this call uses a config draft (signed, can't be forged)


def mint_call_token(claims: CallClaims, secret: str, ttl_s: int) -> str:
    now = int(time.time())
    payload = {
        "iss": claims.client_id,
        "aud": AUDIENCE,
        "sub": claims.user_id,
        "plan": claims.plan,
        "rid": claims.request_id,
        "sid": claims.session_id,
        **({"cfg": claims.config_draft} if claims.config_draft else {}),
        "jti": uuid.uuid4().hex,
        "iat": now,
        "exp": now + ttl_s,
    }
    return jwt.encode(payload, secret, algorithm=_ALG, headers={"kid": claims.client_id})


def unverified_client_id(token: str) -> str | None:
    try:
        return jwt.get_unverified_header(token).get("kid")
    except jwt.PyJWTError:
        return None


def verify_call_token(token: str, secret: str) -> CallClaims:
    """Raises jwt.PyJWTError on any problem."""
    payload = jwt.decode(
        token,
        secret,
        algorithms=[_ALG],
        audience=AUDIENCE,
        leeway=_LEEWAY_S,
        options={"require": ["exp", "iat", "sub", "iss", "aud", "rid"]},
    )
    return CallClaims(
        client_id=payload["iss"],
        user_id=payload["sub"],
        plan=payload.get("plan", ""),
        request_id=payload["rid"],
        session_id=payload.get("sid"),
        config_draft=payload.get("cfg") if isinstance(payload.get("cfg"), int) else None,
    )

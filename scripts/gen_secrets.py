#!/usr/bin/env python3
"""Generate the agency's *internal* secrets (never provider API keys).

    python3 scripts/gen_secrets.py --env-file .env   # local setup: create/complete .env for docker compose
    python3 scripts/gen_secrets.py --check .env      # show which variables are set / missing (values hidden)
    python3 scripts/gen_secrets.py                   # print dotenv lines (e.g. to paste into Infisical)
    python3 scripts/gen_secrets.py --eddsa           # also an Ed25519 JWT keypair (staging/prod), base64 PEM

--env-file never overwrites a value that is already set: it only adds what's
missing, plus empty GEMINI_API_KEY / TAVILY_API_KEY lines for you to fill in.
Standard library only (plus `cryptography` for --eddsa).
"""
from __future__ import annotations

import base64
import os
import re
import secrets
import sys
from pathlib import Path

CLIENTS = ["EDGE", "EV_RESEARCH", "PRODUCT_FINDER", "FINANCE", "QUALITY"]
PROVIDER_KEYS = ["GEMINI_API_KEY", "TAVILY_API_KEY"]
OPTIONAL_KEYS = ["LANGSMITH_API_KEY"]


def generate(eddsa: bool = False) -> list[str]:
    lines = [f"JWT_SECRET_KEY={secrets.token_urlsafe(48)}"]
    lines += [f"{c}_GATEWAY_SECRET={secrets.token_urlsafe(48)}" for c in CLIENTS]
    lines.append(f"EVAL_USER_PASSWORD={secrets.token_urlsafe(24)}")
    if eddsa:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        key = Ed25519PrivateKey.generate()
        priv = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                 serialization.NoEncryption())
        pub = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        lines = [line for line in lines if not line.startswith("JWT_SECRET_KEY")]
        lines += [f"JWT_PRIVATE_KEY={base64.b64encode(priv).decode()}", f"JWT_PUBLIC_KEY={base64.b64encode(pub).decode()}"]
    return lines


def _keys(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    out = {}
    for line in path.read_text().splitlines():
        m = re.match(r"^\s*([A-Z0-9_]+)\s*=(.*)$", line)
        if m:
            out[m.group(1)] = m.group(2).strip()
    return out


def write_env(path: Path) -> None:
    existing = _keys(path)
    missing = [ln for ln in generate() if ln.split("=", 1)[0] not in existing]
    missing += [f"{k}=" for k in PROVIDER_KEYS if k not in existing]
    if missing:
        old = os.umask(0o077)
        try:
            with path.open("a") as f:
                if path.stat().st_size and not path.read_text().endswith("\n"):
                    f.write("\n")
                f.write("\n".join(missing) + "\n")
        finally:
            os.umask(old)
    path.chmod(0o600)
    print(f"{path}: added {len(missing)} variable(s); existing values untouched.")
    check(path)


def check(path: Path) -> int:
    values = _keys(path)
    expected = [ln.split("=", 1)[0] for ln in generate()] + PROVIDER_KEYS
    gaps = 0
    for k in expected + OPTIONAL_KEYS:
        state = "set" if values.get(k) else ("optional" if k in OPTIONAL_KEYS else "MISSING")
        gaps += state == "MISSING"
        print(f"  {state:<8} {k}")
    if gaps:
        print(f"Fill the MISSING values in {path}, then: docker compose up -d --build")
    return 1 if gaps else 0


def main() -> None:
    args = sys.argv[1:]
    if "--env-file" in args:
        write_env(Path(args[args.index("--env-file") + 1]))
    elif "--check" in args:
        sys.exit(check(Path(args[args.index("--check") + 1])))
    else:
        print("\n".join(generate(eddsa="--eddsa" in args)))


if __name__ == "__main__":
    main()

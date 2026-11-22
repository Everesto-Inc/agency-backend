"""MySQL layer: URL parsing, parameter adaptation, row decoding, migration splitting."""
import json
import uuid
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from pymysql.constants import FIELD_TYPE

from agency_platform.config.settings import PlatformSettings
from agency_platform.db.database import _decode_rows, adapt_params, parse_url, placeholders
from agency_platform.db.migrate import split_statements

ROOT = Path(__file__).resolve().parents[3]


def test_parse_url_with_tls_options():
    info = parse_url("mysql://agency:p%40ss@10.0.1.5:3306/agency?ssl-mode=verify_ca&ssl-ca=/etc/ca.pem")
    assert (info.host, info.port, info.user, info.password, info.database) == ("10.0.1.5", 3306, "agency", "p@ss", "agency")
    assert info.ssl_mode == "VERIFY_CA" and info.ssl_ca == "/etc/ca.pem"
    assert parse_url("mysql://u:p@h/agency?ssl_mode=REQUIRED").ssl_context() is not None
    assert parse_url("mysql+aiomysql://u:p@db/agency").ssl_context() is None  # DISABLED by default (local)


@pytest.mark.parametrize("url", ["postgresql://u:p@h/db", "mysql://u:p@h/", "mysql://u:p@h/db?ssl-mode=bogus"])
def test_parse_url_rejects_invalid(url):
    with pytest.raises(ValueError):
        parse_url(url)


def test_prod_requires_tls_to_database():
    base = {"environment": "prod", "jwt_public_key": "-----BEGIN PUBLIC KEY-----\nx\n-----END PUBLIC KEY-----"}
    with pytest.raises(ValueError, match="ssl-mode"):
        PlatformSettings(**base, database_url="mysql://u:p@heatwave:3306/agency")
    PlatformSettings(**base, database_url="mysql://u:p@heatwave:3306/agency?ssl-mode=REQUIRED")


def test_params_are_adapted():
    uid = uuid.uuid4()
    aware = datetime(2026, 1, 1, 12, 0, tzinfo=timezone(timedelta(hours=2)))
    out = adapt_params((uid, aware, {"a": 1}, ["x"], Decimal("1.5"), None, True))
    assert out == (str(uid), datetime(2026, 1, 1, 10, 0), '{"a": 1}', '["x"]', Decimal("1.5"), None, True)
    assert adapt_params({"u": uid}) == {"u": str(uid)}
    assert adapt_params(None) is None


def test_rows_decode_json_and_utc():
    desc = [("id", FIELD_TYPE.LONG), ("meta", FIELD_TYPE.JSON), ("at", FIELD_TYPE.DATETIME), ("note", FIELD_TYPE.VAR_STRING)]
    rows = _decode_rows(desc, [(1, json.dumps({"k": [1]}).encode(), datetime(2026, 1, 1), None)])
    assert rows == [{"id": 1, "meta": {"k": [1]}, "at": datetime(2026, 1, 1, tzinfo=UTC), "note": None}]


def test_placeholders():
    assert placeholders(3) == "%s, %s, %s"
    with pytest.raises(ValueError):
        placeholders(0)


def test_migrations_split_into_statements():
    sql = "-- header; with semicolon\nCREATE TABLE a (x INT); -- trailing\n\nCREATE TABLE b (\n  y INT -- c\n);\n"
    assert split_statements(sql) == ["CREATE TABLE a (x INT)", "CREATE TABLE b (\n  y INT\n)"]


def test_repo_migrations_are_mysql():
    """Guard against Postgres-isms creeping back into migrations."""
    for path in sorted((ROOT / "migrations").glob("*.sql")):
        sql = path.read_text()
        for pg in ("TIMESTAMPTZ", "JSONB", "BIGSERIAL", "gen_random_uuid", "TEXT[]", "CREATE EXTENSION", "ON CONFLICT"):
            assert pg not in sql, f"{path.name} contains Postgres-only '{pg}'"
        for stmt in split_statements(sql):
            if stmt.upper().startswith("CREATE TABLE"):
                assert "PRIMARY KEY" in stmt, f"{path.name}: HeatWave requires a primary key on every table"

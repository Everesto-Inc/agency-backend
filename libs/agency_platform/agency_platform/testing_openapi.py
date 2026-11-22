"""Shared API-contract assertions: every service's OpenAPI must follow agency conventions."""
from typing import Any

_METHODS = {"get", "post", "put", "patch", "delete"}


def assert_api_conventions(schema: dict[str, Any], *, expected_tags: set[str] | None = None) -> None:
    declared = [t["name"] for t in schema.get("tags", [])]
    assert declared, "service must declare its tag groups"
    seen_ids: set[str] = set()
    for path, ops in schema["paths"].items():
        for method, op in ops.items():
            if method not in _METHODS:
                continue
            where = f"{method.upper()} {path}"
            assert op.get("tags") and set(op["tags"]) <= set(declared), f"{where}: tag missing or undeclared"
            assert op.get("summary"), f"{where}: summary missing"
            oid = op["operationId"]
            assert oid not in seen_ids and "api_v1" not in oid, f"{where}: operationId '{oid}' not clean/unique"
            seen_ids.add(oid)
            responses = op["responses"]
            ok = [c for c in responses if c.startswith("2")]
            assert ok, f"{where}: no success response"
            if ok[0] != "204":
                content = responses[ok[0]].get("content", {})
                json_schema = content.get("application/json", {}).get("schema", {})
                assert json_schema and json_schema != {}, f"{where}: untyped success response"
            for code, resp in responses.items():
                if code.startswith(("4", "5")):
                    ref = resp.get("content", {}).get("application/json", {}).get("schema", {}).get("$ref", "")
                    assert ref.endswith("/ErrorResponse"), f"{where}: {code} must use ErrorResponse"
    assert "HTTPValidationError" not in schema.get("components", {}).get("schemas", {})
    if expected_tags is not None:
        assert expected_tags <= set(declared), f"missing tags: {expected_tags - set(declared)}"

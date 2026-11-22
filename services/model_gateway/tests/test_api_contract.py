from agency_platform.testing_openapi import assert_api_conventions
from model_gateway.main import app


def test_gateway_api_follows_conventions():
    schema = app.openapi()
    assert_api_conventions(schema, expected_tags={"OpenAI-compatible", "Routing"})
    assert "CallToken" in schema["components"]["securitySchemes"]

from agency_platform.testing_openapi import assert_api_conventions
from edge_gateway.main import app


def test_edge_api_follows_conventions():
    schema = app.openapi()
    assert_api_conventions(schema, expected_tags={"Auth", "Account", "Chat", "Topics", "Usage"})
    assert schema["components"]["securitySchemes"]["BearerAuth"]["scheme"] == "bearer"
    assert schema["paths"]["/api/v1/auth/login"]["post"]["operationId"] == "login"
    # protected endpoints declare the scheme (Swagger "Authorize" applies), public ones don't
    assert schema["paths"]["/api/v1/usage/me"]["get"]["security"] == [{"BearerAuth": []}]
    assert "security" not in schema["paths"]["/api/v1/auth/login"]["post"]

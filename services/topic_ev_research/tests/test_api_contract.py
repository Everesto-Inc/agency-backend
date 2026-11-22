from agency_platform.testing_openapi import assert_api_conventions
from ev_research.main import app


def test_topic_api_follows_conventions():
    schema = app.openapi()
    assert_api_conventions(schema)
    assert app.docs_url == "/api/v1/ev_research/docs" and app.openapi_url == "/api/v1/ev_research/openapi.json"
    run = schema["paths"]["/api/v1/ev_research/run"]["post"]
    assert run["operationId"] == "run" and "text/event-stream" in run["responses"]["200"]["content"]

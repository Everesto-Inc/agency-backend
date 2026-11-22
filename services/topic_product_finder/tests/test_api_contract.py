from agency_platform.testing_openapi import assert_api_conventions
from product_finder.main import app


def test_topic_api_follows_conventions():
    schema = app.openapi()
    assert_api_conventions(schema)
    assert app.docs_url == "/api/v1/product_finder/docs" and app.openapi_url == "/api/v1/product_finder/openapi.json"
    run = schema["paths"]["/api/v1/product_finder/run"]["post"]
    assert run["operationId"] == "run" and "text/event-stream" in run["responses"]["200"]["content"]

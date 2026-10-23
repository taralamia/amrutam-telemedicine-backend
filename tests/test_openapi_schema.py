import json
from pathlib import Path

from app.main import app


def test_checked_in_openapi_schema_matches_application_contract():
    schema_path = Path(__file__).parents[1] / "docs" / "openapi.json"
    checked_in_schema = json.loads(schema_path.read_text())
    assert checked_in_schema == app.openapi()


def test_every_retryable_write_documents_idempotency_header():
    schema = app.openapi()
    for path, path_item in schema["paths"].items():
        for method, operation in path_item.items():
            if method not in {"post", "put", "patch", "delete"}:
                continue
            if path == "/v1/webhooks/payments":
                assert {"x-webhook-timestamp", "x-webhook-signature"} <= {
                    parameter["name"]
                    for parameter in operation.get("parameters", [])
                    if parameter["in"] == "header"
                }
                continue
            assert "Idempotency-Key" in {
                parameter["name"]
                for parameter in operation.get("parameters", [])
                if parameter["in"] == "header"
            }, f"{method.upper()} {path} must document an Idempotency-Key"

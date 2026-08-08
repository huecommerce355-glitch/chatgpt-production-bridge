from pathlib import Path

import yaml


OPENAPI_PATH = Path(__file__).parents[1] / "references" / "openapi.yaml"
STRATEGY_PATHS = {
    "/v1/strategy/context": "getStrategyContext",
    "/v1/strategy/knowledge": "readStrategyKnowledge",
    "/v1/strategy/adr": "proposeStrategyAdr",
    "/v1/strategy/handoff": "executeStrategyHandoff",
}


def load_spec():
    with OPENAPI_PATH.open(encoding="utf-8") as spec_file:
        return yaml.safe_load(spec_file)


def test_openapi_paths_have_operation_ids_and_match_bridge_actions():
    spec = load_spec()
    assert spec["openapi"] == "3.0.3"
    assert spec["paths"]["/health"]["get"]["operationId"] == "healthCheck"
    assert set(spec["paths"]) == {"/health", *STRATEGY_PATHS}

    for path, operation_id in STRATEGY_PATHS.items():
        operation = spec["paths"][path]["post"]
        assert operation["operationId"] == operation_id
        assert operation["security"] == [{"ApiKeyAuth": []}]


def test_openapi_api_key_security_scheme_is_correct():
    spec = load_spec()
    assert spec["components"]["securitySchemes"]["ApiKeyAuth"] == {
        "type": "apiKey",
        "in": "header",
        "name": "X-API-Key",
    }


def test_openapi_schema_references_are_complete():
    spec = load_spec()

    def walk(value):
        if isinstance(value, dict):
            if "$ref" in value:
                assert value["$ref"].startswith("#/")
                target = spec
                for part in value["$ref"][2:].split("/"):
                    assert isinstance(target, dict) and part in target
                    target = target[part]
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(spec)

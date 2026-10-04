"""Unit tests for the variable detector module."""

import json

from tripl.core.analyzers.variable_detector import detect_variables


class TestJsonDetection:
    def test_simple_json_with_high_cardinality_key(self):
        values = [json.dumps({"spot_id": i, "type": "banner"}) for i in range(200)]
        result = detect_variables("property", values, cardinality_threshold=100)
        assert result is not None
        assert "${spot_id}" in result.template
        # "type" is low-cardinality (one unique value), so the template carries
        # that value literally rather than a ``${_low:type}`` token nothing ever
        # expanded.
        assert json.loads(result.template)["type"] == "banner"
        assert any(v.name == "spot_id" for v in result.variables)
        assert any(v.inferred_type == "number" for v in result.variables)

    def test_json_all_keys_high_cardinality(self):
        values = [json.dumps({"user_id": i, "session_id": i + 1000}) for i in range(200)]
        result = detect_variables("data", values, cardinality_threshold=100)
        assert result is not None
        assert "${user_id}" in result.template
        assert "${session_id}" in result.template
        assert len(result.variables) == 2

    def test_json_all_keys_low_cardinality(self):
        values = [json.dumps({"color": "red", "size": "large"}) for _ in range(50)] + [
            json.dumps({"color": "blue", "size": "small"}) for _ in range(50)
        ]
        result = detect_variables("props", values, cardinality_threshold=100)
        assert result is not None
        # Both keys are low-cardinality
        assert len(result.variables) == 0

    def test_non_json_returns_none_for_json_path(self):
        values = ["not json", "also not json", "still not"]
        result = detect_variables("col", values, cardinality_threshold=100)
        assert result is not None
        # Should fall through to generic detection or fallback
        assert "${col}" in result.template

    def test_mixed_json_and_non_json(self):
        values = ['{"a": 1}', "not json", '{"a": 2}']
        result = detect_variables("col", values, cardinality_threshold=100)
        assert result is not None
        # With mixed, JSON detection fails; falls through to other patterns

    def test_json_string_values(self):
        values = [json.dumps({"page": f"/page/{i}", "action": "click"}) for i in range(200)]
        result = detect_variables("event_data", values, cardinality_threshold=100)
        assert result is not None
        assert "${page}" in result.template
        page_var = next(v for v in result.variables if v.name == "page")
        assert page_var.inferred_type == "string"


class TestPathDetection:
    def test_url_with_numeric_id(self):
        values = [f"/users/{i}/profile" for i in range(200)]
        result = detect_variables("screen", values, cardinality_threshold=100)
        assert result is not None
        assert "/users/" in result.template
        assert "/profile" in result.template
        assert len(result.variables) >= 1

    def test_url_with_uuid(self):
        import uuid

        values = [f"/items/{uuid.uuid4()}/detail" for _ in range(200)]
        result = detect_variables("page", values, cardinality_threshold=100)
        assert result is not None
        assert "/items/" in result.template
        assert "/detail" in result.template

    def test_constant_path(self):
        values = ["/home/dashboard"] * 200
        result = detect_variables("screen", values, cardinality_threshold=100)
        # Only 1 unique value but detect_variables still runs since called for high-card columns
        assert result is not None

    def test_http_urls_stripped(self):
        values = [f"https://example.com/users/{i}/profile" for i in range(200)]
        result = detect_variables("url", values, cardinality_threshold=100)
        assert result is not None
        assert "example.com" not in result.template  # protocol stripped


class TestNumericDetection:
    def test_all_integers(self):
        values = [str(i) for i in range(200)]
        result = detect_variables("count", values, cardinality_threshold=100)
        assert result is not None
        assert result.template == "${count}"
        assert result.variables[0].inferred_type == "number"

    def test_all_floats(self):
        values = [f"{i}.{i}" for i in range(200)]
        result = detect_variables("amount", values, cardinality_threshold=100)
        assert result is not None
        assert result.template == "${amount}"
        assert result.variables[0].inferred_type == "number"


class TestUuidDetection:
    def test_all_uuids(self):
        import uuid

        values = [str(uuid.uuid4()) for _ in range(200)]
        result = detect_variables("request_id", values, cardinality_threshold=100)
        assert result is not None
        assert result.template == "${request_id}"
        assert result.variables[0].inferred_type == "string"


class TestGenericStringDetection:
    def test_underscore_separated_with_variable(self):
        values = [f"order_{i}" for i in range(200)]
        result = detect_variables("ref", values, cardinality_threshold=100)
        assert result is not None
        assert "order" in result.template
        assert "${" in result.template

    def test_dash_separated_with_variable(self):
        values = [f"user-{i}-active" for i in range(200)]
        result = detect_variables("tag", values, cardinality_threshold=100)
        assert result is not None
        assert "${" in result.template

    def test_no_pattern_fallback(self):
        # Completely random strings with no pattern
        import hashlib

        values = [hashlib.md5(str(i).encode()).hexdigest() for i in range(200)]
        result = detect_variables("hash", values, cardinality_threshold=100)
        assert result is not None
        # Should fall back to ${column_name}
        assert result.template == "${hash}"

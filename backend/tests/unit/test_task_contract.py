from __future__ import annotations

import copy
import json

from jsonschema import Draft202012Validator, FormatChecker
from scripts.check_task_contract import contract_directory


def test_existing_task_create_example_matches_schema_with_uuid_format_check() -> None:
    directory = contract_directory()
    schema = json.loads((directory / "task-create-v2.schema.json").read_text(encoding="utf-8"))
    example = json.loads((directory / "task-create-v2.example.json").read_text(encoding="utf-8"))

    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    assert list(validator.iter_errors(example)) == []


def test_schema_rejects_bad_uuid_and_unknown_property() -> None:
    directory = contract_directory()
    schema = json.loads((directory / "task-create-v2.schema.json").read_text(encoding="utf-8"))
    example = json.loads((directory / "task-create-v2.example.json").read_text(encoding="utf-8"))
    invalid = copy.deepcopy(example)
    invalid["reference_set_id"] = "not-a-uuid"
    invalid["user_id"] = "caller-cannot-choose-owner"

    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    errors = list(validator.iter_errors(invalid))
    assert any(error.validator == "format" and error.validator_value == "uuid" for error in errors)
    assert any("Additional properties" in error.message for error in errors)

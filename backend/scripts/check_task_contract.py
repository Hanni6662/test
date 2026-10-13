from __future__ import annotations

import json
import os
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker


def contract_directory() -> Path:
    configured = os.environ.get("TASK_CONTRACTS_DIR")
    if configured:
        return Path(configured)
    return Path(__file__).resolve().parents[2] / "docs" / "contracts"


def validate_example(schema_path: Path, example_path: Path) -> list[str]:
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    example = json.loads(example_path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    return [error.message for error in validator.iter_errors(example)]


def main() -> int:
    directory = contract_directory()
    schema_path = directory / "task-create-v2.schema.json"
    example_path = directory / "task-create-v2.example.json"
    errors = validate_example(schema_path, example_path)
    if errors:
        for error in errors:
            print(error)
        return 1
    print("task-create-v2 schema and example are valid (Draft 2020-12; UUID format checked).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


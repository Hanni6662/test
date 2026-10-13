"""Build the existing deliver contract as a design output schema; no prompt changes."""
import ast
import argparse
import json
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent


def obj(properties):
    return {'type': 'object', 'properties': properties, 'required': list(properties),
            'additionalProperties': False}


def string(limit):
    return {'type': 'string', 'minLength': 1, 'maxLength': limit}


def build_schema(document):
    deliver = next(n['data'] for n in document['workflow']['graph']['nodes'] if n['id'] == 'deliver')
    assignment = next(n for n in ast.parse(deliver['code']).body
                      if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'TYPES' for t in n.targets))
    types = ast.literal_eval(assignment.value)
    refs = {'type': 'array', 'maxItems': 3, 'items': {'type': 'string', 'enum': ['AUX01', 'AUX02', 'AUX03']}}
    return obj({'images': {'type': 'array', 'minItems': 1, 'maxItems': 8, 'items': obj({
        'type': {'type': 'string', 'enum': types},
        'canvas': string(100),
        'reference_plan': obj({
            'identity_anchor_id': {'type': 'string', 'enum': ['PRIMARY']},
            'view_reference_ids': refs,
            'excluded_reference_ids': {'type': 'array', 'maxItems': 0, 'items': {'type': 'string'}},
            'target_view': string(200),
        }),
        'resources': {'type': 'array', 'minItems': 1, 'items': obj({
            'field': {'type': 'string', 'enum': ['1', '2', '3', '4', '5']}, 'use': string(1200)})},
        'design_prompt': string(16000),
    })}})


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--workflow', type=Path, default=HERE / 'v34-real-handoff-no-secrets.yml')
    parser.add_argument('--output', type=Path, default=HERE / 'design-output.schema.json')
    args = parser.parse_args()
    document = yaml.safe_load(args.workflow.read_text(encoding='utf-8'))
    schema = build_schema(document)
    args.output.write_text(json.dumps(schema, ensure_ascii=False, indent=2), encoding='utf-8')
    print('Design schema created from the unchanged deliver contract')

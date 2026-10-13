"""Patch one verified provider release without changing any Dify workflow or credentials."""
import argparse
import hashlib
import json
import shutil
from pathlib import Path

PACKAGE = 'openai_api_compatible-0.0.68@ba61c5eccb814be7364117c0bec3710a51e09b20f31e37243e747a00f9377969'
ORIGINAL_SHA = '2f1a5493116f350ad34d12a2243ca8df9bd4c499b5f04de9749bfcc137affb9d'
PATCHED_SHA = {
    'da9068b7463219c38cd59950a817397cc75f62fb2fe3e22ab6e5d0e9b97948c9',
    '33242837ebfe15a1ba65ff315e85816a19636e8da24bcd03c139494897f8a058',
}
MARKER = '        # Local image-message compatibility fix; workflow graph stays unchanged.\n'
SIGNATURE = '    ) -> Union[LLMResult, Generator]:\n        if credentials.get("api_type") == "responses":'
IMPORT = 'from models.llm.vision_message_compat import coalesce_gateway_image_messages\n'


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def patch_source(text):
    if text.count(SIGNATURE) != 1 or text.count('import requests\n') != 1:
        raise ValueError('Unexpected provider source; no files changed')
    result = text.replace('import requests\n', 'import requests\n' + IMPORT, 1)
    result = result.replace(SIGNATURE, '    ) -> Union[LLMResult, Generator]:\n' + MARKER +
        '        prompt_messages = coalesce_gateway_image_messages(prompt_messages, model, credentials)\n' +
        '        if credentials.get("api_type") == "responses":', 1)
    compile(result, 'llm.py', 'exec')
    return result


def install(plugin_root, backup_dir, check_only=False):
    plugin_root, backup_dir = Path(plugin_root).resolve(), Path(backup_dir).resolve()
    package = plugin_root / PACKAGE
    source = package / 'models/llm/llm.py'
    helper_source = Path(__file__).resolve().with_name('vision_message_compat.py')
    helper = source.with_name('vision_message_compat.py')
    if backup_dir.is_relative_to(plugin_root) or backup_dir.is_relative_to(Path(__file__).resolve().parents[2]):
        raise ValueError('Backup directory must be outside plugin storage and the delivery package')
    if not source.is_file():
        raise ValueError('Verified OpenAI API Compatible 0.0.68 package not found; no files changed')
    before = sha(source)
    if before != ORIGINAL_SHA and before not in PATCHED_SHA:
        raise ValueError('Provider source hash is unrecognized; no files changed')
    if helper.exists() and sha(helper) != sha(helper_source):
        raise ValueError('Existing helper differs; preserve and inspect it before retrying')
    text = source.read_text(encoding='utf-8')
    result = patch_source(text) if before == ORIGINAL_SHA else text
    compile(helper_source.read_text(encoding='utf-8'), 'vision_message_compat.py', 'exec')
    original = backup_dir / 'provider-llm-before.py'
    if original.exists() and sha(original) != ORIGINAL_SHA:
        raise ValueError('Existing backup is unrecognized; no files changed')
    if check_only:
        return {'compatible': True, 'installed': before in PATCHED_SHA and helper.exists(),
                'workflow_modified': False, 'credentials_modified': False}
    if before == ORIGINAL_SHA:
        backup_dir.mkdir(parents=True, exist_ok=True)
        if not original.exists():
            shutil.copy2(source, original)
    shutil.copy2(helper_source, helper)
    if before == ORIGINAL_SHA:
        temporary = source.with_suffix('.py.patch-tmp')
        temporary.write_bytes(result.encode('utf-8'))
        temporary.replace(source)
    return {'installed': True, 'provider_sha256': sha(source), 'helper_sha256': sha(helper),
            'backup_available': original.exists(), 'workflow_modified': False, 'credentials_modified': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plugin-root', required=True, help='Recipient storage/cwd/langgenius directory')
    parser.add_argument('--backup-dir', required=True, help='Private backup directory outside package and plugin storage')
    parser.add_argument('--check-only', action='store_true')
    args = parser.parse_args()
    try:
        result = install(args.plugin_root, args.backup_dir, args.check_only)
    except ValueError as exc:
        parser.exit(1, str(exc) + '\n')
    print(json.dumps(result))


if __name__ == '__main__':
    main()

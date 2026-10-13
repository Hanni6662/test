"""No Docker, network, credentials or workflow mutation."""
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import install_vision_message_compat as installer

FIXTURE = '''import requests
class Provider:
    def _invoke(
        self, prompt_messages, model, credentials
    ) -> Union[LLMResult, Generator]:
        if credentials.get("api_type") == "responses":
            pass
'''


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.storage = self.root / 'storage'
        self.source = self.storage / installer.PACKAGE / 'models/llm/llm.py'
        self.source.parent.mkdir(parents=True)
        self.source.write_bytes(FIXTURE.encode())
        self.backup = self.root / 'backup'
        original_sha = hashlib.sha256(FIXTURE.encode()).hexdigest()
        patched_sha = hashlib.sha256(installer.patch_source(FIXTURE).encode()).hexdigest()
        for name, value in [('ORIGINAL_SHA', original_sha), ('PATCHED_SHA', {patched_sha})]:
            context = patch.object(installer, name, value)
            context.start()
            self.addCleanup(context.stop)

    def test_check_only_does_not_write(self):
        result = installer.install(self.storage, self.backup, True)
        self.assertTrue(result['compatible'])
        self.assertFalse(result['installed'])
        self.assertEqual(self.source.read_text(), FIXTURE)
        self.assertFalse(self.backup.exists())

    def test_install_is_idempotent_and_preserves_original_backup(self):
        installer.install(self.storage, self.backup)
        first = self.source.read_bytes()
        result = installer.install(self.storage, self.backup)
        self.assertTrue(result['installed'])
        self.assertEqual(self.source.read_bytes(), first)
        self.assertEqual((self.backup / 'provider-llm-before.py').read_text(), FIXTURE)
        self.assertEqual(self.source.read_text().count(installer.MARKER), 1)

    def test_unrecognized_source_is_preserved(self):
        self.source.write_text('foreign provider implementation')
        with self.assertRaises(ValueError):
            installer.install(self.storage, self.backup)
        self.assertEqual(self.source.read_text(), 'foreign provider implementation')
        self.assertFalse(self.backup.exists())

    def test_conflicting_helper_is_preserved(self):
        helper = self.source.with_name('vision_message_compat.py')
        helper.write_text('custom transport')
        with self.assertRaises(ValueError):
            installer.install(self.storage, self.backup)
        self.assertEqual(helper.read_text(), 'custom transport')
        self.assertEqual(self.source.read_text(), FIXTURE)

    def test_backup_inside_plugin_storage_is_rejected(self):
        with self.assertRaises(ValueError):
            installer.install(self.storage, self.storage / 'backup')
        self.assertEqual(self.source.read_text(), FIXTURE)

    def test_backup_inside_delivery_package_is_rejected(self):
        with self.assertRaises(ValueError):
            installer.install(self.storage, Path(__file__).resolve().parent / 'private-backup')
        self.assertEqual(self.source.read_text(), FIXTURE)


if __name__ == '__main__':
    unittest.main()

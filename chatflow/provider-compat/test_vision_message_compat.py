"""Tests against the real provider SDK; no HTTP or paid model calls."""
import json
import unittest
from dify_plugin.entities.model.message import (
    UserPromptMessage, AssistantPromptMessage, SystemPromptMessage,
    TextPromptMessageContent, ImagePromptMessageContent, PromptMessageContentType,
)
from vision_message_compat import coalesce_gateway_image_messages

CREDENTIALS = {'endpoint_url': 'https://api.aipaibox.com/v1'}
MODEL = 'claude-sonnet-5-5'


def image(name):
    return ImagePromptMessageContent(format='png', base64_data=name, mime_type='image/png')


class CompatibilityTests(unittest.TestCase):
    def test_primary_and_auxiliary_content_order_and_original_objects(self):
        messages = [SystemPromptMessage(content='rules'), UserPromptMessage(content='PRIMARY'),
            UserPromptMessage(content=[image('primary')]), UserPromptMessage(content='AUX01'),
            UserPromptMessage(content=[image('aux')]), UserPromptMessage(content='final question')]
        before = [m.model_dump() for m in messages]
        merged = coalesce_gateway_image_messages(messages, MODEL, CREDENTIALS)
        self.assertEqual(len(merged), 2)
        self.assertEqual([p.type for p in merged[1].content], [PromptMessageContentType.TEXT,
            PromptMessageContentType.IMAGE, PromptMessageContentType.TEXT,
            PromptMessageContentType.IMAGE, PromptMessageContentType.TEXT])
        self.assertEqual([p.base64_data for p in merged[1].content if p.type == PromptMessageContentType.IMAGE], ['primary', 'aux'])
        self.assertEqual([m.model_dump() for m in messages], before)

    def test_other_gateway_and_model_remain_unchanged(self):
        messages = [UserPromptMessage(content='text'), UserPromptMessage(content=[image('photo')])]
        self.assertIs(coalesce_gateway_image_messages(messages, 'other-model', CREDENTIALS), messages)
        self.assertIs(coalesce_gateway_image_messages(messages, MODEL, {'endpoint_url':'https://example.com/v1'}), messages)

    def test_role_boundaries_remain_intact(self):
        messages = [UserPromptMessage(content=[image('photo')]), AssistantPromptMessage(content='answer'),
            UserPromptMessage(content='next text')]
        self.assertEqual(coalesce_gateway_image_messages(messages, MODEL, CREDENTIALS), messages)

    def test_text_only_and_already_mixed_messages_stay_intact(self):
        for messages in ([UserPromptMessage(content='first'), UserPromptMessage(content='second')],
            [UserPromptMessage(content=[TextPromptMessageContent(data='text'), image('photo')])]):
            self.assertEqual(coalesce_gateway_image_messages(messages, MODEL, CREDENTIALS), messages)


if __name__ == '__main__':
    unittest.main()

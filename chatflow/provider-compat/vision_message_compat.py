"""Preserve image input for the configured gateway without editing workflow prompts."""
from urllib.parse import urlparse

from dify_plugin.entities.model.message import (
    PromptMessageContentType,
    TextPromptMessageContent,
    UserPromptMessage,
)


def _has_image(message):
    return isinstance(message.content, list) and any(
        part.type == PromptMessageContentType.IMAGE for part in message.content
    )


def _parts(message):
    if isinstance(message.content, list):
        return list(message.content)
    return [TextPromptMessageContent(data=message.content)] if message.content else []


def coalesce_gateway_image_messages(messages, model, credentials):
    """Merge adjacent user messages only where this gateway otherwise loses an image.

    Preserve every content part and its order; never cross system/assistant/tool
    boundaries, and leave other models, gateways and text-only turns unchanged.
    Copy merged messages so the original workflow prompt objects remain intact.
    """
    if model != "claude-sonnet-5-5" or urlparse(credentials.get("endpoint_url", "")).hostname != "api.aipaibox.com":
        return messages
    result = []
    for message in messages:
        if (result and isinstance(message, UserPromptMessage)
            and isinstance(result[-1], UserPromptMessage)
            and (_has_image(message) or _has_image(result[-1]))):
            previous = result.pop()
            result.append(previous.model_copy(deep=True, update={
                "content": _parts(previous) + _parts(message),
            }))
        else:
            result.append(message)
    return result

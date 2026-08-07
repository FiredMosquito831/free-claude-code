"""Modality inspection of Anthropic protocol requests."""

from .models import ContentBlockImage, MessagesRequest


def request_carries_image(request: MessagesRequest) -> bool:
    """Return whether any message in the request contains an image block.

    Only message content is inspected. ``system`` is text-or-text-blocks in the
    Anthropic schema and tool results reach the model as their own blocks, so a
    scan of message content covers every path an image can arrive on.
    """
    return any(
        isinstance(block, ContentBlockImage)
        for message in request.messages
        if isinstance(message.content, list)
        for block in message.content
    )

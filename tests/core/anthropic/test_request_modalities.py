"""Where an image can hide in an Anthropic request.

The gap these cover shipped for four releases: only top-level blocks were
inspected, so an image a *tool* returned -- reading a PNG, an MCP screenshot --
never triggered vision routing and never appeared in analytics.
"""

from my_claude_code.core.anthropic import request_carries_image, request_image_inputs
from my_claude_code.core.anthropic.models import MessagesRequest

_IMAGE: dict[str, object] = {
    "type": "image",
    "source": {"type": "base64", "media_type": "image/png", "data": "aGVsbG8="},
}
_DOCUMENT: dict[str, object] = {
    "type": "document",
    "source": {
        "type": "base64",
        "media_type": "application/pdf",
        "data": "JVBERi0=",
    },
}


def _request(messages: list[dict[str, object]]) -> MessagesRequest:
    return MessagesRequest.model_validate(
        {"model": "claude-sonnet-5", "messages": messages}
    )


def _tool_result(content: object) -> dict[str, object]:
    return {"type": "tool_result", "tool_use_id": "t1", "content": content}


def test_a_pasted_image_is_found():
    request = _request(
        [{"role": "user", "content": [{"type": "text", "text": "x"}, _IMAGE]}]
    )

    assert request_carries_image(request) is True
    assert len(request_image_inputs(request)) == 1


def test_an_image_returned_by_a_tool_is_found():
    request = _request([{"role": "user", "content": [_tool_result([_IMAGE])]}])

    assert request_carries_image(request) is True
    assert request_image_inputs(request)[0].media_type == "image/png"


def test_a_tool_result_carrying_a_single_block_is_found():
    request = _request([{"role": "user", "content": [_tool_result(_IMAGE)]}])

    assert request_carries_image(request) is True


def test_a_pdf_document_counts_as_something_to_look_at():
    request = _request([{"role": "user", "content": [_DOCUMENT]}])

    assert request_carries_image(request) is True
    assert request_image_inputs(request)[0].kind == "document"


def test_text_and_tool_results_without_images_carry_nothing():
    request = _request(
        [
            {"role": "user", "content": [{"type": "text", "text": "hello"}]},
            {"role": "user", "content": [_tool_result("a plain string result")]},
            {
                "role": "user",
                "content": [_tool_result([{"type": "text", "text": "x"}])],
            },
        ]
    )

    assert request_carries_image(request) is False
    assert request_image_inputs(request) == ()


def test_every_image_is_returned_in_order():
    request = _request(
        [
            {"role": "user", "content": [_IMAGE]},
            {"role": "user", "content": [_tool_result([_DOCUMENT, _IMAGE])]},
        ]
    )

    assert [image.kind for image in request_image_inputs(request)] == [
        "image",
        "document",
        "image",
    ]


def test_a_url_source_is_counted_without_data():
    request = _request(
        [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {"type": "url", "url": "https://example.test/a.png"},
                    }
                ],
            }
        ]
    )

    image = request_image_inputs(request)[0]
    assert image.data is None
    assert image.url == "https://example.test/a.png"
    assert image.approx_bytes is None


def test_string_content_is_not_walked():
    request = _request([{"role": "user", "content": "just text"}])

    assert request_carries_image(request) is False


def test_a_deeply_nested_payload_cannot_walk_forever():
    nested: dict[str, object] = _tool_result([_IMAGE])
    for _ in range(20):
        nested = _tool_result([nested])
    request = _request([{"role": "user", "content": [nested]}])

    # The bound is the point: it returns rather than recursing on a payload
    # built to be pathological.
    assert request_image_inputs(request) == ()


def test_approx_bytes_estimates_the_decoded_size():
    request = _request([{"role": "user", "content": [_IMAGE]}])

    # "aGVsbG8=" decodes to b"hello".
    assert request_image_inputs(request)[0].approx_bytes == 5

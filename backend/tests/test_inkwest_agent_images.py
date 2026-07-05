"""Tests for image input through the inkwest-agent loop (P1.8).

Covers the Converse image-block translation (format detection, the IMAGE-capability
gate, size/format limits), the PROTOCOL_VERSION 4 attachments parse + back-compat
defaults, and the loop wiring that merges image blocks onto the user turn.
"""

from unittest.mock import MagicMock

import pytest

from app.services.events import ChatEvent
from inkwest_agent import loop as loop_mod
from inkwest_agent.converse import (
    MAX_IMAGE_BYTES,
    image_blocks_for_attachments,
)
from inkwest_agent.protocol import (
    AttachmentSpec,
    TurnSpec,
    TurnSpecError,
    parse_turn_spec,
)

SESSION_ID = "11111111-1111-1111-1111-111111111111"

_BASE_SPEC = {
    "session_id": SESSION_ID,
    "user_message": "what's wrong here?",
    "model": "anthropic.claude-opus-4-v1:0",
    "system_prompt": "s",
}


# --------------------------------------------------------------------------- #
# protocol — attachments parse + back-compat
# --------------------------------------------------------------------------- #


def test_protocol_parses_attachments():
    spec = parse_turn_spec(
        {
            **_BASE_SPEC,
            "input_modalities": ["TEXT", "IMAGE"],
            "attachments": [
                {
                    "path": "/workdir/x.png",
                    "content_type": "image/png",
                    "filename": "x.png",
                    "size": 99,
                }
            ],
        }
    )
    assert spec.input_modalities == ["TEXT", "IMAGE"]
    assert spec.image_capable is True
    assert spec.attachments == [
        AttachmentSpec(path="/workdir/x.png", content_type="image/png", filename="x.png", size=99)
    ]


def test_protocol_attachments_default_empty_backcompat():
    # A pre-P1.8 spec (no attachments / no input_modalities) parses with empty defaults
    # and is text-only — the loop sends no image blocks.
    spec = parse_turn_spec(_BASE_SPEC)
    assert spec.attachments == []
    assert spec.input_modalities == []
    assert spec.image_capable is False


def test_protocol_attachment_requires_path():
    with pytest.raises(TurnSpecError, match="missing 'path'"):
        parse_turn_spec({**_BASE_SPEC, "attachments": [{"content_type": "image/png"}]})


def test_protocol_attachment_rejects_unknown_key():
    with pytest.raises(TurnSpecError, match="unknown field"):
        parse_turn_spec({**_BASE_SPEC, "attachments": [{"path": "/x", "bogus": 1}]})


def test_protocol_attachments_must_be_list():
    with pytest.raises(TurnSpecError, match="'attachments' must be a list"):
        parse_turn_spec({**_BASE_SPEC, "attachments": {"path": "/x"}})


def test_protocol_attachment_size_must_be_int():
    with pytest.raises(TurnSpecError, match="'attachments.size' must be an int"):
        parse_turn_spec({**_BASE_SPEC, "attachments": [{"path": "/x", "size": "big"}]})


def test_image_capable_case_insensitive():
    assert TurnSpec(**_BASE_SPEC, input_modalities=["Image"]).image_capable is True
    assert TurnSpec(**_BASE_SPEC, input_modalities=["TEXT"]).image_capable is False


# --------------------------------------------------------------------------- #
# converse — image-block translation
# --------------------------------------------------------------------------- #

# A 1x1 PNG (real bytes so format detection and reads exercise the real path).
_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06"
    b"\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05"
    b"\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _att(tmp_path, name: str, data: bytes, content_type: str) -> AttachmentSpec:
    p = tmp_path / name
    p.write_bytes(data)
    return AttachmentSpec(path=str(p), content_type=content_type, filename=name)


def test_image_block_from_png(tmp_path):
    att = _att(tmp_path, "diagram.png", _PNG, "image/png")
    blocks, skipped = image_blocks_for_attachments([att], image_capable=True)
    assert skipped == []
    assert blocks == [{"image": {"format": "png", "source": {"bytes": _PNG}}}]


def test_format_detected_by_content_type_jpg_alias(tmp_path):
    att = _att(tmp_path, "p.bin", _PNG, "image/jpg")  # ext doesn't say image; type does
    blocks, _ = image_blocks_for_attachments([att], image_capable=True)
    assert blocks[0]["image"]["format"] == "jpeg"  # image/jpg normalized to jpeg


def test_format_detected_by_extension_when_type_generic(tmp_path):
    att = _att(tmp_path, "shot.webp", _PNG, "application/octet-stream")
    blocks, _ = image_blocks_for_attachments([att], image_capable=True)
    assert blocks[0]["image"]["format"] == "webp"


def test_gate_skips_for_text_only_model(tmp_path):
    # The IMAGE-capability gate: a text-only model gets NO image blocks (Converse rejects
    # them and would fail the whole turn). The image is reported skipped.
    att = _att(tmp_path, "diagram.png", _PNG, "image/png")
    blocks, skipped = image_blocks_for_attachments([att], image_capable=False)
    assert blocks == []
    assert len(skipped) == 1 and "does not accept image input" in skipped[0]


def test_non_image_attachment_ignored(tmp_path):
    # A text attachment is simply not image-translated (no skip note, no block).
    att = _att(tmp_path, "notes.txt", b"hello", "text/plain")
    blocks, skipped = image_blocks_for_attachments([att], image_capable=True)
    assert blocks == [] and skipped == []


def test_unsupported_image_format_skipped(tmp_path):
    # An image type Converse doesn't accept (svg/tiff/...) is skipped with a note.
    att = _att(tmp_path, "vector.svg", b"<svg/>", "image/svg+xml")
    blocks, skipped = image_blocks_for_attachments([att], image_capable=True)
    assert blocks == []
    assert len(skipped) == 1 and "unsupported image format" in skipped[0]


def test_oversized_image_skipped(tmp_path):
    big = b"\x89PNG\r\n\x1a\n" + b"\x00" * (MAX_IMAGE_BYTES + 1)
    att = _att(tmp_path, "huge.png", big, "image/png")
    blocks, skipped = image_blocks_for_attachments([att], image_capable=True)
    assert blocks == []
    assert len(skipped) == 1 and "image limit" in skipped[0]


def test_empty_file_skipped(tmp_path):
    att = _att(tmp_path, "empty.png", b"", "image/png")
    blocks, skipped = image_blocks_for_attachments([att], image_capable=True)
    assert blocks == []
    assert len(skipped) == 1 and "empty" in skipped[0]


def test_missing_file_skipped_not_crashes():
    # A path that doesn't exist on disk must not crash the turn — skipped with a reason.
    att = AttachmentSpec(path="/workdir/gone.png", content_type="image/png", filename="gone.png")
    blocks, skipped = image_blocks_for_attachments([att], image_capable=True)
    assert blocks == []
    assert len(skipped) == 1 and "cannot read" in skipped[0]


# --------------------------------------------------------------------------- #
# loop wiring — image blocks on the user turn + status note
# --------------------------------------------------------------------------- #


def _client(responses: list) -> MagicMock:
    client = MagicMock()
    client.converse = MagicMock(side_effect=responses)
    return client


def _final(text: str) -> dict:
    return {
        "usage": {"inputTokens": 1, "outputTokens": 1},
        "output": {"message": {"content": [{"text": text}]}},
        "stopReason": "end_turn",
    }


async def _collect(spec: TurnSpec, client) -> list[ChatEvent]:
    return [ev async for ev in loop_mod.run_turn(spec, client)]


@pytest.mark.asyncio
async def test_loop_merges_image_block_onto_user_turn(tmp_path):
    p = tmp_path / "diagram.png"
    p.write_bytes(_PNG)
    spec = parse_turn_spec(
        {
            **_BASE_SPEC,
            "input_modalities": ["TEXT", "IMAGE"],
            "attachments": [
                {"path": str(p), "content_type": "image/png", "filename": "diagram.png"}
            ],
        }
    )
    client = _client([_final("looks fine")])
    events = await _collect(spec, client)
    assert events[-1].type == "done"
    # The user turn sent to Converse carries the image block before the text.
    sent_messages = client.converse.call_args.kwargs["messages"]
    user_content = sent_messages[-1]["content"]
    assert user_content[0] == {"image": {"format": "png", "source": {"bytes": _PNG}}}
    assert any(b.get("text") == spec.user_message for b in user_content)


@pytest.mark.asyncio
async def test_loop_emits_status_when_image_skipped_for_text_only_model(tmp_path):
    p = tmp_path / "diagram.png"
    p.write_bytes(_PNG)
    spec = parse_turn_spec(
        {
            **_BASE_SPEC,
            "input_modalities": ["TEXT"],  # text-only model
            "attachments": [
                {"path": str(p), "content_type": "image/png", "filename": "diagram.png"}
            ],
        }
    )
    client = _client([_final("ok")])
    events = await _collect(spec, client)
    status = [e for e in events if e.type == "status_update"]
    assert status and "not sent as images" in (status[0].content or "")
    # No image block reached Converse (text-only gate).
    user_content = client.converse.call_args.kwargs["messages"][-1]["content"]
    assert all("image" not in b for b in user_content)


@pytest.mark.asyncio
async def test_loop_image_blocks_and_cache_marker_coexist_on_last_message(tmp_path):
    """Image merge and the cachePoint marker both touch the last message (
    open-point 3): images must stay in front, the marker last — deterministic ordering
    so the cached prefix isn't silently broken."""
    p = tmp_path / "diagram.png"
    p.write_bytes(_PNG)
    spec = parse_turn_spec(
        {
            **_BASE_SPEC,
            "input_modalities": ["TEXT", "IMAGE"],
            "attachments": [
                {"path": str(p), "content_type": "image/png", "filename": "diagram.png"}
            ],
        }
    )
    client = _client([_final("looks fine")])
    events = await _collect(spec, client)
    assert events[-1].type == "done"
    user_content = client.converse.call_args.kwargs["messages"][-1]["content"]
    assert user_content[0] == {"image": {"format": "png", "source": {"bytes": _PNG}}}
    assert user_content[-1] == {"cachePoint": {"type": "default"}}
    assert any(b.get("text") == spec.user_message for b in user_content)

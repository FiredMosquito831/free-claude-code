"""Storing the images a request carried, and getting rid of them again."""

import base64
import io
from pathlib import Path

from PIL import Image

from my_claude_code.core.request_images import CapturedImage
from my_claude_code.core.request_log import RequestLogStore, RequestRecord


def _thumbnail() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (32, 24), (5, 5, 5)).save(buffer, format="WEBP")
    return buffer.getvalue()


def _image(sha: str = "abc123") -> CapturedImage:
    return CapturedImage(
        sha256=sha,
        kind="image",
        media_type="image/png",
        source_bytes=2_048_000,
        width=1600,
        height=1200,
        thumbnail=_thumbnail(),
        thumbnail_media_type="image/webp",
    )


def _record(request_id: str, images: tuple[CapturedImage, ...]) -> RequestRecord:
    return RequestRecord(
        id=request_id,
        endpoint="/v1/messages",
        protocol="anthropic",
        requested_model="claude-sonnet-5",
        provider="nous_portal",
        resolved_model="tencent/hy3:free",
        input_image_count=len(images) or None,
        images=images,
    )


def test_an_image_round_trips_into_the_detail_payload(tmp_path: Path):
    path = tmp_path / "requests.db"
    store = RequestLogStore(path)
    store.enqueue(_record("r1", (_image(),)))
    store.close()

    reader = RequestLogStore(path)
    try:
        row = reader.get_request("r1")
        assert row is not None
        assert row["input_image_count"] == 1
        image = row["input_images"][0]
        assert image["media_type"] == "image/png"
        assert image["width"] == 1600
        assert image["source_bytes"] == 2_048_000
        assert base64.b64decode(image["thumbnail_base64"]) == _thumbnail()
    finally:
        reader.close()


def test_the_same_image_on_many_requests_is_stored_once(tmp_path: Path):
    path = tmp_path / "requests.db"
    store = RequestLogStore(path)
    for index in range(5):
        store.enqueue(_record(f"r{index}", (_image(),)))
    store.close()

    reader = RequestLogStore(path)
    try:
        # Read the tables directly: dedup and orphan cleanup are storage-level
        # facts with no public accessor.
        with reader._connection() as conn:
            blobs = conn.execute("SELECT COUNT(*) FROM image_blobs").fetchone()[0]
            links = conn.execute("SELECT COUNT(*) FROM request_images").fetchone()[0]
        assert (blobs, links) == (1, 5)
    finally:
        reader.close()


def test_images_appear_in_order(tmp_path: Path):
    path = tmp_path / "requests.db"
    store = RequestLogStore(path)
    store.enqueue(_record("r1", (_image("aaa"), _image("bbb"), _image("ccc"))))
    store.close()

    reader = RequestLogStore(path)
    try:
        row = reader.get_request("r1")
        assert row is not None
        assert [image["sha256"] for image in row["input_images"]] == [
            "aaa",
            "bbb",
            "ccc",
        ]
    finally:
        reader.close()


def test_a_request_without_images_reports_an_empty_list(tmp_path: Path):
    path = tmp_path / "requests.db"
    store = RequestLogStore(path)
    store.enqueue(_record("r1", ()))
    store.close()

    reader = RequestLogStore(path)
    try:
        row = reader.get_request("r1")
        assert row is not None
        assert row["input_images"] == []
        assert row["input_image_count"] is None
    finally:
        reader.close()


def test_retention_takes_the_pictures_with_the_rows(tmp_path: Path):
    path = tmp_path / "requests.db"
    store = RequestLogStore(path, max_rows=1)
    store.enqueue(_record("old", (_image("old-image"),)))
    store.enqueue(_record("new", (_image("new-image"),)))
    store.close()

    reader = RequestLogStore(path, max_rows=1)
    try:
        reader.prune()
        # Read the tables directly: dedup and orphan cleanup are storage-level
        # facts with no public accessor.
        with reader._connection() as conn:
            shas = {
                str(row[0])
                for row in conn.execute("SELECT sha FROM image_blobs").fetchall()
            }
        # The surviving row keeps its picture; the pruned one's is gone rather
        # than orphaned, which is what would otherwise grow the file forever.
        assert shas == {"new-image"}
    finally:
        reader.close()


def test_a_shared_image_survives_until_its_last_request_is_pruned(tmp_path: Path):
    path = tmp_path / "requests.db"
    store = RequestLogStore(path, max_rows=1)
    store.enqueue(_record("old", (_image("shared"),)))
    store.enqueue(_record("new", (_image("shared"),)))
    store.close()

    reader = RequestLogStore(path, max_rows=1)
    try:
        reader.prune()
        row = reader.get_request("new")
        assert row is not None
        assert row["input_images"][0]["sha256"] == "shared"
    finally:
        reader.close()


def test_clearing_the_log_removes_the_images(tmp_path: Path):
    path = tmp_path / "requests.db"
    store = RequestLogStore(path)
    store.enqueue(_record("r1", (_image(),)))
    store.close()

    reader = RequestLogStore(path)
    try:
        reader.clear()
        # Read the tables directly: dedup and orphan cleanup are storage-level
        # facts with no public accessor.
        with reader._connection() as conn:
            blobs = conn.execute("SELECT COUNT(*) FROM image_blobs").fetchone()[0]
            links = conn.execute("SELECT COUNT(*) FROM request_images").fetchone()[0]
        assert (blobs, links) == (0, 0)
    finally:
        reader.close()


def test_stats_counts_requests_that_carried_an_image(tmp_path: Path):
    path = tmp_path / "requests.db"
    store = RequestLogStore(path)
    store.enqueue(_record("r1", (_image(),)))
    store.enqueue(_record("r2", ()))
    store.close()

    reader = RequestLogStore(path)
    try:
        assert reader.stats()["with_images"] == 1
    finally:
        reader.close()


def _diverted(request_id: str, *, diverted_from: str | None, diversion: str | None):
    record = _record(request_id, (_image(request_id),))
    record.route_chain = "chatgpt_oauth/gpt-5.6-luna,nous_portal/step-3.7:free"
    record.route_attempt = 0
    record.route_diverted_from = diverted_from
    record.route_diversion = diversion
    return record


def test_an_image_with_no_vision_route_is_counted_apart_from_a_diversion(
    tmp_path: Path,
):
    """The safety net working and the safety net having nowhere to put the
    request are different facts, and the counters must not merge them."""
    path = tmp_path / "requests.db"
    store = RequestLogStore(path)
    store.enqueue(
        _diverted(
            "real", diverted_from="nous_portal/tencent/hy3:free", diversion="vision"
        )
    )
    store.enqueue(
        _diverted("blind", diverted_from=None, diversion="vision_unavailable")
    )
    store.close()

    reader = RequestLogStore(path)
    try:
        stats = reader.stats()
        assert stats["diverted"] == 1
        assert stats["vision_unavailable"] == 1
        assert stats["with_images"] == 2
        # The lifetime counter must agree with the window one.
        assert reader.lifetime()["diverted"] == 1
    finally:
        reader.close()

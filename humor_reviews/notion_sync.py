from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any

import requests

from .place_metadata import format_place_location


NOTION_API_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
NOTION_FILES_VERSION = "2026-03-11"
DEFAULT_NOTION_AREA_PAGE_ID = "31486d6f-ebef-4ef5-98e1-266912e15376"


class NotionSyncError(RuntimeError):
    pass


@dataclass
class NotionPage:
    page_id: str
    url: str


@dataclass
class NotionUpload:
    file_upload_id: str


def create_review_page(review: dict[str, Any]) -> NotionPage:
    token = os.getenv("NOTION_ACCESS_TOKEN", "").strip()
    database_id = (
        os.getenv("NOTION_DATABASE_ID", "").strip()
        or os.getenv("NOTION_DATA_SOURCE_ID", "").strip()
    )
    if not token:
        raise NotionSyncError("Missing NOTION_ACCESS_TOKEN.")
    if not database_id:
        raise NotionSyncError("Missing NOTION_DATABASE_ID or NOTION_DATA_SOURCE_ID.")

    schema = _fetch_database_schema(token, database_id)
    title_property = _title_property_name(schema)
    payload = {
        "parent": {"database_id": database_id},
        "properties": _build_properties(review, title_property, schema),
        "children": _build_children(review),
    }
    response = requests.post(
        f"{NOTION_API_BASE}/pages",
        headers=_headers(token),
        json=payload,
        timeout=30,
    )
    if response.status_code >= 400:
        raise NotionSyncError(
            f"Notion create page failed ({response.status_code}): {response.text[:400]}"
        )
    data = response.json()
    page_id = str(data.get("id") or "")
    if page_id:
        _set_page_icon(token, page_id, "⭐")
    return NotionPage(page_id=page_id, url=str(data.get("url") or ""))


def sync_place_reviews_page(
    place: dict[str, Any],
    reviews: list[dict[str, Any]],
    page_id: str = "",
    page_url: str = "",
    review_images: dict[str, bytes] | None = None,
) -> NotionPage:
    token = os.getenv("NOTION_ACCESS_TOKEN", "").strip()
    database_id = (
        os.getenv("NOTION_DATABASE_ID", "").strip()
        or os.getenv("NOTION_DATA_SOURCE_ID", "").strip()
    )
    if not token:
        raise NotionSyncError("Missing NOTION_ACCESS_TOKEN.")
    if not database_id:
        raise NotionSyncError("Missing NOTION_DATABASE_ID or NOTION_DATA_SOURCE_ID.")
    if not reviews:
        raise NotionSyncError("No accepted reviews to export.")

    schema = _fetch_database_schema(token, database_id)
    title_property = _title_property_name(schema)
    properties = _build_place_properties(place, reviews, title_property, schema)
    image_uploads = _upload_place_review_images(token, reviews, review_images or {})
    children = _build_place_children(reviews, image_uploads)
    normalized_page_id = page_id.strip()

    if normalized_page_id:
        response = requests.patch(
            f"{NOTION_API_BASE}/pages/{normalized_page_id}",
            headers=_headers(token),
            json={"properties": properties},
            timeout=30,
        )
        if response.status_code >= 400:
            raise NotionSyncError(
                f"Notion update page failed ({response.status_code}): {response.text[:400]}"
            )
        data = response.json()
        _replace_page_children(token, normalized_page_id, children)
        return NotionPage(
            page_id=normalized_page_id,
            url=str(data.get("url") or page_url),
        )

    payload: dict[str, Any] = {
        "parent": {"database_id": database_id},
        "properties": properties,
    }
    if not image_uploads:
        payload["children"] = children[:100]
    response = requests.post(
        f"{NOTION_API_BASE}/pages",
        headers=_headers(token),
        json=payload,
        timeout=30,
    )
    if response.status_code >= 400:
        raise NotionSyncError(
            f"Notion create page failed ({response.status_code}): {response.text[:400]}"
        )
    data = response.json()
    created_page_id = str(data.get("id") or "")
    if not created_page_id:
        raise NotionSyncError("Notion create page did not return a page ID.")
    if image_uploads:
        _append_children(token, created_page_id, children)
    else:
        _append_children(token, created_page_id, children[100:])
    _set_page_icon(token, created_page_id, "⭐")
    return NotionPage(page_id=created_page_id, url=str(data.get("url") or ""))


def append_review_image(page_id: str, image_bytes: bytes, filename: str) -> NotionUpload:
    token = os.getenv("NOTION_ACCESS_TOKEN", "").strip()
    if not token:
        raise NotionSyncError("Missing NOTION_ACCESS_TOKEN.")
    if not page_id.strip():
        raise NotionSyncError("Missing Notion page ID.")
    if not image_bytes:
        raise NotionSyncError("Image payload is empty.")

    upload_id = _create_file_upload(token, filename)
    _send_file_upload(token, upload_id, filename, image_bytes)
    _append_image_block(token, page_id, upload_id)
    return NotionUpload(file_upload_id=upload_id)


def _fetch_database_schema(token: str, database_id: str) -> dict[str, Any]:
    response = requests.get(
        f"{NOTION_API_BASE}/databases/{database_id}",
        headers=_headers(token),
        timeout=30,
    )
    if response.status_code >= 400:
        raise NotionSyncError(
            f"Notion read database failed ({response.status_code}): {response.text[:400]}"
        )
    return response.json().get("properties") or {}


def _title_property_name(properties: dict[str, Any]) -> str:
    for name, meta in properties.items():
        if isinstance(meta, dict) and meta.get("type") == "title":
            return str(name)
    raise NotionSyncError("No title property found in the Notion database.")


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def _file_headers(token: str, content_type: str = "application/json") -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_FILES_VERSION,
        "Content-Type": content_type,
    }


def _page_title(review: dict[str, Any]) -> str:
    place = str(review.get("place_name") or "Sitio").strip()
    reviewer = str(review.get("reviewer_name") or "Anónimo").strip()
    submitted_by = str(review.get("submitted_by") or "").strip()
    title = f"{place} - {reviewer}"
    if submitted_by:
        title = f"{submitted_by} -- {title}"
    return title[:180]


def _build_properties(
    review: dict[str, Any], title_property: str, schema: dict[str, Any]
) -> dict[str, Any]:
    properties: dict[str, Any] = {
        title_property: {
            "title": [
                {
                    "text": {
                        "content": _page_title(review),
                    }
                }
            ]
        }
    }

    review_url = str(review.get("review_url") or "").strip()
    owner_reply = str(review.get("owner_reply_text") or "").strip()
    area_page_id = (
        os.getenv("NOTION_AREA_PAGE_ID", "").strip() or DEFAULT_NOTION_AREA_PAGE_ID
    )

    if _has_property(schema, "URL", "url") and review_url:
        properties["URL"] = {"url": review_url}
    if _has_property(schema, "Type", "select"):
        properties["Type"] = {"select": {"name": "Review"}}
    if _has_property(schema, "Scope", "select"):
        properties["Scope"] = {"select": {"name": "personal"}}
    # Some Notion databases omit relation properties in the schema payload even
    # though they are writable on page create/update, so we set Area directly.
    if area_page_id:
        properties["Area"] = {"relation": [{"id": area_page_id}]}
    if _has_property(schema, "Tags", "multi_select") and owner_reply:
        properties["Tags"] = {"multi_select": [{"name": "Respuesta del propietario"}]}

    return properties


def _build_place_properties(
    place: dict[str, Any],
    reviews: list[dict[str, Any]],
    title_property: str,
    schema: dict[str, Any],
) -> dict[str, Any]:
    properties = _build_properties(reviews[0], title_property, schema)
    properties[title_property] = {
        "title": [{"text": {"content": _place_page_title(place, reviews)}}]
    }
    place_url = str(place.get("place_url") or reviews[0].get("review_url") or "").strip()
    if _has_property(schema, "URL", "url") and place_url:
        properties["URL"] = {"url": place_url}
    if _has_property(schema, "Tags", "multi_select"):
        has_owner_reply = any(str(review.get("owner_reply_text") or "").strip() for review in reviews)
        properties["Tags"] = {
            "multi_select": ([{"name": "Respuesta del propietario"}] if has_owner_reply else [])
        }
    return properties


def _place_page_title(place: dict[str, Any], reviews: list[dict[str, Any]]) -> str:
    place_name = str(place.get("place_name") or "Sitio").strip() or "Sitio"
    location = format_place_location(
        str(place.get("place_address") or ""),
        str(place.get("place_country") or ""),
    )
    try:
        rating = float(place.get("average_rating"))
    except (TypeError, ValueError):
        rating = 0.0
    rating_label = f"{rating:.1f}".replace(".", ",") + "/5" if rating > 0 else "Sin puntuación"
    review_count = len(reviews)
    count_label = (
        "1 reseña seleccionada"
        if review_count == 1
        else f"{review_count} reseñas seleccionadas"
    )
    suffix = f" · {location} · {rating_label} · {count_label}"
    return f"{place_name[:max(1, 180 - len(suffix))]}{suffix}"[:180]


def _build_children(review: dict[str, Any]) -> list[dict[str, Any]]:
    children: list[dict[str, Any]] = []
    reviewer = str(review.get("reviewer_name") or "Anónimo").strip() or "Anónimo"
    children.extend(_paragraph_block(reviewer))
    children.extend(_quote_blocks(str(review.get("review_text") or "").strip() or "(sin texto)"))

    owner_reply = str(review.get("owner_reply_text") or "").strip()
    if owner_reply:
        children.extend(_heading_block("Respuesta de propietario"))
        children.extend(_quote_blocks(owner_reply))
    return children


def _build_place_children(
    reviews: list[dict[str, Any]],
    image_uploads: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    children: list[dict[str, Any]] = []
    image_uploads = image_uploads or {}
    for index, review in enumerate(reviews, start=1):
        score = int(review.get("humor_score") or 0)
        children.extend(_heading_block(f"Reseña {index} · Humor {score}/100"))
        children.extend(_build_children(review))
        upload_id = image_uploads.get(str(review.get("review_id") or ""))
        if upload_id:
            children.append(_image_block(upload_id))
        if index < len(reviews):
            children.append({"object": "block", "type": "divider", "divider": {}})
    return children


def _heading_block(text: str) -> list[dict[str, Any]]:
    return [
        {
            "object": "block",
            "type": "heading_2",
            "heading_2": {"rich_text": _rich_text(text)},
        }
    ]


def _paragraph_block(text: str) -> list[dict[str, Any]]:
    return [
        {
            "object": "block",
            "type": "paragraph",
            "paragraph": {"rich_text": _rich_text(text)},
        }
    ]


def _quote_blocks(text: str) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for line in [line.strip() for line in text.splitlines() if line.strip()]:
        blocks.append(
            {
                "object": "block",
                "type": "quote",
                "quote": {"rich_text": _rich_text(line)},
            }
        )
    if not blocks:
        blocks.extend(_paragraph_block("(vacío)"))
    return blocks


def _rich_text(text: str) -> list[dict[str, Any]]:
    return [{"type": "text", "text": {"content": text[:1900]}}]


def _has_property(schema: dict[str, Any], name: str, expected_type: str) -> bool:
    meta = schema.get(name)
    return isinstance(meta, dict) and meta.get("type") == expected_type


def _replace_page_children(token: str, page_id: str, children: list[dict[str, Any]]) -> None:
    cursor = ""
    block_ids: list[str] = []
    while True:
        params: dict[str, Any] = {"page_size": 100}
        if cursor:
            params["start_cursor"] = cursor
        response = requests.get(
            f"{NOTION_API_BASE}/blocks/{page_id}/children",
            headers=_headers(token),
            params=params,
            timeout=30,
        )
        if response.status_code >= 400:
            raise NotionSyncError(
                f"Notion read page blocks failed ({response.status_code}): {response.text[:400]}"
            )
        data = response.json()
        block_ids.extend(
            str(block.get("id") or "")
            for block in data.get("results") or []
            if block.get("id")
        )
        if not data.get("has_more"):
            break
        cursor = str(data.get("next_cursor") or "")
        if not cursor:
            break

    for block_id in block_ids:
        response = requests.delete(
            f"{NOTION_API_BASE}/blocks/{block_id}",
            headers=_headers(token),
            timeout=30,
        )
        if response.status_code >= 400:
            raise NotionSyncError(
                f"Notion remove page block failed ({response.status_code}): {response.text[:400]}"
            )
    _append_children(token, page_id, children)


def _append_children(token: str, page_id: str, children: list[dict[str, Any]]) -> None:
    groups: list[tuple[bool, list[dict[str, Any]]]] = []
    for child in children:
        uses_file_upload = child.get("type") == "image"
        if not groups or groups[-1][0] != uses_file_upload:
            groups.append((uses_file_upload, []))
        groups[-1][1].append(child)

    for uses_file_upload, group in groups:
        for offset in range(0, len(group), 100):
            response = requests.patch(
                f"{NOTION_API_BASE}/blocks/{page_id}/children",
                headers=_file_headers(token) if uses_file_upload else _headers(token),
                json={"children": group[offset : offset + 100]},
                timeout=30,
            )
            if response.status_code >= 400:
                raise NotionSyncError(
                    f"Notion append page blocks failed ({response.status_code}): {response.text[:400]}"
                )


def _upload_place_review_images(
    token: str,
    reviews: list[dict[str, Any]],
    review_images: dict[str, bytes],
) -> dict[str, str]:
    uploads: dict[str, str] = {}
    for index, review in enumerate(reviews, start=1):
        review_id = str(review.get("review_id") or "").strip()
        image_bytes = review_images.get(review_id, b"")
        if not review_id or not image_bytes:
            continue
        place_name = str(review.get("place_name") or "resena").strip()
        reviewer_name = str(review.get("reviewer_name") or f"{index}").strip()
        filename_stem = re.sub(
            r"[^A-Za-z0-9._-]+", "-", f"{place_name}-{reviewer_name}"
        ).strip("-.")
        filename = f"{(filename_stem or f'resena-{index}')[:176]}.png"
        upload_id = _create_file_upload(token, filename)
        _send_file_upload(token, upload_id, filename, image_bytes)
        uploads[review_id] = upload_id
    return uploads


def _create_file_upload(token: str, filename: str) -> str:
    response = requests.post(
        f"{NOTION_API_BASE}/file_uploads",
        headers=_file_headers(token),
        json={
            "mode": "single_part",
            "filename": filename[:180],
            "content_type": "image/png",
        },
        timeout=30,
    )
    if response.status_code >= 400:
        raise NotionSyncError(
            f"Notion create file upload failed ({response.status_code}): {response.text[:400]}"
        )
    upload_id = str((response.json() or {}).get("id") or "").strip()
    if not upload_id:
        raise NotionSyncError("Notion file upload did not return an upload ID.")
    return upload_id


def _send_file_upload(token: str, upload_id: str, filename: str, image_bytes: bytes) -> None:
    response = requests.post(
        f"{NOTION_API_BASE}/file_uploads/{upload_id}/send",
        headers={
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_FILES_VERSION,
        },
        files={"file": (filename[:180], image_bytes, "image/png")},
        timeout=60,
    )
    if response.status_code >= 400:
        raise NotionSyncError(
            f"Notion send file upload failed ({response.status_code}): {response.text[:400]}"
        )


def _append_image_block(token: str, page_id: str, upload_id: str) -> None:
    payload = {
        "children": [
            _image_block(upload_id)
        ]
    }
    response = requests.patch(
        f"{NOTION_API_BASE}/blocks/{page_id}/children",
        headers=_file_headers(token),
        json=payload,
        timeout=30,
    )
    if response.status_code >= 400:
        raise NotionSyncError(
            f"Notion append image block failed ({response.status_code}): {response.text[:400]}"
        )


def _image_block(upload_id: str) -> dict[str, Any]:
    return {
        "object": "block",
        "type": "image",
        "image": {
            "caption": [],
            "type": "file_upload",
            "file_upload": {"id": upload_id},
        },
    }


def _set_page_icon(token: str, page_id: str, emoji: str) -> None:
    response = requests.patch(
        f"{NOTION_API_BASE}/pages/{page_id}",
        headers=_headers(token),
        json={"icon": {"type": "emoji", "emoji": emoji}},
        timeout=30,
    )
    if response.status_code >= 400:
        raise NotionSyncError(
            f"Notion update page icon failed ({response.status_code}): {response.text[:400]}"
        )

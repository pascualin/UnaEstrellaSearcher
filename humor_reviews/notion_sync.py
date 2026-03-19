from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import requests


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
    return f"{place} - {reviewer}"[:180]


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
            {
                "object": "block",
                "type": "image",
                "image": {
                    "caption": [],
                    "type": "file_upload",
                    "file_upload": {"id": upload_id},
                },
            }
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

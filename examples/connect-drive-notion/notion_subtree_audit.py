"""Inventory everything a Notion connection was actually handed.

Notion access is not a scope. A connection sees exactly the pages a human
added it to, plus -- and this is the part nobody plans for -- every child page
underneath them. Notion's own docs say it plainly: "Sharing a parent page with
the connection grants access to all of its child pages as well."

So the honest first move after someone shares a page with your agent is to
walk the subtree and count what arrived. This script does that, and nothing
else: no writes, no comments, no page creation.

Environment:
    NOTION_TOKEN     internal connection secret, or an OAuth access token
    NOTION_ROOT_ID   optional: one page id to walk. Omitted, it walks
                     everything /v1/search returns, which is everything shared.

Run:
    python3 notion_subtree_audit.py
"""

from __future__ import annotations

import os
import sys
import time
from typing import Any, Iterator

import requests

API = "https://api.notion.com/v1"

# Pin the version. Notion treats the header as required and a missing one is a
# 400 missing_version. Read the changelog before you move this string:
# https://developers.notion.com/page/changelog
NOTION_VERSION = "2026-03-11"

# "an average of three requests per second, with some bursts beyond the
# average allowed" -- https://developers.notion.com/reference/request-limits
MIN_INTERVAL_SECONDS = 1 / 3

MAX_DEPTH = 12
_last_call = 0.0


class NotShared(RuntimeError):
    """404 object_not_found. The page may not exist, or may not be shared.

    Notion returns the same code for both, deliberately -- it will not confirm
    the existence of something you were not given.
    """


class CapabilityDenied(RuntimeError):
    """403 restricted_resource. The token is valid; this operation is not."""


class NotionError(RuntimeError):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(f"{status} {code}: {message}")
        self.status = status
        self.code = code


def _throttle() -> None:
    global _last_call
    wait = MIN_INTERVAL_SECONDS - (time.monotonic() - _last_call)
    if wait > 0:
        time.sleep(wait)
    _last_call = time.monotonic()


def call(
    method: str,
    path: str,
    token: str,
    params: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    headers = {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }

    for attempt in range(5):
        _throttle()
        response = requests.request(
            method,
            f"{API}{path}",
            headers=headers,
            params=params,
            json=json_body,
            timeout=60,
        )
        if response.ok:
            return response.json()

        try:
            body = response.json()
        except ValueError:
            body = {}
        code = body.get("code", "")
        message = body.get("message", response.text[:200])

        if response.status_code in (429, 529) and attempt < 4:
            # Retry-After is "an integer number of seconds". Honour it rather
            # than inventing a backoff curve on top of it.
            delay = float(response.headers.get("Retry-After", 2**attempt))
            time.sleep(delay)
            continue
        if response.status_code >= 500 and attempt < 4:
            time.sleep(2**attempt)
            continue
        if code == "object_not_found":
            raise NotShared(message)
        if code == "restricted_resource":
            raise CapabilityDenied(message)
        raise NotionError(response.status_code, code, message)

    raise NotionError(429, "rate_limited", "gave up after 5 attempts")


def shared_roots(token: str) -> Iterator[dict[str, Any]]:
    """Every object /v1/search returns: what the connection can currently see.

    Search "searches all parent or child pages and data_sources that have been
    shared with a connection", so this is the closest thing to an inventory
    Notion offers. It is not a permission list -- it is the flattened result of
    however many parents somebody shared.
    """
    cursor: str | None = None
    while True:
        body: dict[str, Any] = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        page = call("POST", "/search", token, json_body=body)
        for item in page.get("results", []):
            yield item
        if not page.get("has_more"):
            return
        cursor = page.get("next_cursor")


def block_children(token: str, block_id: str) -> Iterator[dict[str, Any]]:
    cursor: str | None = None
    while True:
        params: dict[str, Any] = {"page_size": 100}
        if cursor:
            params["start_cursor"] = cursor
        page = call("GET", f"/blocks/{block_id}/children", token, params=params)
        for block in page.get("results", []):
            yield block
        if not page.get("has_more"):
            return
        cursor = page.get("next_cursor")


def title_of(obj: dict[str, Any]) -> str:
    """Titles live in different places depending on the object. Handle all of them."""
    if obj.get("object") == "block":
        block_type = obj.get("type", "")
        nested = obj.get(block_type, {})
        if isinstance(nested, dict) and isinstance(nested.get("title"), str):
            return nested["title"]

    title = obj.get("title")
    if isinstance(title, list):
        return "".join(part.get("plain_text", "") for part in title) or "<untitled>"

    for prop in (obj.get("properties") or {}).values():
        if isinstance(prop, dict) and prop.get("type") == "title":
            return "".join(
                part.get("plain_text", "") for part in prop.get("title", [])
            ) or "<untitled>"

    return "<untitled>"


def walk(token: str, page_id: str, depth: int = 0, seen: set[str] | None = None) -> int:
    """Print the subtree reachable from one page and return how many pages it holds."""
    seen = seen if seen is not None else set()
    if page_id in seen or depth > MAX_DEPTH:
        return 0
    seen.add(page_id)

    count = 1
    try:
        children = list(block_children(token, page_id))
    except NotShared:
        print(f"{'  ' * depth}! {page_id}: not shared with this connection")
        return 0
    except CapabilityDenied as error:
        # A connection without the "Read content" capability gets here even
        # though the page is shared with it.
        print(f"{'  ' * depth}! {page_id}: capability denied -- {error}")
        return 0

    for block in children:
        block_type = block.get("type")
        if block_type == "child_page":
            print(f"{'  ' * depth}  page: {title_of(block)}")
            count += walk(token, block["id"], depth + 1, seen)
        elif block_type == "child_database":
            # Databases hold rows the walk above never touches. Every row is a
            # page, and every row came with the parent.
            print(f"{'  ' * depth}  database: {title_of(block)} (rows not counted)")

    return count


def main() -> None:
    token = os.environ.get("NOTION_TOKEN")
    if not token:
        sys.exit("Set NOTION_TOKEN to the connection secret.")

    root = os.environ.get("NOTION_ROOT_ID")
    if root:
        roots = [{"id": root, "object": "page"}]
    else:
        roots = list(shared_roots(token))
        print(f"/v1/search returned {len(roots)} object(s) shared with this connection\n")

    total = 0
    for item in roots:
        if item.get("object") not in ("page", None):
            print(f"- {item.get('object')}: {title_of(item)} ({item.get('id')})")
            continue
        print(f"- {title_of(item)} ({item.get('id')})")
        total += walk(token, item["id"], depth=1)

    print(f"\nreachable pages: {total}")
    print(
        "If that number is larger than the number of pages a human meant to "
        "share, the grant is wrong and no code change fixes it. Remove the "
        "connection from the parent and add it to the leaves."
    )


if __name__ == "__main__":
    main()

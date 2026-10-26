"""Exports return a short-lived signed link (DownloadLink); these helpers follow it like a browser would."""

from __future__ import annotations

import httpx
from fastapi.testclient import TestClient


def follow(client: TestClient, response: httpx.Response) -> httpx.Response:
    """The downloaded file behind an export response ({url, file_name, expires_in, ...})."""
    assert response.status_code == 200, response.text
    link = response.json()
    assert link["url"].startswith("/files/exports/") and link["expires_in"] == 600
    file = client.get(link["url"])
    assert file.status_code == 200, file.text
    assert link["file_name"] in file.headers["content-disposition"]
    return file

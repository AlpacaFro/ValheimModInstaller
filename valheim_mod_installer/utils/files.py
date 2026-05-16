import re
from pathlib import Path
from typing import Optional
from urllib.parse import unquote, urlparse

import requests


def sanitize_filename(value: str, fallback: str = "download") -> str:
    """Return a Windows-safe filename while preserving readable mod names."""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" ._")
    return cleaned or fallback


def content_disposition_filename(header_value: Optional[str]) -> Optional[str]:
    if not header_value:
        return None

    match = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', header_value, re.IGNORECASE)
    if not match:
        return None
    return sanitize_filename(unquote(match.group(1)))


def guess_download_filename(mod: dict, response: requests.Response) -> str:
    header_name = content_disposition_filename(response.headers.get("content-disposition"))
    if header_name:
        return header_name

    url_path = unquote(urlparse(response.url).path)
    url_name = sanitize_filename(Path(url_path).name, "")
    if url_name and "." in url_name:
        return url_name

    content_type = response.headers.get("content-type", "").lower()
    if "zip" in content_type:
        suffix = ".zip"
    elif "octet-stream" in content_type:
        suffix = ".bin"
    else:
        suffix = ".download"

    return f"{sanitize_filename(mod['name'])}{suffix}"

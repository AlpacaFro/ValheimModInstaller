from pathlib import Path
from typing import Optional, Tuple
from urllib.parse import quote, unquote, urlparse

import requests

from .downloader import REQUEST_TIMEOUT, VALID_DOWNLOAD_EXTENSIONS


class UnsupportedURL(ValueError):
    """Raised when a URL is neither a direct download nor a supported Thunderstore package page."""


def parse_thunderstore_package_url(url: str) -> Optional[Tuple[str, str]]:
    """Extract (author, package) from the Thunderstore Valheim page URL formats we support."""
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    parts = [unquote(part) for part in parsed.path.strip("/").split("/") if part]

    if host in {"thunderstore.io", "www.thunderstore.io", "new.thunderstore.io"}:
        if len(parts) >= 5 and parts[0] == "c" and parts[1] == "valheim" and parts[2] == "p":
            return parts[3], parts[4]

    if host == "valheim.thunderstore.io":
        if len(parts) >= 3 and parts[0] == "package":
            return parts[1], parts[2]

    return None


def get_latest_thunderstore_download_url(author: str, package: str) -> str:
    """Ask Thunderstore's package API for the latest version download URL."""
    info = get_latest_thunderstore_package_info(author, package)
    if info.get("download_url"):
        return str(info["download_url"])
    raise ValueError("Thunderstore API response did not include a latest version download URL.")


def get_latest_thunderstore_package_info(author: str, package: str) -> dict:
    """Ask Thunderstore's package API for latest version metadata."""
    safe_author = quote(author, safe="")
    safe_package = quote(package, safe="")
    api_url = f"https://thunderstore.io/api/experimental/package/{safe_author}/{safe_package}/"

    response = requests.get(api_url, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise ValueError("Thunderstore API response was not a JSON object.")

    if data.get("download_url"):
        return {
            "version_number": str(data.get("version_number", "")),
            "download_url": str(data["download_url"]),
            "raw": data,
        }

    latest = data.get("latest")
    if isinstance(latest, dict) and latest.get("download_url"):
        return {
            "version_number": str(latest.get("version_number", "")),
            "download_url": str(latest["download_url"]),
            "raw": data,
        }

    versions = data.get("versions")
    if isinstance(versions, list) and versions:
        clean_versions = [version for version in versions if isinstance(version, dict)]
        has_dates = any(version.get("date_created") or version.get("created_at") for version in clean_versions)
        if has_dates:
            clean_versions = sorted(
                clean_versions,
                key=lambda version: version.get("date_created") or version.get("created_at") or "",
                reverse=True,
            )
        for version in clean_versions:
            if version.get("download_url"):
                return {
                    "version_number": str(version.get("version_number", "")),
                    "download_url": str(version["download_url"]),
                    "raw": data,
                }

    raise ValueError("Thunderstore API response did not include a latest version download URL.")


def is_direct_download_response(url: str, response: requests.Response) -> bool:
    """Check that a direct URL looks like a mod archive/binary instead of an HTML page."""
    content_type = response.headers.get("content-type", "").lower()
    extension = Path(unquote(urlparse(response.url or url).path)).suffix.lower()
    return extension in VALID_DOWNLOAD_EXTENSIONS or any(
        marker in content_type for marker in ("zip", "octet-stream", "x-msdownload")
    )


def resolve_download_url(url: str) -> str:
    """Return a real downloadable file URL for either direct links or supported Thunderstore pages."""
    package_id = parse_thunderstore_package_url(url)
    if package_id:
        author, package = package_id
        return get_latest_thunderstore_download_url(author, package)

    response = requests.head(url, allow_redirects=True, timeout=REQUEST_TIMEOUT)
    if response.status_code == 405:
        response.close()
        response = requests.get(url, stream=True, allow_redirects=True, timeout=REQUEST_TIMEOUT)

    try:
        if response.status_code != 200:
            raise ValueError(f"The URL returned HTTP {response.status_code}.")
        if not is_direct_download_response(url, response):
            raise UnsupportedURL(
                "Unsupported URL. Use a direct .zip/.dll download URL or a Thunderstore Valheim package page."
            )
        return response.url or url
    finally:
        response.close()

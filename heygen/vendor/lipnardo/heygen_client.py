#!/usr/bin/env python3
from __future__ import annotations
"""Lipnardo -- Shared HeyGen API Client

Importable module providing authentication, HTTP requests, polling,
and download utilities for all Lipnardo scripts.

NOT a standalone CLI. Import via:
    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from heygen_client import resolve_api_key, api_request, poll_video_status, ...

Dependencies: Python 3.8+ stdlib only (urllib.request, json, time, etc.)
Optional: pip install requests (auto-detected, used if available)
"""

import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

API_BASE = "https://api.heygen.com"
CONFIG_PATH = Path.home() / ".heygen" / "config.json"
DEFAULT_TIMEOUT = 30

# Try to use requests if available, fall back to urllib
try:
    import requests as _requests_lib
    _HAS_REQUESTS = True
except ImportError:
    _HAS_REQUESTS = False


class HeyGenAPIError(Exception):
    """Wraps HeyGen API errors with structured information."""

    def __init__(self, status_code: int, error_code: str = "", message: str = "",
                 endpoint: str = ""):
        self.status_code = status_code
        self.error_code = error_code
        self.message = message
        self.endpoint = endpoint
        super().__init__(f"HeyGen API error {status_code} on {endpoint}: {message}")

    def to_dict(self) -> dict:
        return {
            "error": True,
            "status_code": self.status_code,
            "error_code": self.error_code,
            "message": self.message,
            "endpoint": self.endpoint,
        }


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def output_json(data: dict) -> None:
    """Print structured JSON to stdout as the script's final result."""
    print(json.dumps(data, indent=2))


def log_json(data: dict) -> None:
    """Print structured JSON to stderr for progress/debug logging."""
    print(json.dumps(data), file=sys.stderr)


def error_exit(message: str, code: int = 1, **extra) -> None:
    """Print a structured error to stdout and exit."""
    result = {"error": True, "message": message}
    result.update(extra)
    output_json(result)
    sys.exit(code)


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

def resolve_api_key(cli_key: str | None = None) -> str:
    """Resolve HeyGen API key using tiered priority.

    Priority:
        1. Explicit cli_key argument (from --api-key flag)
        2. HEYGEN_API_KEY environment variable
        3. ~/.heygen/config.json file
        4. Exit with error and setup instructions

    Returns:
        API key string

    Raises:
        SystemExit if no key found
    """
    # Priority 1: CLI argument
    if cli_key:
        return cli_key

    # Priority 2: Environment variable
    env_key = os.environ.get("HEYGEN_API_KEY")
    if env_key:
        return env_key

    # Priority 3: Config file
    if CONFIG_PATH.exists():
        # Warn if config file is readable by group/others (API key exposure risk)
        import stat
        try:
            mode = CONFIG_PATH.stat().st_mode
            if mode & (stat.S_IRGRP | stat.S_IROTH):
                log_json({
                    "warning": f"Config file {CONFIG_PATH} is readable by other users. "
                               f"Run: chmod 600 {CONFIG_PATH}"
                })
        except OSError:
            pass
        try:
            with open(CONFIG_PATH, "r") as f:
                config = json.load(f)
            file_key = config.get("api_key", "").strip()
            if file_key:
                return file_key
        except (json.JSONDecodeError, OSError) as e:
            log_json({"warning": f"Failed to read {CONFIG_PATH}: {e}"})

    # Priority 4: Error with instructions
    error_exit(
        "No HeyGen API key found. Configure one of:\n"
        "  1. Export HEYGEN_API_KEY=your-key-here\n"
        "  2. Pass --api-key your-key-here\n"
        f"  3. Create {CONFIG_PATH} with: {{\"api_key\": \"your-key-here\"}}\n"
        "\nGet your key from: https://app.heygen.com/settings/api"
    )


# ---------------------------------------------------------------------------
# HTTP Client
# ---------------------------------------------------------------------------

def _urllib_request(method: str, url: str, headers: dict,
                    body: bytes | None = None, timeout: int = DEFAULT_TIMEOUT) -> tuple:
    """Make an HTTP request using urllib (stdlib).

    Returns:
        (status_code, response_dict)
    """
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    # Create SSL context that handles most certificate scenarios
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            raw = resp.read().decode("utf-8")
            try:
                data = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                data = {"raw_response": raw}
            return resp.status, data
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            data = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            data = {"raw_response": raw}
        return e.code, data
    except urllib.error.URLError as e:
        raise HeyGenAPIError(0, "CONNECTION_ERROR", str(e.reason), url)
    except TimeoutError:
        raise HeyGenAPIError(0, "TIMEOUT", f"Request timed out after {timeout}s", url)


def _requests_request(method: str, url: str, headers: dict,
                      body: bytes | None = None, timeout: int = DEFAULT_TIMEOUT) -> tuple:
    """Make an HTTP request using the requests library (optional).

    Returns:
        (status_code, response_dict)
    """
    kwargs = {"headers": headers, "timeout": timeout}
    if body:
        kwargs["data"] = body
    resp = _requests_lib.request(method, url, **kwargs)
    try:
        data = resp.json() if resp.text else {}
    except ValueError:
        data = {"raw_response": resp.text}
    return resp.status_code, data


def api_request(method: str, path: str, api_key: str,
                body: dict | None = None, timeout: int = DEFAULT_TIMEOUT,
                max_retries: int = 3) -> dict:
    """Make an authenticated request to the HeyGen API.

    Args:
        method: HTTP method (GET, POST, DELETE)
        path: API path (e.g., /v2/videos/abc123)
        api_key: HeyGen API key
        body: Request body dict (JSON-encoded automatically)
        timeout: Request timeout in seconds
        max_retries: Max retries for 429/5xx errors

    Returns:
        Parsed response dict

    Raises:
        HeyGenAPIError on non-retryable errors or exhausted retries
    """
    url = f"{API_BASE}{path}" if path.startswith("/") else f"{API_BASE}/{path}"
    headers = {
        "X-Api-Key": api_key,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    encoded_body = json.dumps(body).encode("utf-8") if body else None

    # Select HTTP backend
    do_request = _requests_request if _HAS_REQUESTS else _urllib_request

    last_error = None
    for attempt in range(max_retries + 1):
        try:
            status, data = do_request(method, url, headers, encoded_body, timeout)
        except HeyGenAPIError:
            raise
        except Exception as e:
            raise HeyGenAPIError(0, "REQUEST_FAILED", str(e), path)

        # Success
        if 200 <= status < 300:
            return data

        # Rate limit (429) — retry with exponential backoff
        if status == 429:
            last_error = HeyGenAPIError(status, "RATE_LIMITED",
                                        data.get("message", "Rate limited"), path)
            if attempt < max_retries:
                delay = (2 ** attempt) * 2  # 2s, 4s, 8s
                log_json({"retry": True, "status": 429, "attempt": attempt + 1,
                           "delay_seconds": delay, "endpoint": path})
                time.sleep(delay)
                continue

        # Server errors (5xx) — retry with fixed delay
        if 500 <= status < 600:
            last_error = HeyGenAPIError(status, "SERVER_ERROR",
                                        data.get("message", f"Server error {status}"), path)
            if attempt < max_retries:
                delay = 5
                log_json({"retry": True, "status": status, "attempt": attempt + 1,
                           "delay_seconds": delay, "endpoint": path})
                time.sleep(delay)
                continue

        # Client errors (4xx except 429) — no retry
        error_msg = data.get("message") or data.get("error", {}).get("message", f"HTTP {status}")
        error_code = data.get("code") or data.get("error", {}).get("code", "")
        raise HeyGenAPIError(status, str(error_code), error_msg, path)

    # Exhausted retries
    if last_error:
        raise last_error
    raise HeyGenAPIError(0, "UNKNOWN", "Request failed after retries", path)


# ---------------------------------------------------------------------------
# Multipart upload (for assets)
# ---------------------------------------------------------------------------

def api_upload(path: str, api_key: str, file_path: str,
               fields: dict | None = None, timeout: int = 120) -> dict:
    """Upload a file to the HeyGen API using multipart/form-data.

    Args:
        path: API path (e.g., /v1/asset)
        api_key: HeyGen API key
        file_path: Local path to file to upload
        fields: Additional form fields
        timeout: Request timeout in seconds

    Returns:
        Parsed response dict
    """
    import mimetypes
    import uuid

    url = f"{API_BASE}{path}" if path.startswith("/") else f"{API_BASE}/{path}"
    boundary = uuid.uuid4().hex
    filename = os.path.basename(file_path)
    mime_type = mimetypes.guess_type(file_path)[0] or "application/octet-stream"

    # Build multipart body
    parts = []

    # Add form fields
    if fields:
        for key, value in fields.items():
            parts.append(
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{key}"\r\n\r\n'
                f"{value}\r\n"
            )

    # Add file
    with open(file_path, "rb") as f:
        file_data = f.read()

    # Escape quotes/backslashes in filename to prevent header injection
    safe_filename = filename.replace("\\", "_").replace('"', "_").replace("\r", "_").replace("\n", "_")
    file_header = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{safe_filename}"\r\n'
        f"Content-Type: {mime_type}\r\n\r\n"
    )
    closing = f"\r\n--{boundary}--\r\n"

    body = b""
    for part in parts:
        body += part.encode("utf-8")
    body += file_header.encode("utf-8") + file_data + closing.encode("utf-8")

    headers = {
        "X-Api-Key": api_key,
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "Accept": "application/json",
    }

    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            data = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            data = {"raw_response": raw}
        error_msg = data.get("message", f"Upload failed with HTTP {e.code}")
        raise HeyGenAPIError(e.code, "UPLOAD_FAILED", error_msg, path)


# ---------------------------------------------------------------------------
# Video Status Polling
# ---------------------------------------------------------------------------

def poll_video_status(video_id: str, api_key: str,
                      initial_interval: int = 10,
                      max_interval: int = 60,
                      max_wait: int = 1800) -> dict:
    """Poll for video completion with exponential backoff.

    Polls GET /v2/videos/{video_id} until status is completed or failed.

    Backoff sequence: 10s, 20s, 40s, 60s, 60s, ...
    Maximum wait: 1800s (30 minutes, matching HeyGen's max video length)

    Args:
        video_id: HeyGen video ID
        api_key: HeyGen API key
        initial_interval: Starting poll interval in seconds
        max_interval: Maximum poll interval in seconds
        max_wait: Maximum total wait time in seconds

    Returns:
        Final status dict from the API

    Raises:
        HeyGenAPIError on timeout or API errors
    """
    interval = initial_interval
    elapsed = 0
    attempt = 0

    while elapsed < max_wait:
        attempt += 1
        try:
            resp = api_request("GET", f"/v2/videos/{video_id}", api_key)
        except HeyGenAPIError as e:
            # Tolerate transient errors during polling
            if e.status_code >= 500:
                log_json({"polling": True, "video_id": video_id, "attempt": attempt,
                           "warning": f"Transient error: {e.message}"})
                time.sleep(interval)
                elapsed += interval
                continue
            raise

        # Extract status — API nests data differently depending on version
        data = resp.get("data", resp)
        status = data.get("status", "unknown")

        log_json({
            "polling": True,
            "video_id": video_id,
            "attempt": attempt,
            "elapsed_seconds": elapsed,
            "video_status": status,
        })

        if status == "completed":
            return data

        if status == "failed":
            error_msg = data.get("error", data.get("message", "Video generation failed"))
            raise HeyGenAPIError(
                200, "GENERATION_FAILED", str(error_msg),
                f"/v2/videos/{video_id}"
            )

        # Wait with exponential backoff
        time.sleep(interval)
        elapsed += interval
        interval = min(interval * 2, max_interval)

    raise HeyGenAPIError(
        0, "POLLING_TIMEOUT",
        f"Video {video_id} did not complete within {max_wait}s",
        f"/v2/videos/{video_id}"
    )


# ---------------------------------------------------------------------------
# Security helpers
# ---------------------------------------------------------------------------

def _validate_url(url: str) -> None:
    """Reject non-HTTP(S) URLs and private/loopback hosts.

    Prevents SSRF via file://, ftp://, gopher:// schemes and downloads
    targeted at localhost/private network addresses.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        error_exit(f"Only http/https URLs allowed, got: {parsed.scheme}://")
    host = (parsed.hostname or "").lower()
    if host in ("localhost", "127.0.0.1", "::1", "0.0.0.0", ""):
        error_exit("Downloads from localhost are not allowed")


def sanitize_id(value: str, name: str = "id") -> str:
    """Ensure an ID contains only safe characters for filenames and URL paths.

    Allows alphanumerics, underscores, dots, and hyphens. Blocks path traversal
    (`/`, `\\`), shell metacharacters, and other dangerous input. Used to
    sanitize IDs returned from the HeyGen API before incorporating them into
    local file paths or downstream URLs.
    """
    if not value or not re.match(r'^[a-zA-Z0-9_.-]+$', value):
        error_exit(f"Invalid {name}: contains unsafe characters")
    return value


# ---------------------------------------------------------------------------
# File Download
# ---------------------------------------------------------------------------

def download_file(url: str, output_path: str, timeout: int = 300) -> str:
    """Download a file from a URL to a local path.

    Args:
        url: URL to download (must be http/https; localhost is blocked)
        output_path: Local file path to save to
        timeout: Download timeout in seconds

    Returns:
        Absolute path to the downloaded file
    """
    _validate_url(url)
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)

    ctx = ssl.create_default_context()

    if _HAS_REQUESTS:
        resp = _requests_lib.get(url, timeout=timeout, stream=True)
        resp.raise_for_status()
        with open(output, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)
    else:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            with open(output, "wb") as f:
                while True:
                    chunk = resp.read(8192)
                    if not chunk:
                        break
                    f.write(chunk)

    return str(output.resolve())


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def ensure_output_dir(output_dir: str | None = None) -> Path:
    """Ensure the output directory exists and return its Path.

    Default: ~/Documents/lipnardo_videos/
    """
    if output_dir:
        p = Path(output_dir)
    else:
        p = Path.home() / "Documents" / "lipnardo_videos"
    p.mkdir(parents=True, exist_ok=True)
    return p


def timestamp_filename(prefix: str = "video", ext: str = ".mp4") -> str:
    """Generate a timestamped filename like video_20260415_103022.mp4"""
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return f"{prefix}_{ts}{ext}"


def add_common_args(parser: "argparse.ArgumentParser") -> None:
    """Add common CLI arguments shared by all scripts."""
    parser.add_argument("--api-key", help="HeyGen API key (or set HEYGEN_API_KEY env)")
    parser.add_argument("--output-dir", help="Output directory (default: ~/Documents/lipnardo_videos/)")

"""Best-effort failure notifications to healthchecks.io / ntfy / webhooks.

``notify_failure`` never raises: it swallows and logs dispatch errors so a
broken or unreachable notification target cannot fail the sync itself.
"""

from __future__ import annotations

import logging
import urllib.parse

import requests

logger = logging.getLogger("edupagetasks.notify")

_TIMEOUT_S = 10.0


def notify_failure(url: str, subject: str, body: str) -> None:
    """POST/GET one best-effort failure notification. Empty url is a no-op."""
    if not url:
        return
    try:
        _dispatch(url, subject, body)
    except Exception as exc:  # noqa: BLE001 - never propagate to the caller
        logger.warning("notification to %s failed: %s", url, exc)


def _dispatch(url: str, subject: str, body: str) -> None:
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").lower()
    path = parsed.path.strip("/")
    if host == "hc-ping.com":
        uuid = path.split("/", 1)[0]
        if uuid:
            requests.get(f"https://hc-ping.com/{uuid}/fail", timeout=_TIMEOUT_S)
            return
    if host == "healthchecks.io":
        parts = path.split("/")
        if len(parts) >= 2 and parts[0] == "ping" and parts[1]:
            requests.get(
                f"https://healthchecks.io/ping/{parts[1]}/fail", timeout=_TIMEOUT_S
            )
            return
    if host == "ntfy.sh":
        topic = path.split("/", 1)[0]
        if topic:
            requests.post(
                f"https://ntfy.sh/{topic}",
                data=body,
                headers={"X-Title": subject},
                timeout=_TIMEOUT_S,
            )
            return
    if parsed.scheme in ("http", "https"):
        payload = {"subject": subject, "body": body, "message": f"{subject}: {body}"}
        requests.post(url, json=payload, timeout=_TIMEOUT_S)

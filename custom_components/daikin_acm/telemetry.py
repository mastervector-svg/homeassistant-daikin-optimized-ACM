"""KEY crowdsourcing — community-driven MAC-to-KEY database.

Two mechanisms:
1. LOOKUP: keys.json in this repo — checked first, no external calls
2. CONTRIBUTE: opens a pre-filled GitHub issue for the user to submit

No external servers. Everything is public and verifiable in the repo.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from urllib.parse import quote

_LOGGER = logging.getLogger(__name__)

KEYS_FILE = Path(__file__).parent / "keys.json"
GITHUB_REPO = "maxcloud-acm/homeassistant-daikin-optimized-ACM"
ISSUE_TITLE_PREFIX = "KEY: "


def _load_keys() -> dict[str, dict]:
    """Load keys.json from the integration directory."""
    try:
        if KEYS_FILE.exists():
            data = json.loads(KEYS_FILE.read_text())
            return {entry["mac"]: entry for entry in data if "mac" in entry}
    except Exception as err:
        _LOGGER.debug("Failed to load keys.json: %s", err)
    return {}


def lookup_key(mac: str) -> str | None:
    """Look up KEY for a MAC address from the local keys.json database.

    Returns the KEY string if found, None otherwise.
    """
    mac_clean = mac.upper().replace(":", "")
    db = _load_keys()
    entry = db.get(mac_clean)
    if entry:
        return entry.get("key")
    return None


def generate_contribution_url(
    mac: str,
    key: str,
    model: str = "",
    firmware_ver: str = "",
    serial: str = "",
) -> str:
    """Generate a GitHub issue URL pre-filled with MAC+KEY data.

    The user clicks this link to submit their key pair as a public
    GitHub issue. Maintainers then merge it into keys.json.
    """
    mac_clean = mac.upper().replace(":", "")
    title = f"{ISSUE_TITLE_PREFIX}{mac_clean[:6]}...{mac_clean[-4:]}"
    body = (
        f"## New KEY contribution\n\n"
        f"| Field | Value |\n"
        f"|-------|-------|\n"
        f"| MAC | `{mac_clean}` |\n"
        f"| KEY | `{key}` |\n"
        f"| Model | `{model}` |\n"
        f"| Firmware | `{firmware_ver}` |\n"
        f"| Serial | `{serial}` |\n\n"
        f"---\n"
        f"*Submitted via Daikin ACM integration.*\n"
    )
    url = (
        f"https://github.com/{GITHUB_REPO}/issues/new"
        f"?title={quote(title)}"
        f"&body={quote(body)}"
        f"&labels=key-contribution"
    )
    return url


def get_known_count() -> int:
    """Return the number of known MAC→KEY pairs in the database."""
    return len(_load_keys())

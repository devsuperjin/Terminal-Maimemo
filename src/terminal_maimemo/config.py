"""Local configuration / credential storage for the TUI."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

APP_NAME = "terminal_maimemo"

# The study API authenticates via the `sid` cookie (HttpOnly, Path=/study)
# that the login callback sets on tc-apis.maimemo.com.
CREDENTIAL_KEYS = {"sid", "token", "access_token", "maimemo_token", "authorization", "auth"}


def extract_credential(raw: str) -> str:
    """Extract the sid / token value from whatever the user pastes.

    Accepts a plain sid/token string, a ``Bearer ...`` value, a ``sid=...`` or
    ``token=...`` cookie fragment, a full ``Cookie:`` header, or a
    ``token: ...``-style fragment.  Returns "" when nothing looks like one.
    """
    raw = (raw or "").strip().strip('"').strip("'")
    if not raw:
        return ""
    if raw.startswith("Bearer "):
        return raw[7:].strip()
    if "=" in raw and (";" in raw or raw.count("=") > 1):
        # cookie header / cookie-jar line: pick the credential-ish key
        for part in raw.split(";"):
            key, _, value = part.strip().partition("=")
            if key.strip().lower() in CREDENTIAL_KEYS:
                value = value.strip()
                if value.startswith("Bearer "):
                    value = value[7:]
                return value
    m = re.search(r"(?:sid|token|access_token|authorization)\s*[:=]\s*[\"']?([A-Za-z0-9._~+/=-]+)", raw, re.I)
    if m:
        return m.group(1)
    # plain credential (JWT has dots; otherwise just a long alnum string)
    if re.fullmatch(r"[A-Za-z0-9._~+/=-]{10,}", raw):
        return raw
    return ""


def config_dir() -> Path:
    override = os.environ.get("MAIMEMO_CONFIG_DIR")
    if override:
        d = Path(override)
    else:
        d = Path.home() / ".terminal_maimemo"
    d.mkdir(parents=True, exist_ok=True)
    return d


def config_path() -> Path:
    return config_dir() / "config.json"


DEFAULT_CONFIG: dict = {
    "phone": "",
    "study_method": "EC",  # EC = English->Chinese, CE = Chinese->English
    "review_more_count": 10,
    "show_grade_buttons": False,
    "audio_enabled": True,  # Enable/disable audio playback
    "sid": "",
    "user": {},
}


def load_config() -> dict:
    try:
        data = json.loads(config_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return dict(DEFAULT_CONFIG)
    merged = dict(DEFAULT_CONFIG)
    merged.update(data)
    # migrate legacy "token" field to "sid"
    if not merged.get("sid") and merged.get("token"):
        merged["sid"] = merged["token"]
    return merged


def save_config(cfg: dict) -> None:
    config_path().write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def load_sid() -> str:
    return load_config().get("sid", "")


def save_sid(sid: str, user: dict | None = None) -> None:
    cfg = load_config()
    cfg["sid"] = sid
    if user:
        cfg["user"] = user
    save_config(cfg)


def clear_sid() -> None:
    cfg = load_config()
    cfg["sid"] = ""
    save_config(cfg)


# backward-compatible aliases
load_token = load_sid
save_token = save_sid
clear_token = clear_sid

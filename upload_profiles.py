"""
Shared IA upload profile store — used by both the Python IA uploader and the
RClone uploader. Each profile snapshots the GUI fields (identifier, source
folder, metadata, options) so a repeat upload self-populates with one click.
Stored in ia_upload_profiles.json (gitignored).
"""

import json
import os
import time
from pathlib import Path

_PATH = Path(__file__).parent / "ia_upload_profiles.json"


def _read() -> list:
    try:
        return json.loads(_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []


def list_profiles(uploader: str | None = None) -> list:
    """All profiles (optionally filtered by uploader), newest first.
    Adds folder_missing so the GUI can flag moved/renamed source folders."""
    profiles = _read()
    if uploader:
        profiles = [p for p in profiles if p.get("uploader") == uploader]
    for p in profiles:
        folder = (p.get("fields") or {}).get("folder") or ""
        p["folder_missing"] = bool(folder) and not os.path.isdir(folder)
    return sorted(profiles, key=lambda p: p.get("saved", ""), reverse=True)


def save_profile(name: str, uploader: str, fields: dict) -> dict:
    name = (name or "").strip()
    if not name:
        return {"ok": False, "error": "Profile needs a name"}
    profiles = _read()
    profiles = [p for p in profiles
                if not (p.get("name") == name and p.get("uploader") == uploader)]
    profiles.append({
        "name":     name,
        "uploader": uploader,
        "saved":    time.strftime("%Y-%m-%d %H:%M"),
        "fields":   fields or {},
    })
    _PATH.write_text(json.dumps(profiles, indent=1), encoding="utf-8")
    return {"ok": True, "count": len(profiles)}


def delete_profile(name: str, uploader: str) -> dict:
    profiles = _read()
    kept = [p for p in profiles
            if not (p.get("name") == name and p.get("uploader") == uploader)]
    _PATH.write_text(json.dumps(kept, indent=1), encoding="utf-8")
    return {"ok": True, "deleted": len(profiles) - len(kept)}

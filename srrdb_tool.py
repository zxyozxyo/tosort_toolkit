"""
srrdb.com Scene RAR Rebuilder
Downloads SRR files, reconstructs scene RARs, extracts NFO/SFV, creates samples.
Requires: pip install pyReScene
"""

# Python 3.13 removed nntplib; stub it out before rescene imports it
import sys as _sys
import types as _types
if "nntplib" not in _sys.modules:
    _sys.modules["nntplib"] = _types.ModuleType("nntplib")

import os
import sys
import json
import shutil
import re
import time
import tempfile
import threading
import subprocess
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import HTTPError
from urllib.parse import quote

# rescene expects rar executables named YYYY-MM-DD_rar<MAJOR><MINOR>[b<N>].exe
# The date is the WinRAR release date and is used to sort versions for reconstruction.
_WINRAR_DATES: dict[str, str] = {
    "420": "2013-06-15",
    "411": "2013-03-14",
    "410": "2013-01-22",
    "401": "2012-09-09",
    "400": "2012-01-11",
    "393": "2010-12-23",
    "392": "2010-09-28",
    "391": "2010-07-19",
    "390": "2009-09-23",
    "380": "2008-09-22",
    "371": "2007-07-05",
    "370": "2007-06-07",
    "362": "2006-05-30",
    "361": "2006-04-05",
    "360": "2005-11-21",
    "351": "2005-09-26",
    "350": "2005-08-22",
    "342": "2005-02-22",
    "341": "2004-11-04",
    "340": "2004-09-10",
    "330": "2004-07-20",
    "320": "2004-05-20",
    "310": "2004-01-26",
    "302": "2003-10-01",
    "301": "2003-06-04",
    "300": "2003-03-10",
    "293": "2002-08-14",
    "291": "2001-11-29",
    "290": "2001-08-29",
    "281": "2001-07-19",
    "280": "2001-06-01",
    "272": "2001-01-29",
    "271": "2001-01-11",
    "270": "2000-11-30",
    "260": "2000-05-20",
    # RAR 5.x (released 2013–2020)
    "500": "2013-10-12", "501": "2013-12-05",
    "510": "2014-04-16", "511": "2014-05-21",
    "520": "2014-12-18", "521": "2015-06-11",
    "530": "2015-08-10", "531": "2016-04-21",
    "540": "2016-10-25", "550": "2017-05-16",
    "560": "2018-02-05", "561": "2018-06-05",
    "570": "2019-05-06", "571": "2019-08-15",
    "580": "2020-01-14", "590": "2020-05-07", "591": "2020-07-27",
    # RAR 6.x (released 2020–)
    "600": "2020-12-08", "601": "2021-01-25", "602": "2021-07-08",
}
# regex for rescene-format rar executables
_RESCENE_RAR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_rar\d+(?:b\d)?\.(exe)?$", re.IGNORECASE)

import webview

SRRDB_API    = "https://api.srrdb.com/v1"
SRRDB_DL_SRR = "https://www.srrdb.com/download/srr/{}"

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
HEADERS = {"User-Agent": _UA, "Accept": "application/json, */*"}

MEDIA_EXTS = {
    ".avi", ".mkv", ".mp4", ".m4v", ".mov", ".wmv",
    ".iso", ".img", ".bin", ".cue", ".nrg", ".vob", ".ts", ".m2ts",
}

# Metadata files placed in the output folder before/besides reconstruction
META_EXTS = {".srr", ".nfo", ".sfv", ".nzb", ".jpg", ".jpeg", ".png", ".diz", ".txt"}


def _normalize_name(name: str) -> str:
    """Convert folder/file names to scene dot-notation for better srrdb search."""
    # Replace spaces, underscores, hyphens-surrounded-by-spaces with dots
    name = re.sub(r"[\s_]+", ".", name)
    # Collapse multiple dots
    name = re.sub(r"\.{2,}", ".", name)
    return name.strip(".")


def _find_script(base_name: str) -> str | None:
    """Find srr.py / srs.py in the current Python's Scripts directory."""
    scripts_dir = Path(sys.executable).parent / "Scripts"
    for candidate in (base_name + ".py", base_name + ".exe", base_name):
        p = scripts_dir / candidate
        if p.exists():
            return str(p)
    # Also check PATH
    found = shutil.which(base_name) or shutil.which(base_name + ".exe")
    return found or None


class SrrdbToolAPI:
    def __init__(self):
        self._window  = None
        self._stop    = threading.Event()
        self._running = False
        self._app_dir = Path(__file__).parent / "apps"
        self._srr_script = None
        self._srs_script = None

    def set_window(self, w):
        self._window = w

    # ── Internal ──────────────────────────────────────────────────────────────

    def _emit(self, event: str, data: dict):
        if not self._window:
            return
        try:
            # Escape for the enclosing single-quoted JS string literal:
            # backslashes first (Windows paths!), then quotes. Without this,
            # any message containing a backslash fails JSON.parse in the GUI
            # and the log line is silently dropped.
            payload = (json.dumps(data, ensure_ascii=True)
                       .replace("\\", "\\\\").replace("'", "\\'"))
            self._window.evaluate_js(
                f"window.srrEvent('{event}', JSON.parse('{payload}'))"
            )
        except Exception:
            pass

    def _log(self, msg: str, cls: str = "info"):
        self._emit("log", {"msg": msg, "cls": cls})

    # Be a polite API client: srrdb publishes no rate limits but runs anti-bot
    # protection, so throttle to ~1 req/s, retry once on 429/503 with backoff,
    # and cache GET responses for the session (auto-match re-tests the same
    # releases across runs).
    _API_MIN_INTERVAL = 1.0
    _api_lock = threading.Lock()
    _api_last_call = 0.0
    _api_cache: dict[str, dict] = {}

    def clear_session_cache(self) -> dict:
        """Drop cached API responses — used by the GUI Rescan button so a
        re-test reflects current disk + srrdb state, not remembered results."""
        n = len(SrrdbToolAPI._api_cache)
        SrrdbToolAPI._api_cache.clear()
        return {"ok": True, "cleared": n}

    def _api_get(self, url: str) -> dict:
        cached = self._api_cache.get(url)
        if cached is not None:
            return cached
        with SrrdbToolAPI._api_lock:
            wait = SrrdbToolAPI._api_last_call + self._API_MIN_INTERVAL - time.time()
            if wait > 0:
                time.sleep(wait)
            SrrdbToolAPI._api_last_call = time.time()
        req = Request(url, headers=HEADERS)
        try:
            with urlopen(req, timeout=15) as r:
                data = json.loads(r.read().decode("utf-8", errors="replace"))
        except HTTPError as e:
            if e.code in (429, 503):
                retry_after = min(int(e.headers.get("Retry-After", 10) or 10), 60)
                self._log(
                    f"  srrdb rate limit (HTTP {e.code}) — waiting {retry_after}s…",
                    "warn",
                )
                time.sleep(retry_after)
                with urlopen(Request(url, headers=HEADERS), timeout=15) as r:
                    data = json.loads(r.read().decode("utf-8", errors="replace"))
            else:
                raise
        if len(self._api_cache) > 500:
            self._api_cache.clear()
        self._api_cache[url] = data
        return data

    # ── rescene / script detection ────────────────────────────────────────────

    def check_rescene(self) -> dict:
        """Check whether pyReScene is importable and srr/srs scripts are findable."""
        import_ok = False
        version   = None
        try:
            import rescene.main  # type: ignore
            version   = getattr(rescene, "__version__", "installed")
            import_ok = True
        except Exception:
            pass

        self._srr_script = _find_script("srr")
        self._srs_script = _find_script("srs")

        srr_ok = import_ok or bool(self._srr_script)
        srs_ok = bool(self._srs_script)

        winrar_pack = self._app_dir / "winrar_pack-4.20"
        rar_versions = []
        if winrar_pack.is_dir():
            rar_versions = [f.name for f in winrar_pack.iterdir()
                            if f.is_file() and _RESCENE_RAR_RE.match(f.name)]
        has_pack = winrar_pack.is_dir() and any(winrar_pack.rglob("wrar*.exe"))

        return {
            "rescene":      srr_ok,
            "srs":          srs_ok,
            "import_ok":    import_ok,
            "version":      version,
            "srr_script":   self._srr_script,
            "srs_script":   self._srs_script,
            "hint":         "pip install pyReScene" if not srr_ok else None,
            "rar_versions": rar_versions,
            "has_pack":     has_pack,
        }

    def setup_rar_executables(self) -> dict:
        """
        Extract rar.exe from each wrar*.exe installer in apps/winrar_pack-4.20/ and
        save as YYYY-MM-DD_rar<MAJOR><MINOR>.exe (the format rescene requires).
        Originals (wrar*.exe) and any other files are never touched.
        Old wrongly-named rar_X.YY.exe files from a previous run are removed.
        """
        winrar_pack = self._app_dir / "winrar_pack-4.20"
        if not winrar_pack.is_dir():
            return {"ok": False, "error": "apps/winrar_pack-4.20/ not found"}

        # Find 7z
        seven_zip = None
        for name in ("7z.exe", "7za.exe", "7zr.exe"):
            p = self._app_dir / name
            if p.exists():
                seven_zip = str(p)
                break
        if not seven_zip:
            seven_zip = shutil.which("7z") or shutil.which("7za")
        if not seven_zip:
            return {"ok": False, "error": "7z.exe not found in apps/ — needed to unpack installers"}

        installers = sorted(winrar_pack.rglob("wrar*.exe"))
        if not installers:
            return {"ok": False, "error": "No wrar*.exe installers found in apps/winrar_pack-4.20/ (searched recursively)"}

        done = skipped = failed = 0
        messages = []

        # Remove old wrongly-named files from previous (broken) runs
        old_re = re.compile(r"^rar[_\-]\d+[\._]\d+\.exe$", re.IGNORECASE)
        for f in winrar_pack.iterdir():
            if f.is_file() and old_re.match(f.name):
                try:
                    f.unlink()
                    messages.append(f"  Removed old {f.name}")
                except Exception:
                    pass

        # RAR 5.x+ executables ARE useful: post-2013 scene releases were made
        # with modern WinRAR in RAR4 mode (-ma4). The rebuilder injects -ma4
        # into every 5.x+ invocation so they always produce RAR4 output that
        # rescene can read back.
        for installer in installers:
            m = re.match(r"wrar(\d)(\d{2})(b\d)?\.exe$", installer.name, re.IGNORECASE)
            if not m:
                continue
            major, minor, beta = m.group(1), m.group(2), (m.group(3) or "")
            ver_key = f"{major}{minor}"
            date = _WINRAR_DATES.get(ver_key, f"200{major}-01-01")  # fallback date
            target_name = f"{date}_rar{ver_key}{beta}.exe"
            target = winrar_pack / target_name

            # Remove any existing file for this version with a WRONG date
            stale_re = re.compile(
                rf"^\d{{4}}-\d{{2}}-\d{{2}}_rar{re.escape(ver_key)}{re.escape(beta)}\.exe$",
                re.IGNORECASE,
            )
            for stale in list(winrar_pack.iterdir()):
                if stale.is_file() and stale_re.match(stale.name) and stale != target:
                    try:
                        stale.unlink()
                        messages.append(f"  Removed stale {stale.name}")
                    except Exception:
                        pass

            if target.exists():
                messages.append(f"  {target_name} — already exists, skipped")
                skipped += 1
                continue

            try:
                with tempfile.TemporaryDirectory() as tmpdir:
                    subprocess.run(
                        [seven_zip, "e", str(installer), "rar.exe", f"-o{tmpdir}", "-y"],
                        capture_output=True, text=True, timeout=30,
                    )
                    extracted = Path(tmpdir) / "rar.exe"
                    if extracted.exists():
                        shutil.copy2(str(extracted), str(target))
                        messages.append(f"  {target_name} ✓  (from {installer.name})")
                        done += 1
                    else:
                        messages.append(f"  {installer.name} — rar.exe not found inside archive")
                        failed += 1
            except subprocess.TimeoutExpired:
                messages.append(f"  {installer.name} — timed out")
                failed += 1
            except Exception as e:
                messages.append(f"  {installer.name} — ERROR: {e}")
                failed += 1

        return {"ok": True, "done": done, "skipped": skipped, "failed": failed, "messages": messages}

    # ── Browse ────────────────────────────────────────────────────────────────

    def browse_folder(self) -> str:
        try:
            import tkinter as tk
            from tkinter import filedialog
            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            path = filedialog.askdirectory()
            root.destroy()
            return path or ""
        except Exception:
            return ""

    # ── Folder scanning ───────────────────────────────────────────────────────

    def scan_folder(self, folder: str) -> dict:
        """Detect release name and content files in a single folder.
        Also checks immediate subdirectories so double-nested releases are found."""
        base = Path(folder)
        if not base.is_dir():
            return {"ok": False, "error": "Not a folder"}

        def _glob_with_sub(pattern: str) -> list[Path]:
            """Glob top-level, then fall back to immediate subfolders if empty."""
            hits = sorted(base.glob(pattern))
            if not hits:
                for sub in base.iterdir():
                    if sub.is_dir():
                        hits.extend(sub.glob(pattern))
                hits.sort()
            return hits

        nfos  = _glob_with_sub("*.nfo") or _glob_with_sub("*.NFO")
        sfvs  = _glob_with_sub("*.sfv") or _glob_with_sub("*.SFV")
        media = sorted(
            f for f in base.rglob("*")
            if f.is_file() and f.suffix.lower() in MEDIA_EXTS
        )
        if nfos:
            raw = nfos[0].stem
        elif sfvs:
            raw = sfvs[0].stem
        else:
            raw = base.name
        guess = _normalize_name(raw)
        return {
            "ok":            True,
            "release_guess": guess,
            "nfos":          [f.name for f in nfos],
            "sfvs":          [f.name for f in sfvs],
            "media":         [f.name for f in media],
        }

    def scan_subfolders(self, folder: str) -> list:
        """Return a scan dict for each immediate subfolder."""
        base = Path(folder)
        if not base.is_dir():
            return []
        results = []
        for sub in sorted(base.iterdir()):
            if not sub.is_dir():
                continue
            info = self.scan_folder(str(sub))
            results.append({
                "path":          str(sub),
                "name":          sub.name,
                "release_guess": info.get("release_guess", _normalize_name(sub.name)),
                "media_count":   len(info.get("media", [])),
                "has_nfo":       bool(info.get("nfos")),
                "has_sfv":       bool(info.get("sfvs")),
            })
        return results

    # ── srrdb.com API ─────────────────────────────────────────────────────────

    def _do_search(self, q: str) -> dict:
        """Raw srrdb search for exactly the query string q."""
        try:
            data = self._api_get(f"{SRRDB_API}/search/{quote(q)}")
            results = [
                {
                    "release": r.get("release", ""),
                    "date":    (r.get("date") or "")[:10],
                    "hasNFO":  r.get("hasNFO") == "yes",
                    "hasSRS":  r.get("hasSRS") == "yes",
                }
                for r in data.get("results", [])[:100]
            ]
            return {
                "ok":      True,
                "count":   int(data.get("resultsCount", 0)),
                "results": results,
                "query":   q,
            }
        except HTTPError as e:
            return {"ok": False, "error": f"HTTP {e.code}"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def search_srrdb(self, query: str) -> dict:
        """Search srrdb.com — normalises spacing to dots, falls back to underscores."""
        q = _normalize_name(query.strip())
        if not q:
            return {"ok": False, "error": "Empty query"}
        res = self._do_search(q)
        if res.get("ok") and res["count"] == 0 and "." in q:
            q_alt = q.replace(".", "_")
            res2 = self._do_search(q_alt)
            if res2.get("ok") and res2["count"] > 0:
                return res2
        return res

    def search_srrdb_progressive(self, query: str) -> dict:
        """
        Name search with progressive fallback for poorly-named folders.
        Tries the full name first, then strips trailing dot-tokens one at a time
        (removes platform/region suffixes), then tries without the group tag.
        Returns the first query that gets results, or the 0-result response.
        """
        q = _normalize_name(query.strip())
        if not q:
            return {"ok": False, "error": "Empty query"}

        candidates: list[str] = [q]

        # Try with underscores variant inline
        def _try(attempt: str) -> dict | None:
            res = self._do_search(attempt)
            if res.get("ok") and res["count"] > 0:
                res["query"] = attempt
                return res
            if "." in attempt:
                res2 = self._do_search(attempt.replace(".", "_"))
                if res2.get("ok") and res2["count"] > 0:
                    res2["query"] = attempt.replace(".", "_")
                    return res2
            return None

        # Strip the group tag (everything after the last hyphen) as a high-priority variant
        m = re.match(r"^(.+)-([A-Za-z0-9]{2,12})$", q)
        if m:
            candidates.append(m.group(1))  # without -GROUP

        # Progressively remove trailing dot-tokens (handles region/platform suffixes)
        parts = q.split(".")
        for trim in range(1, min(5, len(parts))):
            shorter = ".".join(parts[:-trim])
            if shorter and shorter not in candidates:
                candidates.append(shorter)

        for attempt in candidates:
            hit = _try(attempt)
            if hit:
                hit["query_trimmed"] = (attempt != q)  # flag that we simplified
                return hit

        # Nothing found — return zero-result response with original query
        return {"ok": True, "count": 0, "results": [], "query": q, "query_trimmed": False}

    def search_by_crc(self, crc32_hex: str) -> dict:
        """Search srrdb.com by archived-file CRC32 — the CRC of the content file
        INSIDE the RARs (e.g. the movie file), not the RAR volume CRCs from an
        SFV (those are not searchable). Exact-match, name-independent."""
        crc = crc32_hex.strip().upper().zfill(8)
        try:
            data = self._api_get(f"{SRRDB_API}/search/archive-crc:{crc}")
            results = [
                {
                    "release": r.get("release", ""),
                    "date":    (r.get("date") or "")[:10],
                    "hasNFO":  r.get("hasNFO") == "yes",
                    "hasSRS":  r.get("hasSRS") == "yes",
                }
                for r in data.get("results", [])[:10]
            ]
            return {
                "ok":      True,
                "count":   int(data.get("resultsCount", 0)),
                "results": results,
                "query":   f"archive-crc:{crc}",
                "by_crc":  True,
            }
        except HTTPError as e:
            return {"ok": False, "error": f"HTTP {e.code}"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    @staticmethod
    def _parse_sfv(sfv_path: str) -> list:
        """Parse an SFV file and return list of (filename, crc32_hex) tuples."""
        entries = []
        try:
            with open(sfv_path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.rstrip()
                    if not line or line.startswith(";"):
                        continue
                    parts = line.rsplit(" ", 1)
                    if len(parts) == 2 and len(parts[1]) == 8:
                        try:
                            int(parts[1], 16)
                            entries.append((parts[0].strip(), parts[1].upper()))
                        except ValueError:
                            pass
        except Exception:
            pass
        return entries

    def search_by_content_hash(self, folder: str) -> dict:
        """Hash the largest media file in the folder (CRC32) and look it up on
        srrdb via archive-crc. Works no matter how files/folders are named —
        the CRC of the original content file is stored in the database."""
        base = Path(folder)
        media = sorted(
            (f for f in base.rglob("*")
             if f.is_file() and f.suffix.lower() in MEDIA_EXTS),
            key=lambda f: f.stat().st_size, reverse=True,
        )
        if not media:
            return {"ok": True, "count": 0, "results": [], "query": "(no media file)"}
        target = media[0]
        self._log(
            f"  Hashing {target.name} ({target.stat().st_size:,} B) "
            "for exact srrdb CRC lookup…", "dim",
        )
        t0 = time.time()
        crc = self._crc32_file(str(target))
        self._log(f"  CRC32 = {crc:08X} ({time.time() - t0:.0f}s)", "dim")
        return self.search_by_crc(f"{crc:08X}")

    def search_from_folder(self, folder: str) -> dict:
        """
        Progressive name search (NFO/SFV stem preferred over folder name), then
        content-hash CRC fallback when the name finds nothing.
        Looks for SFV/NFO in the folder AND one level of subfolders.
        Returns the same structure as search_srrdb plus 'method' field.
        """
        base = Path(folder)

        sfv_paths: list[Path] = (
            sorted(base.glob("*.sfv")) + sorted(base.glob("*.SFV"))
            + sorted(p for sub in base.iterdir() if sub.is_dir()
                     for p in list(sub.glob("*.sfv")) + list(sub.glob("*.SFV")))
        )
        nfo_paths: list[Path] = (
            sorted(base.glob("*.nfo")) + sorted(base.glob("*.NFO"))
            + sorted(p for sub in base.iterdir() if sub.is_dir()
                     for p in list(sub.glob("*.nfo")) + list(sub.glob("*.NFO")))
        )
        if nfo_paths:
            name_hint = nfo_paths[0].stem
        elif sfv_paths:
            name_hint = sfv_paths[0].stem
        else:
            name_hint = base.name

        res = self.search_srrdb_progressive(name_hint)
        trimmed = res.pop("query_trimmed", False)
        if res.get("ok") and res.get("count", 0) > 0:
            res["method"] = ("name (simplified)" if trimmed else "name")
            return res

        # Name found nothing — hash the content file for an exact CRC match
        hres = self.search_by_content_hash(folder)
        if hres.get("ok") and hres.get("count", 0) > 0:
            hres["method"] = f"content CRC32 ({hres.get('query', '')})"
            return hres

        res["method"] = ("name (simplified)" if trimmed else "name")
        return res

    def get_release_details(self, release_name: str) -> dict:
        try:
            data = self._api_get(f"{SRRDB_API}/details/{quote(release_name)}")
            return {"ok": True, "details": data}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def score_release_against_folder(self, release_name: str, content_dir: str) -> float:
        """
        Score how well a release matches a content folder (0.0–1.0).
        Uses srrdb /details API to get expected archived files — no SRR download needed.
        Matches by filename (case-insensitive) and file size (within 1%).
        """
        try:
            data = self._api_get(f"{SRRDB_API}/details/{quote(release_name)}")
            archived = data.get("archived-files", [])
            if not archived:
                return 0.0
            content = {
                f.name.lower(): f.stat().st_size
                for f in Path(content_dir).rglob("*")
                if f.is_file()
            }
            content_sizes = set(content.values())
            score = 0.0
            for af in archived:
                name = af.get("name", "").lower()
                size = int(af.get("size", -1))
                if name in content:
                    local_size = content[name]
                    # Size within 1% → strong hit; name only → weak hit
                    if size <= 0 or abs(local_size - size) / max(size, 1) < 0.01:
                        score += 1.0
                    else:
                        score += 0.3
                elif size > 0 and size in content_sizes:
                    # Renamed content file — an exact byte-size match is a
                    # near-unique fingerprint for large media files.
                    score += 0.7
            return score / len(archived)
        except Exception:
            return 0.0

    def find_best_match(self, candidates: list, content_dir: str, max_test: int = 10) -> dict:
        """
        Score up to max_test candidates against content_dir using the details API.
        Returns {release, score} for the best match, or {release: None} if nothing scores > 0.
        """
        best_release = None
        best_score   = 0.0
        tested = 0
        for c in candidates[:max_test]:
            release = c.get("release", "") if isinstance(c, dict) else str(c)
            if not release:
                continue
            score = self.score_release_against_folder(release, content_dir)
            self._log(f"    [{score:.0%}] {release}", "dim")
            tested += 1
            if score > best_score:
                best_score   = score
                best_release = release
            if best_score >= 0.999:
                break  # perfect match — skip remaining API calls
        return {"release": best_release, "score": best_score, "tested": tested}

    # ── SRR download ──────────────────────────────────────────────────────────

    def _do_download_srr(self, release_name: str, dest: Path) -> dict:
        """Single attempt — download SRR for exact release_name into dest folder."""
        url      = SRRDB_DL_SRR.format(quote(release_name))
        out_path = dest / f"{release_name}.srr"
        self._log(f"  → {url}", "dim")
        try:
            req = Request(url, headers={**HEADERS, "Referer": "https://www.srrdb.com/"})
            with urlopen(req, timeout=60) as r:
                data = r.read()
            if not data:
                return {"ok": False, "not_found": False, "error": "Empty response"}
            if data[:2] == b"PK":
                import zipfile, io as _io
                with zipfile.ZipFile(_io.BytesIO(data)) as z:
                    srr_names = [n for n in z.namelist() if n.lower().endswith(".srr")]
                    if not srr_names:
                        return {"ok": False, "not_found": False, "error": "ZIP contained no SRR"}
                    out_path.write_bytes(z.read(srr_names[0]))
            else:
                out_path.write_bytes(data)
            return {"ok": True, "srr_path": str(out_path), "size": out_path.stat().st_size}
        except HTTPError as e:
            return {"ok": False, "not_found": e.code == 404, "error": f"HTTP {e.code}"}
        except Exception as e:
            return {"ok": False, "not_found": False, "error": str(e)}

    def download_srr(self, release_name: str, dest_dir: str) -> dict:
        """
        Download SRR, automatically retrying with dots/underscores swapped if the
        first attempt 404s (srrdb stores some releases with underscores, some with dots).
        """
        dest = Path(dest_dir)
        dest.mkdir(parents=True, exist_ok=True)

        names_to_try = [release_name]
        alt = release_name.replace(".", "_") if "." in release_name else release_name.replace("_", ".")
        if alt != release_name:
            names_to_try.append(alt)

        for name in names_to_try:
            result = self._do_download_srr(name, dest)
            if result["ok"]:
                if name != release_name:
                    self._log(f"  (srrdb uses '{name}' — saved under that name)", "dim")
                return result
            if not result.get("not_found"):
                return result  # non-404 error, don't retry
        tried = " / ".join(names_to_try)
        return {"ok": False, "error": f"Release not found on srrdb.com (tried: {tried})"}

    # ── rescene operations ────────────────────────────────────────────────────

    def _srr_extract(self, srr_path: str, out_dir: str) -> dict:
        """Extract stored files (NFO, SFV, SRS…) from an SRR using the rescene Python API."""
        Path(out_dir).mkdir(parents=True, exist_ok=True)
        try:
            import rescene.main as rm  # type: ignore
            rm.extract_files(
                srr_file=str(srr_path),
                out_folder=str(out_dir),
                extract_paths=True,
            )
            files = [str(f.relative_to(out_dir)) for f in Path(out_dir).rglob("*") if f.is_file()]
            return {"ok": True, "files": files}
        except ImportError:
            return {"ok": False, "error": "rescene not available — pip install pyReScene"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def _find_rar_dir(self) -> str | None:
        """Find directory with rescene-format rar executables (YYYY-MM-DD_rar*.exe)."""
        for candidate in (self._app_dir / "winrar_pack-4.20", self._app_dir):
            if candidate.is_dir() and any(
                _RESCENE_RAR_RE.match(f.name) for f in candidate.iterdir() if f.is_file()
            ):
                return str(candidate)
        return None

    @staticmethod
    def _rar_set_prefix(vol_name: str) -> str:
        """Group RAR volume names into sets: name.rar/name.r00 → 'name';
        name.part01.rar → 'name'. Keeps any stored path (e.g. 'Subs/name')."""
        base = re.sub(r"\.[^.]+$", "", vol_name)
        base = re.sub(r"\.part\d+$", "", base, flags=re.IGNORECASE)
        return base

    def _srr_rar_sets(self, srr_path: str) -> dict:
        """Map each RAR set in the SRR to the content files packed inside it.
        Returns {set_prefix: {"volumes": [...], "packed": [...]}}."""
        from rescene.rar import RarReader, BlockType  # type: ignore
        sets: dict[str, dict] = {}
        current = None
        for block in RarReader(str(srr_path)).read_all():
            if block.rawtype == BlockType.SrrRarFile:
                current = self._rar_set_prefix(getattr(block, "file_name", ""))
                entry = sets.setdefault(current, {"volumes": [], "packed": []})
                entry["volumes"].append(getattr(block, "file_name", ""))
            elif block.rawtype == BlockType.RarPackedFile and current is not None:
                fname = getattr(block, "file_name", "")
                if fname and fname not in sets[current]["packed"]:
                    sets[current]["packed"].append(fname)
        return sets

    def _srr_reconstruct(self, srr_path: str, content_dir: str, out_dir: str,
                         log_rar_pack: bool = True) -> dict:
        """Reconstruct RARs using the rescene Python API.

        SRRs can describe multiple RAR sets (movie + Subs + …). Sets whose
        source files are missing from the content folder (typically subtitle
        data, which is not inside the video file) are skipped individually so
        they cannot take the rebuildable sets down with them."""
        import io
        from contextlib import redirect_stdout, redirect_stderr

        Path(out_dir).mkdir(parents=True, exist_ok=True)

        rar_dir = self._find_rar_dir()
        if log_rar_pack:
            if rar_dir:
                rar_exes = [f.name for f in Path(rar_dir).iterdir()
                            if f.is_file() and _RESCENE_RAR_RE.match(f.name)]
                self._log(f"  RAR pack: {len(rar_exes)} version(s) — {', '.join(sorted(rar_exes))}", "dim")
            else:
                self._log("  No rar_X.YY.exe found — use Setup RAR versions button if release is compressed", "dim")

        # Heartbeat so large files don't look frozen
        done_flag = threading.Event()
        def _hb():
            secs = 0
            while not done_flag.wait(30):
                secs += 30
                self._log(f"  Still reconstructing… ({secs}s)", "dim")
        threading.Thread(target=_hb, daemon=True).start()

        def _explain(err: str) -> str:
            if "rar5" in err.lower() or "not yet supported" in err.lower():
                return ("RAR5 data encountered — if the release is RAR5 it cannot be "
                        "rebuilt (pyReScene 0.7 limit). If the SRR info above says RAR4, "
                        "a RAR 5.x+ rar.exe in the pack poisoned the test archive — "
                        "click Setup RAR versions to purge 5.x+ binaries, then retry")
            if "rar executable" in err.lower() or "no rar" in err.lower():
                return err + " — use Setup RAR versions button then retry"
            if "no good rar" in err.lower():
                return err + " — exact WinRAR version not in pack; try adding more versions"
            return err

        buf = io.StringIO()
        try:
            import rescene.main as rm  # type: ignore

            # RAR 5.x+ binaries create RAR5-format archives by default, which
            # rescene cannot read back — but with -ma4 they produce RAR4 output
            # and become valid candidates for post-2013 releases (scene groups
            # kept using RAR4 format with modern WinRAR versions). rescene
            # predates RAR5, so inject the switch into its rar.exe invocations.
            if not getattr(SrrdbToolAPI, "_ma4_patched", False):
                _orig_popen = rm.custom_popen
                _r5plus = re.compile(r"\d{4}-\d{2}-\d{2}_rar[5-9]\d\d(b\d)?\.exe$",
                                     re.IGNORECASE)
                def _inject(cmd):
                    if (len(cmd) >= 3 and str(cmd[1]).lower() == "a"
                            and _r5plus.search(str(cmd[0]))
                            and "-ma4" not in cmd):
                        return [cmd[0], cmd[1], "-ma4"] + list(cmd[2:])
                    return cmd
                def _popen_ma4(cmd, *a, **kw):
                    try:
                        cmd = _inject(cmd)
                    except Exception:
                        pass
                    return _orig_popen(cmd, *a, **kw)
                rm.custom_popen = _popen_ma4
                # The final full-file compression calls subprocess.Popen with
                # RarExecutable.full() directly — patch that path as well.
                _orig_full = rm.RarExecutable.full
                def _full_ma4(rar_self):
                    try:
                        return _inject(_orig_full(rar_self))
                    except Exception:
                        return _orig_full(rar_self)
                rm.RarExecutable.full = _full_ma4
                SrrdbToolAPI._ma4_patched = True

            # Stream rescene's internal events (version testing, rar.exe errors,
            # CRC results) to the GUI — essential visibility for compressed
            # rebuilds, which report almost nothing on stdout.
            if not getattr(SrrdbToolAPI, "_rescene_subscribed", False):
                _noisy = {rm.MsgCode.BLOCK, rm.MsgCode.RBLOCK, rm.MsgCode.FBLOCK,
                          rm.MsgCode.STORING}
                def _on_rescene_event(e, _self=self, _noisy=_noisy):
                    try:
                        msg = str(getattr(e, "message", "") or "").strip()
                        if msg and getattr(e, "code", None) not in _noisy:
                            _self._log(f"    rescene: {msg}", "dim")
                    except Exception:
                        pass
                rm.subscribe(_on_rescene_event)
                SrrdbToolAPI._rescene_subscribed = True

            # Work out which RAR sets have their source content available
            try:
                rar_sets = self._srr_rar_sets(srr_path)
            except Exception:
                rar_sets = {}
            content_names = {
                f.name.lower() for f in Path(content_dir).rglob("*") if f.is_file()
            }
            skip_parts: list[str] = []
            run_parts:  list[str] = []
            if len(rar_sets) > 1:
                self._log(f"  SRR describes {len(rar_sets)} RAR sets:", "dim")
                for prefix, info in rar_sets.items():
                    missing = [p for p in info["packed"]
                               if Path(p).name.lower() not in content_names]
                    if missing:
                        skip_parts.append(prefix)
                        self._log(
                            f"    ✗ {prefix} — skipped, source not in content folder: "
                            f"{', '.join(Path(m).name for m in missing[:4])}"
                            + ("…" if len(missing) > 4 else ""), "warn",
                        )
                    else:
                        run_parts.append(prefix)
                        self._log(f"    ✓ {prefix} ({len(info['volumes'])} volume(s))", "dim")
                if not run_parts:
                    return {"ok": False, "files": [], "output": "",
                            "error": "No RAR set has its source files in the content folder"}

            base_kwargs: dict = dict(
                srr_file=str(srr_path),
                in_folder=str(content_dir),
                out_folder=str(out_dir),
                extract_files=False,
                # Content files are often renamed (hash names, year fixes).
                # Only consulted when the stored name is missing on disk:
                # falls back to matching by file size + extension.
                auto_locate_renamed=True,
            )
            if rar_dir:
                base_kwargs["rar_executable_dir"] = rar_dir

            errors: list[str] = []
            ok_any = False
            # One call per reconstructable set (srr_part prefix wildcard), or a
            # single plain call when the SRR has just one set.
            for part in (run_parts if skip_parts else [None]):
                kwargs = dict(base_kwargs)
                if part is not None:
                    kwargs["srr_part"] = f"{part}.*"
                try:
                    with redirect_stdout(buf), redirect_stderr(buf):
                        result = rm.reconstruct(**kwargs)
                    if result is False:
                        errors.append(_explain(
                            buf.getvalue().strip() or "Reconstruction failed (no reason given)"))
                    else:
                        ok_any = True
                except Exception as e:
                    errors.append(_explain(str(e)))

            output_text = buf.getvalue()
            # Recursive: multi-CD releases reconstruct into CD1/, CD2/ …
            # subfolders (stored paths). Exclude our own working dirs.
            _SKIP_DIRS = {"_stored", "_subs_tmp", "_iso_m2ts_tmp", "Sample"}
            out_files = [
                str(f.relative_to(out_dir)) for f in Path(out_dir).rglob("*")
                if f.is_file() and not (_SKIP_DIRS & set(f.relative_to(out_dir).parts[:-1]))
            ]
            if ok_any:
                return {"ok": True, "files": out_files, "output": output_text,
                        "error": "; ".join(errors) if errors else None}
            return {"ok": False, "files": out_files, "output": output_text,
                    "error": "; ".join(errors) or "Reconstruction failed"}
        except ImportError:
            return {"ok": False, "files": [], "output": "", "error": "rescene not available — pip install pyReScene"}
        except Exception as e:
            return {"ok": False, "files": [], "output": buf.getvalue(), "error": _explain(str(e))}
        finally:
            done_flag.set()

    def _reconstruct_nested(self, nsrr: Path, content_dir: str, out_root: Path,
                            stored_dir: Path) -> dict:
        """Rebuild one nested SRR (vobsubs etc.). Sources are assembled by exact
        filename from the content folder and anything already produced in the
        output (inner subs RARs feed outer ones) into a small temp pool —
        subtitle files are tiny, so copying is cheap."""
        try:
            sets = self._srr_rar_sets(str(nsrr))
        except Exception as e:
            return {"ok": False, "error": str(e)}
        needed = {Path(p).name.lower()
                  for info in sets.values() for p in info["packed"]}
        if not needed:
            return {"ok": False, "error": "no packed files described"}
        # If every RAR this SRR describes already exists (content or output),
        # there is nothing to rebuild — common for inner vobsub RARs that were
        # kept alongside the content.
        vol_names = {Path(v).name.lower()
                     for info in sets.values() for v in info["volumes"]}
        existing = {
            f.name.lower()
            for root in (Path(content_dir), out_root)
            for f in root.rglob("*") if f.is_file()
        }
        if vol_names and vol_names <= existing:
            return {"ok": True, "produced": [], "already": True}
        pool = out_root / "_subs_tmp"
        pool.mkdir(parents=True, exist_ok=True)
        try:
            missing = set(needed)
            for root in (Path(content_dir), out_root):
                if not missing:
                    break
                for f in root.rglob("*"):
                    if (f.is_file() and f.name.lower() in missing
                            and pool not in f.parents):
                        shutil.copy2(str(f), str(pool / f.name))
                        missing.discard(f.name.lower())
            if missing:
                return {"ok": False, "error":
                        "source not found: " + ", ".join(sorted(missing)[:4])}
            rel_parent = nsrr.parent.relative_to(stored_dir)
            n_out = out_root / rel_parent
            rc = self._srr_reconstruct(str(nsrr), str(pool), str(n_out),
                                       log_rar_pack=False)
            if rc["ok"]:
                produced = [f for f in rc["files"]
                            if Path(f).suffix.lower() not in META_EXTS]
                if produced:
                    return {"ok": True, "produced": produced}
                return {"ok": False, "error": "nothing produced"}
            return {"ok": False, "error": rc.get("error") or "failed"}
        finally:
            shutil.rmtree(str(pool), ignore_errors=True)

    def _srr_list(self, srr_path: str) -> bool:
        """Log SRR contents. Returns True if RAR5 format (reconstruction not supported)."""
        try:
            from rescene.rar import (RarReader, BlockType, COMPR_STORING,  # type: ignore
                                     RAR5_MARKER_BLOCK)
            stored, rars = [], []
            content: dict[str, tuple[int, bool]] = {}
            has_compressed = False
            is_rar5 = False

            for block in RarReader(str(srr_path)).read_all():
                bt = block.rawtype
                if bt == BlockType.SrrHeader:
                    self._log(f"    Creating app: {getattr(block, 'appname', '?')}", "dim")
                elif bt == BlockType.SrrStoredFile:
                    stored.append(getattr(block, "file_name", "?"))
                elif bt == BlockType.SrrRarFile:
                    rars.append(getattr(block, "file_name", "?"))
                    # Peek at embedded RAR header to detect RAR5 format
                    raw = getattr(block, "_file_data", None) or getattr(block, "data", b"")
                    if raw and raw[:8] == RAR5_MARKER_BLOCK:
                        is_rar5 = True
                elif bt == BlockType.RarPackedFile:
                    fname  = getattr(block, "file_name", "?")
                    size   = getattr(block, "unpacked_size", 0)
                    method = getattr(block, "compression_method", COMPR_STORING)
                    compressed = (method != COMPR_STORING)
                    if compressed:
                        has_compressed = True
                    if fname not in content:
                        content[fname] = (size, compressed)

            if stored:
                self._log(f"    Stored ({len(stored)}): {', '.join(stored)}", "dim")
            if rars:
                self._log(f"    RARs ({len(rars)}): {', '.join(rars[:5])}"
                          + (f" …+{len(rars)-5}" if len(rars) > 5 else ""), "dim")
            if content:
                parts = []
                for fname, (sz, comp) in list(content.items())[:4]:
                    parts.append(f"{fname} ({sz:,} B, {'COMPRESSED' if comp else 'stored'})")
                self._log(f"    Content ({len(content)} unique): {'; '.join(parts)}"
                          + (f" …+{len(content)-4}" if len(content) > 4 else ""), "dim")
                if is_rar5:
                    self._log("    ⛔ RAR5 format — pyReScene 0.7 cannot reconstruct these", "err")
                elif has_compressed:
                    self._log("    ⚠ Compressed RARs — needs exact WinRAR version from pack", "warn")
                else:
                    self._log("    Uncompressed RARs — pyReScene handles natively", "dim")
            return is_rar5
        except Exception as e:
            self._log(f"    (SRR read error: {e})", "dim")
            return False

    # Shims for three Python 3.12+ incompatibilities in pyReScene 0.7, applied
    # before importing resample.srs:
    #   time.clock() removed in 3.8; distutils removed in 3.12 (used by
    #   resample.fpcalc); locale.format() removed in 3.12 (used by rescene's
    #   sep() in the rebuild results display — crashing there strands the
    #   rebuilt sample as a .tmp file before the CRC check and rename).
    _SRS_WRAPPER = (
        "import time, types, shutil, sys, locale; "
        "time.clock = time.perf_counter; "
        "locale.format = locale.format_string; "
        "_ds = types.ModuleType('distutils'); "
        "_dss = types.ModuleType('distutils.spawn'); "
        "_dss.find_executable = shutil.which; "
        "sys.modules.setdefault('distutils', _ds); "
        "sys.modules.setdefault('distutils.spawn', _dss); "
        "from resample.srs import main; main(sys.argv[1:])"
    )

    def _srs_info(self, srs_path: str) -> dict:
        """Run `srs -l` on an SRS file: log its metadata and return parsed fields
        (notably 'type', e.g. STREAM / MKV / AVI)."""
        info: dict = {}
        try:
            ri = subprocess.run(
                [sys.executable, "-c", self._SRS_WRAPPER, str(srs_path), "-l"],
                capture_output=True, text=True, timeout=60,
            )
            for line in (ri.stdout.strip() + "\n" + ri.stderr.strip()).splitlines():
                line = line.strip()
                if not line:
                    continue
                self._log(f"    srs-info: {line}", "dim")
                if m := re.match(r"^SRS Type\s*:\s*(\S+)", line):
                    info["type"] = m.group(1).upper()
                elif m := re.match(r"^Sample Name\s*:\s*(.+)$", line):
                    info["name"] = m.group(1).strip()
                elif m := re.match(r"^Sample Size\s*:\s*([\d,]+)", line):
                    info["size"] = int(m.group(1).replace(",", ""))
                elif m := re.match(r"^Sample CRC\s*:\s*([0-9A-Fa-f]{1,8})", line):
                    info["crc"] = int(m.group(1), 16)
        except Exception:
            pass
        return info

    @staticmethod
    def _crc32_file(path: str) -> int:
        import zlib
        crc = 0
        with open(path, "rb") as f:
            while chunk := f.read(1 << 20):
                crc = zlib.crc32(chunk, crc)
        return crc & 0xFFFFFFFF

    def _srs_create_sample(self, srs_path: str, video_path: str, out_dir: str) -> dict:
        """Create scene sample from SRS file + full video.
        -y skips the overwrite prompt (which would block waiting for stdin)."""
        if not (_find_script("srs") or self._srs_script):
            return {"ok": False, "error": "srs script not found (installed with pyReScene)"}
        Path(out_dir).mkdir(parents=True, exist_ok=True)
        cmd = [sys.executable, "-c", self._SRS_WRAPPER,
               str(srs_path), str(video_path), "-o", str(out_dir), "-y"]
        self._log(f"    srs: {Path(srs_path).name} + {Path(video_path).name} → {Path(out_dir).name}", "dim")
        try:
            t0 = time.time()
            # Generous timeout: locating the sample in a full Blu-ray stream is a
            # linear scan of the whole file (can be 40+ GB).
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=7200)
            elapsed = time.time() - t0
            output = "\n".join(filter(None, [r.stdout.strip(), r.stderr.strip()]))
            # Strip the terminal progress spinner (written as backspace+char pairs)
            output = re.sub(r"\x08.", "", output).strip()
            for line in output.splitlines():
                if line.strip():
                    self._log(f"    srs: {line}", "dim")
            self._log(f"    srs finished in {elapsed:.0f}s (exit {r.returncode})", "dim")

            out_p = Path(out_dir)
            all_files = [f for f in out_p.iterdir() if f.is_file()] if out_p.exists() else []
            created = [f for f in all_files if f.suffix.lower() != ".tmp"]
            tmp_files = [f for f in all_files if f.suffix.lower() == ".tmp"]

            # pyReScene bug: replace_result() can fail its final os.rename silently,
            # leaving '<sample name>-<random>.tmp' behind while srs still exits 0
            # and reports success. Recover the intended name and rename it ourselves.
            if not created and tmp_files and r.returncode == 0:
                tmp = max(tmp_files, key=lambda f: f.stat().st_mtime)
                m = re.match(r"^(.+)-[A-Za-z0-9_]+\.tmp$", tmp.name)
                if m:
                    target = out_p / m.group(1)
                    if not target.exists():
                        tmp.rename(target)
                        self._log(f"    (renamed lingering temp file → {target.name})", "dim")
                        created = [target]
            if r.returncode == 0:
                # Success — clear any stale temp files from older runs
                for tf in tmp_files:
                    if tf.exists() and tf not in created:
                        tf.unlink(missing_ok=True)
            elif tmp_files:
                # Failure — keep temp files for inspection, just report them
                names = ", ".join(f.name for f in tmp_files if f.exists())
                if names:
                    self._log(f"    (temp file(s) left in Sample dir: {names})", "dim")

            if created and r.returncode == 0:
                return {"ok": True, "output": output, "files": [f.name for f in created]}
            # Full output is already logged above — the error field should be
            # just the reason (last meaningful line), not the whole blob.
            err_line = next(
                (l.strip() for l in reversed(output.splitlines()) if l.strip()), "")
            if r.returncode != 0:
                return {"ok": False, "output": output,
                        "error": err_line or f"srs exited {r.returncode}"}
            return {"ok": False, "output": output,
                    "error": f"srs ran (exit 0) but no file created: {err_line}"}
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "Sample creation timed out (2-hour limit)"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def _find_7z(self) -> str | None:
        for name in ("7z.exe", "7za.exe"):
            p = self._app_dir / name
            if p.exists():
                return str(p)
        return shutil.which("7z") or shutil.which("7za")

    def _list_iso_m2ts(self, iso_path: str) -> dict:
        """List all M2TS streams inside a Blu-ray ISO using 7z.
        Returns {"ok", "seven_zip", "entries": [(path_in_iso, size), …]}."""
        seven_zip = self._find_7z()
        if not seven_zip:
            return {"ok": False, "error": "7z.exe not found in apps/ — needed to read ISO"}
        try:
            r = subprocess.run(
                [seven_zip, "l", "-slt", "-r", iso_path, "*.m2ts"],
                capture_output=True, text=True, timeout=60,
            )
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "ISO listing timed out"}
        except Exception as e:
            return {"ok": False, "error": f"7z list: {e}"}

        # Parse technical listing blocks: "Path = ...\nSize = ..."
        entries: list[tuple[str, int]] = []
        cur_path = cur_size = None
        for line in r.stdout.splitlines():
            if line.startswith("Path = "):
                cur_path = line[7:].strip()
                cur_size = None
            elif line.startswith("Size = ") and cur_path:
                try:
                    cur_size = int(line[7:].strip())
                except ValueError:
                    pass
            elif not line.strip() and cur_path and cur_size is not None:
                if cur_path.lower().endswith(".m2ts") and cur_size > 0:
                    entries.append((cur_path, cur_size))
                cur_path = cur_size = None
        if cur_path and cur_size and cur_path.lower().endswith(".m2ts"):
            entries.append((cur_path, cur_size))

        if not entries:
            return {"ok": False, "error": "No M2TS streams found in ISO"}
        return {"ok": True, "seven_zip": seven_zip, "entries": entries}

    def _carve_nonscene_sample(self, iso_path: str, out_dir: str,
                               base_name: str, target_bytes: int | None) -> dict:
        """Carve a NON-SCENE preview clip from the start of the ISO's main M2TS
        stream. M2TS is valid from byte 0 (opens with PAT/PMT), so streaming the
        first N bytes via `7z -so` yields a playable clip without extracting the
        full stream. The result is clearly named NONSCENE and is NOT the scene
        sample — it will never CRC-match the SRS."""
        lst = self._list_iso_m2ts(iso_path)
        if not lst["ok"]:
            return {"ok": False, "error": lst["error"]}
        best_path, best_size = max(lst["entries"], key=lambda x: x[1])

        # Size the clip like the real scene sample when known; sane bounds.
        target = target_bytes or (100 << 20)
        target = max(16 << 20, min(target, 300 << 20, best_size))
        target -= target % 192  # end on an M2TS packet boundary

        Path(out_dir).mkdir(parents=True, exist_ok=True)
        out_file = Path(out_dir) / f"NONSCENE-{base_name}-preview.m2ts"
        try:
            proc = subprocess.Popen(
                [lst["seven_zip"], "e", "-so", iso_path, best_path],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            )
            remaining = target
            with open(out_file, "wb") as f:
                while remaining > 0:
                    chunk = proc.stdout.read(min(1 << 20, remaining))
                    if not chunk:
                        break
                    f.write(chunk)
                    remaining -= len(chunk)
            proc.kill()
            proc.wait(timeout=30)
        except Exception as e:
            return {"ok": False, "error": f"carve failed: {e}"}
        if not out_file.exists() or out_file.stat().st_size == 0:
            return {"ok": False, "error": "no data carved from ISO"}
        return {"ok": True, "path": str(out_file), "name": out_file.name,
                "size": out_file.stat().st_size}

    def _extract_m2ts_from_iso(self, iso_path: str, tmp_dir: str) -> dict:
        """
        List all M2TS streams inside a Blu-ray ISO using 7z, pick the largest
        (main feature), extract it to tmp_dir, and return its path.
        Caller is responsible for deleting tmp_dir afterwards.
        """
        lst = self._list_iso_m2ts(iso_path)
        if not lst["ok"]:
            return {"ok": False, "error": lst["error"]}
        seven_zip = lst["seven_zip"]
        entries = lst["entries"]

        # Pick largest (main feature, not trailers/extras)
        best_path, best_size = max(entries, key=lambda x: x[1])
        best_name = Path(best_path).name
        gb = best_size / 1_073_741_824
        self._log(
            f"  ISO contains {len(entries)} M2TS stream(s); extracting {best_name} ({gb:.1f} GB) — "
            "this may take several minutes…",
            "dim",
        )

        Path(tmp_dir).mkdir(parents=True, exist_ok=True)
        # 2 — extract just that one file (7z 'e' drops the directory structure)
        try:
            r2 = subprocess.run(
                [seven_zip, "e", iso_path, best_path, f"-o{tmp_dir}", "-y"],
                capture_output=True, text=True, timeout=14400,  # 4 h
            )
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "ISO extraction timed out (4-hour limit)"}
        except Exception as e:
            return {"ok": False, "error": f"7z extract: {e}"}

        extracted = Path(tmp_dir) / best_name
        if not extracted.exists():
            err = r2.stderr.strip() or r2.stdout.strip() or "file not found after extraction"
            return {"ok": False, "error": f"Extraction failed: {err}"}

        self._log(f"  Extracted {best_name} ({extracted.stat().st_size:,} B)", "dim")
        return {"ok": True, "path": str(extracted), "name": best_name}

    # ── Single-release processing thread ─────────────────────────────────────

    def start_process(self, config: dict) -> bool:
        if self._running:
            self._log("Already running.", "warn")
            return False
        self._stop.clear()
        threading.Thread(target=self._process_one, args=(config,), daemon=True).start()
        return True

    def stop_process(self):
        self._stop.set()

    def _process_one(self, config: dict, batch_mode: bool = False):
        if not batch_mode:
            self._running = True
            self._emit("status", {"state": "running"})

        release     = (config.get("release_name") or "").strip()
        candidates  = config.get("candidates", [])   # list of {release, hasNFO, hasSRS}
        max_test    = int(config.get("max_test", 5))
        content_dir      = (config.get("content_dir") or "").strip()
        dest_dir         = (config.get("dest_dir") or "").strip()
        do_sample        = config.get("do_sample", False)
        extract_iso_m2ts = config.get("extract_iso_m2ts", False)
        non_scene_sample = config.get("non_scene_sample", False)
        # Queue rows in the GUI are keyed by the original folder path — keep it
        # for job events even after auto-match renames or nesting descent.
        queue_path = content_dir

        # Per-job outcome for the queue status and the end-of-batch summary
        summary = {
            "release": release or (Path(queue_path).name if queue_path else "?"),
            "ok": True, "rars": None, "sample": None, "subs": None, "note": "",
        }

        self._emit("job_start", {"content_dir": queue_path, "release": release})

        # Resolve double-nesting: if the selected folder has no files (only one subdir),
        # descend into it. Handles the case where batch source → release folder → content.
        if content_dir and Path(content_dir).is_dir():
            p = Path(content_dir)
            children = list(p.iterdir())
            if children and all(c.is_dir() for c in children) and len(children) == 1:
                content_dir = str(children[0])
                self._log(f"  (descended into subfolder: {children[0].name})", "dim")

        try:
            if not dest_dir:
                self._log("ERROR: No output folder.", "err"); raise ValueError()

            # If no confirmed release name, score candidates against content folder
            if not release:
                if not content_dir or not Path(content_dir).is_dir():
                    self._log("ERROR: Need a content folder to auto-match candidates.", "err")
                    raise ValueError()
                best = {"release": None, "score": 0.0}
                if candidates:
                    self._log(
                        f"  Auto-matching — testing up to {min(len(candidates), max_test)}"
                        f" of {len(candidates)} candidate(s)…", "dim"
                    )
                    best = self.find_best_match(candidates, content_dir, max_test)
                if best["release"] and best["score"] >= 0.5:
                    release = best["release"]
                    self._log(f"  ✓ Matched: {release}  ({best['score']:.0%} confidence)", "ok")
                else:
                    # Candidates are junk (a bad folder name can still match
                    # unrelated releases by name) or absent — an exact content
                    # CRC lookup beats them all.
                    if candidates:
                        self._log(
                            "  Candidates don't match content — trying exact "
                            "content CRC lookup…", "dim",
                        )
                    hres = self.search_by_content_hash(content_dir)
                    if hres.get("ok") and hres.get("results"):
                        release = hres["results"][0]["release"]
                        self._log(f"  ✓ Matched by content CRC: {release}", "ok")
                    elif candidates:
                        top = best.get("release", "?")
                        self._log(
                            f"  No confident match (best: {top} at {best['score']:.0%}) "
                            "and the content CRC is not in srrdb — the file is likely "
                            "NON-SCENE (P2P/custom rip) or modified. Set the release "
                            "name manually if you know it.", "err",
                        )
                        raise RuntimeError("not in srrdb — possibly non-scene")
                    else:
                        self._log(
                            "  No name match and the content CRC is not in srrdb — "
                            "the file is likely NON-SCENE (P2P/custom rip) or modified.",
                            "err",
                        )
                        raise RuntimeError("not in srrdb — possibly non-scene")

            summary["release"] = release
            out_root = Path(dest_dir) / release
            stored_dir = out_root / "_stored"

            self._log(f"▶ {release}", "info")

            # 1 — Download SRR (check both dot and underscore variants in cache)
            alt_release = release.replace(".", "_") if "." in release else release.replace("_", ".")
            srr_file = out_root / f"{release}.srr"
            srr_file_alt = out_root / f"{alt_release}.srr"
            if srr_file.exists():
                self._log(f"  SRR cached ({srr_file.stat().st_size:,} B)", "dim")
            elif srr_file_alt.exists():
                srr_file = srr_file_alt
                self._log(f"  SRR cached ({srr_file.stat().st_size:,} B) [{alt_release}.srr]", "dim")
            else:
                self._log("  Downloading SRR from srrdb.com…", "dim")
                dl = self.download_srr(release, str(out_root))
                if not dl["ok"]:
                    if dl.get("not_found"):
                        self._log(
                            f"  No SRR on srrdb.com for '{release}' — the database has "
                            "no record of this release. It may be a NON-SCENE file "
                            "(P2P/custom rip) or the release name is wrong.", "err",
                        )
                        raise RuntimeError("no SRR on srrdb — possibly non-scene")
                    self._log(f"  ERROR: {dl['error']}", "err"); raise RuntimeError(dl["error"])
                srr_file = Path(dl["srr_path"])  # use actual saved path (may be alt name)
                self._log(f"  SRR downloaded ({dl['size']:,} B)", "ok")

            if self._stop.is_set(): raise InterruptedError()

            # 2 — Extract stored files (NFO, SFV, SRS)
            self._log("  Extracting stored files (NFO, SFV, SRS)…", "dim")
            ex = self._srr_extract(str(srr_file), str(stored_dir))
            if ex["ok"]:
                files = ex.get("files", [])
                self._log(f"  Stored: {', '.join(files) if files else 'none found'}", "ok" if files else "dim")
                # Move NFO/SFV up to release root for convenience
                for f in stored_dir.rglob("*"):
                    if f.is_file() and f.suffix.lower() in {".nfo", ".sfv"}:
                        dest_f = out_root / f.name
                        if not dest_f.exists():
                            shutil.copy2(str(f), str(dest_f))
            else:
                self._log(f"  Extract WARN: {ex['error']}", "warn")

            if self._stop.is_set(): raise InterruptedError()

            # 3 — Reconstruct RARs
            self._log("  SRR info:", "dim")
            is_rar5 = self._srr_list(str(srr_file))

            if is_rar5:
                self._log("  Skipping reconstruction — RAR5 not supported by pyReScene 0.7", "err")
                summary["ok"] = False
                summary["note"] = "RAR5 — not supported"
            elif content_dir and Path(content_dir).is_dir():
                # Show what's in the content folder so mismatches are obvious
                cdir = Path(content_dir)
                citems = sorted(cdir.iterdir(), key=lambda f: (f.is_dir(), f.name.lower()))
                self._log(f"  Content folder ({len(citems)} item(s)):", "dim")
                for ci in citems[:12]:
                    tag = "[dir]" if ci.is_dir() else f"{ci.stat().st_size:,} B"
                    self._log(f"    {tag:>14}  {ci.name}", "dim")
                if len(citems) > 12:
                    self._log(f"    … and {len(citems) - 12} more", "dim")

                self._log("  Reconstructing RARs…", "dim")
                rc = self._srr_reconstruct(str(srr_file), content_dir, str(out_root))
                for line in (rc.get("output") or "").splitlines():
                    line = line.strip()
                    if line:
                        self._log(f"    {line}", "dim")
                if rc["ok"] and rc.get("files"):
                    # Exclude metadata files already placed there before reconstruction
                    produced = [f for f in rc["files"]
                                if Path(f).suffix.lower() not in META_EXTS]
                    self._log(
                        f"  Produced ({len(produced)}): {', '.join(produced[:8])}"
                        + ("…" if len(produced) > 8 else ""),
                        "ok" if produced else "warn",
                    )
                    summary["rars"] = len(produced)
                    if not produced:
                        self._log("  No archive files produced", "warn")
                        summary["ok"] = False
                        summary["note"] = "no RARs produced"
                else:
                    self._log(f"  Reconstruct ERROR: {rc.get('error', 'unknown')}", "err")
                    summary["ok"] = False
                    summary["note"] = (rc.get("error") or "reconstruct error")[:100]

                # Nested SRRs (e.g. Subs/xxx.subs.srr) describe extra RAR sets
                # such as vobsubs — sometimes two levels deep (per-CD inner RARs
                # inside an outer subs RAR). Extract deeper SRRs first, then
                # rebuild in passes so inner sets become sources for outer ones.
                if stored_dir.exists():
                    found: list[Path] = []
                    scan = sorted(stored_dir.rglob("*.srr"))
                    while scan:
                        nsrr = scan.pop(0)
                        if nsrr in found:
                            continue
                        found.append(nsrr)
                        try:
                            self._srr_extract(str(nsrr), str(nsrr.parent))
                        except Exception:
                            pass
                        for extra in sorted(nsrr.parent.rglob("*.srr")):
                            if extra not in found and extra not in scan:
                                scan.append(extra)

                    pending = found
                    nested_errors: dict = {}
                    for _pass in range(3):
                        if not pending:
                            break
                        remaining = []
                        progressed = False
                        for nsrr in pending:
                            self._log(f"  Nested SRR: {nsrr.name} — reconstructing…", "dim")
                            nrc = self._reconstruct_nested(
                                nsrr, content_dir, out_root, stored_dir)
                            if nrc["ok"]:
                                if nrc.get("already"):
                                    self._log(
                                        "    Target RAR(s) already present — "
                                        "nothing to rebuild", "dim")
                                else:
                                    self._log(
                                        f"    Produced: {', '.join(nrc['produced'][:6])}"
                                        + ("…" if len(nrc["produced"]) > 6 else ""), "ok")
                                progressed = True
                            else:
                                nested_errors[nsrr] = nrc.get("error") or "?"
                                remaining.append(nsrr)
                        pending = remaining
                        if not progressed:
                            break
                    for nsrr in pending:
                        self._log(
                            f"  Nested SRR {nsrr.name} skipped — {nested_errors[nsrr]}",
                            "warn",
                        )
            else:
                self._log("  No content folder — NFO/SFV extracted only", "dim")
                summary["note"] = "no content folder — NFO/SFV only"

            if self._stop.is_set(): raise InterruptedError()

            # 4 — Sample creation
            if do_sample and content_dir and Path(content_dir).is_dir():
                srs_files = (list(stored_dir.rglob("*.srs")) + list(stored_dir.rglob("*.SRS"))
                             if stored_dir.exists() else [])
                if srs_files:
                    _DISC_EXTS = {".iso", ".img", ".bin", ".nrg"}
                    _STREAM_EXTS = {".avi", ".mkv", ".mp4", ".m4v", ".mov",
                                    ".wmv", ".m2ts", ".ts", ".vob"}
                    srs_meta = self._srs_info(str(srs_files[0]))
                    srs_type    = srs_meta.get("type", "")
                    sample_size = srs_meta.get("size")
                    sample_crc  = srs_meta.get("crc")
                    sample_name = srs_meta.get("name")

                    # If a file matching the sample's exact size is already in the
                    # content folder, it may BE the sample — verify CRC and copy.
                    sample_done = False
                    if sample_size:
                        for cand in Path(content_dir).rglob("*"):
                            if not (cand.is_file() and cand.stat().st_size == sample_size):
                                continue
                            crc = self._crc32_file(str(cand))
                            if sample_crc is not None and crc == sample_crc:
                                sample_dir = out_root / "Sample"
                                sample_dir.mkdir(parents=True, exist_ok=True)
                                dest = sample_dir / (sample_name or cand.name)
                                if not dest.exists():
                                    shutil.copy2(str(cand), str(dest))
                                self._log(
                                    f"  Sample already in content folder — CRC verified "
                                    f"({crc:08X}) ✓ copied as {dest.name}", "ok",
                                )
                                sample_done = True
                                summary["sample"] = "verified ✓"
                                break

                    # Prefer video/stream files over disc images — and never use a
                    # file that IS the sample as the rebuild source.
                    stream_media = [
                        f for f in Path(content_dir).rglob("*")
                        if f.is_file() and f.suffix.lower() in _STREAM_EXTS
                        and f.stat().st_size != sample_size
                    ]
                    disc_media = [
                        f for f in Path(content_dir).iterdir()
                        if f.is_file() and f.suffix.lower() in _DISC_EXTS
                    ]
                    if sample_done:
                        stream_media, disc_media = [], []
                    media_file = (sorted(stream_media, key=lambda f: f.stat().st_size, reverse=True) or disc_media or [None])[0]
                    _iso_tmp: Path | None = None
                    if media_file and media_file.suffix.lower() in _DISC_EXTS:
                        if srs_type == "STREAM":
                            # STREAM SRS = raw byte matching. A scan of the ISO
                            # covers every stream on the disc — extracting an
                            # M2TS first adds nothing, so use the ISO directly.
                            self._log(
                                "  STREAM-type SRS — scanning the ISO directly "
                                "(covers all streams on the disc)", "dim",
                            )
                        elif extract_iso_m2ts:
                            _iso_tmp = out_root / "_iso_m2ts_tmp"
                            ext_result = self._extract_m2ts_from_iso(
                                str(media_file), str(_iso_tmp)
                            )
                            if ext_result["ok"]:
                                media_file = Path(ext_result["path"])
                            else:
                                self._log(f"  ISO extract FAILED: {ext_result['error']}", "err")
                                media_file = None
                        else:
                            self._log(
                                f"  Sample skipped — only a disc image ({media_file.suffix}) found; "
                                "tick 'Extract M2TS from ISO' to auto-extract the Blu-ray stream "
                                "(requires disk space ≈ size of the main video stream).",
                                "dim",
                            )
                            media_file = None
                    if media_file:
                        self._log("  Creating sample…", "dim")
                        # Multi-CD releases: the SRS stores byte offsets into the
                        # exact CD the sample was cut from. Wrong CD → rebuild
                        # completes with the right size but wrong CRC (pyReScene
                        # mislabels this "LOL xvid issue"). Try each stream file.
                        attempts = [media_file] + [
                            f for f in stream_media[:4] if f != media_file
                        ]
                        samp = None
                        for i, mf in enumerate(attempts):
                            if i:
                                self._log(
                                    f"  Sample source mismatch — retrying with "
                                    f"{mf.name}…", "dim",
                                )
                            samp = self._srs_create_sample(
                                str(srs_files[0]), str(mf),
                                str(out_root / "Sample"),
                            )
                            if samp["ok"]:
                                break
                            err_l = samp.get("error", "").lower()
                            if not ("rebuild failed" in err_l or "signature" in err_l
                                    or "extract correct amount" in err_l):
                                break  # non-retryable error
                        # Fallback: the sample may have been cut from a different
                        # stream on the disc — one raw scan of the ISO covers all
                        # of its M2TS files at once.
                        if (not samp["ok"] and _iso_tmp and disc_media
                                and "signature" in samp.get("error", "").lower()):
                            self._log(
                                "  Not found in main stream — retrying against the full ISO "
                                "(covers all streams on the disc)…", "dim",
                            )
                            samp = self._srs_create_sample(
                                str(srs_files[0]), str(disc_media[0]),
                                str(out_root / "Sample"),
                            )
                        if samp["ok"]:
                            files_str = ", ".join(samp.get("files", []))
                            self._log(f"  Sample created ✓ — {files_str}", "ok")
                            summary["sample"] = "created ✓"
                        elif "signature" in samp.get("error", "").lower() and disc_media:
                            self._log(
                                "  Sample FAILED: the sample's bytes are not on the disc. "
                                "The group remuxed the sample when cutting it (the SRS "
                                "signature is the remux tool's own header bytes), so this "
                                "sample cannot be rebuilt from the ISO by any byte-matching "
                                "tool. The SRS can still verify an existing sample file.", "err",
                            )
                            summary["sample"] = "unrebuildable (BD remux)"
                            if non_scene_sample:
                                self._log(
                                    "  Creating NON-SCENE preview clip from the disc's "
                                    "main stream instead…", "dim",
                                )
                                carve = self._carve_nonscene_sample(
                                    str(disc_media[0]), str(out_root / "Sample"),
                                    release.lower(), sample_size,
                                )
                                if carve["ok"]:
                                    self._log(
                                        f"  Non-scene preview created — {carve['name']} "
                                        f"({carve['size']:,} B). NOT a scene file; it will "
                                        "never CRC-match the SRS.", "ok",
                                    )
                                    summary["sample"] += " + NONSCENE preview"
                                else:
                                    self._log(
                                        f"  Non-scene preview failed: {carve['error']}", "warn",
                                    )
                        elif "signature" in samp.get("error", "").lower():
                            m_trk = re.search(r"track (\d+)", samp["error"], re.IGNORECASE)
                            trk = int(m_trk.group(1)) if m_trk else 1
                            if trk > 1:
                                self._log(
                                    f"  Sample FAILED: track {trk} of the sample does not "
                                    "exist in the main video — the group's sample contains "
                                    "extra/re-encoded data, so it cannot be rebuilt "
                                    "byte-perfect from the movie file.", "err",
                                )
                                summary["sample"] = "unrebuildable (extra track)"
                            else:
                                self._log(
                                    "  Sample FAILED: the sample's video data was not found "
                                    "in this file — the content may not match this exact "
                                    "release (wrong source or re-encode).", "err",
                                )
                                summary["sample"] = "failed (content mismatch)"
                        else:
                            self._log(f"  Sample FAILED: {samp['error']}", "err")
                            summary["sample"] = "failed"
                    elif not sample_done and not stream_media and not disc_media:
                        self._log("  No media file for sample", "dim")
                        summary["sample"] = "no media file"
                    # Clean up extracted M2TS
                    if _iso_tmp and _iso_tmp.exists():
                        shutil.rmtree(str(_iso_tmp), ignore_errors=True)
                        self._log("  Cleaned up temp M2TS extract", "dim")
                else:
                    txt_placeholders = (list(stored_dir.rglob("*sample*.txt"))
                                        if stored_dir.exists() else [])
                    if txt_placeholders:
                        self._log(
                            "  No SRS — this usenet-sourced SRR stores only a sample "
                            "info .txt placeholder, so the sample cannot be rebuilt", "dim",
                        )
                        summary["sample"] = "no SRS (usenet SRR)"
                    else:
                        self._log("  No SRS in stored files", "dim")
                        summary["sample"] = "no SRS"

            # 5 — Move stored extras (Proof/, Subs/, jpgs …) into the release
            # folder, preserving their relative paths, then drop empty _stored.
            # Runs after sample creation, which reads the SRS from _stored.
            if stored_dir.exists():
                moved = 0
                for f in sorted(stored_dir.rglob("*")):
                    if not f.is_file():
                        continue
                    dest_f = out_root / f.relative_to(stored_dir)
                    if dest_f.exists():
                        f.unlink()  # duplicate — NFO/SFV were already copied up
                        continue
                    dest_f.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(f), str(dest_f))
                    moved += 1
                for d in sorted((p for p in stored_dir.rglob("*") if p.is_dir()),
                                reverse=True):
                    try:
                        d.rmdir()
                    except OSError:
                        pass
                try:
                    stored_dir.rmdir()
                except OSError:
                    pass
                if moved:
                    self._log(f"  Moved {moved} stored extra(s) into release folder", "dim")

            # 6 — Verify the Subs folder actually contains what its SFV expects.
            # If the subs RAR(s) could not be rebuilt, the folder holds only
            # metadata — rename it so the gap is impossible to miss.
            subs_dir = out_root / "Subs"
            if subs_dir.is_dir():
                expected = []
                for s in list(subs_dir.glob("*.sfv")) + list(subs_dir.glob("*.SFV")):
                    expected += [fn for fn, _ in self._parse_sfv(str(s))]
                if expected:
                    missing_subs = [fn for fn in expected
                                    if not (subs_dir / fn).exists()]
                    subs_ok = not missing_subs
                else:
                    subs_ok = any(
                        re.search(r"\.(rar|r\d\d|\d{3})$", f.name, re.IGNORECASE)
                        for f in subs_dir.iterdir() if f.is_file()
                    )
                if subs_ok:
                    summary["subs"] = "✓"
                else:
                    summary["subs"] = "NOT produced"
                    target = out_root / "Subs_NOT_PRODUCED"
                    try:
                        if target.exists():
                            # previous run already renamed — merge contents
                            for f in subs_dir.iterdir():
                                dst = target / f.name
                                if not dst.exists():
                                    shutil.move(str(f), str(dst))
                            subs_dir.rmdir()
                        else:
                            subs_dir.rename(target)
                        self._log(
                            "  ⚠ Subs NOT produced — subtitle sources missing; "
                            "folder renamed to Subs_NOT_PRODUCED", "warn",
                        )
                    except OSError as e:
                        self._log(f"  ⚠ Subs NOT produced (rename failed: {e})", "warn")

            if summary["ok"]:
                self._log(f"  ✓ Done → {out_root}", "ok")
            else:
                self._log(f"  ⚠ Finished with errors → {out_root}", "warn")
            self._emit("job_done", {"release": release, "content_dir": queue_path,
                                    "ok": summary["ok"]})

        except (ValueError, RuntimeError) as e:
            summary["ok"] = False
            summary["note"] = summary["note"] or str(e) or "failed"
            self._emit("job_done", {"release": release, "content_dir": queue_path, "ok": False})
        except InterruptedError:
            self._log("  Stopped.", "warn")
            summary["ok"] = False
            summary["note"] = "stopped"
            self._emit("job_done", {"release": release, "content_dir": queue_path, "ok": False, "stopped": True})
        except Exception as e:
            self._log(f"  FAILED: {e}", "err")
            summary["ok"] = False
            summary["note"] = str(e)[:100]
            self._emit("job_done", {"release": release, "content_dir": queue_path, "ok": False})
        finally:
            # Failed jobs leave no folder behind: remove the output release dir
            # when the job failed AND it holds nothing substantial — extras,
            # the cached SRR and stub RAR headers only (every non-metadata file
            # ≤ 64 KB). A folder from an earlier successful run has full-size
            # volumes and is never touched. User-stopped jobs are kept.
            try:
                if (not summary["ok"] and summary.get("note") != "stopped"
                        and "out_root" in locals() and out_root.is_dir()):
                    substantial = any(
                        f.stat().st_size > 65536
                        for f in out_root.rglob("*")
                        if f.is_file() and f.suffix.lower() not in META_EXTS
                    )
                    if not substantial:
                        shutil.rmtree(str(out_root), ignore_errors=True)
                        self._log("  Removed failed output folder", "dim")
            except Exception:
                pass
            # Prune empty directories: a failed job creates the release folder
            # before writing anything; failed sample attempts leave an empty
            # Sample/. rmdir only removes empty dirs, so content is never at risk.
            try:
                if "out_root" in locals() and out_root.is_dir():
                    for d in sorted((p for p in out_root.rglob("*") if p.is_dir()),
                                    reverse=True):
                        try:
                            d.rmdir()
                        except OSError:
                            pass
                    try:
                        out_root.rmdir()
                    except OSError:
                        pass
            except Exception:
                pass
            if not batch_mode:
                self._running = False
                self._emit("status", {"state": "done"})
        return summary

    # ── Batch processing ──────────────────────────────────────────────────────

    def start_batch(self, jobs: list) -> bool:
        if self._running:
            self._log("Already running.", "warn")
            return False
        self._stop.clear()
        threading.Thread(target=self._batch_thread, args=(jobs,), daemon=True).start()
        return True

    def _batch_thread(self, jobs: list):
        self._running = True
        self._emit("status", {"state": "running"})
        total = len(jobs)
        results = []
        for i, job in enumerate(jobs):
            if self._stop.is_set():
                break
            self._emit("batch_progress", {"current": i + 1, "total": total})
            self._log(f"\n[{i + 1}/{total}]", "dim")
            results.append(self._process_one(job, batch_mode=True))

        ok_n = sum(1 for r in results if r["ok"])
        self._log(f"\n══ Batch summary — {ok_n}/{len(results)} succeeded ══", "info")
        for r in results:
            if r["ok"]:
                parts = []
                if r["rars"] is not None:
                    parts.append(f"{r['rars']} RARs")
                if r["sample"]:
                    parts.append(f"sample: {r['sample']}")
                if r.get("subs"):
                    parts.append(f"subs: {r['subs']}")
                if r["note"]:
                    parts.append(r["note"])
                cls = "warn" if r.get("subs") == "NOT produced" else "ok"
                self._log(f"  ✓ {r['release']} — {', '.join(parts) or 'done'}", cls)
            else:
                detail = r["note"] or "failed"
                if r["sample"]:
                    detail += f" (sample: {r['sample']})"
                if r.get("subs") == "NOT produced":
                    detail += " (subs: NOT produced)"
                self._log(f"  ✗ {r['release']} — {detail}", "err")
        self._running = False
        self._emit("status", {"state": "done"})

"""
Internet Archive Uploader Backend
Handles concurrent uploads with per-file progress tracking.
"""

import os
import time
import threading
import json
import hashlib
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    from internetarchive import get_item, get_session
    HAS_IA = True
except ImportError:
    HAS_IA = False


class IAUploaderAPI:
    def __init__(self):
        self._window = None
        self._stop_flag = threading.Event()
        self._running = False
        self._lock = threading.Lock()
        self._upload_stats = {}  # file_key -> {bytes_done, total, speed, status}
        self._fixdat_names: set = set()
        self._fixdat_path: str = ""
        self._auto_load_fixdat()

    def set_window(self, window):
        self._window = window

    # ── Fixdat filter ─────────────────────────────────────────────────────────

    @staticmethod
    def _parse_fixdat(path: str) -> set:
        """Parse Logiqx XML fixdat, return set of game name strings."""
        names = set()
        try:
            import xml.etree.ElementTree as ET
            root = ET.parse(path).getroot()
            for game in root.iter("game"):
                n = game.get("name", "").strip()
                if n:
                    names.add(n)
        except Exception:
            pass
        return names

    def _auto_load_fixdat(self):
        try:
            p = self._load_uploader_settings().get("fixdat_path", "")
            if p and Path(p).exists():
                names = self._parse_fixdat(p)
                if names:
                    self._fixdat_names = names
                    self._fixdat_path = p
        except Exception:
            pass

    def _load_uploader_settings(self) -> dict:
        try:
            p = Path(__file__).parent / "ia_uploader.json"
            if p.exists():
                with open(p) as f:
                    return json.load(f)
        except Exception:
            pass
        return {}

    def _save_uploader_setting(self, key: str, value):
        try:
            p = Path(__file__).parent / "ia_uploader.json"
            cfg = self._load_uploader_settings()
            cfg[key] = value
            with open(p, "w") as f:
                json.dump(cfg, f, indent=2)
        except Exception:
            pass

    def load_uploader_settings(self) -> dict:
        return self._load_uploader_settings()

    def browse_fixdat(self) -> str:
        try:
            import tkinter as tk
            from tkinter import filedialog
            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            path = filedialog.askopenfilename(
                filetypes=[("DAT files", "*.dat *.xml"), ("All files", "*.*")]
            )
            root.destroy()
            return path if path else ""
        except Exception:
            return ""

    def load_fixdat(self, path: str) -> dict:
        if not path or not Path(path).exists():
            return {"ok": False, "error": "File not found"}
        try:
            names = self._parse_fixdat(path)
            if not names:
                return {"ok": False, "error": "No game entries found in fixdat"}
            self._fixdat_names = names
            self._fixdat_path = path
            self._save_uploader_setting("fixdat_path", path)
            return {"ok": True, "count": len(names), "path": path}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def clear_fixdat(self) -> bool:
        self._fixdat_names = set()
        self._fixdat_path = ""
        self._save_uploader_setting("fixdat_path", "")
        return True

    def get_fixdat_status(self) -> dict:
        return {
            "active": bool(self._fixdat_names),
            "count": len(self._fixdat_names),
            "path": self._fixdat_path,
        }

    def _emit(self, event: str, data: dict):
        if not self._window:
            return
        try:
            import json as _json
            payload = _json.dumps(data, ensure_ascii=True).replace("'", "\\'")
            self._window.evaluate_js(f"window.iaEvent('{event}', JSON.parse('{payload}'))")
        except Exception:
            pass

    def _log(self, msg: str, cls: str = "info"):
        self._emit("log", {"msg": msg, "cls": cls})

    def _progress(self, pct: int, label: str = ""):
        self._emit("progress", {"pct": pct, "label": label})

    def check_ia_installed(self) -> bool:
        return HAS_IA

    def save_credentials(self, access_key: str, secret_key: str) -> bool:
        """Save IA credentials to settings file."""
        try:
            settings_path = Path(__file__).parent / "ia_credentials.json"
            with open(settings_path, "w") as f:
                json.dump({"access_key": access_key, "secret_key": secret_key}, f)
            return True
        except Exception:
            return False

    def load_credentials(self) -> dict:
        """Load saved IA credentials."""
        try:
            settings_path = Path(__file__).parent / "ia_credentials.json"
            if settings_path.exists():
                with open(settings_path) as f:
                    return json.load(f)
        except Exception:
            pass
        return {"access_key": "", "secret_key": ""}

    def fetch_item_metadata(self, identifier: str, access_key: str, secret_key: str) -> dict:
        """Fetch existing item metadata from IA."""
        if not HAS_IA:
            return {"error": "internetarchive not installed"}
        try:
            session = get_session(config={"s3": {"access": access_key, "secret": secret_key}})
            item = session.get_item(identifier)
            md = item.metadata
            if not md:
                return {"exists": False}
            return {
                "exists": True,
                "title": md.get("title", ""),
                "description": md.get("description", ""),
                "creator": md.get("creator", ""),
                "date": md.get("date", ""),
                "subject": md.get("subject", ""),
                "mediatype": md.get("mediatype", "software"),
                "collection": md.get("collection", "opensource"),
                "language": md.get("language", ""),
                "licenseurl": md.get("licenseurl", ""),
            }
        except Exception as e:
            return {"error": str(e)}

    def start_upload(self, config: dict):
        """Start upload in background thread."""
        if self._running:
            self._log("Upload already in progress.", "warn")
            return False
        self._stop_flag.clear()
        t = threading.Thread(target=self._upload_thread, args=(config,), daemon=True)
        t.start()
        return True

    def stop_upload(self):
        """Instant stop — closes HTTP session to abort in-progress uploads."""
        self._stop_flag.set()
        self._log("Stopping immediately...", "warn")
        # Close the requests session to abort any in-progress HTTP transfers
        if hasattr(self, "_requests_session") and self._requests_session:
            try:
                self._requests_session.close()
            except Exception:
                pass
        # Force reset GUI state after short delay
        import threading as _t
        def _force_reset():
            import time as _time
            _time.sleep(3)
            self._running = False
            self._emit("uploadStatus", {"state": "done", "succeeded": 0, "failed": 0})
        _t.Thread(target=_force_reset, daemon=True).start()

    def stop_upload_graceful(self):
        """Stop after current uploads finish — set a graceful flag."""
        self._graceful_stop = True
        self._log("Will stop after current uploads complete...", "warn")

    def set_concurrency(self, n: int):
        """Change concurrency on the fly."""
        self._current_concurrency = max(1, min(12, n))

    def _upload_thread(self, config: dict):
        self._running = True
        self._emit("uploadStatus", {"state": "running"})

        access_key  = config.get("access_key", "")
        secret_key  = config.get("secret_key", "")
        identifier  = config.get("identifier", "").strip()
        files       = config.get("files", [])
        src_folder  = config.get("src_folder", "")  # root folder for relative paths
        metadata    = config.get("metadata", {})
        concurrency = max(1, min(12, int(config.get("concurrency", 1))))
        checksum    = config.get("checksum", True)
        queue_derive= config.get("queue_derive", False)
        verify      = config.get("verify", True)

        if not HAS_IA:
            self._log("ERROR: internetarchive library not installed. Run: pip install internetarchive", "err")
            self._running = False
            self._emit("uploadStatus", {"state": "error"})
            return

        if not access_key or not secret_key:
            self._log("ERROR: No credentials set. Enter your IA S3 keys.", "err")
            self._running = False
            self._emit("uploadStatus", {"state": "error"})
            return

        if not identifier:
            self._log("ERROR: No item identifier set.", "err")
            self._running = False
            self._emit("uploadStatus", {"state": "error"})
            return

        if not files:
            self._log("ERROR: No files selected.", "err")
            self._running = False
            self._emit("uploadStatus", {"state": "error"})
            return

        # Build file list with sizes
        file_paths = [Path(f) for f in files if Path(f).is_file()]
        if not file_paths:
            self._log("ERROR: No valid files found.", "err")
            self._running = False
            self._emit("uploadStatus", {"state": "error"})
            return

        # Apply fixdat filter — skip any file whose stem matches a game entry
        if self._fixdat_names:
            filtered = [fp for fp in file_paths if fp.stem not in self._fixdat_names]
            skipped = len(file_paths) - len(filtered)
            if skipped:
                self._log(f"  Fixdat filter: {skipped} incomplete file(s) excluded from upload", "warn")
            file_paths = filtered
            if not file_paths:
                self._log("ERROR: All files excluded by fixdat filter — nothing to upload.", "err")
                self._running = False
                self._emit("uploadStatus", {"state": "error"})
                return

        total_bytes = sum(f.stat().st_size for f in file_paths)
        # Validate identifier — IA requires: start with letter/number,
        # 5-100 chars, only letters/numbers/dots/hyphens/underscores
        import re as _re_id
        if not _re_id.match(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{4,100}$", identifier):
            self._log("ERROR: Identifier must be 5-100 chars, start with a letter/number, "
                      "and contain only letters, numbers, dots, hyphens, underscores.", "err")
            self._log(f"       Your identifier '{identifier}' has {len(identifier)} chars.", "err")
            self._running = False
            self._emit("uploadStatus", {"state": "error"})
            return

        self._log(f"Starting upload to ia: {identifier}", "info")
        self._log(f"  Files: {len(file_paths)}  Total: {self._fmt_size(total_bytes)}", "info")
        self._log(f"  Concurrency: {concurrency} parallel uploads", "dim")

        # Pre-fetch existing file list once to avoid per-file HTTP checks
        existing_files = set()
        try:
            import requests as _rq_pre
            _r = _rq_pre.get(f"https://archive.org/metadata/{identifier}/files", timeout=15)
            if _r.status_code == 200:
                for _f in _r.json().get("result", []):
                    existing_files.add(_f.get("name", ""))
                if existing_files:
                    self._log(f"  Item has {len(existing_files)} existing file(s) — will skip matches", "dim")
        except Exception:
            pass

        import requests as _requests_mod
        self._requests_session = _requests_mod.Session()

        try:
            # Set credentials as environment variables — most reliable method
            import os as _os
            _os.environ["IAS3ACCESSKEY"] = access_key
            _os.environ["IAS3SECRETKEY"] = secret_key
            session = get_session(config={
                "s3": {"access": access_key, "secret": secret_key},
            })
            item = session.get_item(identifier)
            exists = item.exists
            if exists:
                self._log(f"  Item exists — adding files to: {identifier}", "dim")
            else:
                self._log(f"  New item will be created: {identifier}", "dim")
        except Exception as e:
            self._log(f"ERROR: Could not connect to IA: {e}", "err")
            self._running = False
            self._emit("uploadStatus", {"state": "error"})
            return

        # Upload stats
        completed = []
        failed = []
        bytes_done_total = [0]
        bytes_in_progress = {}  # fname -> bytes sent so far (for active files)
        start_time = time.time()
        lock = threading.Lock()

        # ── Known IA rejected extensions ─────────────────────────────────────────
        IA_REJECTED_EXTS = {
            # IA blocks these for copyright/policy reasons
            '.mkv', '.mp4', '.avi', '.mov', '.wmv', '.flv', '.webm',
            '.m4v', '.mpg', '.mpeg', '.ts', '.vob',
        }
        IA_WARN_EXTS = {'.mkv', '.mp4', '.mov', '.wmv', '.avi'}  # flag before upload

        def check_file_exists_on_ia(fname):
            """Check cached file list — pre-fetched at upload start."""
            return fname in existing_files

        def check_bucket_alive():
            """Check the IA item/bucket is still accessible."""
            try:
                import requests as _rq
                r = _rq.head(
                    f"https://s3.us.archive.org/{identifier}",
                    headers={"Authorization": f"LOW {access_key}:{secret_key}"},
                    timeout=10
                )
                return r.status_code not in (404, 403)
            except Exception:
                return True  # assume ok if check fails

        def upload_one(fpath: Path) -> tuple:
            """Upload a single file with progress tracking."""
            if self._stop_flag.is_set():
                return (fpath, False, "stopped")

            # Check file still exists before proceeding
            if not fpath.exists():
                self._log(f"  ⚠ SKIP: {fpath.name} — file not found (moved/deleted?)", "warn")
                return (fpath, False, "not found")

            # Use relative path from source folder to preserve directory structure
            if src_folder and fpath.is_relative_to(Path(src_folder)):
                fname = str(fpath.relative_to(Path(src_folder))).replace("\\", "/")
            else:
                fname = fpath.name
            fname_l = fname.lower()
            fsize = fpath.stat().st_size
            file_start = time.time()

            # Warn about potentially rejected file types
            ext = fpath.suffix.lower()
            if ext in IA_WARN_EXTS:
                self._log(f"  ⚠ WARNING: {fname} — IA may reject {ext} files. "
                          f"Consider converting to a different format.", "warn")

            # Check if file already exists on IA
            if check_file_exists_on_ia(fname):
                self._log(f"  ↷ SKIP: {fname} — already exists on IA", "dim")
                with lock:
                    bytes_done_total[0] += fsize
                self._emit("fileStatus", {"name": fname, "status": "already", "pct": 100})
                return (fpath, True, "skipped")

            self._log(f"  → Uploading: {fname}  ({self._fmt_size(fsize)})", "dim")
            self._emit("fileStatus", {"name": fname, "size": fsize, "status": "uploading", "pct": 0})

            try:
                import requests as _req

                s3_url = f"https://s3.us.archive.org/{identifier}/{fname}"
                upload_meta = metadata if not item.exists else {}

                headers = {
                    "Authorization": f"LOW {access_key}:{secret_key}",
                    "x-archive-auto-make-bucket": "1",
                    "x-archive-queue-derive": "1" if queue_derive else "0",
                    "x-archive-size-hint": str(fsize),
                    "Content-Type": "application/octet-stream",
                    "Content-Length": str(fsize),
                }
                for k, v in upload_meta.items():
                    if v:
                        if isinstance(v, list):
                            for vi_i, vi in enumerate(v):
                                headers[f"x-archive-meta{vi_i:02d}-{k}"] = str(vi)
                        else:
                            headers[f"x-archive-meta-{k}"] = str(v)

                LARGE_FILE = 100 * 1024 * 1024  # 100MB
                STALL_TIMEOUT = 30  # seconds of no progress before abort

                if fsize > LARGE_FILE:
                    # Large file — use chunked reader with progress tracking
                    bytes_sent = [0]
                    last_progress_time = [time.time()]
                    last_progress_bytes = [0]
                    stall_detected = [False]

                    _outer_stop = self._stop_flag
                    _outer_self = self
                    class ProgressReader:
                        def __init__(self, path, chunk=512*1024):
                            self._f = open(path, 'rb')
                            self._chunk = chunk
                        def read(self, n=-1):
                            if _outer_stop.is_set() or stall_detected[0]:
                                return b''
                            chunk = self._f.read(self._chunk if n < 0 else n)
                            if chunk:
                                bytes_sent[0] += len(chunk)
                                now = time.time()
                                elapsed_s = now - file_start
                                spd = bytes_sent[0] / elapsed_s if elapsed_s > 0 else 0
                                pct = int((bytes_sent[0] / fsize) * 100) if fsize > 0 else 0
                                # Stall detection
                                if bytes_sent[0] > last_progress_bytes[0]:
                                    last_progress_time[0] = now
                                    last_progress_bytes[0] = bytes_sent[0]
                                elif now - last_progress_time[0] > STALL_TIMEOUT:
                                    stall_detected[0] = True
                                    return b''
                                self._emit_prog(pct, bytes_sent[0], spd)
                            return chunk
                        def _emit_prog(self, pct, done, spd):
                            try:
                                _outer_self._emit("fileProgress", {
                                    "name": fname, "bytes_done": done,
                                    "total": fsize, "pct": pct, "speed": spd,
                                })
                                now = time.time()
                                if now - getattr(self, '_last_log', 0) >= 15:
                                    self._last_log = now
                                    _outer_self._log(
                                        f"  ↑ {fname}: {_outer_self._fmt_size(done)}/"
                                        f"{_outer_self._fmt_size(fsize)} ({pct}%)  "
                                        f"{_outer_self._fmt_size(int(spd))}/s", "dim"
                                    )
                            except Exception:
                                pass
                        def __len__(self): return fsize
                        def close(self): self._f.close()
                        def __enter__(self): return self
                        def __exit__(self, *a): self.close()

                    reader = ProgressReader(fpath)
                    try:
                        resp = self._requests_session.put(s3_url, data=reader,
                                        headers=headers, timeout=(60, None))
                    finally:
                        reader.close()

                    if stall_detected[0]:
                        raise Exception(f"Upload stalled — no progress for {STALL_TIMEOUT}s. "
                                        f"IA may have rejected the file or the bucket was deleted.")
                    if self._stop_flag.is_set():
                        return (fpath, False, "stopped")
                else:
                    # Small file — read into memory
                    with open(fpath, "rb") as _f:
                        file_data = _f.read()
                    bytes_in_progress[fname] = fsize  # mark as in-progress
                    resp = self._requests_session.put(s3_url, data=file_data,
                                    headers=headers, timeout=(60, None))
                    elapsed_f = time.time() - file_start
                    spd_f = fsize / elapsed_f if elapsed_f > 0 else 0
                    self._emit("fileProgress", {"name": fname, "bytes_done": fsize,
                                "total": fsize, "pct": 100, "speed": spd_f})

                # Check response
                if resp.status_code in (200, 201):
                    elapsed = time.time() - file_start
                    speed = fsize / elapsed if elapsed > 0 else 0
                    with lock:
                        bytes_done_total[0] += fsize
                        bytes_in_progress.pop(fname, None)
                        elapsed_f = time.time() - file_start
                        completion_log.append((time.time(), fsize))
                    self._log(
                        f"  ✓ Done: {fname}  ({self._fmt_size(fsize)} in "
                        f"{elapsed:.1f}s  {self._fmt_speed(speed)})", "ok"
                    )
                    self._emit("fileStatus", {"name": fname, "status": "done", "pct": 100})
                    return (fpath, True, None)
                elif resp.status_code == 404:
                    raise Exception(f"Bucket not found (404) — IA may have deleted the item '{identifier}'. "
                                    f"Check archive.org/details/{identifier}")
                elif resp.status_code == 403:
                    raise Exception(f"Access denied (403) — check your credentials or item permissions.")
                elif resp.status_code in (503, 429) or "reduce your request rate" in resp.text.lower():
                    raise Exception(f"Please reduce your request rate. - {resp.text[:200]}")
                elif resp.status_code == 415 or "unsupported media" in resp.text.lower():
                    raise Exception(f"File type rejected by IA ({ext}) — {resp.text[:200]}")
                else:
                    raise Exception(f"HTTP {resp.status_code}: {resp.text[:300]}")

            except Exception as e:
                err = str(e)
                print(f"UPLOAD ERROR {fname}: {err}")

                if "stopped" in err.lower() or \
                   "connectionerror" in type(e).__name__.lower() or \
                   "remotedisconnected" in err.lower() or \
                   "connection aborted" in err.lower():
                    self._emit("fileStatus", {"name": fname, "status": "stopped", "pct": 0})
                    return (fpath, False, "stopped")

                # Bucket deleted/unavailable
                if "404" in err or "bucket not found" in err.lower() or "deleted" in err.lower():
                    self._log(f"  ✗ BUCKET DELETED: {err}", "err")
                    self._log(f"  ⚠ IA may have removed the item '{identifier}' — stopping upload.", "err")
                    self._stop_flag.set()
                    self._emit("fileStatus", {"name": fname, "status": "error", "pct": 0})
                    return (fpath, False, err)

                # Stall / rejected file type
                if "stalled" in err.lower() or "rejected" in err.lower() or "415" in err:
                    self._log(f"  ✗ {fname}: {err[:200]}", "err")
                    self._emit("fileStatus", {"name": fname, "status": "error", "pct": 0})
                    return (fpath, False, err)

                # Rate limit — wait and retry
                if "reduce your request rate" in err.lower() or "503" in err or "429" in err:
                    self._log(f"  ⏳ Rate limited — waiting 60s then retrying: {fname}", "warn")
                    self._emit("fileStatus", {"name": fname, "status": "waiting", "pct": 0})
                    time.sleep(60)
                    if not self._stop_flag.is_set():
                        self._log(f"  ↺ Retrying: {fname}", "dim")
                        return upload_one(fpath)  # recursive retry

                self._log(f"  ✗ FAILED: {fname}: {err[:200]}", "err")
                self._emit("fileStatus", {"name": fname, "status": "error", "pct": 0})
                return (fpath, False, err)

        # ── Run uploads ───────────────────────────────────────────────────────
        self._log(f"Uploading {len(file_paths)} file(s) with {concurrency} threads...", "info")

        # Background stats thread — updates display every second
        stats_stop = [False]
        # Track file completion times for speed calculation
        completion_log = []  # list of (time, bytes) when each file completes

        def update_stats():
            while not stats_stop[0]:
                time.sleep(1)
                if stats_stop[0]: break
                now = time.time()
                # Speed = bytes completed in last 30 seconds
                cutoff = now - 30
                recent = [(t,b) for t,b in completion_log if t > cutoff]
                if recent:
                    window = now - recent[0][0]
                    spd = sum(b for _,b in recent) / window if window > 0 else 0
                else:
                    spd = 0  # no recent completions — show nothing yet
                rem = total_bytes - bytes_done_total[0]
                eta = int(rem / spd) if spd > 0 else 0
                done = len(completed) + len(failed)
                pct = int((bytes_done_total[0] / total_bytes) * 100) if total_bytes > 0 else 0
                self._emit("overallProgress", {
                    "done": done, "total": len(file_paths),
                    "bytes_done": bytes_done_total[0],
                    "total_bytes": total_bytes,
                    "pct": pct, "speed": spd, "eta": eta,
                })
        import threading as _st
        _stats_thread = _st.Thread(target=update_stats, daemon=True)
        _stats_thread.start()

        # Dynamic concurrency — read from config every time we submit a new file
        def get_current_concurrency():
            return max(1, min(12, int(self._current_concurrency if
                              hasattr(self, "_current_concurrency") else concurrency)))

        self._current_concurrency = concurrency

        # Upload queue — controlled concurrency with dynamic adjustment
        queue = list(file_paths)
        active_futures = {}

        with ThreadPoolExecutor(max_workers=12) as executor:
            while (queue or active_futures) and not self._stop_flag.is_set():
                # Fill up to current concurrency
                while len(active_futures) < get_current_concurrency() and queue \
                        and not self._stop_flag.is_set():
                    fp = queue.pop(0)
                    fut = executor.submit(upload_one, fp)
                    active_futures[fut] = fp
                    # Small stagger only after initial batch to avoid rate limiting
                    if len(active_futures) > 1:
                        time.sleep(1)

                # Wait for any to complete
                if active_futures:
                    from concurrent.futures import wait as _cf_wait, FIRST_COMPLETED as _FC
                    done_futs, _ = _cf_wait(
                        active_futures, timeout=1, return_when=_FC
                    )
                    for fut in done_futs:
                        active_futures.pop(fut)
                        fpath, success, err = fut.result()
                        if success:
                            completed.append(fpath)
                        else:
                            if err not in ("stopped", "skipped"):
                                failed.append((fpath, err))
                else:
                    time.sleep(0.1)

        stats_stop[0] = True

        # Summary
        elapsed = time.time() - start_time
        self._log("", "")
        self._log(f"Upload complete — {len(completed)} succeeded, {len(failed)} failed  "
                  f"({elapsed:.0f}s total)", "ok" if not failed else "warn")
        if failed:
            for fp, err in failed:
                if err not in ("stopped", "skipped"):
                    self._log(f"  FAILED: {fp.name}: {err[:80]}", "err")

        self._running = False
        self._emit("uploadStatus", {
            "state": "done",
            "succeeded": len(completed),
            "failed": len(failed),
        })

    @staticmethod
    def _fmt_size(n: int) -> str:
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if n < 1024:
                return f"{n:.1f} {unit}"
            n /= 1024
        return f"{n:.1f} PB"

    @staticmethod
    def _fmt_speed(bps: float) -> str:
        """Always show speed in KB/s or MB/s for consistency."""
        kbs = bps / 1024
        if kbs < 1024:
            return f"{kbs:.1f} KB/s"
        return f"{kbs/1024:.2f} MB/s"

    def browse_folder(self) -> str:
        try:
            import tkinter as tk
            from tkinter import filedialog
            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            path = filedialog.askdirectory()
            root.destroy()
            return path if path else ""
        except Exception:
            return ""

    def open_folder_packer(self):
        """
        Opens the IA Folder Packer window (where archive grouping/
        letter-split/depth-based packing now lives). Normally wired
        onto this instance by main.py at window-creation time — this
        fallback just logs a clear message rather than raising if
        somehow called before that wiring happened (e.g. the script
        run standalone outside the main app).
        """
        self._log(
            "Could not open Folder Packer — this only works when launched "
            "via the main ToSort Toolkit app.", "warn"
        )

    def browse_files(self) -> list:
        try:
            import tkinter as tk
            from tkinter import filedialog
            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            paths = filedialog.askopenfilenames()
            root.destroy()
            return list(paths) if paths else []
        except Exception:
            return []

    def get_folder_files(self, folder: str) -> list:
        """Return list of {name, path, size, rel, excluded} for all files in folder."""
        result = []
        try:
            for root, dirs, files in os.walk(folder):
                for f in sorted(files):
                    fp = Path(root) / f
                    excluded = fp.stem in self._fixdat_names if self._fixdat_names else False
                    result.append({
                        "name": fp.name,
                        "path": str(fp),
                        "size": fp.stat().st_size,
                        "rel": str(fp.relative_to(folder)),
                        "excluded": excluded,
                    })
        except Exception:
            pass
        return result


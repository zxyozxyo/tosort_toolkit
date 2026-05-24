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

    def set_window(self, window):
        self._window = window

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
        self._stop_flag.set()
        self._log("Stopping upload after current files complete...", "warn")

    def _upload_thread(self, config: dict):
        self._running = True
        self._emit("uploadStatus", {"state": "running"})

        access_key  = config.get("access_key", "")
        secret_key  = config.get("secret_key", "")
        identifier  = config.get("identifier", "").strip()
        files       = config.get("files", [])
        metadata    = config.get("metadata", {})
        concurrency = max(1, min(4, int(config.get("concurrency", 2))))
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

        total_bytes = sum(f.stat().st_size for f in file_paths)
        self._log(f"Starting upload to ia: {identifier}", "info")
        self._log(f"  Files: {len(file_paths)}  Total: {self._fmt_size(total_bytes)}", "info")
        self._log(f"  Concurrency: {concurrency} parallel uploads", "dim")

        try:
            session = get_session(config={
                "s3": {"access": access_key, "secret": secret_key}
            })
            item = session.get_item(identifier)
        except Exception as e:
            self._log(f"ERROR: Could not connect to IA: {e}", "err")
            self._running = False
            self._emit("uploadStatus", {"state": "error"})
            return

        # Upload stats
        completed = []
        failed = []
        bytes_done_total = [0]
        start_time = time.time()
        lock = threading.Lock()

        def upload_one(fpath: Path) -> tuple:
            """Upload a single file, return (fpath, success, error)."""
            if self._stop_flag.is_set():
                return (fpath, False, "stopped")

            fname = fpath.name
            fsize = fpath.stat().st_size
            file_start = time.time()

            self._log(f"  → Uploading: {fname}  ({self._fmt_size(fsize)})", "dim")
            self._emit("fileStatus", {
                "name": fname, "size": fsize,
                "status": "uploading", "pct": 0
            })

            try:
                # Use requests-based upload with progress tracking
                import requests

                def progress_callback(monitor):
                    """Called by requests-toolbelt with upload progress."""
                    if self._stop_flag.is_set():
                        raise Exception("Upload stopped by user")
                    bytes_sent = monitor.bytes_read
                    pct = int((bytes_sent / fsize) * 100) if fsize > 0 else 100
                    elapsed = time.time() - file_start
                    speed = bytes_sent / elapsed if elapsed > 0 else 0
                    self._emit("fileProgress", {
                        "name": fname,
                        "bytes_done": bytes_sent,
                        "total": fsize,
                        "pct": pct,
                        "speed": speed,
                    })

                # Upload via internetarchive
                responses = item.upload(
                    str(fpath),
                    access_key=access_key,
                    secret_key=secret_key,
                    metadata=metadata if not item.exists else {},
                    checksum=checksum,
                    queue_derive=queue_derive,
                    verbose=False,
                    retries=3,
                    retries_sleep=10,
                )

                elapsed = time.time() - file_start
                speed = fsize / elapsed if elapsed > 0 else 0
                with lock:
                    bytes_done_total[0] += fsize

                self._log(
                    f"  ✓ Done: {fname}  ({self._fmt_size(fsize)} in {elapsed:.1f}s  "
                    f"{self._fmt_size(int(speed))}/s)", "ok"
                )
                self._emit("fileStatus", {
                    "name": fname, "size": fsize,
                    "status": "done", "pct": 100
                })
                return (fpath, True, None)

            except Exception as e:
                err = str(e)
                if "stopped" in err.lower():
                    self._emit("fileStatus", {"name": fname, "status": "stopped", "pct": 0})
                    return (fpath, False, "stopped")
                self._log(f"  ✗ FAILED: {fname}: {err[:120]}", "err")
                self._emit("fileStatus", {"name": fname, "status": "error", "pct": 0})
                return (fpath, False, err)

        # Run concurrent uploads
        self._log(f"Uploading {len(file_paths)} file(s) with {concurrency} threads...", "info")

        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            futures = {executor.submit(upload_one, fp): fp for fp in file_paths}
            done_count = 0
            for future in as_completed(futures):
                if self._stop_flag.is_set():
                    break
                fpath, success, err = future.result()
                done_count += 1
                if success:
                    completed.append(fpath)
                else:
                    failed.append((fpath, err))

                # Overall progress
                pct = int((done_count / len(file_paths)) * 100)
                elapsed = time.time() - start_time
                overall_speed = bytes_done_total[0] / elapsed if elapsed > 0 else 0
                remaining_bytes = total_bytes - bytes_done_total[0]
                eta = int(remaining_bytes / overall_speed) if overall_speed > 0 else 0
                self._emit("overallProgress", {
                    "done": done_count,
                    "total": len(file_paths),
                    "bytes_done": bytes_done_total[0],
                    "total_bytes": total_bytes,
                    "pct": pct,
                    "speed": overall_speed,
                    "eta": eta,
                })

        # Summary
        elapsed = time.time() - start_time
        self._log("", "")
        self._log(f"Upload complete — {len(completed)} succeeded, {len(failed)} failed  "
                  f"({elapsed:.0f}s total)", "ok" if not failed else "warn")
        if failed:
            for fp, err in failed:
                if err != "stopped":
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
        """Return list of {name, path, size} for all files in folder."""
        result = []
        try:
            for root, dirs, files in os.walk(folder):
                for f in sorted(files):
                    fp = Path(root) / f
                    result.append({
                        "name": fp.name,
                        "path": str(fp),
                        "size": fp.stat().st_size,
                        "rel": str(fp.relative_to(folder))
                    })
        except Exception:
            pass
        return result


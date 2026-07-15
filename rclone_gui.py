"""
RClone IA Upload GUI — Standalone
Wraps rclone for Internet Archive uploads with real-time progress.
"""

import os
import re
import threading
import time
import json
import subprocess
from pathlib import Path

import webview


class RCloneAPI:
    def __init__(self):
        self._window = None
        self._proc = None
        self._stop_flag = threading.Event()
        self._running = False
        self._fixdat_names: set = set()
        self._fixdat_path: str = ""

    def set_window(self, w):
        self._window = w

    def _emit(self, event: str, data: dict):
        if not self._window:
            return
        try:
            import json as _j
            payload = _j.dumps(data, ensure_ascii=True).replace("'", "\\'")
            self._window.evaluate_js(
                f"window.rcEvent('{event}', JSON.parse('{payload}'))"
            )
        except Exception:
            pass

    def _log(self, msg: str, cls: str = "info"):
        self._emit("log", {"msg": msg, "cls": cls})

    # ── Credentials / Config ─────────────────────────────────────────────────

    def save_config(self, cfg: dict) -> bool:
        try:
            p = Path(__file__).parent / "rclone_ia.json"
            with open(p, "w") as f:
                json.dump(cfg, f, indent=2)
            return True
        except Exception:
            return False

    def load_config(self) -> dict:
        try:
            p = Path(__file__).parent / "rclone_ia.json"
            if p.exists():
                with open(p) as f:
                    cfg = json.load(f)
                # Auto-restore fixdat if path was saved
                fixdat_path = cfg.get("fixdat_path", "")
                if fixdat_path and Path(fixdat_path).exists():
                    names = self._parse_fixdat(fixdat_path)
                    if names:
                        self._fixdat_names = names
                        self._fixdat_path = fixdat_path
                return cfg
        except Exception:
            pass
        return {}

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
            # Persist alongside other rclone settings
            try:
                cfg_path = Path(__file__).parent / "rclone_ia.json"
                cfg = {}
                if cfg_path.exists():
                    with open(cfg_path) as f:
                        cfg = json.load(f)
                cfg["fixdat_path"] = path
                with open(cfg_path, "w") as f:
                    json.dump(cfg, f, indent=2)
            except Exception:
                pass
            return {"ok": True, "count": len(names), "path": path}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def clear_fixdat(self) -> bool:
        self._fixdat_names = set()
        self._fixdat_path = ""
        try:
            cfg_path = Path(__file__).parent / "rclone_ia.json"
            if cfg_path.exists():
                with open(cfg_path) as f:
                    cfg = json.load(f)
                cfg["fixdat_path"] = ""
                with open(cfg_path, "w") as f:
                    json.dump(cfg, f, indent=2)
        except Exception:
            pass
        return True

    def get_fixdat_status(self) -> dict:
        return {
            "active": bool(self._fixdat_names),
            "count": len(self._fixdat_names),
            "path": self._fixdat_path,
        }

    def write_rclone_conf(self, access_key: str, secret_key: str, derive: bool = False) -> dict:
        _rclone_dir = Path(__file__).parent / "rclone"
        _rclone_dir.mkdir(exist_ok=True)
        conf_path = _rclone_dir / "rclone.conf"
        conf_content = (
            "[archive]\n"
            "type = internetarchive\n"
            f"access_key_id = {access_key}\n"
            f"secret_access_key = {secret_key}\n"
            f"derive = {str(derive).lower()}\n"
        )
        try:
            with open(conf_path, "w") as f:
                f.write(conf_content)
            return {"ok": True, "path": str(conf_path)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def check_rclone(self) -> dict:
        rclone = self._find_rclone()
        if not rclone:
            return {"found": False}
        try:
            r = subprocess.run(
                [rclone, "version"],
                capture_output=True, encoding="utf-8", errors="replace",
                timeout=10
            )
            ver = r.stdout.split("\n")[0] if r.stdout else "unknown"
            return {"found": True, "version": ver, "path": rclone}
        except Exception as e:
            return {"found": False, "error": str(e)}

    def fetch_item_metadata(self, identifier: str) -> dict:
        try:
            import requests as _rq
            r = _rq.get(f"https://archive.org/metadata/{identifier}", timeout=15)
            if r.status_code == 404:
                return {"exists": False}
            if r.status_code != 200:
                return {"error": f"HTTP {r.status_code}"}
            data = r.json()
            md = data.get("metadata", {})
            if not md:
                return {"exists": False}
            return {
                "exists": True,
                "title":       md.get("title", ""),
                "description": md.get("description", ""),
                "creator":     md.get("creator", ""),
                "date":        md.get("date", ""),
                "subject":     md.get("subject", ""),
                "mediatype":   md.get("mediatype", "software"),
                "collection":  md.get("collection", "opensource"),
                "language":    md.get("language", ""),
            }
        except Exception as e:
            return {"error": str(e)}

    def check_conf(self) -> dict:
        # Check rclone/ subfolder first, then app root
        conf = Path(__file__).parent / "rclone" / "rclone.conf"
        if not conf.exists():
            conf = Path(__file__).parent / "rclone.conf"
        if not conf.exists():
            return {"exists": False}
        try:
            content = conf.read_text()
            has_archive = "[archive]" in content
            return {"exists": True, "has_archive": has_archive, "path": str(conf)}
        except Exception:
            return {"exists": False}

    # ── Upload ───────────────────────────────────────────────────────────────

    def start_upload(self, config: dict) -> bool:
        if self._running:
            self._log("Upload already running.", "warn")
            return False
        self._stop_flag.clear()
        t = threading.Thread(target=self._run, args=(config,), daemon=True)
        t.start()
        return True

    def stop_upload(self):
        self._stop_flag.set()
        if self._proc:
            try:
                self._proc.terminate()
            except Exception:
                pass
        self._log("Stopping...", "warn")

    def _run(self, config: dict):
        self._running = True
        self._emit("status", {"state": "running"})

        src        = config.get("src", "").strip()
        identifier = config.get("identifier", "").strip()
        transfers  = int(config.get("transfers", 4))
        checkhash  = config.get("checkhash", False)
        wait_arch  = config.get("wait_archive", "5m0s")
        verbose    = config.get("verbose", "-vv")  # "-v" or "-vv" — -vv needed for "Unchanged skipping" file-status tracking
        derive     = config.get("derive", False)
        metadata   = config.get("metadata", {})

        rclone = self._find_rclone()
        if not rclone:
            self._log("ERROR: rclone not found. Place rclone.exe in the script folder.", "err")
            self._running = False
            self._emit("status", {"state": "error"})
            return

        conf = Path(__file__).parent / "rclone" / "rclone.conf"
        if not conf.exists():
            conf = Path(__file__).parent / "rclone.conf"
        if not conf.exists():
            self._log("ERROR: rclone.conf not found. Set up credentials first.", "err")
            self._running = False
            self._emit("status", {"state": "error"})
            return

        if not src or not Path(src).exists():
            self._log(f"ERROR: Source folder not found: {src}", "err")
            self._running = False
            self._emit("status", {"state": "error"})
            return

        if not identifier:
            self._log("ERROR: No item identifier set.", "err")
            self._running = False
            self._emit("status", {"state": "error"})
            return

        # Build rclone command
        cmd = [
            rclone, "copy",
            "--config", str(conf),
            "--transfers", str(transfers),
            "--no-check-certificate",
            "--progress",
            "--stats", "2s",
            verbose,
        ]

        if not checkhash:
            cmd.append("--internetarchive-disable-checksum")

        # derive: controlled via rclone.conf derive= setting
        if wait_arch and derive:
            cmd += ["--internetarchive-wait-archive", wait_arch]

        for k, v in metadata.items():
            if v:
                if isinstance(v, list):
                    for vi in v:
                        if vi:
                            cmd += ["--internetarchive-item-metadata", f"{k}={vi}"]
                else:
                    cmd += ["--internetarchive-item-metadata", f"{k}={v}"]

        # Apply fixdat filter — add --exclude for each incomplete file found in source
        if self._fixdat_names:
            try:
                excluded_count = 0
                for entry in Path(src).iterdir():
                    if entry.is_file() and entry.stem in self._fixdat_names:
                        cmd += ["--exclude", entry.name]
                        excluded_count += 1
                if excluded_count:
                    self._log(f"  Fixdat filter: {excluded_count} incomplete file(s) will be skipped", "warn")
            except Exception:
                pass

        cmd += [src, f"archive:{identifier}"]

        self._log("Starting rclone upload", "info")
        self._log(f"  Source:     {src}", "dim")
        self._log(f"  Identifier: {identifier}", "dim")
        self._log(f"  Transfers:  {transfers}  Verbose: {verbose}  Derive: {derive}", "dim")
        for _mk, _mv in metadata.items():
            if _mv:
                self._log(f"  Meta: {_mk} = {_mv}", "dim")
        self._log(f"  Command:    {' '.join(cmd[:6])}...", "dim")
        self._log("", "")

        start_time = time.time()

        try:
            self._proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                creationflags=0x08000000 if os.name == "nt" else 0,
            )

            for raw_line in self._proc.stdout:
                if self._stop_flag.is_set():
                    break
                try:
                    line = raw_line.decode("utf-8", errors="replace").rstrip()
                except Exception:
                    continue
                if not line:
                    continue

                # Parse for stats and active file tracking — every line
                # must be parsed regardless of whether it gets logged
                self._parse_line(line)

                # Log filtering — at -vv rclone emits a continuous
                # flood of DEBUG lines (Sizes identical, size = N OK,
                # Unchanged skipping, Checks: N/N, Checking:, etc.)
                # that are useful for diagnosing broken transfers but
                # make the visible log panel unreadable for a normal
                # monitoring view. Use an allowlist: only show lines
                # that are genuinely worth a user's attention, and
                # suppress the rest silently (parsing above still sees
                # everything for file-queue status tracking).

                # Always suppress: progress-panel lines (handled by
                # the stats widget), per-file check/transfer progress
                # lines, and all DEBUG-level lines (they're only
                # needed by the parser, not for human reading)
                if "Transferred:" in line:
                    continue
                if line.strip().startswith("*"):
                    continue
                if " DEBUG " in line or "DEBUG : " in line:
                    continue
                # Suppress noisy periodic stats blocks
                if line.strip().startswith("Checks:"):
                    continue
                if line.strip().startswith("Elapsed time:"):
                    continue
                if line.strip() in ("Checking:", "Transferring:"):
                    continue
                if "Listed " in line and "Checks:" in line:
                    continue

                # Everything that made it past the suppressors gets a
                # colour and goes to the log panel
                cls = "dim"
                if "ERROR" in line or " error" in line.lower():
                    cls = "err"
                elif "Copied" in line:
                    cls = "ok"
                elif "NOTICE" in line:
                    cls = "warn"

                self._log(line, cls)

            self._proc.wait()
            elapsed = time.time() - start_time
            rc = self._proc.returncode

            self._log("", "")
            if rc == 0:
                self._log(f"Upload complete — {elapsed:.0f}s total", "ok")
                self._emit("status", {"state": "done", "rc": 0})
            elif self._stop_flag.is_set():
                self._log("Upload stopped by user.", "warn")
                self._emit("status", {"state": "stopped"})
            else:
                self._log(f"rclone exited with code {rc} — check log for errors", "err")
                self._emit("status", {"state": "error", "rc": rc})

        except Exception as e:
            self._log(f"ERROR: {e}", "err")
            self._emit("status", {"state": "error"})
        finally:
            self._running = False
            self._proc = None

    def _parse_line(self, line: str):
        """Parse stats lines for progress panel, and per-file transfer
        lines for the live file-queue display."""
        try:
            # Transferred: prefix format
            m = re.search(
                r'Transferred:\s+([\d.]+\s*\S+)\s*/\s*([\d.]+\s*\S+),\s*(\d+)%,\s*([\d.]+\s*\S+),\s*ETA\s+(\S+)',
                line
            )
            if m:
                self._emit("progress", {
                    "done": m.group(1), "total": m.group(2),
                    "pct": int(m.group(3)), "speed": m.group(4), "eta": m.group(5),
                })
                return
            # Bare stats: 159 MiB / 365 GiB, 0%, 8.712 MiB/s, ETA 11h55m (xfr#0/136)
            m2 = re.search(
                r'([\d.]+\s*[KMGT]?i?B)\s*/\s*([\d.]+\s*[KMGT]?i?B),\s*(\d+)%,\s*([\d.]+\s*[KMGT]?i?B/s),\s*ETA\s+(\S+)',
                line
            )
            if m2:
                self._emit("progress", {
                    "done": m2.group(1), "total": m2.group(2),
                    "pct": int(m2.group(3)), "speed": m2.group(4), "eta": m2.group(5),
                })
                return

            self._parse_file_line(line)
        except Exception:
            pass

    def _parse_file_line(self, line: str):
        """
        Extracts per-file transfer status for the live file-queue
        display. rclone reports per-file activity in two DIFFERENT
        styles that need to be handled separately:

        1. --progress's "Transferring:" block lines, prefixed with
           "*" — these can have the filename TRUNCATED in the middle
           with an ellipsis for long paths (e.g.
           "Games/RPGs/D&D 5th Edi…10/Human Paladin 10.pdf"), so they
           are ONLY used for live in-progress percentage updates via a
           best-effort prefix/suffix match — never for marking a file
           definitively done, since a truncated name can't be trusted
           to uniquely and exactly identify the real file.

           Examples:
             " * file.zip: transferring"
             " * file.zip:  45% /120.000Mi, 5.234Mi/s, 1m30s"

        2. -v's plain INFO/ERROR log lines — these carry the FULL,
           untruncated relative path, and are the only signal trusted
           for marking a file definitively done or errored.

           Examples:
             "2026/06/21 10:00:00 INFO  : sub/file.zip: Copied (new)"
             "2026/06/21 10:00:00 ERROR : sub/file.zip: Failed to copy: ..."
        """
        # Style 2 first (more specific, untruncated, authoritative for
        # completion) — matches a line containing "INFO"/"ERROR"/"NOTICE"
        # followed by " : name: rest". Real-world testing showed rclone
        # logs "Unchanged skipping" at DEBUG level even under plain -v
        # (not just -vv as might be assumed) — DEBUG is included here
        # specifically to catch that, but the level alone is NOT used
        # to decide anything; only the matched `rest` text below
        # determines what (if anything) gets emitted, so other noisy
        # DEBUG lines ("Sizes identical", "size = ... OK", etc.) are
        # correctly ignored rather than misread as a status change.
        m = re.match(r'^\S+\s+\S+\s+(DEBUG|INFO|ERROR|NOTICE)\s*:\s+(.+?):\s+(.*)$', line)
        if m:
            level, name, rest = m.group(1), m.group(2), m.group(3)
            name = name.replace("\\", "/")
            if level == "ERROR":
                self._emit("fileStatus", {"name": name, "status": "error"})
            elif "Copied" in rest or "Deleted" in rest:
                self._emit("fileStatus", {"name": name, "status": "done"})
            elif "Unchanged skipping" in rest:
                # File already matches what's on the remote — rclone
                # correctly didn't re-upload it, since it was already
                # there from a previous run. Distinct status from
                # "done" (newly uploaded THIS run) so the queue can
                # show the difference and both can still be cleared
                # together via "Clear Uploaded".
                self._emit("fileStatus", {"name": name, "status": "already"})
            return

        # Style 1: progress-block lines, prefixed with "*"
        stripped = line.strip()
        if not stripped.startswith('*'):
            return
        body = stripped[1:].strip()

        # "name: transferring" (just started, 0%)
        m2 = re.match(r'^(.+?):\s*transferring\s*$', body, re.IGNORECASE)
        if m2:
            self._emit("fileProgress", {"name": m2.group(1).replace("\\", "/"), "pct": 0})
            return

        # "name: NN% /size, speed, eta"
        m3 = re.match(r'^(.+?):\s*(\d+)%\s*/', body)
        if m3:
            self._emit("fileProgress", {
                "name": m3.group(1).replace("\\", "/"), "pct": int(m3.group(2))
            })
            return

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _find_rclone(self) -> str:
        import shutil
        app = Path(__file__).parent
        search_dirs = [app, app / "rclone", app / "apps"]
        for name in ["rclone.exe", "rclone"]:
            for d in search_dirs:
                local = d / name
                if local.exists():
                    return str(local)
        return shutil.which("rclone") or ""

    def open_folder_packer(self):
        """
        Opens the IA Folder Packer window. Normally wired onto this
        instance by main.py at window-creation time — this fallback
        just logs a clear message rather than raising if somehow
        called before that wiring happened (e.g. the script run
        standalone outside the main app).
        """
        self._log(
            "Could not open Folder Packer — this only works when launched "
            "via the main ToSort Toolkit app.", "warn"
        )

    def get_folder_files(self, folder: str) -> list:
        """
        Return {name, rel, size} for every file under `folder`,
        recursively — used to populate the file-queue display BEFORE
        the upload starts, so the person can see what's about to be
        transferred. `rel` is the path relative to `folder` with
        forward slashes (normalised the same way rclone reports
        filenames), matching the IA uploader's get_folder_files
        convention so the same display/matching logic works the same
        way across both uploaders. This is a pure directory listing —
        no hashing, no network calls — so it stays fast even on very
        large folders.
        """
        result = []
        try:
            base = Path(folder)
            for root, dirs, files in os.walk(folder):
                for f in sorted(files):
                    fp = Path(root) / f
                    try:
                        size = fp.stat().st_size
                    except Exception:
                        size = 0
                    rel = str(fp.relative_to(base)).replace("\\", "/")
                    excluded = fp.stem in self._fixdat_names if self._fixdat_names else False
                    result.append({"name": fp.name, "rel": rel, "path": str(fp), "size": size, "excluded": excluded})
        except Exception:
            pass
        return result

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

    # ── Upload profiles (shared store with the Python IA uploader) ───────────

    def profiles_list(self) -> list:
        import upload_profiles
        return upload_profiles.list_profiles("rclone")

    def profiles_save(self, name: str, fields: dict) -> dict:
        import upload_profiles
        return upload_profiles.save_profile(name, "rclone", fields)

    def profiles_delete(self, name: str) -> dict:
        import upload_profiles
        return upload_profiles.delete_profile(name, "rclone")


def main():
    api = RCloneAPI()
    window = webview.create_window(
        title="IA RClone Uploader",
        url=str(Path(__file__).parent / "gui" / "rclone_gui.html"),
        js_api=api,
        width=900,
        height=950,
        min_size=(700, 600),
        background_color="#0d0f12",
    )
    api.set_window(window)
    webview.start(debug=False)


if __name__ == "__main__":
    main()

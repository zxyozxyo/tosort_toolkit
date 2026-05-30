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
                    return json.load(f)
        except Exception:
            pass
        return {}

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
        verbose    = config.get("verbose", "-v")   # "-v" or "-vv"
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
            "--metadata",
            "--progress",
            "--stats", "2s",
            verbose,
        ]

        if not checkhash:
            cmd.append("--internetarchive-disable-checksum")

        # derive is controlled via rclone.conf (set when saving credentials)
        if wait_arch and derive:
            cmd += ["--internetarchive-wait-archive", wait_arch]

        for k, v in metadata.items():
            if v:
                if isinstance(v, list):
                    for vi in v:
                        if vi:
                            cmd += ["--metadata-set", f"{k}={vi}"]
                else:
                    cmd += ["--metadata-set", f"{k}={v}"]

        cmd += [src, f"archive:{identifier}"]

        self._log("Starting rclone upload", "info")
        self._log(f"  Source:     {src}", "dim")
        self._log(f"  Identifier: {identifier}", "dim")
        self._log(f"  Transfers:  {transfers}  Verbose: {verbose}  Derive: {derive}", "dim")
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

                # Parse for stats and active file tracking
                self._parse_line(line)

                # Log colouring
                cls = "dim"
                if "Transferred:" in line and "ETA" in line:
                    continue  # shown in progress panel
                elif line.startswith("*") and ("%" in line or "transferring" in line):
                    continue  # shown in active panel
                elif "ERROR" in line or "error" in line.lower():
                    cls = "err"
                elif "Copied" in line or "copied" in line:
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
        """Parse stats lines for progress panel."""
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
        except Exception:
            pass


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


def main():
    api = RCloneAPI()
    window = webview.create_window(
        title="RClone IA Uploader",
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

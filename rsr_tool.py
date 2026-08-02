"""
RSR — Reproducible Scene Release
================================

A capture-time format that GUARANTEES byte-exact archive reconstruction,
where a v1 `.srr` can only make it *possible*.

The difference is where the brute force happens. pyReScene reassembles an
archive block by block at REBUILD time, having to infer the WinRAR build and
the thread count from evidence the SRR never recorded. We capture while the
ORIGINAL archives are still on disk, so we can find the exact recipe, run it,
and byte-compare the result against the real thing before writing anything.

    reconstruction = replay the original `rar a` command

That one decision is why this module handles what the rebuilder cannot:

  * RAR5      — pyReScene 0.7 can't write it; rar.exe always could.
  * solid     — one command packs the whole solid set, no member surgery.
  * .001/.002 — a volume naming scheme, not a structural problem.

Anything the replay still gets wrong (header-level: timestamps, attributes,
host-OS byte) is stored as a per-volume delta, so a verified .rsr is verified
in the literal sense: we ran it and the bytes matched.

Nothing here ever writes to the folder being scanned.
"""

import os
import re
import io
import csv
import json
import time
import zlib
import shutil
import base64
import sqlite3
import hashlib
import zipfile
import tempfile
import platform
import threading
import subprocess
import traceback
from pathlib import Path
from datetime import datetime, timezone

import webview


RSR_VERSION   = 1
RSR_MAGIC     = "RSR/1 Reproducible Scene Release"
CONFIG_NAME   = "rsr_tool.json"
DB_NAME       = "rsr_index.db"

RAR4_SIG      = b"Rar!\x1a\x07\x00"
RAR5_SIG      = b"Rar!\x1a\x07\x01\x00"

# A replay that lands this close is a header-level difference worth storing as
# a patch. Anything bigger means the recipe is wrong, not the headers, and we
# refuse to pretend otherwise.
DELTA_MAX_BYTES = 2_000_000

# Thread counts in the order they actually win (measured across ~1,200 captured
# rebuilds: mt8 dominates, then mt1/mt3/mt4). Swept OUTERMOST so the common
# counts cover the whole build pack before a rare one is tried anywhere.
MT_ORDER = (8, 4, 2, 1, 3, 6, 5, 7, 0)

# RAR3/4 reject -mt above 16 outright ("Unknown option: mt17"); only RAR5+
# binaries reach 32 — and a RAR5 binary still emits RAR4 via -ma4, which is the
# only route by which a RAR4 archive can carry -mt>16.
RAR4_MT_CAP   = 16

DICT_LETTER   = {64: "A", 128: "B", 256: "C", 512: "D",
                 1024: "E", 2048: "F", 4096: "G"}
RAR4_DICTS    = (4096, 1024, 2048, 512, 256, 128, 64)
RAR5_DICTS    = (32768, 4096, 16384, 65536, 8192, 2048, 1024, 512, 256, 128)

HOST_OS       = {0: "MS-DOS", 1: "OS/2", 2: "Windows", 3: "Unix",
                 4: "macOS", 5: "BeOS"}

_R5_EXE       = re.compile(r"\d{4}-\d{2}-\d{2}_rar[5-9]\d\d(b\d)?\.exe$", re.I)
_R4_EXE       = re.compile(r"\d{4}-\d{2}-\d{2}_rar[0-4]\d")
_DATE_PREFIX  = re.compile(r"^\d{4}-\d{2}-\d{2}[-_]")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file_sha256(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for blk in iter(lambda: f.read(chunk), b""):
            h.update(blk)
    return h.hexdigest()


def _file_crc32(path: Path, chunk: int = 1 << 20) -> int:
    c = 0
    with open(path, "rb") as f:
        for blk in iter(lambda: f.read(chunk), b""):
            c = zlib.crc32(blk, c)
    return c & 0xFFFFFFFF


def _num(value, fallback, cast):
    """Coerce a GUI form value, falling back to the stored one on junk.

    Same defensive shape as misc_tools._num — one unparseable field must never
    take the rest of the settings down with it."""
    for candidate in (value, fallback):
        if candidate is None or candidate == "":
            continue
        try:
            return cast(candidate)
        except (TypeError, ValueError):
            continue
    return cast(0)


def _exe_label(fname: str) -> str:
    """'2012-03-15_rar411.exe' → '2012-03-15 4.11' (matches srrdb_tool)."""
    m = re.match(r"(\d{4}-\d{2}-\d{2})_rar(\d)(\d\d)(b\d)?", fname)
    if not m:
        return fname
    d, maj, mnr, beta = m.groups()
    return f"{d} {maj}.{mnr}" + (f" {beta}" if beta else "")


def _win_attrs(path: Path) -> int | None:
    """Windows file attribute mask, or None off Windows. RAR stores it in the
    file header, so a rebuild that ignores it can differ by a byte."""
    if os.name != "nt":
        return None
    try:
        import ctypes
        v = ctypes.windll.kernel32.GetFileAttributesW(str(path))
        return None if v == 0xFFFFFFFF else int(v)
    except Exception:
        return None


def _set_win_attrs(path: Path, attrs: int | None):
    if os.name != "nt" or not attrs:
        return
    try:
        import ctypes
        # Only the flags RAR actually round-trips; never set READONLY, which
        # would then block our own cleanup of the work directory.
        ctypes.windll.kernel32.SetFileAttributesW(str(path), int(attrs) & 0x26)
    except Exception:
        pass


def _rmtree(path: Path):
    """rmtree that survives read-only files restored from an archive."""
    def _onerror(func, p, _exc):
        try:
            os.chmod(p, 0o700)
            func(p)
        except Exception:
            pass
    shutil.rmtree(path, onerror=_onerror)


def _release_name(folder: Path) -> str:
    """Folder name with any dats.site date prefix stripped, so the store is
    keyed by the ACTUAL release name (point 5 — browsable by hand)."""
    return _DATE_PREFIX.sub("", folder.name)


# ══════════════════════════════════════════════════════════════════════════
#  Volume discovery
# ══════════════════════════════════════════════════════════════════════════

def _rar_format(path: Path) -> str | None:
    """'RAR4' / 'RAR5' / None, from the marker block every volume carries."""
    try:
        with open(path, "rb") as f:
            head = f.read(8)
    except OSError:
        return None
    if head[:8] == RAR5_SIG:
        return "RAR5"
    if head[:7] == RAR4_SIG:
        return "RAR4"
    return None


_VOL_PATTERNS = (
    # (regex on the full name, naming scheme, sort key extractor)
    (re.compile(r"^(?P<stem>.+)\.part(?P<n>\d+)\.rar$", re.I), "part"),
    (re.compile(r"^(?P<stem>.+)\.r(?P<n>\d{2,3})$",     re.I), "old"),
    (re.compile(r"^(?P<stem>.+)\.rar$",                 re.I), "old"),
    (re.compile(r"^(?P<stem>.+?)\.(?P<n>\d{3})$",       re.I), "numeric"),
)


def _classify_volume(name: str):
    """(stem, scheme, index) for a candidate volume filename, or None.

    Index orders volumes within a set: an old-style head `.rar` sorts before
    `.r00`, and `.001` sorts before `.002`."""
    for rx, scheme in _VOL_PATTERNS:
        m = rx.match(name)
        if not m:
            continue
        stem = m.group("stem")
        if scheme == "old":
            n = m.groupdict().get("n")
            idx = -1 if n is None else int(n)      # `.rar` is volume zero
        else:
            idx = int(m.group("n"))
        return stem, scheme, idx
    return None


def group_archive_sets(base: Path) -> list[dict]:
    """Every archive set under `base`, grouped by volume family.

    Extension-blind by design: the current Archive Inspector globs `*.rar` and
    therefore cannot see a `.001` set at all — it reports "no archives found"
    rather than failing, which is the worst kind of miss. Here membership is
    decided by the RAR marker block, and a `.001` family whose later parts have
    NO marker is recognised as a plain byte-split instead."""
    families: dict[tuple, list[tuple[int, Path]]] = {}
    for p in sorted(base.rglob("*")):
        if not p.is_file():
            continue
        cls = _classify_volume(p.name)
        if not cls:
            continue
        stem, scheme, idx = cls
        families.setdefault((str(p.parent), stem, scheme), []).append((idx, p))

    sets = []
    for (parent, stem, scheme), vols in families.items():
        vols.sort()
        paths = [p for _, p in vols]
        fmt = _rar_format(paths[0])
        if not fmt:
            continue                       # not an archive family at all
        # A byte-split archive: only the FIRST part carries the marker, the
        # rest are raw continuation bytes. Concatenate to recover one archive.
        split = len(paths) > 1 and not any(_rar_format(p) for p in paths[1:])
        sets.append({
            "stem": stem,
            "dir": parent,
            "scheme": scheme,
            "format": fmt,
            "volumes": paths,
            "byte_split": split,
        })
    # Deterministic order, and a part-style set must not also surface as its
    # own `.rar` family (part01.rar matches the old-style `.rar` rule too).
    part_stems = {(s["dir"], s["stem"]) for s in sets if s["scheme"] == "part"}
    sets = [s for s in sets
            if not (s["scheme"] == "old"
                    and (s["dir"], re.sub(r"\.part\d+$", "", s["stem"]))
                    in part_stems)]
    sets.sort(key=lambda s: (s["dir"], s["stem"]))
    return sets


def new_numbering(head: Path) -> bool:
    """Whether the archive was written with `.partN.rar` numbering.

    This comes from the main header, NOT the filenames — a set can be renamed
    to `.001` on the way out of a topsite while still carrying the new-numbering
    flag, and both rarfile's volume walk and our own replay have to follow the
    header rather than the disk."""
    fmt = _rar_format(head)
    if fmt == "RAR5":
        return True                      # RAR5 has no old scheme
    try:
        with open(head, "rb") as f:
            data = f.read(12)
    except OSError:
        return False
    if len(data) < 12 or data[9] != 0x73:      # main header block type
        return False
    return bool(int.from_bytes(data[10:12], "little") & 0x0010)


def shadow_set(volumes: list[Path], work: Path, byte_split: bool,
               newnum: bool = False) -> Path:
    """A read-only stand-in for the set that rarfile can walk.

    They both derive the next volume's name from the current one and know only
    the `.rar`/`.r00` and `.partN.rar` schemes — a `.001` set is invisible to
    them. Hardlinking the volumes under old-style names costs nothing and no
    longer touches the originals. A byte-split archive is concatenated back
    into one file instead."""
    work.mkdir(parents=True, exist_ok=True)
    if byte_split:
        joined = work / "joined.rar"
        with open(joined, "wb") as out:
            for v in volumes:
                with open(v, "rb") as f:
                    shutil.copyfileobj(f, out, 1 << 20)
        return joined
    if newnum:
        names = [f"shadow.part{i:02d}.rar" for i in range(1, len(volumes) + 1)]
    else:
        names = ["shadow.rar"] + [f"shadow.r{i:02d}"
                                  for i in range(len(volumes) - 1)]
    for src, nm in zip(volumes, names):
        dst = work / nm
        if dst.exists():
            dst.unlink()
        try:
            os.link(src, dst)
        except OSError:
            shutil.copy2(src, dst)
    return work / names[0]


# ══════════════════════════════════════════════════════════════════════════
#  Packed streams
# ══════════════════════════════════════════════════════════════════════════

def packed_blocks(head: Path) -> dict[str, list[tuple[str, int, int]]]:
    """{packed file name: [(volume, offset, length), …]} — where each file's
    COMPRESSED bytes physically live, in order, across the whole set.

    rarfile's `info_callback` fires for every parsed block, continuation blocks
    included, and every block carries its volume, data offset and payload size.
    That gives us the compressed stream for RAR4 *and* RAR5 from one code path;
    pyReScene's RarStream refuses RAR5 outright, which is the single reason the
    old probe could never look at a RAR5 release."""
    import rarfile
    blocks: dict[str, list] = {}

    def cb(h):
        if h.type != rarfile.RAR_BLOCK_FILE or int(getattr(h, "add_size", 0)) <= 0:
            return
        try:
            if h.is_dir():
                return
        except Exception:
            pass
        if not h.filename:
            return
        blocks.setdefault(h.filename, []).append(
            (str(h.volume_file), int(h.data_offset), int(h.add_size)))

    rf = rarfile.RarFile(str(head), info_callback=cb)
    rf.close()
    return blocks


def stream_digest(blocks: list[tuple[str, int, int]]) -> tuple[int, bytes]:
    """(total length, SHA-1) of a packed file's compressed bytes."""
    h = hashlib.sha1()
    total = 0
    for vol, off, size in blocks:
        with open(vol, "rb") as f:
            f.seek(off)
            left = size
            while left > 0:
                chunk = f.read(min(1 << 20, left))
                if not chunk:
                    break
                h.update(chunk)
                total += len(chunk)
                left -= len(chunk)
    return total, h.digest()


# ══════════════════════════════════════════════════════════════════════════
#  Delta (residual header patch)
# ══════════════════════════════════════════════════════════════════════════

def diff_bytes(produced: bytes, original: bytes) -> bytes | None:
    """A patch turning `produced` into `original`, or None if too big to be a
    header residual. Format: repeated <u64 offset><u32 len><original bytes>."""
    if len(produced) != len(original):
        return None
    out = bytearray()
    total = 0
    i, n = 0, len(original)
    while i < n:
        if produced[i] == original[i]:
            i += 1
            continue
        # Extend the differing run, closing over gaps of fewer than 16 equal
        # bytes so one patched header doesn't become fifty tiny records.
        j = end = i
        equal = 0
        while j < n:
            if produced[j] != original[j]:
                end = j + 1
                equal = 0
            else:
                equal += 1
                if equal >= 16:
                    break
            j += 1
        run = original[i:end]
        total += len(run)
        if total > DELTA_MAX_BYTES:
            return None
        out += (i.to_bytes(8, "little") + len(run).to_bytes(4, "little") + run)
        i = end
    return bytes(out)


def apply_delta(produced: bytes, patch: bytes) -> bytes:
    buf = bytearray(produced)
    i = 0
    while i < len(patch):
        off = int.from_bytes(patch[i:i + 8], "little")
        ln = int.from_bytes(patch[i + 8:i + 12], "little")
        i += 12
        buf[off:off + ln] = patch[i:i + ln]
        i += ln
    return bytes(buf)


# ══════════════════════════════════════════════════════════════════════════
#  API
# ══════════════════════════════════════════════════════════════════════════

class RsrToolAPI:
    def __init__(self):
        self._window = None
        self._stop = threading.Event()
        self._skip = threading.Event()
        self._running = False
        self._procs: set = set()
        self._proc_lock = threading.Lock()
        self._app_dir = Path(__file__).parent

    # ── plumbing ──────────────────────────────────────────────────────────

    def set_window(self, w):
        self._window = w

    def _emit(self, event: str, data: dict):
        if not self._window:
            return
        try:
            payload = (json.dumps(data, ensure_ascii=True)
                       .replace("\\", "\\\\").replace("'", "\\'"))
            self._window.evaluate_js(
                f"window.rsrEvent('{event}', JSON.parse('{payload}'))")
        except Exception:
            pass

    def _log(self, msg: str, cls: str = "info"):
        self._emit("log", {"msg": msg, "cls": cls})

    def _progress(self, msg: str):
        self._emit("progress", {"msg": msg})

    def browse_folder(self) -> str:
        try:
            import tkinter as tk
            from tkinter import filedialog
            root = tk.Tk(); root.withdraw()
            root.attributes("-topmost", True)
            path = filedialog.askdirectory()
            root.destroy()
            return path or ""
        except Exception:
            return ""

    def browse_rsr(self) -> str:
        try:
            import tkinter as tk
            from tkinter import filedialog
            root = tk.Tk(); root.withdraw()
            root.attributes("-topmost", True)
            path = filedialog.askopenfilename(
                filetypes=[("RSR files", "*.rsr"), ("All files", "*.*")])
            root.destroy()
            return path or ""
        except Exception:
            return ""

    def stop(self) -> dict:
        self._stop.set()
        self._kill_procs()
        self._log("Stop requested — finishing the current step…", "warn")
        return {"ok": True}

    def skip(self) -> dict:
        """Abandon the release being captured RIGHT NOW and move to the next.

        A hard skip: it kills the rar.exe currently running, because the thing
        worth skipping is almost always a sweep grinding through 232 builds on
        a set that will not match, and 'skip after the current step' there
        means waiting out the whole step. The event is cleared as the next
        release starts, so a skip never leaks into the one after it."""
        if not self._running:
            return {"ok": False, "error": "nothing running"}
        self._skip.set()
        self._kill_procs()
        self._log("  ⏭ Skip requested — abandoning this release now.", "warn")
        return {"ok": True}

    def _kill_procs(self):
        """Kill whatever rar.exe is running for this job. Safe to call when
        nothing is: the set is empty and the loop does nothing."""
        with self._proc_lock:
            procs = list(self._procs)
        for p in procs:
            try:
                p.kill()
            except Exception:
                pass

    def _run(self, cmd: list, timeout: int) -> bool:
        """Run a pack/extract command so that stop and skip can interrupt it.

        subprocess.run() is unkillable from another thread, so a skip pressed
        during a 900 s sweep step did nothing until that step finished. This
        polls instead, and terminates the child the moment either event is set.
        Returns True only if the command ran to completion on its own."""
        try:
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE)
        except Exception:
            return False
        with self._proc_lock:
            self._procs.add(p)
        try:
            deadline = time.monotonic() + timeout
            while True:
                try:
                    p.wait(timeout=0.25)
                    return True
                except subprocess.TimeoutExpired:
                    pass
                if (self._stop.is_set() or self._skip.is_set()
                        or time.monotonic() > deadline):
                    try:
                        p.kill()
                        p.wait(timeout=10)
                    except Exception:
                        pass
                    return False
        finally:
            with self._proc_lock:
                self._procs.discard(p)

    # ── settings ──────────────────────────────────────────────────────────

    @property
    def _config_path(self) -> Path:
        return self._app_dir / CONFIG_NAME

    def get_settings(self) -> dict:
        try:
            cfg = json.loads(self._config_path.read_text("utf-8"))
        except Exception:
            cfg = {}
        return {
            "source": cfg.get("source", ""),
            "store": cfg.get("store", str(self._app_dir / "rsr_store")),
            "max_mt": _num(cfg.get("max_mt"), 16, int),
            "embed_extras": bool(cfg.get("embed_extras", True)),
            "embed_max_mb": _num(cfg.get("embed_max_mb"), 16, int),
            "write_srr": bool(cfg.get("write_srr", True)),
            "skip_done": bool(cfg.get("skip_done", True)),
            "dict_ladder": bool(cfg.get("dict_ladder", True)),
        }

    def save_settings(self, s: dict) -> dict:
        cur = self.get_settings()
        s = s or {}
        out = {
            "source": (s.get("source") or cur["source"]).strip(),
            "store": (s.get("store") or cur["store"]).strip(),
            "max_mt": max(0, min(32, _num(s.get("max_mt"), cur["max_mt"], int))),
            "embed_extras": bool(s.get("embed_extras", cur["embed_extras"])),
            "embed_max_mb": max(0, min(4096, _num(s.get("embed_max_mb"),
                                                  cur["embed_max_mb"], int))),
            "write_srr": bool(s.get("write_srr", cur["write_srr"])),
            "skip_done": bool(s.get("skip_done", cur["skip_done"])),
            "dict_ladder": bool(s.get("dict_ladder", cur["dict_ladder"])),
        }
        try:
            self._config_path.write_text(json.dumps(out, indent=2), "utf-8")
        except Exception as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, "settings": out}

    # ── the WinRAR build pack ─────────────────────────────────────────────

    def _pack_exes(self) -> list[Path]:
        pack = self._app_dir / "apps" / "winrar_pack-4.20"
        return sorted(pack.glob("*_rar*.exe")) if pack.is_dir() else []

    # ══════════════════════════════════════════════════════════════════
    #  SCAN — capture
    # ══════════════════════════════════════════════════════════════════

    def scan_start(self, cfg: dict) -> dict:
        if self._running:
            return {"ok": False, "error": "Already running"}
        self.save_settings(cfg or {})
        s = self.get_settings()
        src = Path(s["source"])
        if not src.is_dir():
            return {"ok": False, "error": "Source folder not found"}

        def _bg():
            self._running = True
            self._stop.clear()
            self._skip.clear()
            try:
                self._scan_run(src, Path(s["store"]), s)
            except Exception as e:
                self._log(f"Scan error: {e}", "err")
                self._log(traceback.format_exc(), "dim")
            finally:
                self._running = False
                self._progress("")
                self._emit("scan_done", {})

        threading.Thread(target=_bg, daemon=True).start()
        return {"ok": True, "started": True}

    @staticmethod
    def _release_folders(src: Path) -> list[Path]:
        """A source may be one release folder, or a parent full of them.

        Archives sitting loose in `src` mean `src` IS the release; otherwise
        every subfolder is one."""
        if any(_classify_volume(p.name) for p in src.iterdir() if p.is_file()):
            return [src]
        subs = [p for p in sorted(src.iterdir()) if p.is_dir()]
        return subs or [src]

    def _scan_run(self, src: Path, store: Path, s: dict):
        exes = self._pack_exes()
        if not exes:
            self._log("WinRAR pack not found (apps/winrar_pack-4.20/*.exe) — "
                      "capture needs the build pack.", "err")
            return
        try:
            import rarfile  # noqa: F401
        except ImportError as e:
            self._log(f"Missing dependency: {e} (need rarfile).", "err")
            return

        store.mkdir(parents=True, exist_ok=True)
        folders = self._release_folders(src)
        self._log(f"{len(folders)} release folder(s) under {src}", "info")
        self._log(f"Build pack: {len(exes)} exe(s)   ·   store: {store}", "dim")

        done = ok = failed = skipped = 0
        for i, folder in enumerate(folders, 1):
            if self._stop.is_set():
                self._log("Stopped.", "warn")
                break
            rel = _release_name(folder)
            # Clear any skip from the PREVIOUS release here, not when the skip
            # fires — otherwise a skip pressed late in one release could still
            # be set as the next one starts and silently skip that too.
            self._skip.clear()
            self._emit("row", {"name": rel, "status": "running"})
            self._log("", "")
            self._log(f"══ [{i}/{len(folders)}] {rel} ══", "info")
            if s["skip_done"] and (store / rel / f"{rel}.rsr").is_file():
                self._log("  Already captured — skipping "
                          "(untick 'Skip captured' to redo).", "dim")
                self._emit("row", {"name": rel, "status": "skipped"})
                skipped += 1
                continue
            try:
                res = self._capture_release(folder, store, s, exes)
            except Exception as e:
                self._log(f"  ERROR: {e}", "err")
                self._log(traceback.format_exc(), "dim")
                res = {"ok": False, "error": str(e)}
            if self._skip.is_set():
                # Skipped by hand: not a failure, and nothing partial is left
                # in the store — _capture_release only writes a .rsr after its
                # own rebuild has verified.
                self._log("  ⏭ Skipped by request — nothing written.", "warn")
                self._emit("row", {"name": rel, "status": "skipped"})
                skipped += 1
                continue
            done += 1
            if res.get("ok"):
                ok += 1
                self._emit("row", {"name": rel, "status": "done",
                                   "recipe": res.get("recipe", "")})
            else:
                failed += 1
                self._emit("row", {"name": rel, "status": "error",
                                   "recipe": res.get("error", "")})

        self._log("", "")
        self._log(f"Capture complete — {ok} verified, {failed} failed, "
                  f"{skipped} skipped.", "ok" if failed == 0 else "warn")

    # ── one release ───────────────────────────────────────────────────────

    def _capture_release(self, folder: Path, store: Path, s: dict,
                         exes: list[Path]) -> dict:
        rel = _release_name(folder)
        sets = group_archive_sets(folder)
        if not sets:
            self._log("  No archive set found in this folder.", "warn")
            return {"ok": False, "error": "no archive"}

        work = Path(tempfile.mkdtemp(prefix="rsr-"))
        manifest = {
            "rsr_version": RSR_VERSION,
            "magic": RSR_MAGIC,
            "created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "tool": f"tosort_toolkit rsr_tool {RSR_VERSION}",
            "host": {"platform": platform.platform(),
                     "python": platform.python_version()},
            "release": rel,
            "source_folder": str(folder),
            "sets": [],
        }
        embedded: dict[str, bytes] = {}      # path inside the .rsr → bytes
        all_ok = True
        try:
            for si, st in enumerate(sets):
                if self._stop.is_set() or self._skip.is_set():
                    return {"ok": False,
                            "error": "skipped" if self._skip.is_set()
                            else "stopped"}
                res = self._capture_set(st, si, folder, work / f"set{si}",
                                        s, exes, embedded)
                if not res.get("ok"):
                    all_ok = False
                    self._log(f"  ✗ {st['stem']}: {res.get('error')}", "err")
                manifest["sets"].append(res.get("set", {"stem": st["stem"],
                                                        "error": res.get("error")}))

            if not manifest["sets"]:
                return {"ok": False, "error": "nothing captured"}

            # A .rsr is only ever written when EVERY set captured and verified.
            # Writing a partial one is worse than writing none: it cannot
            # rebuild the set that failed, and `skip_done` would then pass over
            # the release on every future run, so the failure becomes permanent
            # and invisible. (Caught by the skip test — an abandoned release
            # still produced an 8 MB "PARTIAL" .rsr, because the failed set
            # recorded no content classification and the loose content then
            # looked like a sidecar.)
            verified = all(x.get("verify") in ("exact", "delta")
                           for x in manifest["sets"] if "recipe" in x)
            if not (all_ok and verified):
                self._log("  ✗ Not every set captured and verified — writing "
                          "NO .rsr, so a re-run still sees this release.", "err")
                return {"ok": False, "error": "one or more sets unverified"}

            # Sidecars: everything loose in the release folder that is not a
            # volume — .nfo, .sfv, proof jpg, file_id.diz, Proof/ and Sample/
            # subfolders. They are part of the release but appear in no
            # archive, so nothing above ever looked at them and a rebuild
            # produced a folder missing its own nfo. Kilobytes; carry them.
            manifest["sidecars"] = self._capture_sidecars(folder, s, embedded,
                                                          manifest)

            # Optional legacy .srr, embedded verbatim so a .rsr can always emit
            # one for the existing ecosystem without us re-deriving structure.
            if s["write_srr"]:
                srr = self._make_srr(folder, sets, work)
                if srr:
                    embedded["release.srr"] = srr
                    manifest["srr"] = "release.srr"

            out_dir = store / rel
            out_dir.mkdir(parents=True, exist_ok=True)
            rsr_path = out_dir / f"{rel}.rsr"
            self._write_rsr(rsr_path, manifest, embedded)

            # Extras and sidecars are ALSO written loose beside the .rsr —
            # point 5/6: a human should be able to open the release folder in
            # the store and just look at the proof.
            for name, data in embedded.items():
                if not name.split("/")[0] in ("extras", "sidecars"):
                    continue
                dst = out_dir / name
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes(data)
            if "release.srr" in embedded:
                (out_dir / f"{rel}.srr").write_bytes(embedded["release.srr"])

            self._db_record(manifest, rsr_path)
            recipe = next((x["recipe"] for x in manifest["sets"]
                           if x.get("recipe")), {})
            self._log(f"  ✓ {rsr_path.name} written "
                      f"({rsr_path.stat().st_size:,} B) — VERIFIED", "ok")
            return {"ok": True,
                    "recipe": f"{recipe.get('version', '?')} -mt{recipe.get('mt', '?')}",
                    "error": ""}
        finally:
            _rmtree(work)

    # ── release-folder sidecars ───────────────────────────────────────────

    def _capture_sidecars(self, folder: Path, s: dict, embedded: dict,
                          manifest: dict) -> list[dict]:
        """Loose files in the release folder that belong to no archive.

        The .nfo and .sfv are the obvious ones, but this also picks up a proof
        jpg sitting beside the rars and whole Proof/ and Sample/ subfolders.
        None of them appear in any packed-file list, so the per-set capture is
        blind to them and a rebuilt folder came out without its own nfo.

        The embed cap applies here too: a big loose file is content or a stray
        unpacked copy, and either way is not a sidecar worth carrying."""
        cap = max(0, int(s.get("embed_max_mb", 16))) * 1024 * 1024
        # A proof jpg that is BOTH packed and loose is the normal scene shape,
        # and it was already embedded as an extra. Point the sidecar at those
        # same bytes rather than storing an identical second copy — that alone
        # was 827 KB of the 1.66 MB Monster_High .rsr.
        by_hash = {_sha256(v): k for k, v in embedded.items()}
        # A loose copy of the CONTENT is not a sidecar. It is under the cap
        # whenever the content is (an 8 MB rom unpacked beside its own rars),
        # and embedding it would put back the very bytes the content rule just
        # took out. Identify it by the (size, crc32) the set capture recorded.
        content_ids = {(f.get("size"), f.get("crc32"))
                       for st in manifest.get("sets", [])
                       for f in st.get("files", [])
                       if f.get("source") == "content"}
        out: list[dict] = []
        for p in sorted(folder.rglob("*")):
            if not p.is_file() or _classify_volume(p.name):
                continue
            rel = p.relative_to(folder).as_posix()
            size = p.stat().st_size
            if (size, _file_crc32(p)) in content_ids:
                self._log(f"    (skipping loose {rel} — it is the set's "
                          "content, supplied at rebuild)", "dim")
                continue
            if cap and size >= cap:
                self._log(f"    (skipping loose {rel} — {size:,} B is over the "
                          "embed cap, treated as content)", "dim")
                continue
            data = p.read_bytes()
            sha = _sha256(data)
            rec = {"name": rel, "size": size, "crc32": _file_crc32(p),
                   "sha256": sha, "mtime_ns": p.stat().st_mtime_ns,
                   "win_attrs": _win_attrs(p)}
            if s.get("embed_extras", True):
                key = by_hash.get(sha)
                if key is None:
                    key = f"sidecars/{rel}"
                    embedded[key] = data
                    by_hash[sha] = key
                rec["stored"] = key
            out.append(rec)
        if out:
            self._log(f"  {len(out)} sidecar(s) captured: "
                      + ", ".join(x["name"] for x in out[:6])
                      + (" …" if len(out) > 6 else ""), "ok")
        return out

    # ── one archive set ───────────────────────────────────────────────────

    def _capture_set(self, st: dict, si: int, folder: Path, work: Path,
                     s: dict, exes: list[Path], embedded: dict) -> dict:
        import rarfile

        work.mkdir(parents=True, exist_ok=True)
        vols = st["volumes"]
        self._log(f"  {st['stem']}  ·  {st['format']}  ·  {len(vols)} volume(s)"
                  f"  ·  {st['scheme']} naming"
                  + ("  ·  BYTE-SPLIT" if st["byte_split"] else ""), "dim")

        newnum = new_numbering(vols[0])
        head = shadow_set(vols, work / "shadow", st["byte_split"], newnum)
        rf = rarfile.RarFile(str(head))
        try:
            infos = [i for i in rf.infolist() if i.is_file()]
            comment = rf.comment
        finally:
            rf.close()
        if not infos:
            return {"ok": False, "error": "no packed files"}

        meta = self._read_files(infos, st["format"])
        solid = any(f["solid"] for f in meta)
        comp = next((f for f in meta if f["method"] != 0), meta[0])
        level = comp["method"]
        dict_kb = comp["dict_kb"]

        self._log(f"    -m{level}  -md{dict_kb}KB  "
                  f"{'-s (SOLID)' if solid else '-s-'}  ·  {len(meta)} file(s)"
                  + ("  ·  archive comment present" if comment else ""), "dim")
        for f in meta[:12]:
            self._log(f"      {f['name']}  {f['size']:,} → {f['packed_size']:,} B  "
                      f"CRC={f['crc32']:08X}  [-m{f['method']}]", "dim")
        if len(meta) > 12:
            self._log(f"      … and {len(meta) - 12} more", "dim")

        # Target streams: the exact compressed bytes of every packed file,
        # read header-independently so a header difference can never be
        # mistaken for a compression difference.
        try:
            blocks = packed_blocks(head)
        except Exception as e:
            return {"ok": False, "error": f"cannot read packed blocks: {e}"}
        targets = {f["name"]: stream_digest(blocks[f["name"]])
                   for f in meta if f["name"] in blocks}
        missing = [f["name"] for f in meta if f["name"] not in blocks]
        if missing:
            self._log(f"    ⚠ no packed data located for: "
                      f"{', '.join(missing[:4])}", "warn")
        if not targets:
            return {"ok": False, "error": "no readable streams"}

        # Extract the real sources with a pack rar.exe (no unrar backend
        # needed), into our work dir — the scanned folder is never touched.
        srcdir = work / "src"
        srcdir.mkdir(exist_ok=True)
        ok_x = self._run([str(exes[-1]), "x", "-y", "-o+", str(head),
                          str(srcdir) + os.sep], timeout=3600)
        if self._skip.is_set() or self._stop.is_set():
            return {"ok": False, "error": "skipped"}
        order = [f["name"] for f in meta]
        src_files = [srcdir / n for n in order]
        if not all(p.is_file() for p in src_files):
            return {"ok": False,
                    "error": "extraction incomplete"
                             + ("" if ok_x else " (extract did not finish)")}

        # The timestamp rar.exe just restored is the one that will be written
        # back into the header, at full 100 ns resolution. Record THAT rather
        # than the header's own 2-second DOS field: it is the exact input the
        # verification below is about to prove correct, so the rebuild can
        # recreate the same conditions instead of approximating them.
        for f, sp in zip(meta, src_files):
            stt = sp.stat()
            f["mtime_ns"] = stt.st_mtime_ns
            f["win_attrs"] = _win_attrs(sp)

        # Which files must travel INSIDE the .rsr?
        #
        # There are exactly two kinds of packed file, and the split is by ROLE,
        # not by where a copy happens to sit today:
        #
        #   CONTENT — the thing the release exists to carry. The operator has
        #     it in their unpacked set and hands it to the rebuild, so its bytes
        #     must NEVER be embedded. The original test ("anything not loose in
        #     the release folder") got this exactly backwards: in a real scene
        #     folder (rars + nfo + sfv + jpg) the content is the ONE file that
        #     is never loose, so it was embedded every time — a 67 MB .rsr for
        #     Monster_High…PUSSYCAT.
        #
        #   EXTRA — proof jpg, file_id.diz, sample stub. These travel inside,
        #     UNCONDITIONALLY. Being loose in the source folder today is not a
        #     reason to leave one out: at rebuild the operator supplies the
        #     content folder, which holds the game and nothing else, so a
        #     skipped loose jpg is a source that exists nowhere (exactly how
        #     Monster_High failed to rebuild once the content fix went in).
        #     The .rsr must be self-sufficient bar the content.
        #
        # Content is identified two ways, because a size cap alone cannot do it
        # — small NDS roms are 8 MB, under any cap generous enough to hold a
        # proof jpg:
        #   * the LARGEST file in the set is content by definition, and
        #   * anything at or over the embed cap is content regardless of rank.
        # What remains is always smaller than what it is proof OF, so embedding
        # it costs kilobytes.
        cap = max(0, int(s.get("embed_max_mb", 16))) * 1024 * 1024
        biggest = max((f["size"] or 0) for f in meta) if meta else 0
        extras = []
        content_only = []
        for f, sp in zip(meta, src_files):
            loose = folder / Path(f["name"]).name
            f["loose"] = (loose.is_file() and loose.stat().st_size == f["size"]
                          and _file_crc32(loose) == f["crc32"])
            if (f["size"] or 0) >= biggest or (cap and (f["size"] or 0) >= cap):
                # Content: recorded in the manifest so the rebuild demands it,
                # but its bytes stay out of the .rsr.
                f["source"] = "content"
                content_only.append(f["name"])
                continue
            f["source"] = "extra"
            data = sp.read_bytes()
            f["sha256"] = _sha256(data)
            base = Path(f["name"]).name
            key = f"extras/{base}"
            if key in embedded and embedded[key] != data:
                key = f"extras/{st['stem']}/{base}"     # two sets, same name
            if s["embed_extras"]:
                embedded[key] = data
                f["stored"] = key
            extras.append(f["name"])
        if extras:
            self._log(f"    {len(extras)} extra(s) captured: "
                      + ", ".join(Path(x).name for x in extras[:6])
                      + (" …" if len(extras) > 6 else ""), "ok")
            if not s["embed_extras"]:
                self._log("    ⚠ 'Embed extras' is OFF — these bytes exist "
                          "nowhere else, so this .rsr will NOT rebuild.", "warn")
        if content_only:
            self._log("    content (supplied at rebuild, not embedded): "
                      + ", ".join(Path(x).name for x in content_only[:4])
                      + (" …" if len(content_only) > 4 else ""), "dim")

        # ── find the recipe ───────────────────────────────────────────────
        cands = self._dict_candidates(st["format"], dict_kb, s["dict_ladder"])
        recipe = None
        for di, dkb in enumerate(cands):
            if self._stop.is_set() or self._skip.is_set():
                return {"ok": False,
                        "error": "skipped" if self._skip.is_set() else "stopped"}
            if di:
                self._log(f"    retrying with -md{dkb}KB "
                          f"(header dictionary didn't reproduce)", "dim")
            recipe = self._sweep_recipe(st["format"], exes, level, dkb, solid,
                                        src_files, targets, work, s["max_mt"])
            if recipe:
                break
        if not recipe:
            self._log("    ✗ no build × -mt reproduces these streams — the "
                      "exact build is outside the pack.", "err")
            return {"ok": False, "error": "recipe not found",
                    "set": {"stem": st["stem"], "format": st["format"],
                            "files": meta, "wall": True}}

        self._log(f"    ✓ RECIPE: {recipe['version']} -mt{recipe['mt']} "
                  f"(-m{level} -md{recipe['dict_kb']}KB "
                  f"{'-s' if solid else '-s-'}) — all {len(targets)} stream(s) "
                  "byte-exact.", "ok")

        # ── replay the whole set and byte-compare every volume ────────────
        # A byte-split set is ONE archive chopped up afterwards, so the replay
        # must not pass -v at all — the chunk boundaries are a property of the
        # splitter, not of RAR, and are restored from the volume records.
        vol_bytes = 0 if st["byte_split"] or len(vols) == 1 \
            else vols[0].stat().st_size
        recipe.update({"level": level, "solid": solid,
                       "volume_bytes": vol_bytes, "naming": st["scheme"],
                       "new_numbering": newnum,
                       "byte_split": st["byte_split"],
                       "comment": bool(comment)})
        verify, volmeta, deltas = self._verify_replay(
            recipe, src_files, vols, work, comment, st, si)
        embedded.update(deltas)
        recipe["verify"] = verify

        if verify == "exact":
            self._log(f"    ✓ REPLAY EXACT — all {len(vols)} volume(s) "
                      "byte-identical to the originals.", "ok")
        elif verify == "delta":
            n = len(deltas)
            self._log(f"    ✓ REPLAY + DELTA — {n} volume(s) needed a header "
                      f"patch ({sum(len(v) for v in deltas.values()):,} B "
                      "total); reconstruction is still byte-exact.", "ok")
        else:
            self._log("    ⚠ replay did NOT reproduce the volumes and the "
                      "residual is too large to store — captured as "
                      "UNVERIFIED.", "warn")

        return {
            "ok": verify in ("exact", "delta"),
            "error": "" if verify != "none" else "replay unverified",
            "set": {
                "stem": st["stem"],
                "format": st["format"],
                "scheme": st["scheme"],
                "byte_split": st["byte_split"],
                "solid": solid,
                "comment_b64": base64.b64encode(comment.encode("utf-8", "replace")
                                                ).decode() if comment else None,
                "recipe": recipe,
                "verify": verify,
                "order": order,
                "files": meta,
                "volumes": volmeta,
            },
        }

    # ── header decode ─────────────────────────────────────────────────────

    _RAR4_DICT_BITS = {0: 64, 1: 128, 2: 256, 3: 512,
                       4: 1024, 5: 2048, 6: 4096}

    def _read_files(self, infos, fmt: str) -> list[dict]:
        """Per-file record. Deliberately captures more than v1 needs — host OS,
        attributes, timestamps, extract version — because a field we didn't
        keep is a rescan of the whole corpus later."""
        out = []
        for idx, i in enumerate(infos):
            flags = i.flags or 0
            method = (i.compress_type or 0x30) - 0x30
            if fmt == "RAR4":
                dkb = self._RAR4_DICT_BITS.get((flags >> 5) & 7, 4096)
            else:
                # RAR5 compression info: bits 7-9 method, bits 10-13 the
                # minimum dictionary as 128 KB << n.
                cf = getattr(i, "file_compress_flags", None)
                dkb = (128 << ((cf >> 10) & 0xF)) if cf is not None else 32768
            solid = bool(flags & 0x10)     # rarfile normalises RAR5 onto this
            mtime = None
            if getattr(i, "mtime", None):
                try:
                    mtime = i.mtime.isoformat()
                except Exception:
                    mtime = None
            out.append({
                "order": idx,
                "name": i.filename,
                "size": int(i.file_size or 0),
                "packed_size": int(i.compress_size or 0),
                "crc32": int(i.CRC or 0) & 0xFFFFFFFF,
                "sha256": None,
                "method": method,
                "dict_kb": dkb,
                "solid": solid,
                "host_os": HOST_OS.get(i.host_os, str(i.host_os)),
                "attrs": int(i.mode or 0),
                "mtime": mtime,
                "date_time": list(i.date_time) if i.date_time else None,
                "extract_version": i.extract_version,
                "flags": flags,
                "source": "folder",
            })
        return out

    def _dict_candidates(self, fmt: str, primary: int, ladder: bool) -> list[int]:
        if not ladder:
            return [primary]
        pool = RAR5_DICTS if fmt == "RAR5" else RAR4_DICTS
        return [primary] + [d for d in pool if d != primary]

    # ── the sweep ─────────────────────────────────────────────────────────

    def _pack_args(self, ex: Path, fmt: str, level: int, dict_kb: int,
                   mt: int) -> list[str] | None:
        """Command prefix for this (build, format, dict, thread count), or None
        when the build cannot produce this archive at all."""
        is_r5 = bool(_R5_EXE.search(ex.name))
        if fmt == "RAR5":
            if not is_r5:
                return None                      # RAR4-era build can't write RAR5
            return [str(ex), "a", f"-m{level}", "-ma5", f"-md{dict_kb}k"]
        if is_r5:
            # A RAR5 binary still emits RAR4 with -ma4 — and it is the only
            # way a RAR4 archive can carry -mt above 16.
            return [str(ex), "a", f"-m{level}", "-ma4", f"-md{dict_kb}k"]
        if mt > RAR4_MT_CAP:
            return None
        letter = DICT_LETTER.get(dict_kb)
        return [str(ex), "a", f"-m{level}",
                f"-md{letter}" if letter else f"-md{dict_kb}"]

    def _sweep_recipe(self, fmt, exes, level, dict_kb, solid, src_files,
                      targets, work, max_mt) -> dict | None:
        """Pack every file TOGETHER at each (build, -mt) and keep the combo
        whose streams match byte for byte.

        All files in one command, always: a set packed by a single `rar a`
        compresses each stream in context of the others, so hunting them in
        isolation — the thing rescene is forced to do — cannot reproduce them
        at any build or thread count. No family dedup either; fingerprinting
        one small file to skip builds is what manufactured false walls before,
        because a small file cannot discriminate builds that differ only on
        larger input."""
        mts = [n for n in (*MT_ORDER, *range(9, max_mt + 1)) if n <= max_mt]
        # RAR4 archives are most likely from RAR4-era builds — try those first,
        # but never DROP the RAR5 binaries (they reach -mt>16 via -ma4).
        if fmt == "RAR4":
            r4 = [e for e in exes if _R4_EXE.match(e.name)]
            if r4 and len(r4) < len(exes):
                exes = r4 + [e for e in exes if e not in set(r4)]
        else:
            exes = [e for e in exes if _R5_EXE.search(e.name)]
            if not exes:
                self._log("    no RAR5-capable build in the pack.", "err")
                return None

        probe = work / "probe.rar"
        srcs = [str(p) for p in src_files]
        tried = 0
        for mi, n in enumerate(mts, 1):
            if self._stop.is_set() or self._skip.is_set():
                return None
            for ex in exes:
                if self._stop.is_set() or self._skip.is_set():
                    return None
                pre = self._pack_args(ex, fmt, level, dict_kb, n)
                if pre is None:
                    continue
                tried += 1
                if tried % 8 == 0:
                    self._progress(f"sweep -mt{n} ({mi}/{len(mts)}) · "
                                   f"{_exe_label(ex.name)} · {tried} combos")
                for junk in work.glob("probe.*"):
                    try:
                        junk.unlink()
                    except OSError:
                        pass
                cmd = pre + ["-s" if solid else "-s-", "-ds", f"-mt{n}",
                             "-o+", "-ep", "-idcd", str(probe), *srcs]
                if not self._run(cmd, timeout=900):
                    continue
                if not probe.is_file():
                    continue
                if self._streams_match(probe, targets):
                    return {"exe": ex.name, "version": _exe_label(ex.name),
                            "mt": n, "dict_kb": dict_kb, "tried": tried}
        self._log(f"    swept {tried} combo(s) across {len(exes)} build(s) "
                  f"× -mt 0–{max_mt}.", "dim")
        return None

    @staticmethod
    def _streams_match(probe: Path, targets: dict) -> bool:
        try:
            blocks = packed_blocks(probe)
        except Exception:
            return False
        for name, target in targets.items():
            if name not in blocks or stream_digest(blocks[name]) != target:
                return False
        return True

    # ── replay + verify ───────────────────────────────────────────────────

    def _replay_cmd(self, recipe: dict, target: Path, srcs: list[str],
                    comment_file: Path | None, fmt: str) -> list[str] | None:
        ex = self._app_dir / "apps" / "winrar_pack-4.20" / recipe["exe"]
        if not ex.is_file():
            return None
        pre = self._pack_args(ex, fmt, recipe["level"], recipe["dict_kb"],
                              recipe["mt"])
        if pre is None:
            return None
        cmd = pre + ["-s" if recipe["solid"] else "-s-", "-ds",
                     f"-mt{recipe['mt']}", "-o+", "-ep", "-idcd", "-y"]
        if recipe.get("volume_bytes"):
            cmd.append(f"-v{recipe['volume_bytes']}b")
            if not recipe.get("new_numbering"):
                cmd.append("-vn")          # .rar/.r00 rather than .partN.rar
        if comment_file:
            cmd.append(f"-z{comment_file}")
        return cmd + [str(target), *srcs]

    def _replay(self, recipe: dict, src_files, work: Path, comment,
                fmt: str) -> list[Path] | None:
        """Run the original command again and return the volumes it produced,
        in order."""
        out = work / "replay"
        if out.exists():
            _rmtree(out)
        out.mkdir(parents=True)
        cfile = None
        if comment:
            cfile = work / "comment.txt"
            cfile.write_text(comment, encoding="utf-8", errors="replace")
        cmd = self._replay_cmd(recipe, out / "replay.rar",
                               [str(p) for p in src_files], cfile, fmt)
        if cmd is None:
            return None
        if not self._run(cmd, timeout=3600):
            return None
        made = sorted(p for p in out.iterdir() if p.is_file())
        if not made:
            return None
        ordered = [p for p in made if _classify_volume(p.name)]
        ordered.sort(key=lambda p: _classify_volume(p.name)[2])
        return ordered or made

    def _verify_replay(self, recipe, src_files, vols, work, comment, st, si=0):
        """The whole point of capture-time: don't claim the recipe works, run
        it and compare. Returns ('exact'|'delta'|'none', volume records,
        {path-in-rsr: patch bytes})."""
        produced = self._replay(recipe, src_files, work, comment, st["format"])
        volmeta, deltas = [], {}
        if not produced:
            for v in vols:
                volmeta.append({"name": v.name, "size": v.stat().st_size,
                                "sha256": _file_sha256(v), "delta": None})
            return "none", volmeta, {}

        if st["byte_split"]:
            # The originals are slices of one archive; compare the join.
            joined = b"".join(v.read_bytes() for v in vols)
            got = b"".join(p.read_bytes() for p in produced)
            same = got == joined
            patch = None if same else diff_bytes(got, joined)
            for v in vols:
                raw = v.read_bytes()
                volmeta.append({"name": v.name, "size": len(raw),
                                "sha256": _sha256(raw),
                                "head_sha": _sha256(raw[:4096]), "delta": None})
            if same:
                return "exact", volmeta, {}
            if patch is None:
                return "none", volmeta, {}
            key = f"deltas/{si}_joined.bin"
            deltas[key] = patch
            volmeta[0]["delta"] = key
            return "delta", volmeta, deltas

        if len(produced) != len(vols):
            self._log(f"    ⚠ replay made {len(produced)} volume(s), original "
                      f"has {len(vols)} — volume size not reproduced.", "warn")
            for v in vols:
                volmeta.append({"name": v.name, "size": v.stat().st_size,
                                "sha256": _file_sha256(v), "delta": None})
            return "none", volmeta, {}

        verdict = "exact"
        for idx, (p, v) in enumerate(zip(produced, vols)):
            orig = v.read_bytes()
            got = p.read_bytes()
            rec = {"name": v.name, "size": len(orig),
                   "sha256": _sha256(orig),
                   "head_sha": _sha256(orig[:4096]), "delta": None}
            if got != orig:
                patch = diff_bytes(got, orig)
                if patch is None:
                    volmeta.append(rec)
                    return "none", volmeta, {}
                key = f"deltas/{si}_{idx:04d}.bin"
                deltas[key] = patch
                rec["delta"] = key
                verdict = "delta"
            volmeta.append(rec)
        return verdict, volmeta, deltas

    # ── legacy .srr ───────────────────────────────────────────────────────

    def _make_srr(self, folder: Path, sets: list[dict], work: Path) -> bytes | None:
        """A v1 .srr embedded verbatim, so a .rsr can always emit one for the
        existing ecosystem without us re-deriving the block structure."""
        try:
            # A PRIVATE rescene: the srrdb rebuilder shares this process and
            # patches the shared module with its own stop flags, reconstruct
            # deadline and log subscriber. Importing normally meant our SRR
            # progress printed into ITS window, and an srrdb Stop could abort
            # a capture that has nothing to do with it. See rescene_guard.
            from rescene_guard import load_private
            rm = load_private()
        except ImportError:
            return None
        heads = []
        for st in sets:
            if st["byte_split"] or st["scheme"] == "numeric":
                continue          # srr.exe only understands the standard names
            heads.append(str(st["volumes"][0]))
        if not heads:
            return None
        stored = [str(p) for p in folder.iterdir()
                  if p.is_file() and p.suffix.lower() in
                  (".nfo", ".sfv", ".diz", ".txt")]
        out = work / "legacy.srr"
        try:
            rm.create_srr(str(out), heads, in_folder=str(folder),
                          store_files=stored, save_paths=False,
                          compressed=True)   # scene sets are compressed; without
                                             # this rescene refuses outright
        except Exception as e:
            self._log(f"    (no legacy .srr: {e})", "dim")
            return None
        return out.read_bytes() if out.is_file() else None

    # ── container ─────────────────────────────────────────────────────────

    def _write_rsr(self, path: Path, manifest: dict, embedded: dict):
        """A .rsr is a ZIP with our magic in the archive comment.

        Any tool on the planet can open it and look, which matters for a format
        nobody else has a parser for yet — and the versioned manifest means we
        can move to a bespoke container later without stranding what we write
        today."""
        tmp = path.with_suffix(".rsr.tmp")
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
            z.comment = RSR_MAGIC.encode()
            z.writestr("manifest.json", json.dumps(manifest, indent=1))
            for name, data in embedded.items():
                # Extras are already-compressed payloads far more often than
                # not; storing them keeps the container honest about its size.
                ct = (zipfile.ZIP_STORED if name.startswith("extras/")
                      else zipfile.ZIP_DEFLATED)
                z.writestr(name, data, compress_type=ct)
        if path.exists():
            path.unlink()
        tmp.rename(path)

    @staticmethod
    def read_rsr(path: Path) -> tuple[dict, zipfile.ZipFile]:
        z = zipfile.ZipFile(str(path))
        manifest = json.loads(z.read("manifest.json"))
        return manifest, z

    # ══════════════════════════════════════════════════════════════════
    #  REBUILD
    # ══════════════════════════════════════════════════════════════════

    def rebuild_start(self, cfg: dict) -> dict:
        if self._running:
            return {"ok": False, "error": "Already running"}
        rsr = Path((cfg or {}).get("rsr", "").strip())
        content = Path((cfg or {}).get("content", "").strip())
        out = Path((cfg or {}).get("out", "").strip())
        if not rsr.is_file():
            return {"ok": False, "error": ".rsr file not found"}
        if not content.is_dir():
            return {"ok": False, "error": "Content folder not found"}
        if not str(out):
            return {"ok": False, "error": "Output folder required"}

        def _bg():
            self._running = True
            self._stop.clear()
            try:
                self._rebuild_run(rsr, content, out)
            except Exception as e:
                self._log(f"Rebuild error: {e}", "err")
                self._log(traceback.format_exc(), "dim")
            finally:
                self._running = False
                self._progress("")
                self._emit("scan_done", {})

        threading.Thread(target=_bg, daemon=True).start()
        return {"ok": True, "started": True}

    def _rebuild_run(self, rsr: Path, content: Path, out: Path) -> dict:
        manifest, z = self.read_rsr(rsr)
        try:
            rel = manifest.get("release", rsr.stem)
            self._log(f"══ REBUILD {rel} ══", "info")
            self._log(f"  captured {manifest.get('created_utc')} by "
                      f"{manifest.get('tool')}", "dim")
            out.mkdir(parents=True, exist_ok=True)
            work = Path(tempfile.mkdtemp(prefix="rsr-rb-"))
            ok_all = True
            try:
                for st in manifest.get("sets", []):
                    if self._stop.is_set():
                        self._log("Stopped.", "warn")
                        break
                    if not st.get("recipe"):
                        self._log(f"  {st.get('stem')}: no recipe captured — "
                                  "cannot rebuild this set.", "err")
                        ok_all = False
                        continue
                    ok_all &= self._rebuild_set(st, manifest, z, content, out, work)
                self._restore_sidecars(manifest, z, out)
            finally:
                _rmtree(work)
            self._log("", "")
            self._log("✓ Rebuild complete — every volume verified."
                      if ok_all else
                      "✗ Rebuild finished with errors — see above.",
                      "ok" if ok_all else "err")
            return {"ok": ok_all}
        finally:
            z.close()

    def _rebuild_set(self, st, manifest, z, content: Path, out: Path,
                     work: Path) -> bool:
        recipe = st["recipe"]
        stem = st["stem"]
        self._log(f"  {stem}: replaying {recipe['version']} -mt{recipe['mt']} "
                  f"(-m{recipe['level']} -md{recipe['dict_kb']}KB "
                  f"{'-s' if recipe['solid'] else '-s-'})", "info")

        # Gather sources: loose content from the user's folder, archive-only
        # extras straight out of the container.
        setwork = work / stem.replace(os.sep, "_")
        srcdir = setwork / "src"
        srcdir.mkdir(parents=True, exist_ok=True)
        srcs = []
        for f in sorted(st["files"], key=lambda x: x["order"]):
            base = Path(f["name"]).name
            dst = srcdir / base
            if f.get("stored"):
                dst.write_bytes(z.read(f["stored"]))
            else:
                found = content / base
                if not found.is_file():
                    hits = list(content.rglob(base))
                    found = hits[0] if hits else None
                if not found or not Path(found).is_file():
                    self._log(f"    ✗ missing source: {base}", "err")
                    return False
                shutil.copy2(found, dst)
            if f.get("crc32") is not None and _file_crc32(dst) != f["crc32"]:
                self._log(f"    ✗ {base}: CRC does not match what was "
                          "captured — wrong file.", "err")
                return False
            # Recreate the exact source state the capture verified against —
            # RAR writes the timestamp and attributes into the file header, so
            # a second of drift is a different archive.
            try:
                if f.get("mtime_ns"):
                    os.utime(dst, ns=(f["mtime_ns"], f["mtime_ns"]))
                elif f.get("mtime"):
                    ts = datetime.fromisoformat(f["mtime"]).timestamp()
                    os.utime(dst, (ts, ts))
            except Exception:
                pass
            _set_win_attrs(dst, f.get("win_attrs"))
            srcs.append(dst)

        comment = None
        if st.get("comment_b64"):
            comment = base64.b64decode(st["comment_b64"]).decode("utf-8", "replace")
        produced = self._replay(recipe, srcs, setwork, comment, st["format"])
        if not produced:
            self._log("    ✗ replay produced nothing.", "err")
            return False

        vols = st["volumes"]
        if st.get("byte_split"):
            data = b"".join(p.read_bytes() for p in produced)
            d0 = vols[0].get("delta")
            if d0:
                data = apply_delta(data, z.read(d0))
            pos = 0
            for v in vols:
                chunk = data[pos:pos + v["size"]]
                pos += v["size"]
                if _sha256(chunk) != v["sha256"]:
                    self._log(f"    ✗ {v['name']}: hash mismatch.", "err")
                    return False
                (out / v["name"]).write_bytes(chunk)
                self._log(f"    ✓ {v['name']}  {v['size']:,} B", "dim")
            self._restore_extras(st, z, out)
            return True

        if len(produced) != len(vols):
            self._log(f"    ✗ replay made {len(produced)} volume(s), expected "
                      f"{len(vols)}.", "err")
            return False
        for p, v in zip(produced, vols):
            data = p.read_bytes()
            if v.get("delta"):
                data = apply_delta(data, z.read(v["delta"]))
            if _sha256(data) != v["sha256"]:
                self._log(f"    ✗ {v['name']}: hash mismatch after replay "
                          f"({self._mismatch_hint(data, v)}).", "err")
                return False
            (out / v["name"]).write_bytes(data)
        self._log(f"    ✓ {len(vols)} volume(s) rebuilt, every one hash-exact.",
                  "ok")

        self._restore_extras(st, z, out)
        return True

    def _restore_sidecars(self, manifest, z, out: Path):
        """Put the .nfo / .sfv / proof back beside the rebuilt volumes, with the
        timestamps and attributes they were captured with — a release folder
        without its nfo is not the release."""
        n = 0
        for f in manifest.get("sidecars", []):
            if not f.get("stored"):
                continue
            dst = out / f["name"]
            dst.parent.mkdir(parents=True, exist_ok=True)
            data = z.read(f["stored"])
            if f.get("sha256") and _sha256(data) != f["sha256"]:
                self._log(f"    ✗ sidecar {f['name']}: stored bytes do not "
                          "match the captured hash.", "err")
                continue
            dst.write_bytes(data)
            try:
                if f.get("mtime_ns"):
                    os.utime(dst, ns=(f["mtime_ns"], f["mtime_ns"]))
            except Exception:
                pass
            _set_win_attrs(dst, f.get("win_attrs"))
            n += 1
        if n:
            self._log(f"    ✓ {n} sidecar(s) restored.", "ok")

    @staticmethod
    def _restore_extras(st, z, out: Path):
        """Archive-only files belong in the rebuilt release folder too, not
        only inside the archive we just made."""
        for f in st["files"]:
            if not f.get("stored"):
                continue
            dst = out / Path(f["name"]).name
            if not dst.exists():
                dst.write_bytes(z.read(f["stored"]))

    @staticmethod
    def _mismatch_hint(data: bytes, vol: dict) -> str:
        """Say HOW a rebuilt volume differs — a handful of bytes in the first
        few hundred is a header field we failed to restore; a size change or a
        difference deep in the payload means the compressed stream itself is
        wrong, which is a different problem entirely."""
        if len(data) != vol.get("size"):
            return f"size {len(data):,} vs {vol.get('size'):,} — wrong recipe"
        if vol.get("head_sha") and _sha256(data[:4096]) != vol["head_sha"]:
            return "header block differs — timestamp/attribute not restored"
        return "header matches, compressed payload differs"

    # ══════════════════════════════════════════════════════════════════
    #  Index
    # ══════════════════════════════════════════════════════════════════

    @property
    def _db_path(self) -> Path:
        return self._app_dir / DB_NAME

    def _db(self) -> sqlite3.Connection:
        con = sqlite3.connect(str(self._db_path))
        con.execute("""CREATE TABLE IF NOT EXISTS releases(
            name TEXT PRIMARY KEY, rsr_path TEXT, created TEXT,
            format TEXT, sets INT, files INT, volumes INT,
            verified INT, verify TEXT, recipe_exe TEXT, recipe_version TEXT,
            mt INT, level INT, dict_kb INT, solid INT, extras INT,
            total_bytes INT)""")
        con.execute("""CREATE TABLE IF NOT EXISTS files(
            release TEXT, set_stem TEXT, name TEXT, size INT, packed_size INT,
            crc32 INT, sha256 TEXT, method INT, source TEXT)""")
        con.execute("CREATE INDEX IF NOT EXISTS ix_files_name ON files(name)")
        con.execute("CREATE INDEX IF NOT EXISTS ix_files_crc ON files(crc32)")
        con.execute("CREATE INDEX IF NOT EXISTS ix_files_rel ON files(release)")
        return con

    def _db_record(self, manifest: dict, rsr_path: Path):
        rel = manifest["release"]
        sets = manifest.get("sets", [])
        recipe = next((s["recipe"] for s in sets if s.get("recipe")), {})
        nfiles = sum(len(s.get("files", [])) for s in sets)
        nvols = sum(len(s.get("volumes", [])) for s in sets)
        nextra = sum(1 for s in sets for f in s.get("files", [])
                     if f.get("source") == "extra")
        verified = int(all(s.get("verify") in ("exact", "delta") for s in sets))
        con = self._db()
        try:
            con.execute("DELETE FROM releases WHERE name=?", (rel,))
            con.execute("DELETE FROM files WHERE release=?", (rel,))
            con.execute(
                "INSERT INTO releases VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (rel, str(rsr_path), manifest.get("created_utc"),
                 sets[0].get("format") if sets else "",
                 len(sets), nfiles, nvols, verified,
                 sets[0].get("verify") if sets else "",
                 recipe.get("exe", ""), recipe.get("version", ""),
                 recipe.get("mt", -1), recipe.get("level", -1),
                 recipe.get("dict_kb", 0), int(recipe.get("solid", False)),
                 nextra,
                 sum(v.get("size", 0) for s in sets for v in s.get("volumes", []))))
            for s in sets:
                for f in s.get("files", []):
                    con.execute("INSERT INTO files VALUES(?,?,?,?,?,?,?,?,?)",
                                (rel, s.get("stem"), f["name"], f["size"],
                                 f["packed_size"], f["crc32"], f.get("sha256"),
                                 f["method"], f.get("source")))
            con.commit()
        finally:
            con.close()

    def db_stats(self) -> dict:
        if not self._db_path.is_file():
            return {"ok": True, "releases": 0, "files": 0, "verified": 0}
        con = self._db()
        try:
            r = con.execute("SELECT COUNT(*), COALESCE(SUM(verified),0), "
                            "COALESCE(SUM(extras),0) FROM releases").fetchone()
            f = con.execute("SELECT COUNT(*) FROM files").fetchone()
            return {"ok": True, "releases": r[0], "verified": r[1],
                    "extras": r[2], "files": f[0]}
        finally:
            con.close()

    def db_search(self, query: str, limit: int = 200) -> dict:
        """Search by release name, packed file name, or CRC32 (hex or decimal)."""
        q = (query or "").strip()
        if not self._db_path.is_file():
            return {"ok": True, "rows": []}
        con = self._db()
        try:
            if not q:
                rows = con.execute(
                    "SELECT name, recipe_version, mt, verified, files, extras, "
                    "rsr_path FROM releases ORDER BY created DESC LIMIT ?",
                    (limit,)).fetchall()
            else:
                crc = None
                try:
                    crc = int(q, 16) if re.fullmatch(r"[0-9a-fA-F]{8}", q) else int(q)
                except ValueError:
                    pass
                like = f"%{q}%"
                sql = ("SELECT DISTINCT r.name, r.recipe_version, r.mt, "
                       "r.verified, r.files, r.extras, r.rsr_path "
                       "FROM releases r LEFT JOIN files f ON f.release=r.name "
                       "WHERE r.name LIKE ? OR f.name LIKE ?")
                args = [like, like]
                if crc is not None:
                    sql += " OR f.crc32=?"
                    args.append(crc)
                sql += " ORDER BY r.name LIMIT ?"
                args.append(limit)
                rows = con.execute(sql, args).fetchall()
            return {"ok": True, "rows": [
                {"name": r[0], "version": r[1], "mt": r[2], "verified": bool(r[3]),
                 "files": r[4], "extras": r[5], "path": r[6]} for r in rows]}
        finally:
            con.close()

    def open_store(self, path: str) -> dict:
        try:
            p = Path(path)
            target = p.parent if p.is_file() else p
            os.startfile(str(target))          # noqa: S606 (Windows only)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def inspect_rsr(self, path: str) -> dict:
        """Manifest summary for a .rsr, for the GUI's detail view."""
        try:
            manifest, z = self.read_rsr(Path(path))
            names = z.namelist()
            z.close()
            return {"ok": True, "manifest": manifest, "entries": names}
        except Exception as e:
            return {"ok": False, "error": str(e)}


def main():
    api = RsrToolAPI()
    window = webview.create_window(
        title="RSR Scanner — ToSort Toolkit",
        url=str(Path(__file__).parent / "gui" / "rsr_tool.html"),
        js_api=api,
        width=1000,
        height=800,
        min_size=(760, 560),
        background_color="#0d0f12",
    )
    api.set_window(window)
    webview.start(debug=False)


if __name__ == "__main__":
    main()

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
import struct
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

# Controls the log tells the operator to go and use. Quoted from here rather
# than typed into each message, because they had already drifted: the log said
# "tick 'Retry known walls'" for a checkbox actually labelled "Retry releases
# already swept to exhaustion", which sends someone hunting for a control that
# does not exist under that name. t_labels.py checks these against the HTML.
UI_SKIP_DONE   = "Skip releases already captured"
UI_RETRY_WALLS = "Retry releases already swept to exhaustion"
UI_FINISH_ONE  = "Finish this one"

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
# A bracketed status the archive puts BEFORE the date: "[NUKED] 2013-…".
_TAG_PREFIX   = re.compile(r"^\[([^\]]{1,24})\]\s*")


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


def _human_bytes(n: int) -> str:
    """Size in the unit a human would have used. 0.00 GB tells you nothing."""
    for unit, step in (("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)):
        if n >= step:
            return f"{n / step:,.2f} {unit}"
    return f"{n:,} B"


# Files a release can consist ENTIRELY of and still be a real release: the
# metadata-only fixes. Deliberately a whitelist — an unrecognised extension
# means "an archive kind we don't handle yet", which must stay a warning rather
# than be quietly filed as metadata.
_SIDECAR_EXT = {".nfo", ".sfv", ".diz", ".txt", ".jpg", ".jpeg", ".png", ".gif",
                ".m3u", ".srr", ".srs", ".md5", ".sha1", ".log", ".cue", ".par2"}

_FIX_TAGS = ("DIRFIX", "NFOFIX", "PROOFFIX", "SFVFIX", "SAMPLEFIX",
             "RARFIX", "SUBFIX", "SYNCFIX")


def _fix_tag(rel: str) -> str:
    """The scene FIX tag in a release name ('DIRFIX'), or ''.

    Tokens only, so a game called `Dirfixer` is not mistaken for one."""
    for t in re.split(r"[._\-]+", (rel or "").upper()):
        if t in _FIX_TAGS:
            return t
    return ""


def _exe_year(fname: str) -> int:
    """Release year of a build in the pack, from its date prefix (0 if none)."""
    m = re.match(r"(\d{4})-\d{2}-\d{2}_", fname)
    return int(m.group(1)) if m else 0


def _exe_number(fname: str) -> int:
    """'…_rar360b2.exe' → 360. 0 when the name says nothing."""
    m = re.match(r"\d{4}-\d{2}-\d{2}_rar(\d{3})", fname)
    return int(m.group(1)) if m else 0


def sfv_expected(folder: Path) -> dict[str, int]:
    """{filename: crc32} from every .sfv in the release folder.

    The scene ships the checksums with the release. Nothing here ever read
    them, which meant a damaged volume looked exactly like a recipe we could
    not find — the sweep would grind the whole space and report a wall for a
    set that was never reproducible by anyone."""
    want: dict[str, int] = {}
    for sfv in sorted(folder.rglob("*.sfv")):
        try:
            text = sfv.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith(";"):
                continue
            name, _, crc = line.rpartition(" ")
            name, crc = name.strip(), crc.strip()
            if not name or len(crc) != 8:
                continue
            try:
                want[name.lower()] = int(crc, 16)
            except ValueError:
                continue
    return want


def sfv_check(folder: Path, paths) -> list[dict]:
    """Which of these files disagree with the .sfv. Files it does not mention
    are not reported — silence there means unknown, not good."""
    want = sfv_expected(folder)
    bad = []
    for p in paths:
        exp = want.get(p.name.lower())
        if exp is None or not p.is_file():
            continue
        got = _file_crc32(p)
        if got != exp:
            bad.append({"name": p.name, "expected": exp, "actual": got,
                        "size": p.stat().st_size})
    return bad


def _no_window() -> dict:
    """Spawn a console child without flashing a window at the operator.

    One rar.exe running for four minutes is barely noticeable. A ZIP sweep is
    seventeen zipper invocations PER ENTRY, each a console app that pops a
    window for a few milliseconds — a strobe over the screen for as long as the
    scan runs. CREATE_NO_WINDOW alone is not always enough for a process
    started from a GUI parent, so the hidden-window STARTUPINFO goes with it."""
    if os.name != "nt":
        return {}
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0                              # SW_HIDE
    return {"startupinfo": si,
            "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}


def _method_groups(meta: list[dict]) -> list[tuple[int, int]]:
    """[(compression level, how many consecutive files)] in archive order.

    A set whose files carry different methods was not produced by one command.
    Phantasy_Star_Zero-XPA is four files at three methods:

        xdelta.exe -m5, xpa-ps0c.bat -m5, xpa-ps0u.crack -m0, xpa-ps0c.nfo -m3

    -m3 is rar's DEFAULT, i.e. a command with no -m at all — the nfo was
    appended afterwards. Packing everything at one level can never reproduce
    that, at any build, thread count or dictionary, so the sweep searched a
    space the answer was not in. Replayed as one command per group it matches
    on combo 1.

    Grouping is by RUN rather than by value: rar appends, so the order files
    appear in the archive is the order the commands ran."""
    out: list[list[int]] = []
    for f in meta:
        m = int(f.get("method", 0))
        if out and out[-1][0] == m:
            out[-1][1] += 1
        else:
            out.append([m, 1])
    return [(lvl, n) for lvl, n in out]


def _mt_label(exe: str, mt) -> str:
    """'-mt8', or 'no -mt' for a build that has no such switch — reporting
    '-mt0' for 3.00 would describe a command nobody could run."""
    return f"-mt{mt}" if _supports_mt(exe or "") else "no -mt"


def _supports_mt(fname: str) -> bool:
    """Does this build understand -mt at all?

    Multithreading arrived in WinRAR 3.60. Every earlier build rejects the
    switch outright — which mattered far more than it sounds, because the sweep
    put -mt on every command: 74 of the 232 builds in the pack, a third of it,
    could never compress anything and failed in milliseconds while still being
    counted as tried. They are also precisely the CONTEMPORARY builds for an
    early release. Matchstick_USA_NDS-BAHAMUT walled twice against the full
    3,944 combos and is reproduced byte-exact by 3.00 with no -mt at all."""
    return _exe_number(fname) >= 360


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


# Every file attribute Windows lets you set directly. RAR copies the source
# file's attribute DWORD into EVERY file header — and for a split file that is
# every volume — so reproducing an archive means reproducing the attributes.
_SETTABLE_ATTRS = (0x1 | 0x2 | 0x4 | 0x20 | 0x80 | 0x100 | 0x1000 | 0x2000
                   | 0x20000)


def _set_win_attrs(path: Path, attrs: int | None):
    """Put the file into the attribute state the archive was packed from.

    This used to mask with 0x26 — archive, hidden, system — on the reasoning
    that those are "the flags RAR actually round-trips" and that READONLY would
    block our own cleanup. Both halves were wrong.

    RAR round-trips the whole DWORD. LiTE's releases were packed from files
    carrying 0x2020 (ARCHIVE | NOT_CONTENT_INDEXED); the mask dropped 0x2000,
    so a rebuild packed a 0x0020 file and every volume header differed from the
    original by those bits. Capture never noticed because IT extracts its
    sources with rar, which restores the full attributes — so the release
    verified at capture and then failed to rebuild, which is the worst possible
    place to disagree. Measured on Star_Wars_The_Clone_Wars-LiTE: with the mask,
    volume one differs; with the real attributes, it matches.

    READONLY is safe to set: _rmtree already clears it, which is precisely why
    that helper exists."""
    if os.name != "nt" or not attrs:
        return
    try:
        import ctypes
        ctypes.windll.kernel32.SetFileAttributesW(
            str(path), int(attrs) & _SETTABLE_ATTRS)
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


def _zip_entry_size(sizes: dict, name: str) -> int:
    return int(sizes.get(name, 0) or 0)


def _release_tag(name: str) -> str:
    """A bracketed status the archive keeps in FRONT of the date — '[NUKED] '.

    Kept as data rather than thrown away: whether a release was nuked is worth
    recording, it just has no business being part of the release NAME."""
    m = _TAG_PREFIX.match(name or "")
    return m.group(1).upper() if m else ""


def _release_name(folder: Path) -> str:
    """Folder name with any bracketed status AND dats.site date prefix
    stripped, so the store is keyed by the ACTUAL release name (point 5 —
    browsable by hand).

    The tag has to come off FIRST. `[NUKED] 2013-12-01-Cookie_Shop…` kept both
    the tag and the date, because the date rule is anchored and the tag pushed
    the date off the front — so those releases were stored under
    `NDS/Unknown/[NUKED] 2013-…` and, worse, `_release_year` found no year,
    which silently disables the date-proximity build ordering in the sweep."""
    return _DATE_PREFIX.sub("", _TAG_PREFIX.sub("", folder.name))


# Scene platform tags, matched on the underscore/dot/dash separated TOKENS of a
# release name and never as a substring — "WII" and "PC" appear inside plenty of
# ordinary words. Aliases fold the variants that are not worth their own shelf:
# a DSi-enhanced game is still an NDS release, and `NDS_DSi` tokenises to both.
_PLATFORMS = (
    "3DS", "NDS", "NGC", "WIIU", "WII", "PS5", "PS4", "PS3", "PS2", "PS1",
    "PSP", "PSV", "PSX", "XBOX360", "XBOX", "GBA", "GBC", "SWITCH", "MAC",
    "LINUX", "PC",
)
_PLAT_LOOKUP = {p: p for p in _PLATFORMS}
_PLAT_LOOKUP.update({"DSI": "NDS", "NSW": "SWITCH", "XBONE": "XBOX"})


def _release_system(rel: str) -> str:
    """The platform tag in a release name, or 'Unknown'.

    Tokens only. A release is `Name_REGION_LANG_PLATFORM-GROUP`, so splitting on
    the scene separators and looking each token up is exact and cheap. First hit
    wins: the platform sits at the end of the name, before the group, and the
    only tag that follows it is a qualifier of it (`NDS_DSi`)."""
    for t in re.split(r"[._\-]+", rel):
        hit = _PLAT_LOOKUP.get(t.upper())
        if hit:
            return hit
    return "Unknown"


def _release_group(rel: str) -> str:
    """The trailing `-GROUP` of a scene release name.

    This is the sharpest predictor of the packing recipe there is, and by a
    distance: a group is one person with one WinRAR install, so their releases
    share a build and a thread count for years. Measured on the first 48
    captures — EXiMiUS 22/23 on one recipe, ONEUP 8/8, BAHAMUT 7/7, PUSSYCAT
    4/4. ONEUP and BAHAMUT even share a BUILD and differ only on -mt, which is
    exactly the distinction a platform-level prior cannot make."""
    m = re.search(r"-([A-Za-z0-9_.]+)$", rel or "")
    return m.group(1).upper() if m else ""


def _release_year(folder: Path, rel: str) -> str:
    """The release YEAR: the dats.site date prefix when the folder carries one
    (authoritative — it is the scene pre date), else a 19xx/20xx token in the
    name, else 'Unknown'. Never guessed from file mtimes, which say when the
    files were copied, not when the release happened."""
    # Match past any bracketed status, or "[NUKED] 2013-12-01-…" yields no
    # year at all — which turns off the sweep's date-proximity ordering.
    m = re.match(r"^(\d{4})-\d{2}-\d{2}[-_]",
                 _TAG_PREFIX.sub("", folder.name))
    if m:
        return m.group(1)
    for t in re.split(r"[._\-]+", rel):
        if re.fullmatch(r"(19|20)\d{2}", t):
            return t
    return "Unknown"


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

def _explain_error(reason: str) -> str:
    """What a recorded failure actually means, in words.

    The reason string is written for the log, where the surrounding lines
    supply the context. In the detail popup there are no surrounding lines —
    "one or more sets unverified" on its own tells the operator nothing they
    did not already know from the red row. Matched on substrings because a
    reason can carry a set stem and several sets can fail at once."""
    r = (reason or "").lower()
    if "damaged volume" in r or "sfv" in r:
        return ("One or more volumes do not match the CRC32 in the release's "
                "own .sfv, so the bytes on disk are not the bytes that were "
                "released. Nothing can reproduce damage — no build at any "
                "setting ever produced them — so this is not a recipe we "
                "failed to find. Re-download the release, or find its RARFIX: "
                "if a fix release is in the same scan folder, the good volume "
                "is taken from there automatically and the REPAIRED set is "
                "captured instead.")
    if "extraction incomplete" in r:
        return ("The sources could not be extracted whole — a file came out "
                "shorter than its header declares, or rar exited badly. Almost "
                "always a damaged or missing volume in the release folder; "
                "check the .sfv before re-running.")
    if "unverified" in r:
        return ("A recipe WAS found — every compressed stream matched byte for "
                "byte — but replaying the command produced volumes that differ "
                "by more than a header residual, so the capture was refused "
                "rather than written and trusted. This is the interesting kind "
                "of failure: worth reporting, and always re-tried.")
    if "replay could not be run" in r:
        return ("The recipe was found, but the replay produced no volumes at "
                "all. Usually rar was killed mid-write (a timeout on a very "
                "large source) rather than anything wrong with the release — "
                "a re-run on its own is often enough.")
    if "cannot read packed blocks" in r or "no readable streams" in r:
        return ("The archive could not be parsed far enough to find where each "
                "file's compressed bytes live. Encrypted headers, or a "
                "genuinely damaged head volume.")
    if "no packed files" in r:
        return ("The archive opened but contains no file blocks — an empty or "
                "truncated head volume.")
    if "nothing captured" in r:
        return ("No set produced a result. If the folder holds an archive kind "
                "this version does not handle, that is expected; otherwise the "
                "set grouping is worth a look.")
    if "no archive" in r:
        return ("No archive set was recognised in the folder, and the files in "
                "it are not all known sidecar kinds either — so it was not "
                "filed as a metadata-only release.")
    if "carried nothing" in r:
        return "A metadata-only release whose files could not be read."
    if "rsr missing" in r:
        return ("The index points at a .rsr that is no longer on disk. Reindex "
                "the store to clear it.")
    return ("No explanation is recorded for this one — worth reporting, with "
            "the log lines above it.")


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


# ══════════════════════════════════════════════════════════════════════════
#  ZIP
# ══════════════════════════════════════════════════════════════════════════
#
# A ZIP is easier than a RAR set in the one way that matters: we can build the
# CONTAINER ourselves. Every header is stored verbatim and only the compressed
# streams have to be reproduced, so there is no "one command must produce the
# whole archive" constraint — no multi-command problem, no volume problem, and
# header fidelity is exact by construction rather than by modelling flag bits,
# extra fields and entry order (measured: 124 of 272 entries carry a 36-byte
# extra field, and 28 of 60 archives are not in sorted order).
ZIP_LEVELS = (9, 6, 5, 7, 8, 4, 3, 2, 1)
ZIP_MEMS = (8, 9, 7, 6, 5, 4, 3, 2, 1)
ZIP_STRATS = ((0, ""), (1, " filtered"), (3, " rle"), (2, " huffman"),
              (4, " fixed"))


def zip_entries(path: Path) -> list[dict] | None:
    """Every entry, with the exact byte range its compressed data occupies."""
    import zipfile
    try:
        with zipfile.ZipFile(str(path)) as zf:
            infos = zf.infolist()
    except Exception:
        return None
    out = []
    with open(path, "rb") as fh:
        for zi in infos:
            if zi.is_dir():
                continue
            if zi.flag_bits & 0x1:
                return None                      # encrypted; out of scope
            fh.seek(zi.header_offset)
            lh = fh.read(30)
            if lh[:4] != b"PK\x03\x04":
                return None
            n, m = struct.unpack("<HH", lh[26:30])
            out.append({
                "name": zi.filename,
                "size": zi.file_size,
                "packed_size": zi.compress_size,
                "crc32": zi.CRC,
                "method": zi.compress_type,
                "data_offset": zi.header_offset + 30 + n + m,
            })
    return out


def zip_skeleton(raw: bytes, ents: list[dict]) -> tuple[bytes, list]:
    """The archive with every compressed stream cut out, plus where they were.

    Headers, the central directory, the end record, any padding between
    members — all of it is carried verbatim, which is why a rebuild does not
    have to understand a single ZIP header field."""
    holes = sorted((e["data_offset"], e["packed_size"]) for e in ents)
    skel = bytearray()
    pos = 0
    for off, ln in holes:
        skel += raw[pos:off]
        pos = off + ln
    skel += raw[pos:]
    return bytes(skel), [list(h) for h in holes]


def zip_assemble(skel: bytes, holes: list, streams: dict) -> bytes:
    """Put the streams back into the skeleton, in file order."""
    out = bytearray()
    pos = 0
    for off, ln in holes:
        gap = off - (len(out))
        out += skel[pos:pos + gap]
        pos += gap
        out += streams[off]
    out += skel[pos:]
    return bytes(out)


def deflate_with(data: bytes, recipe: dict) -> bytes | None:
    """Reproduce one entry's compressed stream from its recipe."""
    if recipe.get("impl") == "zlib":
        co = zlib.compressobj(int(recipe["level"]), zlib.DEFLATED, -15,
                              int(recipe["mem"]), int(recipe.get("strategy", 0)))
        return co.compress(data) + co.flush()
    return None                                  # tool recipes: see _tool_stream


def recovery_record(head: Path) -> int:
    """Bytes of recovery record in the set, or 0 — `rar a -rr` output.

    An RR is a whole block of data the replay never asked for, so its absence
    is not a header residual: diff_bytes refuses a length change outright and
    the capture is refused as "differs by more than a header residual". Which
    it does, by 46 KB of block that was never the compressor's fault."""
    import rarfile
    total = 0

    def cb(h):
        nonlocal total
        t = getattr(h, "type", 0)
        if t in (0x78, 0x7a) and str(getattr(h, "filename", "") or "") in (
                "RR", "RR%", "Rr", "recovery"):
            total += int(getattr(h, "add_size", 0) or 0)
        elif t == 0x78:                       # old-style block, no name at all
            total += int(getattr(h, "add_size", 0) or 0)

    try:
        rf = rarfile.RarFile(str(head), info_callback=cb)
        rf.close()
    except Exception:
        return 0
    return total


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

DELTA2_MAGIC = b"RSRD2\x00"


def diff_bytes(produced: bytes, original: bytes) -> bytes | None:
    """A patch turning `produced` into `original`, or None if too big to be a
    header residual. Format: repeated <u64 offset><u32 len><original bytes>.

    A LENGTH change gets the second format instead. It used to be refused
    outright, on the reasoning that a patch inserting bytes is not a header
    fixup — true of a missing recovery record, false of the case that actually
    turns up: a file header four bytes longer because the original carries the
    Unicode-name flag (0x0200) for a plain ASCII name, which no switch makes
    rar reproduce. The stream had already matched byte for byte, and the
    capture was thrown away over four bytes of header."""
    if len(produced) != len(original):
        # Insertion or deletion: keep the common ends, carry the middle.
        la, lb = len(produced), len(original)
        p = 0
        while p < min(la, lb) and produced[p] == original[p]:
            p += 1
        s = 0
        while s < min(la, lb) - p and produced[la - 1 - s] == original[lb - 1 - s]:
            s += 1
        mid = original[p:lb - s]
        if len(mid) > DELTA_MAX_BYTES:
            return None
        return (DELTA2_MAGIC + p.to_bytes(8, "little") + s.to_bytes(8, "little")
                + len(mid).to_bytes(4, "little") + mid)
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
    if patch.startswith(DELTA2_MAGIC):
        i = len(DELTA2_MAGIC)
        p = int.from_bytes(patch[i:i + 8], "little")
        s = int.from_bytes(patch[i + 8:i + 16], "little")
        ln = int.from_bytes(patch[i + 16:i + 20], "little")
        mid = patch[i + 20:i + 20 + ln]
        tail = produced[len(produced) - s:] if s else b""
        return produced[:p] + mid + tail
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
        self._deadline = None          # wall clock for the sweep, set per set
        self._budget_min = 0
        self._budget_hit = False
        self._budget_override = False  # operator lifted it for THIS release
        self._consumed: list = []      # content files a rebuild actually used
        self._content_root = None      # never delete the root itself
        self._sweep_pos = (0, "")      # how far a parked sweep got
        self._seeded = False           # recipe priors backfilled this process

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

    def extend_budget(self) -> dict:
        """Lift the time budget for the release being captured RIGHT NOW.

        The counterpart to Skip, and it exists because the per-combo cost is
        only knowable once a combo has run: the log says a release needs 3,944
        combos and the budget covers 3,775, and the sensible answer is 'just
        finish it' — for THIS release, not as a settings change. So the
        override is per-release and is cleared as the next one starts; it can
        never quietly disable the budget for a whole overnight run.

        Deliberately does not touch the saved budget_min."""
        if not self._running:
            return {"ok": False, "error": "nothing running"}
        if self._budget_override:
            return {"ok": True, "already": True}
        self._budget_override = True
        self._deadline = None
        self._log("  ⏱ Budget lifted for THIS release — the sweep will run to "
                  "completion. The next release gets the normal budget again.",
                  "warn")
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

    def _run(self, cmd: list, timeout: int, heartbeat: str = "") -> bool:
        """Run a pack/extract command so that stop and skip can interrupt it.

        subprocess.run() is unkillable from another thread, so a skip pressed
        during a 900 s sweep step did nothing until that step finished. This
        polls instead, and terminates the child the moment either event is set.
        Returns True only if the command ran to completion on its own.

        `heartbeat` names the step for the progress line. On a half-gigabyte
        source a single rar.exe call runs for minutes with nothing on screen,
        which is indistinguishable from a hang — so while one is running with a
        name, the elapsed seconds are ticked out."""
        # DEVNULL, not PIPE. Nothing here ever reads the child's output, and an
        # undrained pipe is a deadlock: rar.exe prints a progress percentage
        # while extracting, fills the few-KB OS pipe buffer, and blocks forever
        # waiting for a reader that does not exist. The sweep never tripped it
        # because those commands pass -idcd and emit almost nothing; a 24-volume
        # 512 MB extract prints plenty, so it hung until the 3600 s timeout
        # killed it — an hour per release, and it consumed the whole search
        # budget before a single combo had been tried.
        try:
            p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, **_no_window())
        except Exception:
            return False
        with self._proc_lock:
            self._procs.add(p)
        try:
            started = time.monotonic()
            deadline = started + timeout
            beat = 0.0
            while True:
                try:
                    p.wait(timeout=0.25)
                    return True
                except subprocess.TimeoutExpired:
                    pass
                now = time.monotonic()
                if heartbeat and now - beat > 1.0:
                    beat = now
                    self._progress(f"{heartbeat} · {now - started:,.0f}s")
                if (self._stop.is_set() or self._skip.is_set()
                        or now > deadline):
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
            "budget_min": _num(cfg.get("budget_min"), 45, int),
            "retry_walls": bool(cfg.get("retry_walls", False)),
            "small_first": bool(cfg.get("small_first", True)),
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
            "budget_min": max(0, min(1440, _num(s.get("budget_min"),
                                                cur["budget_min"], int))),
            "retry_walls": bool(s.get("retry_walls", cur["retry_walls"])),
            "small_first": bool(s.get("small_first", cur["small_first"])),
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

    @staticmethod
    def _store_dir(store: Path, folder: Path, rel: str) -> Path:
        """Where a release's .rsr and its extras live: store/SYSTEM/YEAR/RELEASE.

        A flat store is fine for a test run and unusable at corpus scale — the
        NDS + 3DS sets alone are thousands of folders in one directory. System
        then year is the split that matches how the source is already organised
        and how anyone would go looking."""
        return store / _release_system(rel) / _release_year(folder, rel) / rel

    @staticmethod
    def _existing_rsr(store: Path, folder: Path, rel: str) -> Path | None:
        """The .rsr for this release if one is already captured.

        Checks the flat legacy location as well as the current layout, so
        introducing the subdirectories does not make every previously captured
        release look uncaptured and get redone."""
        for cand in (RsrToolAPI._store_dir(store, folder, rel) / f"{rel}.rsr",
                     store / rel / f"{rel}.rsr"):
            if cand.is_file():
                return cand
        return None

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
        if s.get("small_first"):
            # Learn cheaply, then spend. The cost of a sweep is dominated by
            # recompressing the source, so a wrong guess on an 8 MB rom costs a
            # second and the same wrong guess on a 512 MB rom costs a minute.
            # Doing the small releases first means the big ones arrive with the
            # group's recipe already known and land on combo #1 — the same total
            # corpus, in a fraction of the time.
            def _weight(f: Path) -> int:
                try:
                    return sum(p.stat().st_size for p in f.rglob("*")
                               if p.is_file())
                except OSError:
                    return 0
            self._progress("sizing release folders…")
            folders.sort(key=_weight)
            self._progress("")
            self._log("Smallest releases first — priors learned on cheap "
                      "releases make the expensive ones land on combo #1.",
                      "dim")
        self._log(f"{len(folders)} release folder(s) under {src}", "info")
        self._log(f"Build pack: {len(exes)} exe(s)   ·   store: {store}", "dim")

        done = ok = failed = skipped = zips = parked = walls = meta = 0
        partial = broken = 0
        for i, folder in enumerate(folders, 1):
            if self._stop.is_set():
                self._log("Stopped.", "warn")
                break
            rel = _release_name(folder)
            # Clear any skip from the PREVIOUS release here, not when the skip
            # fires — otherwise a skip pressed late in one release could still
            # be set as the next one starts and silently skip that too.
            self._skip.clear()
            # Same reasoning for the budget flag: _capture_release returns
            # early on a ZIP or an archive-less folder without ever reaching
            # the point where it resets this, so a stale True would relabel the
            # NEXT release as parked.
            self._budget_hit = False
            self._budget_override = False      # per-release only, never sticky
            self._sweep_pos = (0, "")
            self._emit("row", {"name": rel, "status": "running",
                               "kind": "running"})
            self._log("", "")
            self._log(f"══ [{i}/{len(folders)}] {rel} ══", "info")
            if s["skip_done"] and self._existing_rsr(store, folder, rel):
                self._log(f"  Already captured — skipping "
                          f"(untick '{UI_SKIP_DONE}' to redo).", "dim")
                # Say WHY it was skipped in the row itself. A resumed run is
                # mostly these, and "skipped" alone does not distinguish
                # "captured on an earlier run" from "you pressed Skip".
                self._emit("row", {"name": rel, "status": "skipped",
                                   "recipe": "already captured",
                                   "kind": "captured-before"})
                skipped += 1
                continue
            wall = None if s.get("retry_walls") else self._known_wall(rel)
            if wall:
                self._log(f"  Already swept to exhaustion on "
                          f"{(wall[0] or '')[:10]} against {wall[1]} build(s) — "
                          f"skipping (tick '{UI_RETRY_WALLS}' to redo).", "dim")
                self._emit("row", {"name": rel, "status": "skipped",
                                   "recipe": "known wall",
                                   "kind": "wall"})
                walls += 1
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
                self._emit("row", {"name": rel, "status": "skipped",
                                   "recipe": "skipped by hand",
                                   "kind": "skipped"})
                skipped += 1
                continue
            if res.get("error") == "zip release":
                # Out of scope, not a failure. Counted apart so the summary's
                # "failed" figure stays a number worth reading.
                self._emit("row", {"name": rel, "status": "skipped",
                                   "recipe": "zip", "kind": "zip"})
                zips += 1
                continue
            if res.get("damaged"):
                # Not a wall and not our failure: the bytes on disk are not
                # what the release shipped. Recorded so it can say so later,
                # and counted apart so "failed" keeps meaning our problem.
                self._db_miss(rel, "damaged", res.get("error", "sfv mismatch"),
                              len(exes))
                self._emit("row", {"name": rel, "status": "error",
                                   "recipe": "damaged — fails its .sfv",
                                   "kind": "damaged"})
                broken += 1
                continue
            if res.get("partial"):
                # Same reasoning: a fix release is complete in itself, it just
                # is not a set anyone can rebuild on its own.
                self._db_forget(rel)
                self._emit("row", {"name": rel, "status": "skipped",
                                   "recipe": "fix release — partial set",
                                   "kind": "partial"})
                partial += 1
                continue
            if self._budget_hit and not res.get("ok"):
                # Not a wall and not a failure — an unfinished search. Kept
                # apart so a later re-run (with better priors) can be pointed
                # at exactly these, and so the failed count stays meaningful.
                pos, sig = getattr(self, "_sweep_pos", (0, ""))
                self._db_miss(rel, "parked", "time budget", len(exes),
                              combos=pos, order_sig=sig)
                self._emit("row", {"name": rel, "status": "skipped",
                                   "recipe": "parked — time budget",
                                   "kind": "parked"})
                parked += 1
                continue
            done += 1
            if res.get("metadata"):
                # A complete release that simply has no archive. Counted apart
                # so "verified" keeps meaning "a recipe was proved".
                meta += 1
                self._db_forget(rel)
                self._emit("row", {"name": rel, "status": "done",
                                   "recipe": res.get("recipe", "metadata only"),
                                   "kind": "metadata"})
                continue
            if res.get("ok"):
                ok += 1
                self._db_forget(rel)          # it worked; the miss is stale
                self._emit("row", {"name": rel, "status": "done",
                                   "recipe": res.get("recipe", ""),
                                   "kind": "ok"})
            else:
                failed += 1
                err = res.get("error", "")
                if err == "recipe not found":
                    # Exhaustively searched. Remember it against the size of
                    # the pack that failed, so a future scan re-tries only if
                    # the pack has grown.
                    self._db_miss(rel, "wall", err, len(exes))
                else:
                    # Everything else is recorded too, not to suppress a
                    # re-run — errors are always re-tried — but so the release
                    # can still say what happened to it after the log has
                    # scrolled away or the window has been closed.
                    self._db_miss(rel, "error", err, len(exes))
                self._emit("row", {"name": rel, "status": "error",
                                   "recipe": err,
                                   "kind": "wall" if err == "recipe not found"
                                           else "error"})

        self._log("", "")
        self._log(f"Capture complete — {ok} verified, {failed} failed, "
                  f"{skipped} skipped"
                  + (f", {meta} metadata-only release(s) carried" if meta else "")
                  + (f", {walls} known wall(s) passed over" if walls else "")
                  + (f", {parked} parked on the time budget" if parked else "")
                  + (f", {zips} ZIP release(s) out of scope" if zips else "")
                  + (f", {partial} fix release(s) with only part of a set"
                     if partial else "")
                  + (f", {broken} DAMAGED (fail their own .sfv)"
                     if broken else "")
                  + ".", "ok" if failed == 0 else "warn")

    # ── one release ───────────────────────────────────────────────────────

    def _find_pair(self, folder: Path, st: dict, ignore=()):
        """The folder holding the rest of this set, if it is in the scan.

        A RARFIX ships the repaired volume and the release it repairs is
        missing exactly that volume, so neither folder can be captured alone —
        but together they are an ordinary complete set. Matching is on the
        archive STEM (the fix carries the same one, by definition: it has to
        drop into the same set) with strictly disjoint volumes, and the union
        has to run unbroken from the head. Anything less is two copies of one
        release rather than two halves of one, and is left alone.

        Measured over 2,596 folders: 7 stems appear in two folders, 2 of them
        complementary — both genuine RARFIX pairs, no false positives."""
        want = st["stem"]
        # A volume this folder holds but which FAILS its .sfv counts as absent:
        # a fix release that supplies a good copy of exactly that volume is the
        # other half of the set, not a duplicate of it.
        skip = {n.lower() for n in ignore}
        mine = [Path(v) for v in st["volumes"]
                if Path(v).name.lower() not in skip]
        have = {p.name for p in mine}
        for sib in sorted(folder.parent.iterdir()):
            if not sib.is_dir() or sib == folder:
                continue
            for s2 in group_archive_sets(sib):
                if s2["stem"] != want or s2["byte_split"]:
                    continue
                theirs = [Path(v) for v in s2["volumes"]]
                names = {p.name for p in theirs}
                if names & have:
                    continue                    # two copies, not two halves
                if skip and not any(p.name.lower() in skip for p in theirs):
                    # Replacing damaged volumes: the partner has to actually
                    # carry a replacement for one of them, or it is just some
                    # other release that happens to share a stem.
                    continue
                union = sorted(mine + theirs,
                               key=lambda p: _classify_volume(p.name)[2])
                idx = [_classify_volume(p.name)[2] for p in union]
                if idx == list(range(-1, len(idx) - 1)):
                    return sib, s2, union
        return None

    def _capture_release(self, folder: Path, store: Path, s: dict,
                         exes: list[Path]) -> dict:
        rel = _release_name(folder)
        sets = group_archive_sets(folder)
        if not sets:
            # Say WHICH kind of nothing. A third of the NDS corpus is ZIP
            # releases (3308 of 9328 folders), which v1 does not do and is not
            # a defect — but reported identically to a RAR set we failed to
            # find, it would hide real misses in thousands of expected ones.
            kinds = {p.suffix.lower() for p in folder.rglob("*") if p.is_file()}
            if ".zip" in kinds:
                zips = sorted(p for p in folder.rglob("*.zip") if p.is_file())
                return self._capture_zip(folder, store, s, rel, zips)
            # A metadata-only fix (DIRFIX, NFOFIX, …) IS the release — it never
            # had an archive, so reporting "no archive set found" calls a
            # complete release a miss and buries it among the real ones. There
            # is still something worth keeping: the nfo is the entire artefact.
            files = [p for p in folder.rglob("*") if p.is_file()]
            if files and all(p.suffix.lower() in _SIDECAR_EXT for p in files):
                return self._capture_metadata(folder, store, s, rel, files)
            self._log("  No archive set found in this folder"
                      + (f" (contains: {', '.join(sorted(k for k in kinds if k)[:6])})"
                         if kinds else " — folder is empty") + ".", "warn")
            return {"ok": False, "error": "no archive"}

        # Pre-flight against the .sfv the release ships with. A volume that
        # fails its own checksum is DAMAGE, and damage has no recipe: no build
        # at no thread count ever produced those bytes, so sweeping for one is
        # hours spent proving that corruption is not reproducible. This is also
        # the honest answer to "what about the original set the RARFIX fixes" —
        # that set is not a release we failed to capture, it is a broken copy of
        # one, and the correct bytes are in the fix.
        vol_paths = [Path(v) for st in sets for v in st["volumes"]]
        broken_keep: list = []          # (path, sfv record) kept verbatim
        pair_seed = None                # (this folder, partner, joined volumes)
        damaged = sfv_check(folder, vol_paths)
        if damaged:
            names = ", ".join(d["name"] for d in damaged[:4])
            self._log(f"  ✗ {len(damaged)} volume(s) fail the .sfv: {names}"
                      + (" …" if len(damaged) > 4 else ""), "err")
            for d in damaged[:4]:
                self._log(f"      {d['name']}: sfv says {d['expected']:08X}, "
                          f"the file is {d['actual']:08X} "
                          f"({d['size']:,} B)", "dim")
            bad_names = [d["name"] for d in damaged]
            pair = pair_st = None
            for st in sets:
                pair = self._find_pair(folder, st, ignore=bad_names)
                if pair:
                    pair_st = st
                    break
            if not pair:
                self._log("    A damaged volume has no recipe — nothing could "
                          "reproduce these bytes. Looking for a fix release "
                          "found nothing either.", "warn")
                return {"ok": False, "damaged": True,
                        "error": f"damaged volume(s) per the .sfv: {names}"}
            sib, _s2, union = pair
            self._log(f"    ✓ {sib.name} supplies a good copy — capturing the "
                      f"REPAIRED set, and carrying the bad volume(s) as they "
                      f"were.", "ok")
            # Swap the good volumes in. The broken ones are kept aside and
            # carried verbatim: a rebuild then restores BOTH folders exactly as
            # they sat on disk, bad file included, for anyone who wants the
            # release as it was actually distributed rather than as it should
            # have been.
            pair_st["volumes"] = union
            broken_keep = [(folder / d["name"], d) for d in damaged]
            pair_seed = (folder, sib, union)

        work = Path(tempfile.mkdtemp(prefix="rsr-"))
        manifest = {
            "rsr_version": RSR_VERSION,
            "magic": RSR_MAGIC,
            "created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "tool": f"tosort_toolkit rsr_tool {RSR_VERSION}",
            "host": {"platform": platform.platform(),
                     "python": platform.python_version()},
            "release": rel,
            "system": _release_system(rel),
            "year": _release_year(folder, rel),
            "tag": _release_tag(folder.name),
            "source_folder": str(folder),
            "sets": [],
        }
        embedded: dict[str, bytes] = {}      # path inside the .rsr → bytes
        all_ok = True
        # One release must never be able to eat a whole run. The sweep is a
        # product of builds x thread counts x dictionaries, and on a set whose
        # answer is not in the pack at all it is the FULL product every time —
        # 134 MB recompressed on each combo. Park it and move on; the priors
        # learned from the rest of the corpus make a later retry much cheaper.
        #
        # The clock starts at the SWEEP, not here. Reading and extracting the
        # source is unavoidable work that has to happen whatever the budget is,
        # so charging it to the search meant a slow extract could spend the
        # whole allowance and park the release having tried ZERO combos — all
        # of the cost, none of the benefit.
        self._budget_min = max(0, _num(s.get("budget_min"), 0, int))
        self._deadline = None
        self._budget_hit = False
        set_errors: list[str] = []
        pair_used = None            # (this folder, its partner, the base one)
        if pair_seed:
            this, sib, union = pair_seed
            base = this if len(union) > 1 else sib
            pair_used = (this, sib, base)
            manifest["pair"] = sorted({this.name, sib.name})
            pair_origin = {p.name: (this if p.parent == this else sib)
                           for p in union}
        else:
            pair_origin = {}
        try:
            for si, st in enumerate(sets):
                if self._stop.is_set() or self._skip.is_set():
                    return {"ok": False,
                            "error": "skipped" if self._skip.is_set()
                            else "stopped"}
                res = self._capture_set(st, si, folder, work / f"set{si}",
                                        s, exes, embedded)
                if res.get("partial"):
                    # Not a failure and not a wall: a fix release ships the
                    # repaired volume alone, so there is nothing here that could
                    # ever be reproduced from what is in this folder.
                    self._log(f"  {st['stem']}: {res.get('error')} — a partial "
                              "set on disk, not a damaged one: the rest of it "
                              "lives in the release this one pairs with.",
                              "dim")
                    pair = self._find_pair(folder, st)
                    if not pair:
                        self._log("    the other half is not in this scan "
                                  "folder, so there is nothing to join it to.",
                                  "dim")
                        return {"ok": False, "error": res.get("error"),
                                "partial": True}
                    sib, _s2, union = pair
                    # File the joined release under whichever folder holds more
                    # of it, so both halves resolve to the same .rsr whichever
                    # one the scan reaches first.
                    base = folder if len(st["volumes"]) >= len(union) / 2 else sib
                    base_rel = _release_name(base)
                    done = self._existing_rsr(store, base, base_rel)
                    if done and s.get("skip_done"):
                        self._log(f"    already captured with its pair as "
                                  f"{done.name}.", "dim")
                        return {"ok": False, "error": "captured with its pair",
                                "partial": True}
                    self._log(f"    ✓ the rest of the set is in {sib.name} — "
                              f"joining {len(union)} volume(s) and capturing as "
                              f"one release.", "ok")
                    pair_origin = {p.name: (folder if p.parent == folder else sib)
                                   for p in union}
                    pair_used = (folder, sib, base)
                    manifest["release"] = base_rel
                    manifest["system"] = _release_system(base_rel)
                    manifest["year"] = _release_year(base, base_rel)
                    manifest["tag"] = _release_tag(base.name)
                    manifest["pair"] = sorted({folder.name, sib.name})
                    manifest["source_folder"] = str(base)
                    res = self._capture_set(dict(st, volumes=union), si, base,
                                            work / f"set{si}pair", s, exes,
                                            embedded)
                    for v in res.get("set", {}).get("volumes", []):
                        src = pair_origin.get(v.get("name"))
                        if src is not None and src != base:
                            v["folder"] = src.name
                    pair_used = (folder, sib, base)
                if not res.get("ok"):
                    all_ok = False
                    self._log(f"  ✗ {st['stem']}: {res.get('error')}", "err")
                    # Keep the SET's reason. Rolled up to the release it becomes
                    # "one or more sets unverified", which is the one thing the
                    # operator already knows and none of what they need.
                    set_errors.append(f"{st['stem']}: {res.get('error')}")
                if pair_origin:
                    for v in res.get("set", {}).get("volumes", []):
                        src = pair_origin.get(v.get("name"))
                        if src is not None and src != pair_used[2]:
                            v["folder"] = src.name
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
                # Say WHICH kind of failure, because the caller records a wall
                # (exhaustively searched, don't repeat) very differently from a
                # verification miss (a real defect, always worth re-running).
                if any(x.get("wall") for x in manifest["sets"]):
                    return {"ok": False, "error": "recipe not found"}
                return {"ok": False,
                        "error": "; ".join(set_errors) or
                                 "one or more sets unverified"}

            # Sidecars: everything loose in the release folder that is not a
            # volume — .nfo, .sfv, proof jpg, file_id.diz, Proof/ and Sample/
            # subfolders. They are part of the release but appear in no
            # archive, so nothing above ever looked at them and a rebuild
            # produced a folder missing its own nfo. Kilobytes; carry them.
            manifest["sidecars"] = self._capture_sidecars(folder, s, embedded,
                                                          manifest)
            if broken_keep:
                # The damaged volume, kept as it was. It is not part of any
                # recipe — nothing can reproduce it — so it is carried
                # verbatim, and only when it fits the embed cap: a 50 MB bad
                # volume is not worth doubling the .rsr for, and saying so is
                # better than quietly dropping it.
                cap = max(0, int(s.get("embed_max_mb", 16))) * 1024 * 1024
                # Same folder rule as the volumes and sidecars: the base folder
                # of a pair rebuilds at the root, the partner into its own
                # subfolder. Tagging this one unconditionally put the bad volume
                # in a subfolder while its own siblings went to the root.
                home = pair_used[2] if pair_used else folder
                fld = "" if folder == home else folder.name
                kept = []
                for path, rec in broken_keep:
                    if not path.is_file():
                        continue
                    if cap and path.stat().st_size > cap:
                        self._log(f"  ⚠ {path.name} is damaged and too big to "
                                  f"carry ({path.stat().st_size:,} B > the "
                                  f"{cap:,} B embed cap) — recorded, not "
                                  f"stored.", "warn")
                        kept.append({"name": path.name, "folder": fld,
                                     "size": path.stat().st_size,
                                     "crc32": rec["actual"],
                                     "sfv_crc32": rec["expected"],
                                     "stored": None})
                        continue
                    data = path.read_bytes()
                    key = f"damaged/{folder.name}/{path.name}"
                    embedded[key] = data
                    kept.append({"name": path.name, "folder": fld,
                                 "size": len(data), "crc32": rec["actual"],
                                 "sfv_crc32": rec["expected"],
                                 "sha256": _sha256(data), "stored": key})
                    self._log(f"  carried the damaged {path.name} verbatim "
                              f"({len(data):,} B) — a rebuild restores the "
                              f"folder exactly as it was, bad volume and all.",
                              "dim")
                manifest["damaged"] = kept
            if pair_used:
                # Both folders have their own nfo and sfv — a fix release always
                # ships its own. Carry the partner's too, tagged with the folder
                # it belongs in, so a rebuild puts back BOTH folders as they
                # were rather than one merged heap.
                this, sib, base = pair_used
                for f in manifest["sidecars"]:
                    if this != base:
                        f["folder"] = this.name
                extra = self._capture_sidecars(sib, s, embedded, manifest)
                for f in extra:
                    if sib != base:
                        f["folder"] = sib.name
                    # Two folders can hold same-named sidecars with different
                    # bytes; _capture_sidecars keys on the name alone, so give
                    # the partner's their own prefix rather than let one
                    # silently win.
                    key = f.get("stored")
                    if key and key in {x.get("stored")
                                       for x in manifest["sidecars"]}:
                        newkey = f"sidecars/{sib.name}/{f['name']}"
                        if embedded.get(key) is not None:
                            embedded[newkey] = embedded[key]
                        f["stored"] = newkey
                manifest["sidecars"] += extra
                self._log(f"  paired with {sib.name}: {len(extra)} sidecar(s) "
                          "from there as well.", "dim")

            # Optional legacy .srr, embedded verbatim so a .rsr can always emit
            # one for the existing ecosystem without us re-deriving structure.
            if s["write_srr"]:
                srr = self._make_srr(folder, sets, work)
                if srr:
                    embedded["release.srr"] = srr
                    manifest["srr"] = "release.srr"

            # A joined pair is filed under the base release, not under
            # whichever half the scan happened to reach first.
            rel = manifest["release"]
            home = pair_used[2] if pair_used else folder
            out_dir = self._store_dir(store, home, rel)
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
                    "recipe": f"{recipe.get('version', '?')} "
                              f"{_mt_label(recipe.get('exe', ''), recipe.get('mt', '?'))}",
                    "error": ""}
        finally:
            _rmtree(work)

    # ── release-folder sidecars ───────────────────────────────────────────

    # ── ZIP ───────────────────────────────────────────────────────────────

    def _tool_zips(self) -> list[tuple]:
        """(label, argv builder, output name) for every non-zlib deflate on
        this machine. Info-ZIP, 7-Zip and WinRAR each write their own, and the
        corpus uses all three."""
        out = []
        zx = next((self._app_dir / "apps").rglob("zip.exe"), None)
        if zx:
            out += [(f"Info-ZIP -{l}",
                     lambda w, n, l=l, zx=zx: [str(zx), f"-{l}", "-X", "o.zip", n],
                     "o.zip") for l in range(1, 10)]
        sv = self._app_dir / "apps" / "7z.exe"
        if sv.is_file():
            out += [(f"7z -mx{l}",
                     lambda w, n, l=l, sv=sv: [str(sv), "a", "-tzip",
                                               "-mm=Deflate", f"-mx{l}",
                                               "s.zip", n], "s.zip")
                    for l in (5, 9, 1, 3, 7)]
        wr = Path(r"C:\Program Files\WinRAR\WinRAR.exe")
        if wr.is_file():
            out += [(f"WinRAR zip -m{l}",
                     lambda w, n, l=l, wr=wr: [str(wr), "a", "-afzip", f"-m{l}",
                                               "-ep", "-ibck", f"w{l}.zip", n],
                     f"w{l}.zip") for l in (3, 5, 1)]
        return out

    def _tool_stream(self, plan, data: bytes, name: str, work: Path):
        """Compress one file with an external zipper and hand back the raw
        deflate stream it produced."""
        label, argv, outname = plan
        work.mkdir(parents=True, exist_ok=True)
        for junk in work.iterdir():
            try:
                junk.unlink()
            except OSError:
                pass
        safe = Path(name).name or "entry.bin"
        (work / safe).write_bytes(data)
        try:
            subprocess.run(argv(work, safe), cwd=str(work),
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=600, **_no_window())
        except Exception:
            return None
        made = work / outname
        if not made.is_file():
            return None
        ents = zip_entries(made)
        if not ents:
            return None
        e = next((x for x in ents if Path(x["name"]).name == safe), None)
        if e is None:
            return None
        with open(made, "rb") as fh:
            fh.seek(e["data_offset"])
            return fh.read(e["packed_size"])

    def _sweep_zip_entry(self, data: bytes, raw: bytes, name: str, work: Path,
                         deadline=None, grp: str = "") -> dict | None:
        """Which deflate produced this stream.

        zlib first and by likelihood — measured over 272 entries, -9 mem8 and
        -9 mem9 alone account for 137 of the 188 that reproduced. memLevel is
        swept because leaving it at 8 is what made the first survey call 55% of
        the corpus unreproducible when the real figure was 14%."""
        # Deflate emits complete blocks as it goes, so compressing a PREFIX of
        # the input yields a prefix of the full output. That makes a cheap
        # discriminator: run the 405 settings over the first megabyte, and only
        # the handful whose output still agrees with the target pay for the
        # whole file. On a 33 MB rom that is the difference between one sweep
        # of minutes and one of hours.
        # What this group has used before, first. A scene group zips the way it
        # zips, so this is normally a single attempt instead of 405.
        if not getattr(self, "_zip_seeded", False):
            self._zip_seeded = True
            self._seed_zip_priors()
        hot = self._zip_hot(grp)
        if hot:
            self._log(f"      {len(hot)} known deflate(s)"
                      + (f" for {grp}" if grp else "") + " — trying those "
                      "first.", "dim")
        for r in hot:
            if self._stop.is_set() or self._skip.is_set():
                return None
            got = (deflate_with(data, r) if r.get("impl") == "zlib"
                   else self._entry_stream(data, r, name, work))
            if got == raw:
                return dict(r)

        probe = data[:1 << 20] if len(data) > (4 << 20) else None
        total = len(ZIP_STRATS) * len(ZIP_LEVELS) * len(ZIP_MEMS)
        tried = full = 0
        t0 = last = time.monotonic()
        short = Path(name).name
        for strat, sname in ZIP_STRATS:
            for lvl in ZIP_LEVELS:
                for mem in ZIP_MEMS:
                    if self._stop.is_set() or self._skip.is_set():
                        return None
                    tried += 1
                    now = time.monotonic()
                    if now - last > 0.5:
                        last = now
                        self._progress(f"zlib {tried}/{total} · -{lvl} mem{mem}"
                                       f"{sname} · {full} full check(s) · "
                                       f"{short}")
                    if probe is not None:
                        co = zlib.compressobj(lvl, zlib.DEFLATED, -15, mem,
                                              strat)
                        head = co.compress(probe)
                        if head and not raw.startswith(head):
                            continue
                    full += 1
                    co = zlib.compressobj(lvl, zlib.DEFLATED, -15, mem, strat)
                    if co.compress(data) + co.flush() == raw:
                        return {"impl": "zlib", "level": lvl, "mem": mem,
                                "strategy": strat,
                                "label": f"zlib -{lvl} mem{mem}{sname}"}
            if deadline and time.monotonic() > deadline:
                break
        self._log(f"      zlib: {total} setting(s) swept, {full} needed the "
                  f"whole file ({time.monotonic() - t0:,.0f}s) — no match.",
                  "dim")

        # The same pruning for the external zippers, which is where the time
        # actually goes: each one recompresses the entire rom.
        #
        # A tool's output for a 1 MB prefix is only MOSTLY a prefix of the full
        # stream — 7-Zip optimises block boundaries over a lookahead, so the
        # tail of a truncated run diverges. Trimming a fixed margin off the end
        # is not good enough: measured across 17 settings, agreement ran from
        # 92% to 99.9% of the probe output, and a 32 KB trim wrongly pruned
        # Info-ZIP -2 and WinRAR -m1 — both of which would have been real
        # answers for some other release.
        #
        # So compare a fixed slice from the START instead, never more than half
        # the probe output. A wrong setting diverges within the first few
        # hundred bytes, so 64 KB discriminates just as well while sitting far
        # inside the region that provably agrees.
        plans = self._tool_zips()
        pre_raw = None
        if probe is not None:
            pre_raw = probe
        for i, plan in enumerate(plans, 1):
            if self._stop.is_set() or self._skip.is_set():
                return None
            self._progress(f"{plan[0]} · {i}/{len(plans)} · {short}")
            if pre_raw is not None:
                head = self._tool_stream(plan, pre_raw, name, work)
                if head is None:
                    continue
                n = min(65536, len(head) // 2)
                if n and not raw.startswith(head[:n]):
                    continue
                self._log(f"      {plan[0]}: survives the prefix probe — "
                          f"checking the whole file.", "dim")
            if self._tool_stream(plan, data, name, work) == raw:
                return {"impl": "tool", "label": plan[0]}
            if deadline and time.monotonic() > deadline:
                self._log("      ⏱ time budget reached while sweeping "
                          "zippers.", "warn")
                break
        self._log(f"      {len(plans)} zipper setting(s) swept too "
                  f"({time.monotonic() - t0:,.0f}s total).", "dim")
        return None

    def _entry_stream(self, data: bytes, recipe: dict, name: str,
                      work: Path) -> bytes | None:
        if recipe.get("impl") == "zlib":
            return deflate_with(data, recipe)
        for plan in self._tool_zips():
            if plan[0] == recipe.get("label"):
                return self._tool_stream(plan, data, name, work)
        return None

    # ── preflate fallback ─────────────────────────────────────────────────

    def _precomp_exe(self) -> Path | None:
        p = (self._app_dir / "apps" / "zip_pack" / "precomp" / "windows"
             / "precomp.exe")
        return p if p.is_file() else None

    def _pcf_of(self, zp: Path, work: Path) -> bytes | None:
        """precomp -cn of an archive: every deflate stream turned back into its
        source bytes, plus the data needed to re-deflate it EXACTLY.

        This is the answer to a stream no build-and-setting sweep can match.
        preflate derives the parameters from the stream itself instead of
        guessing which zipper wrote it, which is the whole difference: measured
        on Dragon_Dance-Caravan, 3 of 3 streams recompressed and the archive
        restored byte-identical, where 405 zlib settings and 17 zippers had all
        failed. -cn because the payload has to stay findable — a compressed
        .pcf would be a blob we could not cut the content out of."""
        exe = self._precomp_exe()
        if exe is None:
            return None
        work.mkdir(parents=True, exist_ok=True)
        out = work / "a.pcf"
        if out.exists():
            out.unlink()
        # -t+z -d0: ZIP streams only, and do not recurse.
        #
        # Left to itself precomp also unpacks GZip, PNG and JPG — including
        # streams it finds INSIDE the decompressed rom — so the content stops
        # being a contiguous run of bytes in the output and cannot be cut back
        # out. Astrology-iND failed exactly there: 6 recompressed streams where
        # the archive holds 4, the extras being a GZip and a JPG inside the
        # data. Restricted to ZIP with no recursion it is 4 of 4 and the rom is
        # contiguous again.
        if not self._run([str(exe), "-cn", "-t+z", "-d0", f"-o{out}", str(zp)],
                         timeout=3600, heartbeat=f"preflate {zp.name}"):
            return None
        return out.read_bytes() if out.is_file() else None

    def _pcf_restore(self, pcf: bytes, work: Path) -> bytes | None:
        """precomp -r: the .pcf back to the archive it came from."""
        exe = self._precomp_exe()
        if exe is None:
            return None
        work.mkdir(parents=True, exist_ok=True)
        src = work / "r.pcf"
        dst = work / "r.zip"
        for q in (src, dst):
            if q.exists():
                q.unlink()
        src.write_bytes(pcf)
        if not self._run([str(exe), "-r", f"-o{dst}", str(src)], timeout=3600,
                         heartbeat="preflate restore"):
            return None
        return dst.read_bytes() if dst.is_file() else None

    @staticmethod
    def _pcf_cut(pcf: bytes, payloads: list) -> tuple | None:
        """Take the content payloads back out of the .pcf.

        Same trick as the ZIP skeleton: what is left is headers and preflate's
        reconstruction data — 86 KB against 34 MB of rom on Dragon_Dance — and
        the content is supplied again at rebuild."""
        holes = []
        for name, data in payloads:
            if not data:
                return None
            i = pcf.find(data[:1 << 16])
            if i < 0 or pcf[i:i + len(data)] != data:
                return None
            holes.append([i, len(data), name])
        holes.sort()
        skel = bytearray()
        pos = 0
        for off, ln, _n in holes:
            if off < pos:
                return None                      # overlapping payloads
            skel += pcf[pos:off]
            pos = off + ln
        skel += pcf[pos:]
        return bytes(skel), holes

    def _capture_zip(self, folder: Path, store: Path, s: dict, rel: str,
                     zips: list[Path]) -> dict:
        """Capture a ZIP release: headers verbatim, streams by recipe.

        The same rule as everywhere else — the .rsr is written only after it
        has been reassembled here and compared byte for byte with the original
        archive."""
        work = Path(tempfile.mkdtemp(prefix="rsr-zip-"))
        manifest = {
            "rsr_version": RSR_VERSION, "magic": RSR_MAGIC,
            "created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "tool": f"tosort_toolkit rsr_tool {RSR_VERSION}",
            "host": {"platform": platform.platform(),
                     "python": platform.python_version()},
            "release": rel, "system": _release_system(rel),
            "year": _release_year(folder, rel), "tag": _release_tag(folder.name),
            "kind": "zip", "source_folder": str(folder), "sets": [],
        }
        embedded: dict[str, bytes] = {}
        cap = max(0, int(s.get("embed_max_mb", 16))) * 1024 * 1024
        budget = max(0, _num(s.get("budget_min"), 0, int))
        try:
            for zi_no, zp in enumerate(zips):
                raw = zp.read_bytes()
                ents = zip_entries(zp)
                if not ents:
                    self._log(f"  ✗ {zp.name}: cannot read (encrypted, or not "
                              "a plain ZIP).", "err")
                    return {"ok": False, "error": f"{zp.name}: unreadable zip"}
                skel, holes = zip_skeleton(raw, ents)
                self._log(f"  {zp.name}  ·  ZIP  ·  {len(ents)} entr(y/ies)  ·  "
                          f"{len(skel):,} B of header carried verbatim", "dim")
                deadline = (time.monotonic() + budget * 60) if budget else None
                files = []
                streams = {}
                ok = True
                # Same rule as a RAR set: the largest entry is content by
                # definition, and so is anything at or over the embed cap. A
                # size cap alone cannot do it — an 8 MB rom sits under any cap
                # generous enough to hold a proof jpg, and embedding the rom
                # made the first .rsr TWICE the size of the archive it
                # describes.
                biggest = max((e["size"] or 0) for e in ents)
                prefer_pf = self._zip_prefers_preflate(_release_group(rel))
                for e in ents:
                    with open(zp, "rb") as fh:
                        fh.seek(e["data_offset"])
                        rawe = fh.read(e["packed_size"])
                    rec = dict(e)
                    big = ((e["size"] or 0) >= biggest
                           or (cap and (e["size"] or 0) >= cap))
                    rec["source"] = "content" if big else "extra"
                    if e["method"] == 0 and big:
                        rec["recipe"] = {"impl": "stored", "label": "stored"}
                    elif not big:
                        # Small enough to carry the stream itself: exact by
                        # definition, and cheaper than proving a recipe.
                        key = f"zips/{zi_no}/{len(files)}.def"
                        embedded[key] = rawe
                        rec["stored"] = key
                        rec["recipe"] = {"impl": "verbatim", "label": "carried"}
                    elif prefer_pf:
                        self._log(f"    {e['name']}: no setting has ever "
                                  f"reproduced this group's streams — going "
                                  f"straight to preflate.", "dim")
                        ok = False
                        break
                    else:
                        data = zlib.decompress(rawe, -15)
                        # Say what is about to happen and roughly what it
                        # costs. A ZIP sweep is one long silence per rom
                        # otherwise, which reads exactly like a hang.
                        self._log(f"    sweeping {e['name']} "
                                  f"({e['size']:,} → {e['packed_size']:,} B) — "
                                  f"405 zlib setting(s) then "
                                  f"{len(self._tool_zips())} zipper(s), "
                                  f"pruned by a 1 MB prefix probe…", "dim")
                        self._progress(f"sweeping {Path(e['name']).name} "
                                       f"({e['size'] / (1 << 20):,.0f} MB)")
                        t = time.monotonic()
                        r = self._sweep_zip_entry(data, rawe, e["name"],
                                                  work / "tool", deadline,
                                                  grp=_release_group(rel))
                        if not r:
                            self._log(f"    ✗ {e['name']}: no deflate setting "
                                      f"reproduces this stream "
                                      f"({time.monotonic() - t:,.0f}s).", "err")
                            ok = False
                            break

                        self._log(f"    ✓ {e['name']}: {r['label']} "
                                  f"({time.monotonic() - t:,.0f}s)", "ok")
                        # Learn it now, not at write time: a recipe that proved
                        # itself here is the right lead for this group's next
                        # release even if a later entry in THIS one fails.
                        self._db_learn_zip(_release_group(rel), r)
                        rec["recipe"] = r
                    if not big and e["method"] != 0:
                        pass
                    files.append(rec)
                    streams[e["data_offset"]] = rawe
                if not ok:
                    # No setting reproduces the stream — so stop guessing which
                    # zipper wrote it and read the parameters out of the stream.
                    pcf = self._pcf_of(zp, work / "pf")
                    if pcf is None:
                        return {"ok": False,
                                "error": f"{zp.name}: no deflate setting "
                                         f"reproduces it and preflate could "
                                         f"not run"}
                    # Cut EVERY entry's payload out, not just the content.
                    # Contact-WTFE is 19 similar jpgs: cutting only the largest
                    # left a 2.5 MB skeleton for a 2.28 MB archive — bigger
                    # than the thing it describes. The small ones are carried
                    # as extras exactly as they are in a recipe capture, so
                    # what remains is only preflate's reconstruction data.
                    payloads = []
                    with zipfile.ZipFile(zp) as zf:
                        for e in ents:
                            payloads.append((e["name"], zf.read(e["name"])))
                    cut = self._pcf_cut(pcf, payloads)
                    if cut is None:
                        self._log("    ✗ preflate ran, but the content is not "
                                  "a contiguous run in its output — it cannot "
                                  "be cut back out.", "err")
                        return {"ok": False,
                                "error": f"{zp.name}: preflate output could "
                                         f"not be separated from the content"}
                    skel, holes = cut
                    # Prove it here, exactly as a recipe is proved: put the
                    # content back, restore, and byte-compare.
                    by_name = dict(payloads)
                    back = zip_assemble(skel, [[h[0], h[1]] for h in holes],
                                        {h[0]: by_name[h[2]] for h in holes})
                    restored = self._pcf_restore(back, work / "pf")
                    if restored != raw:
                        self._log("    ✗ preflate did not restore this archive "
                                  "byte-exact — refusing it.", "err")
                        return {"ok": False,
                                "error": f"{zp.name}: preflate did not restore "
                                         f"it byte-exact"}
                    key = f"zips/{zi_no}/preflate.bin"
                    embedded[key] = skel
                    self._db_learn_zip(_release_group(rel),
                                       {"impl": "preflate",
                                        "label": self.PREFLATE_LABEL})
                    self._log(f"    ✓ preflate reconstructs this archive "
                              f"byte-exact — carrying {len(skel):,} B of "
                              f"reconstruction data instead of a recipe.", "ok")
                    files = []
                    by_name2 = dict(payloads)
                    for e in ents:
                        rec = dict(e)
                        big = ((e["size"] or 0) >= biggest
                               or (cap and (e["size"] or 0) >= cap))
                        rec["source"] = "content" if big else "extra"
                        rec["recipe"] = {"impl": "preflate", "label": "preflate"}
                        if not big:
                            k = f"zips/{zi_no}/pf/{len(files)}.bin"
                            embedded[k] = by_name2[e["name"]]
                            rec["stored"] = k
                        files.append(rec)
                    total = len(skel) + sum(
                        len(v) for k, v in embedded.items()
                        if k.startswith(f"zips/{zi_no}/"))
                    if total >= len(raw):
                        # A release of nineteen similar jpgs has no small
                        # extras to carry cheaply — Contact-WTFE is exactly
                        # that. Promoting them to content was tried and is
                        # worse: it makes the .rsr a 59% copy of the archive
                        # AND demands all nineteen files back at rebuild. An
                        # honest refusal is the better answer; this shape is
                        # not what the format is for.
                        self._log(f"    ✗ preflate would carry {total:,} B for "
                                  f"a {len(raw):,} B archive — refusing, since "
                                  f"that is no better than keeping the "
                                  f"archive.", "err")
                        return {"ok": False,
                                "error": f"{zp.name}: preflate data is larger "
                                         f"than the archive"}
                    manifest["sets"].append({
                        "stem": zp.stem, "format": "ZIP", "name": zp.name,
                        "size": len(raw), "sha256": _sha256(raw),
                        "method": "preflate", "skeleton": key,
                        "holes": [[h[0], h[1]] for h in holes],
                        "hole_names": [h[2] for h in holes],
                        "files": files, "verify": "exact",
                    })
                    continue

                # Prove it: rebuild the archive from what we are about to store.
                check = {}
                for rec, e in zip(files, ents):
                    if rec["recipe"]["impl"] == "verbatim":
                        check[e["data_offset"]] = embedded[rec["stored"]]
                    elif rec["recipe"]["impl"] == "stored":
                        check[e["data_offset"]] = streams[e["data_offset"]]
                    else:
                        data = zlib.decompress(streams[e["data_offset"]], -15)
                        got = self._entry_stream(data, rec["recipe"],
                                                 e["name"], work / "tool")
                        if got is None:
                            return {"ok": False,
                                    "error": f"{zp.name}: recipe will not replay"}
                        check[e["data_offset"]] = got
                rebuilt = zip_assemble(skel, holes, check)
                if rebuilt != raw:
                    self._log(f"  ✗ {zp.name}: reassembly does not match the "
                              f"original ({len(rebuilt):,} vs {len(raw):,} B).",
                              "err")
                    return {"ok": False, "error": f"{zp.name}: reassembly differs"}
                self._log(f"    ✓ reassembled byte-identical "
                          f"({len(raw):,} B).", "ok")

                skey = f"zips/{zi_no}/skeleton.bin"
                embedded[skey] = skel
                manifest["sets"].append({
                    "stem": zp.stem, "format": "ZIP", "name": zp.name,
                    "size": len(raw), "sha256": _sha256(raw),
                    "skeleton": skey, "holes": holes, "files": files,
                    "verify": "exact",
                })

            manifest["sidecars"] = self._capture_sidecars(folder, s, embedded,
                                                          manifest)
            out_dir = self._store_dir(store, folder, rel)
            out_dir.mkdir(parents=True, exist_ok=True)
            rsr_path = out_dir / f"{rel}.rsr"
            self._write_rsr(rsr_path, manifest, embedded)
            self._db_record(manifest, rsr_path)
            self._db_forget(rel)
            self._log(f"  ✓ {rsr_path.name} written "
                      f"({rsr_path.stat().st_size:,} B) — VERIFIED", "ok")
            return {"ok": True, "recipe": "zip", "error": ""}
        finally:
            _rmtree(work)

    def _capture_metadata(self, folder: Path, store: Path, s: dict, rel: str,
                          files: list) -> dict:
        """Capture a release that is metadata only — a DIRFIX, NFOFIX and the
        like, where the nfo IS the release and there never was an archive.

        Writes a normal .rsr with no sets: nothing to reproduce, so nothing to
        verify, but the files are carried and the release is recorded as
        EXISTING rather than as a failed scan. A rebuild of one restores the
        folder exactly, because sidecar restoration is already set-independent.
        """
        tag = _fix_tag(rel)
        self._log(f"  Metadata-only release{f' ({tag})' if tag else ''} — "
                  f"{len(files)} file(s), no archive. Capturing the files and "
                  "recording it as complete.", "info")
        manifest = {
            "rsr_version": RSR_VERSION,
            "magic": RSR_MAGIC,
            "created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "tool": f"tosort_toolkit rsr_tool {RSR_VERSION}",
            "host": {"platform": platform.platform(),
                     "python": platform.python_version()},
            "release": rel,
            "system": _release_system(rel),
            "year": _release_year(folder, rel),
            "tag": _release_tag(folder.name),
            "source_folder": str(folder),
            "kind": "metadata",
            "fix": tag,
            "sets": [],
        }
        embedded: dict[str, bytes] = {}
        manifest["sidecars"] = self._capture_sidecars(folder, s, embedded,
                                                      manifest)
        if not manifest["sidecars"]:
            self._log("  Nothing could be carried (all files over the embed "
                      "cap?) — not writing a .rsr.", "warn")
            return {"ok": False, "error": "metadata release carried nothing"}

        out_dir = self._store_dir(store, folder, rel)
        out_dir.mkdir(parents=True, exist_ok=True)
        rsr_path = out_dir / f"{rel}.rsr"
        self._write_rsr(rsr_path, manifest, embedded)
        for name, data in embedded.items():
            if name.split("/")[0] not in ("extras", "sidecars"):
                continue
            dst = out_dir / name
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(data)
        self._db_record(manifest, rsr_path)
        self._db_forget(rel)
        self._log(f"  ✓ {rsr_path.name} written "
                  f"({rsr_path.stat().st_size:,} B) — METADATA ONLY", "ok")
        return {"ok": True, "metadata": True,
                "recipe": f"metadata only{f' ({tag})' if tag else ''}",
                "error": ""}

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
        # An archive this capture already describes is not a sidecar either.
        # _classify_volume only knows RAR naming, so a .zip release embedded
        # its own archive verbatim and the .rsr came out at 198% of the size
        # of the thing it was supposed to replace.
        captured = {st.get("name") for st in manifest.get("sets", [])
                    if st.get("name")}
        out: list[dict] = []
        for p in sorted(folder.rglob("*")):
            if not p.is_file() or _classify_volume(p.name):
                continue
            if p.name in captured:
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
        # The end-of-archive block of the last volume says whether ANOTHER
        # volume follows. A RARFIX release is exactly that: the one repaired
        # volume, shipped on its own, with the rest of the set living in the
        # release it fixes. Extracting it can only ever produce a truncated
        # source, which is a complete release reported as a damaged one.
        ends: list[int] = []

        def _end_cb(h):
            if getattr(h, "type", 0) == 0x7b:
                ends.append(int(getattr(h, "flags", 0) or 0))

        try:
            rf = rarfile.RarFile(str(head), info_callback=_end_cb)
        except Exception as e:
            if "first volume" in str(e).lower():
                # Started mid-set: the head volume is in another folder.
                return {"ok": False, "partial": True,
                        "error": "partial set: the first volume is missing"}
            raise
        try:
            infos = [i for i in rf.infolist() if i.is_file()]
            comment = rf.comment
        finally:
            rf.close()
        if ends and ends[-1] & 0x0001:
            return {"ok": False, "partial": True,
                    "error": "partial set: the archive continues into a "
                             "volume that is not in this folder"}
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
        total_src = sum(f["size"] or 0 for f in meta)
        # Say so BEFORE it happens. On a half-gigabyte source this step runs for
        # minutes and used to print nothing between the file listing and the
        # first sweep line, which reads exactly like a hang.
        self._log(f"    extracting {total_src / (1 << 20):,.0f} MB of source(s) "
                  f"from {len(vols)} volume(s)…", "dim")
        self._progress(f"extracting {total_src / (1 << 20):,.0f} MB — "
                       f"{st['stem']}")
        t_x = time.monotonic()
        ok_x = self._run([str(exes[-1]), "x", "-y", "-o+", str(head),
                          str(srcdir) + os.sep], timeout=3600,
                         heartbeat=f"extracting {total_src / (1 << 20):,.0f} MB "
                                   f"· {st['stem']}")
        self._log(f"    extracted in {time.monotonic() - t_x:,.0f}s", "dim")
        if self._skip.is_set() or self._stop.is_set():
            return {"ok": False, "error": "skipped"}
        order = [f["name"] for f in meta]
        src_files = [srcdir / n for n in order]
        # "The file exists" is not "the file is complete". A killed extract
        # leaves a partially written source behind, which passed the old
        # is_file() test and then went on to sweep against TRUNCATED bytes —
        # every combo mismatching, a wall reported for a release that was never
        # actually tested. Check the size the header declares, and treat a
        # non-clean exit as a failure in its own right.
        short = [f["name"] for f, p in zip(meta, src_files)
                 if not p.is_file() or p.stat().st_size != (f["size"] or 0)]
        if short or not ok_x:
            if self._skip.is_set() or self._stop.is_set():
                return {"ok": False, "error": "skipped"}
            detail = (f"{len(short)} file(s) wrong size: {', '.join(short[:3])}"
                      if short else "extract did not finish")
            self._log(f"    ✗ extraction incomplete — {detail}", "err")
            return {"ok": False, "error": f"extraction incomplete ({detail})"}

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
                # but its bytes stay out of the .rsr. Hash it anyway — that
                # hash is how a rebuild finds WHICH .rsr a loose rom belongs to,
                # and the name never can (a rom is named for the game, a .rsr
                # for the release).
                f["source"] = "content"
                self._progress(f"hashing {Path(f['name']).name} "
                               f"({(f['size'] or 0) / (1 << 20):,.0f} MB)")
                f["sha256"] = _file_sha256(sp)
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
        rel = _release_name(folder)
        grp = _release_group(rel)
        year = _release_year(folder, rel)
        year = int(year) if year.isdigit() else 0
        cands = self._dict_candidates(st["format"], dict_kb, s["dict_ladder"])
        # The sweep has to pack the way the original was packed, volumes and
        # all. Not for the volume boundaries — those are a header property the
        # replay fixes up — but because -v changes what rar DOES with a file it
        # cannot compress. Writing one archive, rar compresses, sees the result
        # grew, and rewrites the file as stored; writing volumes it is streaming
        # to a file it may already have closed, so the expanded stream stays.
        #
        # Measured on El_Profesor_Layton (a jpg that grew by 525 B): every one
        # of 2,585 real packs produced a STORED 1,416,837 B stream against a
        # target of 1,417,362, so the release was unreproducible at any build ×
        # any thread count. With -v5000000b, the same build that had already
        # matched the other two files matched all three. Nine hours of sweep
        # said "the exact build is outside the pack"; the build was combo #1.
        #
        # A byte-split set is one archive chopped up afterwards, so it takes no
        # -v — the same rule the replay follows.
        sweep_vol = 0 if st["byte_split"] or len(vols) == 1 \
            else vols[0].stat().st_size
        # Files at different methods mean successive `rar a` calls. Appending
        # is impossible once an archive is split, though — rar refuses to modify
        # a volume set — so a mixed-method VOLUMED set is a shape we cannot
        # replay, and saying so is better than sweeping a space with no answer.
        mgroups = _method_groups(meta)
        if len(mgroups) > 1 and sweep_vol:
            self._log("    ⚠ files at different methods AND volumes — an "
                      "archive cannot be appended to once it is split, so this "
                      "shape cannot be replayed. Sweeping as one command.",
                      "warn")
            mgroups = [(level, len(meta))]
        recipe = None
        budget = getattr(self, "_budget_min", 0)
        self._deadline = None if self._budget_override else (
            (time.monotonic() + budget * 60) if budget else None)
        for di, dkb in enumerate(cands):
            if self._stop.is_set() or self._skip.is_set():
                return {"ok": False,
                        "error": "skipped" if self._skip.is_set() else "stopped"}
            if di:
                self._log(f"    retrying with -md{dkb}KB "
                          f"(header dictionary didn't reproduce)", "dim")
            recipe = self._sweep_recipe(st["format"], exes, level, dkb, solid,
                                        src_files, targets, work, s["max_mt"],
                                        year, grp, self._deadline, rel,
                                        vol_bytes=sweep_vol,
                                        new_numbering=newnum, groups=mgroups)
            if recipe:
                break
            if self._budget_hit:
                break
        if not recipe:
            if self._budget_hit:
                return {"ok": False, "error": "time budget exceeded",
                        "set": {"stem": st["stem"], "format": st["format"],
                                "files": meta, "parked": True}}
            self._log("    ✗ no build × -mt reproduces these streams — the "
                      "exact build is outside the pack.", "err")
            return {"ok": False, "error": "recipe not found",
                    "set": {"stem": st["stem"], "format": st["format"],
                            "files": meta, "wall": True}}
        # Learn it NOW rather than at _db_record time: a recipe that was found
        # but whose replay later fails on a header detail is still the right
        # build for the NEXT release by that group, and that is the whole value
        # of the prior.
        self._db_learn(st["format"], level, grp, recipe)

        self._log(f"    ✓ RECIPE: {recipe['version']} "
                  f"{_mt_label(recipe['exe'], recipe['mt'])} "
                  f"(-m{level} -md{recipe['dict_kb']}KB "
                  f"{'-s' if solid else '-s-'}) — all {len(targets)} stream(s) "
                  "byte-exact.", "ok")

        # ── replay the whole set and byte-compare every volume ────────────
        # A byte-split set is ONE archive chopped up afterwards, so the replay
        # must not pass -v at all — the chunk boundaries are a property of the
        # splitter, not of RAR, and are restored from the volume records.
        vol_bytes = 0 if st["byte_split"] or len(vols) == 1 \
            else vols[0].stat().st_size
        # A recovery record is a block the replay has to be TOLD to make. The
        # percentage is not stored anywhere, but it is recoverable: the RR
        # covers the rest of the archive, so its share of it is the -rr value
        # that was asked for. _verify_replay corrects the guess if the volume
        # lengths come out wrong.
        rr_bytes = recovery_record(head)
        rr_pct = 0
        if rr_bytes:
            arch = sum(v.stat().st_size for v in vols)
            rr_pct = max(1, min(100, round(rr_bytes * 100
                                           / max(arch - rr_bytes, 1))))
            self._log(f"    recovery record: {rr_bytes:,} B (~{rr_pct}% of the "
                      "archive) — the replay will ask for one too.", "dim")
        recipe.update({"level": level, "solid": solid,
                       "volume_bytes": vol_bytes, "naming": st["scheme"],
                       "new_numbering": newnum, "rr_pct": rr_pct,
                       "byte_split": st["byte_split"],
                       "comment": bool(comment)})
        if len(mgroups) > 1:
            # Only when it means something. A single-group recipe replays
            # through the identical code path with no `groups` key at all, so
            # every .rsr written before today still rebuilds unchanged.
            recipe["groups"] = [list(g) for g in mgroups]
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
        elif verify == "failed":
            self._log("    ✗ the replay could not be run — nothing captured.",
                      "err")
        else:
            self._log("    ⚠ replay ran, but the volumes differ by more than a "
                      "header residual — captured as UNVERIFIED.", "warn")

        return {
            "ok": verify in ("exact", "delta"),
            "error": {"exact": "", "delta": "",
                      "failed": "replay could not be run"}.get(
                          verify, "replay unverified"),
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
        """The -md values worth trying, header value first.

        Only ever UPWARD. The dictionary recorded in the file header is the one
        the compressor actually used, and WinRAR can only ever clamp the
        requested -md DOWN (it shrinks the window to fit a small file, never
        grows it past what was asked). So the -md on the original command line
        was >= the header value, and every candidate below it is provably
        unreachable.

        The old ladder walked the whole pool in table order — for a RAR4 set
        whose header already reads 4096 KB (the format maximum, so the ONLY
        possible answer) that was six extra full sweeps of 232 builds x 17
        thread counts, each one recompressing the entire source, all of them
        incapable of matching. On Guitar_Rock_Tour…BAHAMUT — one 134 MB file —
        that is the difference between one failed sweep and seven."""
        if not ladder:
            return [primary]
        pool = RAR5_DICTS if fmt == "RAR5" else RAR4_DICTS
        return [primary] + sorted(d for d in pool if d > primary)

    # ── the sweep ─────────────────────────────────────────────────────────

    def _pack_args(self, ex: Path, fmt: str, level: int, dict_kb: int,
                   mt: int) -> list[str] | None:
        """The whole command up to the archive name, or None when this build
        cannot produce this archive at all.

        Includes -mt, rather than leaving the caller to append it: a build older
        than 3.60 has no such switch, and appending one unconditionally is how a
        third of the pack came to be untestable. Those builds are offered once,
        at the mt=0 slot, instead of failing 17 times."""
        is_r5 = bool(_R5_EXE.search(ex.name))
        if fmt == "RAR5":
            if not is_r5:
                return None                      # RAR4-era build can't write RAR5
            return [str(ex), "a", f"-m{level}", "-ma5", f"-md{dict_kb}k",
                    f"-mt{mt}"]
        if is_r5:
            # A RAR5 binary still emits RAR4 with -ma4 — and it is the only
            # way a RAR4 archive can carry -mt above 16.
            return [str(ex), "a", f"-m{level}", "-ma4", f"-md{dict_kb}k",
                    f"-mt{mt}"]
        letter = DICT_LETTER.get(dict_kb)
        pre = [str(ex), "a", f"-m{level}",
               f"-md{letter}" if letter else f"-md{dict_kb}"]
        if not _supports_mt(ex.name):
            # Single-threaded by construction: one command, so try it once and
            # let every other thread count fall through as a duplicate.
            return pre if mt == 0 else None
        if mt > RAR4_MT_CAP:
            return None
        return pre + [f"-mt{mt}"]

    def _pack_cmds(self, ex: Path, fmt: str, dict_kb: int, mt: int, solid: bool,
                   groups, srcs: list, target: Path, tail=(),
                   vol_args=()) -> list[list[str]] | None:
        """The command SEQUENCE that builds this archive — usually one command.

        `groups` is [(level, count)] over `srcs` in archive order. One entry is
        the ordinary case and produces exactly the command this used to build;
        more than one means the archive was assembled by successive `rar a`
        calls, which is the only way a set can hold files at different methods.

        Volume switches and the recovery record go on the LAST command only:
        -v cannot be combined with appending at all (rar refuses to modify a
        volume set), and an RR is written when the archive is finished."""
        cmds = []
        at = 0
        for gi, (level, count) in enumerate(groups):
            pre = self._pack_args(ex, fmt, level, dict_kb, mt)
            if pre is None:
                return None
            cmd = pre + ["-s" if solid else "-s-", "-ds", "-o+", "-y", "-ep",
                         "-idcd"]
            if gi == len(groups) - 1:
                cmd += list(vol_args) + list(tail)
            cmd += [str(target)] + [str(p) for p in srcs[at:at + count]]
            cmds.append(cmd)
            at += count
        return cmds or None

    @staticmethod
    def _build_rank(name: str, year: int):
        """Sort key for a build against the year the release was pred.

        A release cannot have been packed by a build that did not exist yet, so
        contemporary-or-older builds come first, nearest first; builds newer
        than the release follow, also nearest first. Ordering only — nothing is
        ever dropped, because the pre year comes from a folder prefix or a token
        in the name and neither is worth a false wall."""
        by = _exe_year(name)
        if not year or not by:
            return (2, 0, name)
        if by <= year:
            return (0, year - by, name)
        return (1, by - year, name)

    def _order_combos(self, exes, mts, fmt, level, grp, year):
        """Every (build, -mt) pair — exhaustively, but in the order most likely
        to hit first.

        Two priors, both learned rather than assumed:

        * what has already WON. A corpus is not a random sample of WinRAR
          history; it is a handful of groups who each used one packer for years.
          Across the first 45 captures here, 44 came from a single build
          (3.60 2005-11-21) and -mt2 beat -mt8 three to one — so the static
          MT_ORDER table, measured on a different corpus, was leading with the
          wrong thread count on nearly every release.
        * when the release happened, via _build_rank.

        Nothing is skipped: this returns the identical set of combinations the
        flat sweep did, so a wall found here is still a real wall. Only the
        order changes, and the order is the entire cost when the answer is
        found early."""
        by_name = {e.name: e for e in exes}
        order: list[tuple[Path, int]] = []
        seen: set[tuple[str, int]] = set()
        for exe_name, mt in self._hot_recipes(fmt, level, grp):
            ex = by_name.get(exe_name)
            if ex is None:
                continue
            # mt == -1 is an imported prior: the BUILD is known but the thread
            # count is not, so sweep that one build across every -mt before
            # touching the other 231. Seventeen combos rather than 3,944.
            for n in (mts if mt < 0 else [mt]):
                if n in mts and (exe_name, n) not in seen:
                    seen.add((exe_name, n))
                    order.append((ex, n))
        hot = len(order)
        ranked = sorted(exes, key=lambda e: self._build_rank(e.name, year))
        mts = self._mt_order(mts)

        # Then the tail, and its shape matters more than it looks: the tail is
        # what a release costs when the priors miss, which is precisely the
        # release that costs hours.
        #
        # Sweeping the whole pack at one thread count before trying a second
        # thread count on ANY build spends the first 232 combos on builds that
        # did not exist when the release was pred — 122 of them for a 2009
        # release. Measured over 204 captures: 94% were packed by a build OLDER
        # than themselves and 96% at -mt8 or -mt2, so contemporary builds at the
        # two likely thread counts are worth far more than the entire pack at
        # one. Same 3,944 combos, and the full product still follows as a
        # backstop so nothing is dropped — but the mean position of the winning
        # combo drops from 224 to 109 and the worst case from 1,987 to 827.
        groups = [ranked]
        if year:
            older = [e for e in ranked
                     if _exe_year(e.name) and _exe_year(e.name) <= year]
            if older and len(older) < len(ranked):
                rest = [e for e in ranked if e not in set(older)]
                groups = [older, rest]
        for group in groups:
            for mt in mts[:2]:
                for ex in group:
                    if (ex.name, mt) not in seen:
                        seen.add((ex.name, mt))
                        order.append((ex, mt))
        for mt in mts:
            for ex in ranked:
                if (ex.name, mt) not in seen:
                    seen.add((ex.name, mt))
                    order.append((ex, mt))
        return order, hot

    def _mt_order(self, mts: list[int]) -> list[int]:
        """Thread counts in the order they actually win HERE.

        MT_ORDER is a static table measured on another corpus, and it disagrees
        with this one: it puts -mt4 second, where these 204 captures give
        -mt8 147 wins, -mt2 48, -mt0 6 and -mt4 only 3. Learning it from the
        index costs one query and fixes the second-most-important dimension of
        the sweep the same way the recipe priors fixed the first.

        Anything unseen keeps its MT_ORDER place at the back, so a fresh index
        behaves exactly as before."""
        if not self._db_path.is_file():
            return mts
        try:
            con = self._db()
            try:
                won = [int(m) for m, in con.execute(
                    "SELECT mt FROM recipes WHERE mt>=0 GROUP BY mt "
                    "ORDER BY SUM(hits) DESC, MAX(last_used) DESC")]
            finally:
                con.close()
        except Exception:
            return mts
        lead = [m for m in won if m in mts]
        return lead + [m for m in mts if m not in lead] if lead else mts

    def _sweep_recipe(self, fmt, exes, level, dict_kb, solid, src_files,
                      targets, work, max_mt, year=0, grp="",
                      deadline=None, rel="", vol_bytes=0,
                      new_numbering=True, groups=None) -> dict | None:
        """Pack every file TOGETHER at each (build, -mt) and keep the combo
        whose streams match byte for byte.

        All files in one command, always: a set packed by a single `rar a`
        compresses each stream in context of the others, so hunting them in
        isolation — the thing rescene is forced to do — cannot reproduce them
        at any build or thread count. No family dedup either; fingerprinting
        one small file to skip builds is what manufactured false walls before,
        because a small file cannot discriminate builds that differ only on
        larger input.

        The sweep returns the moment a combo matches — it never runs to
        completion once it has its answer. Ordering is therefore the whole
        game, and _order_combos does that ordering."""
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

        combos, hot = self._order_combos(exes, mts, fmt, level, grp, year)
        if hot:
            self._log(f"    {hot} known recipe(s)"
                      + (f" for {grp}" if grp else "")
                      + " — trying those first.", "dim")

        sig = self._order_sig(combos)
        start, when = self._resume_point(rel, sig)
        if start >= len(combos):
            start = 0
        if start:
            self._log(f"    ▶ resuming after {start:,} combo(s) already tried"
                      + (f" on {when}" if when else "")
                      + f" — {len(combos) - start:,} left to check.", "ok")
            combos = combos[start:]

        # Its own directory: with -v a probe is 41 files, not one, and the head
        # volume is only called probe.rar under old numbering.
        probe_dir = work / "probe"
        vol_args = []
        if vol_bytes:
            vol_args = [f"-v{vol_bytes}b"] + ([] if new_numbering else ["-vn"])
            self._log(f"    packing in {vol_bytes:,} B volumes, as the original "
                      "was — rar treats an incompressible file differently when "
                      "it is streaming to volumes.", "dim")
        srcs = [str(p) for p in src_files]
        groups = list(groups or [(level, len(src_files))])
        if len(groups) > 1:
            self._log("    this set holds files at "
                      f"{len({g[0] for g in groups})} different methods "
                      f"({' then '.join(f'-m{lv}x{n}' for lv, n in groups)}) — "
                      "it was built by that many commands, and the sweep will "
                      "replay them in order.", "dim")
        tried = 0
        total = len(combos)
        t0 = last = time.monotonic()
        for ex, n in combos:
            if self._stop.is_set() or self._skip.is_set():
                return None
            # `tried` guard: always try at least one combo. Parking a release
            # having tested nothing is the worst of both worlds — it pays the
            # extract and the hashing and learns nothing, and the very first
            # combo is the group's best-known recipe, which is the one most
            # likely to just answer it.
            #
            # The override is read from self on every pass, not captured with
            # `deadline` at entry: the operator only learns what a release
            # costs from the per-combo line printed AFTER combo 1, so the
            # button has to be able to lift the limit while the sweep is
            # already running.
            if (tried and deadline and not self._budget_override
                    and time.monotonic() > deadline):
                self._budget_hit = True
                # Hand the position to _scan_run so the next run picks up here
                # instead of re-grinding the same prefix forever.
                self._sweep_pos = (start + tried, sig)
                self._log(f"    ⏱ time budget reached after {tried} combo(s) "
                          f"({start + tried:,} of {start + len(combos):,} "
                          "overall) — parking this release; the next run "
                          "resumes from here.", "warn")
                return None
            cmds = self._pack_cmds(ex, fmt, dict_kb, n, solid, groups, srcs,
                                   probe_dir / "probe.rar", vol_args=vol_args)
            if cmds is None:
                continue
            tried += 1
            # Throttle on TIME, not on a combo count. Every-8-combos was fine
            # when a combo was a fraction of a second, but one combo on a
            # 512 MB source is minutes, so the display sat unchanged for the
            # best part of half an hour and looked stopped.
            now = time.monotonic()
            if now - last > 0.5:
                last = now
                rate = tried / max(now - t0, 1e-6)
                self._progress(
                    f"sweep {tried}/{total} · -mt{n} · {_exe_label(ex.name)}"
                    + (f" · {rate * 60:,.0f}/min" if rate < 8 else "")
                    + (f" · {(deadline - now) / 60:,.0f} min left in budget"
                       if deadline else ""))
            probe_dir.mkdir(parents=True, exist_ok=True)
            for junk in probe_dir.iterdir():
                try:
                    junk.unlink()
                except OSError:
                    pass
            t_one = time.monotonic()
            ran = all(self._run(c, timeout=900,
                                heartbeat=f"sweep {tried}/{total} · -mt{n} · "
                                          f"{_exe_label(ex.name)}")
                      for c in cmds)
            if tried == 1:
                # Say up front what this release is going to cost. One combo
                # tells you whether an exhaustive sweep is minutes or weeks,
                # and that is worth knowing at combo 1 rather than hour 3.
                per = time.monotonic() - t_one
                allows = (int(max(deadline - t_one, 0) / max(per, 1e-6))
                          if deadline else None)
                msg = (f"    ~{per:,.1f}s per combo at this size — "
                       f"{total:,} would take {total * per / 3600:,.1f}h")
                if allows is not None:
                    msg += f"; budget allows about {allows:,}"
                self._log(msg, "dim")
                if allows is not None and allows < total:
                    # Name the shortfall rather than making it a subtraction
                    # the operator has to do in their head while it runs.
                    self._log(f"    ⏱ budget covers {allows:,} of {total:,} "
                              f"— {total - allows:,} short "
                              f"(~{(total - allows) * per / 60:,.0f} min more). "
                              f"Press '{UI_FINISH_ONE}' to lift it for this "
                              f"release.", "warn")
            if not ran:
                continue
            head = self._probe_head(probe_dir)
            if head is None:
                continue
            if self._streams_match(head, targets):
                return {"exe": ex.name, "version": _exe_label(ex.name),
                        "mt": n, "dict_kb": dict_kb, "tried": tried}
        # Count the resumed prefix too: "swept 4 combo(s)" after picking up
        # from 26 reads like a search that barely happened, when in fact the
        # whole space is now covered — which is exactly what makes the wall
        # that follows trustworthy.
        self._log(f"    swept {start + tried:,} combo(s) across {len(exes)} "
                  f"build(s) × -mt 0–{max_mt}"
                  + (f" ({tried:,} this run, {start:,} carried over)"
                     if start else "") + ".", "dim")
        return None

    @staticmethod
    def _probe_head(probe_dir: Path) -> Path | None:
        """First volume of whatever the probe pack produced.

        One archive is probe.rar; volumes are probe.rar + probe.r00… under old
        numbering and probe.part01.rar… under new. Picking the head by volume
        index rather than by name means the sweep does not have to care which,
        and a probe that produced nothing at all reads as None instead of as a
        mismatch."""
        try:
            made = [p for p in probe_dir.iterdir() if p.is_file()]
        except OSError:
            return None
        if not made:
            return None
        vols = [p for p in made if _classify_volume(p.name)]
        if vols:
            return min(vols, key=lambda p: _classify_volume(p.name)[2])
        single = probe_dir / "probe.rar"
        return single if single.is_file() else None

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

    def _replay_cmds(self, recipe: dict, target: Path, srcs: list[str],
                     comment_file: Path | None, fmt: str) -> list[list[str]] | None:
        """The command sequence that rebuilds this set — one entry, normally.

        A recipe carrying `groups` was assembled by successive `rar a` calls,
        so it replays as successive calls too. Everything that finishes the
        archive — volumes, recovery record, comment — belongs to the last one."""
        ex = self._app_dir / "apps" / "winrar_pack-4.20" / recipe["exe"]
        if not ex.is_file():
            return None
        groups = [tuple(g) for g in recipe.get("groups") or []]
        if not groups:
            groups = [(recipe["level"], len(srcs))]
        if sum(n for _, n in groups) != len(srcs):
            return None                    # recipe and sources disagree
        vol_args = []
        if recipe.get("volume_bytes"):
            vol_args = [f"-v{recipe['volume_bytes']}b"]
            if not recipe.get("new_numbering"):
                vol_args.append("-vn")     # .rar/.r00 rather than .partN.rar
        tail = []
        if recipe.get("rr_pct"):
            tail.append(f"-rr{recipe['rr_pct']}p")
        if comment_file:
            tail.append(f"-z{comment_file}")
        return self._pack_cmds(ex, fmt, recipe["dict_kb"], recipe["mt"],
                               recipe["solid"], groups, srcs, target,
                               tail=tail, vol_args=vol_args)

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
        cmds = self._replay_cmds(recipe, out / "replay.rar",
                                 [str(p) for p in src_files], cfile, fmt)
        if not cmds:
            return None
        mb = sum(p.stat().st_size for p in src_files if p.is_file()) / (1 << 20)
        for c in cmds:
            if not self._run(c, timeout=3600,
                             heartbeat=f"replaying {recipe['version']} "
                                       f"{_mt_label(recipe.get('exe', ''), recipe['mt'])}"
                                       f" over {mb:,.0f} MB"):
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
        # The -rr percentage was derived from the block size, which is rounded
        # to whole sectors — so a set can sit between two percentages. If the
        # lengths come out wrong, walk the neighbours rather than throwing away
        # a recipe whose streams were all byte-exact.
        if produced and recipe.get("rr_pct") and not st["byte_split"]:
            want = sum(v.stat().st_size for v in vols)
            if sum(p.stat().st_size for p in produced) != want:
                for pct in (1, 2, 3, 4, 5, 10):
                    if pct == recipe["rr_pct"]:
                        continue
                    alt = self._replay(dict(recipe, rr_pct=pct), src_files,
                                       work, comment, st["format"])
                    if alt and sum(p.stat().st_size for p in alt) == want:
                        self._log(f"    recovery record is -rr{pct}p, not "
                                  f"-rr{recipe['rr_pct']}p — corrected.", "dim")
                        recipe["rr_pct"] = pct
                        produced = alt
                        break
        if not produced:
            # Distinct from "the bytes differ": the replay command itself did
            # not deliver. Reported identically, this cost an hour of hunting a
            # phantom compression difference on Transformers_Prime, whose
            # recipe was in fact perfect — rar.exe had deadlocked on an
            # undrained stdout pipe and been killed at the timeout.
            self._log("    ✗ the replay command produced no volumes at all "
                      "(rar did not finish) — this is NOT a wrong recipe.",
                      "err")
            for v in vols:
                volmeta.append({"name": v.name, "size": v.stat().st_size,
                                "sha256": _file_sha256(v), "delta": None})
            return "failed", volmeta, {}

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
        # Validate the STRING before it becomes a Path: Path("") is Path("."),
        # whose str() is "." and therefore truthy, so an empty Output box used
        # to sail through this check and write the rebuilt volumes into the
        # working directory.
        out_s = (cfg or {}).get("out", "").strip()
        if not out_s:
            return {"ok": False, "error": "Output folder required"}
        rsr = Path((cfg or {}).get("rsr", "").strip())
        content = Path((cfg or {}).get("content", "").strip())
        out = Path(out_s)
        if not rsr.is_file():
            return {"ok": False, "error": ".rsr file not found"}
        if not content.is_dir():
            return {"ok": False, "error": "Content folder not found"}

        def _bg():
            self._running = True
            self._stop.clear()
            self._size_map_cache = {}      # the folder may have changed
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

    # ── find the .rsr for a file, by hash ─────────────────────────────────

    def find_rsr(self, path: str) -> dict:
        """Which captured release is this file the content of?

        By hash, never by name. A rom on disk is named for the game and a .rsr
        for the release, so `Guitar Rock Tour.nds` and
        `Guitar_Rock_Tour_EUR_MULTi6_NDS-BAHAMUT.rsr` share nothing to match on
        — but the CRC32 in the RAR file header is exactly the content's hash,
        and it is already indexed. size+CRC32 is the lookup (that pair IS what
        the scene verifies with); a stored SHA-256 confirms it when the capture
        was recent enough to have one."""
        p = Path(path)
        if not p.is_file():
            return {"ok": False, "error": "not a file"}
        return self._match_content(p)

    def _match_content(self, p: Path) -> dict:
        if not self._db_path.is_file():
            return {"ok": False, "error": "no index yet"}
        size = p.stat().st_size
        crc = _file_crc32(p)
        con = self._db()
        try:
            rows = con.execute(
                "SELECT f.release, f.name, f.sha256, r.rsr_path, r.verified "
                "FROM files f JOIN releases r ON r.name = f.release "
                "WHERE f.size=? AND f.crc32=? AND f.source='content'",
                (size, crc)).fetchall()
        finally:
            con.close()
        if not rows:
            return {"ok": False, "error": "no match",
                    "crc32": f"{crc:08X}", "size": size}
        if len(rows) > 1:
            # Same rom in two releases (a P2P/scene dupe, or a re-pre). Settle
            # it on SHA-256 where we have one, otherwise report the ambiguity
            # rather than picking blind.
            sha = _file_sha256(p)
            exact = [r for r in rows if r[2] == sha]
            if exact:
                rows = exact
        rel, name, sha256, rsr_path, verified = rows[0]
        return {"ok": True, "release": rel, "packed_name": name,
                "rsr": rsr_path, "verified": bool(verified),
                "crc32": f"{crc:08X}", "size": size,
                "ambiguous": [r[0] for r in rows[1:]] if len(rows) > 1 else []}

    # ── batch rebuild ─────────────────────────────────────────────────────

    def rebuild_batch_start(self, cfg: dict) -> dict:
        """Point at a folder of loose content; every file that hashes to a
        captured release is rebuilt into its own folder under the output."""
        if self._running:
            return {"ok": False, "error": "Already running"}
        out_s = (cfg or {}).get("out", "").strip()      # see rebuild_start
        if not out_s:
            return {"ok": False, "error": "Output folder required"}
        root = Path((cfg or {}).get("content", "").strip())
        out = Path(out_s)
        if not root.is_dir():
            return {"ok": False, "error": "Content folder not found"}
        if out.resolve() == root.resolve():
            # Rebuilt volumes landing in the folder being scanned would be
            # re-walked as candidates on the next run.
            return {"ok": False,
                    "error": "Output must not be the content folder"}
        if not self._db_path.is_file():
            return {"ok": False, "error": "No .rsr index yet — capture first"}
        delete_content = bool((cfg or {}).get("delete_content"))

        def _bg():
            self._running = True
            self._stop.clear()
            self._size_map_cache = {}
            try:
                self._rebuild_batch_run(root, out, delete_content)
            except Exception as e:
                self._log(f"Batch rebuild error: {e}", "err")
                self._log(traceback.format_exc(), "dim")
            finally:
                self._running = False
                self._progress("")
                self._emit("scan_done", {})

        threading.Thread(target=_bg, daemon=True).start()
        return {"ok": True, "started": True}

    # Files that are never release content — no point hashing a 2 KB nfo
    # against an index of roms.
    _NOT_CONTENT = {".nfo", ".sfv", ".diz", ".txt", ".jpg", ".jpeg", ".png",
                    ".rsr", ".srr", ".srs", ".md5", ".sha1", ".log"}

    def _content_sizes(self) -> set[int]:
        """Every file size the index knows as content."""
        con = self._db()
        try:
            return {int(r[0]) for r in con.execute(
                "SELECT DISTINCT size FROM files WHERE source='content'")}
        finally:
            con.close()

    def _rebuild_batch_run(self, root: Path, out: Path,
                           delete_content: bool = False):
        self._log("══ BATCH REBUILD ══", "info")
        self._content_root = root
        freed = 0
        if delete_content:
            self._log("  DELETE SOURCES is ON — an unpacked file will be "
                      "removed once its release has rebuilt and every volume "
                      "has been hash-verified. The rebuilt archives are never "
                      "touched.", "warn")
        # Size first, hash second. A stat() is free and a CRC32 is a full read,
        # so pointing this at a whole drive should not mean reading a whole
        # drive: nothing can match unless its size matches a captured content
        # file exactly, and that check throws out everything that isn't even a
        # candidate before a single byte is read.
        sizes = self._content_sizes()
        looked = skipped_size = 0
        cands = []
        for p in sorted(root.rglob("*")):
            if not p.is_file() or p.suffix.lower() in self._NOT_CONTENT:
                continue
            looked += 1
            if p.stat().st_size in sizes:
                cands.append(p)
            else:
                skipped_size += 1
        self._log(f"  {looked} file(s) under {root}; {len(cands)} match a "
                  f"captured content size, {skipped_size} cannot match at all.",
                  "dim")
        self._log("  matching by size + CRC32 against the .rsr index", "dim")

        matched, done, failed, miss = {}, 0, 0, 0
        for i, p in enumerate(cands, 1):
            if self._stop.is_set():
                self._log("Stopped.", "warn")
                return
            self._progress(f"hashing {i}/{len(cands)} · {p.name}")
            hit = self._match_content(p)
            if not hit.get("ok"):
                miss += 1
                self._emit("row", {"name": p.name, "status": "skipped",
                                   "recipe": hit.get("error", "no match"),
                                   "kind": "nomatch"})
                continue
            rel = hit["release"]
            if rel in matched:
                continue                      # one release, one rebuild
            matched[rel] = (p, hit)
        self._progress("")
        self._log(f"  {len(matched)} release(s) matched, {miss} file(s) with no "
                  "entry in the index.", "ok" if matched else "warn")

        for i, (rel, (p, hit)) in enumerate(sorted(matched.items()), 1):
            if self._stop.is_set():
                self._log("Stopped.", "warn")
                break
            self._log("", "")
            self._log(f"══ [{i}/{len(matched)}] {rel} ══", "info")
            self._log(f"  matched {p.name}  CRC={hit['crc32']}  "
                      f"{hit['size']:,} B", "dim")
            if hit.get("ambiguous"):
                self._log(f"  ⚠ that content also appears in: "
                          f"{', '.join(hit['ambiguous'][:3])}", "warn")
            rsr = Path(hit["rsr"])
            if not rsr.is_file():
                self._log(f"  ✗ indexed .rsr is missing from the store: {rsr}",
                          "err")
                self._emit("row", {"name": rel, "status": "error",
                                   "recipe": ".rsr missing", "kind": "error"})
                failed += 1
                continue
            self._emit("row", {"name": rel, "status": "running",
                               "kind": "running"})
            self._consumed = []
            try:
                # The file's own folder is the content root — a rebuild reads
                # only the sources its manifest names, so a folder holding more
                # than this release is fine.
                res = self._rebuild_run(rsr, p.parent, out / rel)
            except Exception as e:
                self._log(f"  ERROR: {e}", "err")
                self._log(traceback.format_exc(), "dim")
                res = {"ok": False}
            if res.get("ok"):
                done += 1
                if delete_content:
                    freed += self._delete_consumed(out)
                self._emit("row", {"name": rel, "status": "done",
                                   "recipe": "rebuilt", "kind": "ok"})
            else:
                failed += 1
                self._emit("row", {"name": rel, "status": "error",
                                   "recipe": "rebuild failed", "kind": "error"})

        self._log("", "")
        self._log(f"Batch rebuild complete — {done} rebuilt, {failed} failed, "
                  f"{miss} unmatched file(s)."
                  + (f" {_human_bytes(freed)} of unpacked sources deleted."
                     if freed else ""), "ok" if not failed else "warn")

    def _delete_consumed(self, out: Path) -> int:
        """Delete the content files this release was rebuilt FROM. Bytes freed.

        Only ever called after a rebuild reported ok, which means every volume
        was hash-compared against the manifest — so the rar set on disk
        provably contains these bytes and the loose copy is redundant.

        Conservative on purpose, because this is the one irreversible thing the
        tool does:
          * only paths `_rebuild_set` actually opened, never a folder sweep;
          * never anything under the output folder, so a rebuilt volume can
            never be mistaken for a source;
          * the file is re-hashed against what the manifest said before it goes
            — if it changed under us since the rebuild read it, it is not the
            file we verified and it stays;
          * a now-empty parent goes too, but never the content root itself.
        """
        freed = 0
        try:
            out_res = out.resolve()
        except OSError:
            return 0
        for src in dict.fromkeys(self._consumed):     # de-dupe, keep order
            try:
                if not src.is_file():
                    continue
                if src.resolve() == out_res or out_res in src.resolve().parents:
                    self._log(f"    ⚠ refusing to delete {src.name}: it is "
                              "inside the output folder.", "warn")
                    continue
                size = src.stat().st_size
                src.unlink()
                freed += size
                self._log(f"    🗑 deleted source {src.name} "
                          f"({_human_bytes(size)}) — it is inside the "
                          "rebuilt rar set now.", "dim")
                parent = src.parent
                if parent != self._content_root and not any(parent.iterdir()):
                    parent.rmdir()
                    self._log(f"    🗑 removed empty folder {parent.name}", "dim")
            except OSError as e:
                self._log(f"    ⚠ could not delete {src.name}: {e}", "warn")
        return freed

    def _rebuild_zip(self, st: dict, z, content: Path, out: Path,
                     work: Path) -> bool:
        """Re-deflate each entry, drop it back into the skeleton, compare."""
        name = st.get("name") or f"{st.get('stem')}.zip"
        if st.get("method") == "preflate":
            return self._rebuild_preflate(st, z, content, out, work, name)
        self._log(f"  {name}: {len(st['files'])} entr(y/ies) into "
                  f"{st['size']:,} B of archive", "dim")
        skel = z.read(st["skeleton"])
        streams = {}
        for f in st["files"]:
            rec = f.get("recipe") or {}
            impl = rec.get("impl")
            off = f["data_offset"]
            if impl == "verbatim":
                streams[off] = z.read(f["stored"])
                continue
            src = self._source_by_hash(content, f)
            if src is None:
                self._log(f"    ✗ missing source: {f['name']} "
                          f"({f['size']:,} B, CRC {f['crc32']:08X})", "err")
                return False
            data = src.read_bytes()
            if impl == "stored":
                streams[off] = data
                continue
            got = self._entry_stream(data, rec, f["name"], work / "tool")
            if got is None:
                self._log(f"    ✗ {f['name']}: cannot replay {rec.get('label')}",
                          "err")
                return False
            streams[off] = got
        rebuilt = zip_assemble(skel, st["holes"], streams)
        if _sha256(rebuilt) != st["sha256"]:
            self._log(f"    ✗ {name}: rebuilt archive does not match "
                      f"({len(rebuilt):,} vs {st['size']:,} B).", "err")
            return False
        dst = out / st.get("folder", "") / name
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(rebuilt)
        self._log(f"    ✓ {name}  {len(rebuilt):,} B — hash-exact.", "ok")
        return True

    def _rebuild_preflate(self, st: dict, z, content: Path, out: Path,
                          work: Path, name: str) -> bool:
        """Put the content back into preflate's output and let it re-deflate.

        No recipe to replay: preflate recorded how the original encoder behaved,
        so the archive comes back byte-exact without anyone knowing which zipper
        made it."""
        if self._precomp_exe() is None:
            self._log("    ✗ this release needs preflate "
                      "(apps/zip_pack/precomp) and it is not here.", "err")
            return False
        holes = [list(h) for h in st.get("holes", [])]
        names = list(st.get("hole_names", []))
        streams = {}
        for (off, ln), nm in zip(holes, names):
            f = next((x for x in st["files"] if x["name"] == nm), None)
            if f is None:
                self._log(f"    ✗ {nm}: not described in the manifest.", "err")
                return False
            if f.get("stored"):
                data = z.read(f["stored"])
                if len(data) != ln:
                    self._log(f"    ✗ {nm}: carried {len(data):,} B, expected "
                              f"{ln:,}.", "err")
                    return False
                streams[off] = data
                continue
            src = self._source_by_hash(content, f)
            if src is None:
                self._log(f"    ✗ missing source: {nm} ({f['size']:,} B, "
                          f"CRC {f['crc32']:08X})", "err")
                return False
            data = src.read_bytes()
            if len(data) != ln:
                self._log(f"    ✗ {nm}: {len(data):,} B, expected {ln:,}.",
                          "err")
                return False
            streams[off] = data
        pcf = zip_assemble(z.read(st["skeleton"]), holes, streams)
        rebuilt = self._pcf_restore(pcf, work / "pf")
        if rebuilt is None:
            self._log("    ✗ preflate could not restore this archive.", "err")
            return False
        if _sha256(rebuilt) != st["sha256"]:
            self._log(f"    ✗ {name}: restored archive does not match "
                      f"({len(rebuilt):,} vs {st['size']:,} B).", "err")
            return False
        dst = out / st.get("folder", "") / name
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(rebuilt)
        self._log(f"    ✓ {name}  {len(rebuilt):,} B — restored by preflate, "
                  f"hash-exact.", "ok")
        return True

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
                    if st.get("format") == "ZIP":
                        ok_all &= self._rebuild_zip(st, z, content, out, work)
                        continue
                    if not st.get("recipe"):
                        self._log(f"  {st.get('stem')}: no recipe captured — "
                                  "cannot rebuild this set.", "err")
                        ok_all = False
                        continue
                    ok_all &= self._rebuild_set(st, manifest, z, content, out, work)
                self._restore_sidecars(manifest, z, out)
                self._restore_damaged(manifest, z, out)
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
        self._log(f"  {stem}: replaying {recipe['version']} "
                  f"{_mt_label(recipe.get('exe', ''), recipe['mt'])} "
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
                    # Name lookup failed — find it by CONTENT instead. The
                    # packed name is a scene abbreviation (`tg-tg.nds`) and the
                    # operator's copy is named for the game, so in batch mode
                    # the names essentially never agree; matching the .rsr by
                    # hash and then demanding a filename match would make the
                    # hash lookup pointless.
                    found = self._source_by_hash(content, f)
                if not found or not Path(found).is_file():
                    self._log(f"    ✗ missing source: {base} "
                              f"({f['size']:,} B, CRC={f['crc32']:08X}) — no "
                              "file of that name or that content in the "
                              "content folder.", "err")
                    return False
                if Path(found).name != base:
                    self._log(f"    · {base} ← {Path(found).name} "
                              "(matched on CRC32)", "dim")
                # Remember exactly which file on disk supplied this packed
                # file. The optional delete-after-rebuild works off THIS list
                # and nothing else — never a folder sweep, never the match the
                # batch runner happened to hash first.
                self._consumed.append(Path(found))
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
            # Prefer what the ARCHIVE recorded over what the loose copy in the
            # release folder happened to have — the archive's value is the one
            # that has to come back out in the header. Only for a Windows-host
            # archive: elsewhere that field is a Unix mode, not a DWORD.
            attrs = f.get("win_attrs")
            if str(f.get("host_os", "")).lower().startswith("win") and f.get("attrs"):
                attrs = f["attrs"]
            _set_win_attrs(dst, attrs)
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
                dst = out / v.get("folder", "") / v["name"]
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes(chunk)
                self._log(f"    ✓ {v.get('folder', '')}{'/' if v.get('folder') else ''}"
                          f"{v['name']}  {v['size']:,} B", "dim")
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
            dst = out / v.get("folder", "") / v["name"]
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(data)
        self._log(f"    ✓ {len(vols)} volume(s) rebuilt, every one hash-exact.",
                  "ok")

        self._restore_extras(st, z, out)
        return True

    def _source_by_hash(self, content: Path, f: dict) -> Path | None:
        """The file in `content` whose bytes ARE this packed file, whatever it
        happens to be called.

        Size narrows it for free, so only genuine candidates are ever read. The
        per-folder size map is cached because a multi-set release asks for this
        once per set and re-walking a rom library each time would be silly."""
        want_size = f.get("size")
        want_crc = f.get("crc32")
        if not want_size or want_crc is None:
            return None
        key = str(content)
        cache = getattr(self, "_size_map_cache", None)
        if cache is None:
            cache = self._size_map_cache = {}
        if key not in cache:
            sizes: dict[int, list[Path]] = {}
            for p in content.rglob("*"):
                try:
                    if p.is_file() and p.suffix.lower() not in self._NOT_CONTENT:
                        sizes.setdefault(p.stat().st_size, []).append(p)
                except OSError:
                    continue
            cache[key] = sizes
        for p in cache[key].get(want_size, []):
            try:
                if _file_crc32(p) == want_crc:
                    return p
            except OSError:
                continue
        return None

    def _restore_damaged(self, manifest, z, out: Path):
        """Put back the volume that failed its .sfv, byte for byte.

        The repaired set is the useful thing, but the release as DISTRIBUTED
        had the bad file in it. Anyone who wants the folder as it actually was
        gets it; the good copy lives in the fix folder, so there is no clash."""
        n = 0
        for f in manifest.get("damaged", []):
            if not f.get("stored"):
                self._log(f"    ! {f['name']} was damaged and not carried "
                          f"(too big) — the rebuilt folder has the GOOD copy "
                          f"only.", "warn")
                continue
            data = z.read(f["stored"])
            if f.get("sha256") and _sha256(data) != f["sha256"]:
                self._log(f"    ✗ damaged {f['name']}: stored bytes do not "
                          "match the captured hash.", "err")
                continue
            dst = out / f.get("folder", "") / f["name"]
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(data)
            n += 1
        if n:
            self._log(f"    ✓ {n} damaged volume(s) restored as they were "
                      f"(they fail the .sfv on purpose).", "ok")

    def _restore_sidecars(self, manifest, z, out: Path):
        """Put the .nfo / .sfv / proof back beside the rebuilt volumes, with the
        timestamps and attributes they were captured with — a release folder
        without its nfo is not the release."""
        n = 0
        for f in manifest.get("sidecars", []):
            if not f.get("stored"):
                continue
            dst = out / f.get("folder", "") / f["name"]
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
        if "kind" not in {r[1] for r in con.execute(
                "PRAGMA table_info(releases)")}:
            # 'archive' (the normal case) or 'metadata' (a DIRFIX/NFOFIX and
            # friends, which have no archive to reproduce). Without it a
            # metadata release looks like an archive release that captured
            # nothing — exactly the confusion this path exists to remove.
            con.execute("ALTER TABLE releases ADD COLUMN kind TEXT "
                        "DEFAULT 'archive'")
        if "tag" not in {r[1] for r in con.execute(
                "PRAGMA table_info(releases)")}:
            # 'NUKED', 'PROPER', ... — stripped from the name so the store key
            # is the real release, but kept because it is real information.
            con.execute("ALTER TABLE releases ADD COLUMN tag TEXT DEFAULT ''")
        con.execute("""CREATE TABLE IF NOT EXISTS files(
            release TEXT, set_stem TEXT, name TEXT, size INT, packed_size INT,
            crc32 INT, sha256 TEXT, method INT, source TEXT)""")
        con.execute("CREATE INDEX IF NOT EXISTS ix_files_name ON files(name)")
        con.execute("CREATE INDEX IF NOT EXISTS ix_files_crc ON files(crc32)")
        con.execute("CREATE INDEX IF NOT EXISTS ix_files_rel ON files(release)")
        con.execute("CREATE INDEX IF NOT EXISTS ix_files_size ON files(size)")
        # The same idea for ZIP: which deflate reproduced a group's streams.
        # A ZIP sweep is 405 zlib settings plus seventeen zippers, all on the
        # rom — the most expensive entry in the release — so knowing that this
        # group zips with "zlib -9 mem8" turns that into one attempt.
        con.execute("""CREATE TABLE IF NOT EXISTS zip_recipes(
            grp TEXT, label TEXT, impl TEXT, level INT, mem INT, strategy INT,
            hits INT DEFAULT 0, last_used TEXT,
            PRIMARY KEY(grp, label))""")
        # What has actually won, so the next sweep can lead with it. Keyed on
        # the GROUP: the first cut of this keyed on `system`, which is "NDS"
        # for every release in an NDS corpus and therefore discriminated
        # nothing at all.
        con.execute("""CREATE TABLE IF NOT EXISTS recipes(
            fmt TEXT, level INT, grp TEXT, exe TEXT, mt INT,
            hits INT DEFAULT 0, last_used TEXT,
            PRIMARY KEY(fmt, level, grp, exe, mt))""")
        cols = {r[1] for r in con.execute("PRAGMA table_info(recipes)")}
        if "grp" not in cols:
            # Built by an earlier version keyed on `system`. Those rows cannot
            # be re-keyed (the group was never stored), but every one of them
            # is re-derivable from `releases`, so drop and re-seed rather than
            # carry a table that answers the wrong question.
            con.execute("DROP TABLE recipes")
            con.execute("""CREATE TABLE recipes(
                fmt TEXT, level INT, grp TEXT, exe TEXT, mt INT,
                hits INT DEFAULT 0, last_used TEXT,
                PRIMARY KEY(fmt, level, grp, exe, mt))""")
            self._seeded = False
        # ...and what has already been proven unreachable, so a resumed scan
        # does not re-grind it. A release that walls writes no .rsr, so
        # skip_done cannot see it and every re-run pays the FULL sweep for it
        # again — with a time budget attached, that is days across a corpus.
        con.execute("""CREATE TABLE IF NOT EXISTS misses(
            release TEXT PRIMARY KEY, kind TEXT, reason TEXT,
            seen TEXT, combos INT, builds INT)""")
        if "order_sig" not in {r[1] for r in con.execute(
                "PRAGMA table_info(misses)")}:
            # Which combo ORDER the recorded progress belongs to. Priors change
            # as the corpus is learned, so a raw offset is only meaningful
            # against the order it was measured in.
            con.execute("ALTER TABLE misses ADD COLUMN order_sig TEXT")
        # Backfill the priors from captures made before this table existed —
        # 45 releases already know the answer, and re-earning it one slow sweep
        # at a time would be silly.
        if not self._seeded:
            self._seeded = True
            try:
                if not con.execute("SELECT 1 FROM recipes LIMIT 1").fetchone():
                    seen: dict[tuple, list] = {}
                    for name, fmt, lvl, exe, mt, created in con.execute(
                            "SELECT name, format, level, recipe_exe, mt, created"
                            " FROM releases WHERE recipe_exe<>'' AND mt>=0 "
                            "AND level>=0"):
                        k = (fmt, int(lvl), _release_group(name), exe, int(mt))
                        row = seen.setdefault(k, [0, ""])
                        row[0] += 1
                        row[1] = max(row[1], created or "")
                    con.executemany(
                        "INSERT OR IGNORE INTO recipes(fmt, level, grp, exe, mt,"
                        " hits, last_used) VALUES(?,?,?,?,?,?,?)",
                        [(*k, v[0], v[1]) for k, v in seen.items()])
                    con.commit()
            except Exception:
                pass
        return con

    def _db_learn_zip(self, grp: str, recipe: dict):
        """Record a deflate that reproduced a stream, so the group's next
        release leads with it."""
        if not recipe or recipe.get("impl") in (None, "stored", "verbatim"):
            return
        try:
            con = self._db()
            try:
                con.execute(
                    "INSERT INTO zip_recipes(grp, label, impl, level, mem, "
                    "strategy, hits, last_used) VALUES(?,?,?,?,?,?,1,?) "
                    "ON CONFLICT(grp, label) DO UPDATE SET hits = hits + 1, "
                    "last_used = excluded.last_used",
                    (grp or "", recipe.get("label", ""), recipe.get("impl", ""),
                     int(recipe.get("level", -1) or -1),
                     int(recipe.get("mem", -1) or -1),
                     int(recipe.get("strategy", 0) or 0),
                     datetime.now(timezone.utc).isoformat(timespec="seconds")))
                con.commit()
            finally:
                con.close()
        except Exception as e:
            self._log(f"      (could not record zip prior: {e})", "dim")

    def _seed_zip_priors(self) -> int:
        """Backfill the ZIP priors from captures already in the store.

        Learning happens when a stream is proved, so with "skip captured" on
        — which is the normal way to run — an established store teaches the
        table nothing: every release it could learn from is skipped before the
        sweep. The recipes are already sitting in those manifests, so read them
        once instead of waiting for the corpus to be captured a second time."""
        if not self._db_path.is_file():
            return 0
        try:
            con = self._db()
            try:
                if con.execute("SELECT 1 FROM zip_recipes LIMIT 1").fetchone():
                    return 0
                rows = con.execute(
                    "SELECT name, rsr_path FROM releases WHERE kind='zip'"
                ).fetchall()
            finally:
                con.close()
        except Exception:
            return 0
        n = 0
        for name, path in rows:
            try:
                manifest, z = self.read_rsr(Path(path))
                z.close()
            except Exception:
                continue
            grp = _release_group(name)
            for st in manifest.get("sets", []):
                for f in st.get("files", []):
                    r = f.get("recipe") or {}
                    if r.get("impl") in ("zlib", "tool"):
                        self._db_learn_zip(grp, r)
                        n += 1
        if n:
            self._log(f"  Seeded {n} deflate prior(s) from the store.", "dim")
        return n

    PREFLATE_LABEL = "preflate"

    def _zip_prefers_preflate(self, grp: str) -> bool:
        """Has this group needed preflate before?

        A group zips the way it zips. If no setting has ever reproduced its
        streams, the next release will not be different — and finding that out
        costs a full grid every time: 33 s on Bobs_Game, and the prefix probe
        cannot even help below 4 MB, where all 405 settings compress in full.
        Across three hundred releases that is hours spent reaching a foregone
        conclusion."""
        if not grp or not self._db_path.is_file():
            return False
        try:
            con = self._db()
            try:
                r = con.execute(
                    "SELECT hits FROM zip_recipes WHERE grp=? AND label=?",
                    (grp, self.PREFLATE_LABEL)).fetchone()
                other = con.execute(
                    "SELECT COUNT(*) FROM zip_recipes WHERE grp=? AND label<>?",
                    (grp, self.PREFLATE_LABEL)).fetchone()
            finally:
                con.close()
        except Exception:
            return False
        # Only when preflate is the ONLY thing that has ever worked for them.
        # A group with a real recipe keeps using it — a recipe is smaller and
        # needs nothing installed at rebuild.
        return bool(r) and not (other and other[0])

    def _zip_hot(self, grp: str) -> list[dict]:
        """Deflates worth trying before the grid: this group's first, then
        whatever has won anywhere. Widening rings, exactly as for RAR."""
        if not self._db_path.is_file():
            return []
        out, seen = [], set()
        try:
            con = self._db()
            try:
                for where, args in ((" WHERE grp=?", (grp,)), ("", ())):
                    if where and not grp:
                        continue
                    for label, impl, lvl, mem, strat in con.execute(
                            "SELECT label, impl, level, mem, strategy FROM "
                            "zip_recipes" + (where or " WHERE 1=1") +
                            " AND impl<>'preflate'" +
                            " ORDER BY hits DESC, last_used DESC LIMIT 12",
                            args):
                        if label in seen:
                            continue
                        seen.add(label)
                        out.append({"impl": impl, "level": lvl, "mem": mem,
                                    "strategy": strat, "label": label})
            finally:
                con.close()
        except Exception:
            return out
        return out

    def _db_learn(self, fmt: str, level: int, grp: str, recipe: dict):
        """Record a winning (build, -mt) so later releases try it first."""
        try:
            con = self._db()
            try:
                con.execute(
                    "INSERT INTO recipes(fmt, level, grp, exe, mt, hits, "
                    "last_used) VALUES(?,?,?,?,?,1,?) "
                    "ON CONFLICT(fmt, level, grp, exe, mt) DO UPDATE SET "
                    "hits = hits + 1, last_used = excluded.last_used",
                    (fmt, int(level), grp or "", recipe["exe"],
                     int(recipe["mt"]),
                     datetime.now(timezone.utc).isoformat(timespec="seconds")))
                con.commit()
            finally:
                con.close()
        except Exception as e:                      # never fail a capture on it
            self._log(f"    (could not record recipe prior: {e})", "dim")

    def _db_miss(self, rel: str, kind: str, reason: str, builds: int = 0,
                 combos: int = 0, order_sig: str = ""):
        """Record a release that produced no .rsr, and WHY.

        'wall' means the sweep ran to exhaustion — retrying is pointless until
        the build pack grows, so it is skipped by default. 'parked' means the
        search was cut short by the budget and is always worth another go, so
        it is recorded for reporting but never skipped."""
        try:
            con = self._db()
            try:
                con.execute(
                    "INSERT INTO misses(release, kind, reason, seen, combos, "
                    "builds, order_sig) VALUES(?,?,?,?,?,?,?) "
                    "ON CONFLICT(release) DO UPDATE "
                    "SET kind=excluded.kind, reason=excluded.reason, "
                    "seen=excluded.seen, builds=excluded.builds, "
                    "combos=excluded.combos, order_sig=excluded.order_sig",
                    (rel, kind, reason,
                     datetime.now(timezone.utc).isoformat(timespec="seconds"),
                     int(combos), builds, order_sig or ""))
                con.commit()
            finally:
                con.close()
        except Exception:
            pass

    def import_srrdb_priors(self) -> dict:
        """Seed the recipe priors from the legacy srrdb rebuild results.

        Those runs measured, per release, which WinRAR build reproduced the
        archive — thousands of them, for groups the .rsr scanner has never met.
        `rar_version` is written by the same _exe_label() formatting used here
        ('2005-11-21 3.60'), so it maps back to an exact exe with no guessing:
        all 2,909 usable records resolve against the 232-build pack.

        Two grades come out of it:
          * the `sweep` records carry the exact exe AND -mt — a full prior;
          * everything else carries the build only, imported with mt=-1, which
            still collapses a 3,944-combo sweep to the 17 thread counts of one
            known build.

        Note the two tools can legitimately disagree on WHICH build (srrdb says
        4.11 for EXiMiUS, our captures say 3.60) because RAR4 output is
        identical across much of the 3.x/4.x family. Both reproduce the streams
        byte-exact, so either is a fine thing to try first — the sweep verifies
        by byte-compare regardless of where the suggestion came from."""
        path = self._app_dir / "srrdb_results.json"
        if not path.is_file():
            return {"ok": False, "error": f"{path.name} not found"}
        try:
            data = json.loads(path.read_text("utf-8"))
        except Exception as e:
            return {"ok": False, "error": f"cannot read {path.name}: {e}"}

        label2exe = {_exe_label(p.name): p.name for p in self._pack_exes()}
        if not label2exe:
            return {"ok": False, "error": "WinRAR build pack not found"}

        exact: dict[tuple, int] = {}      # (grp, exe, mt) -> hits
        build: dict[tuple, int] = {}      # (grp, exe)     -> hits
        rows = skipped = 0
        for r in data if isinstance(data, list) else []:
            if not isinstance(r, dict) or not r.get("ok"):
                continue
            grp = (r.get("group") or "").upper()
            if not grp:
                continue
            found = (r.get("sweep") or {}).get("found") or {}
            exe = found.get("exe")
            if exe and exe in {p.name for p in self._pack_exes()}:
                exact[(grp, exe, int(found.get("mt", -1)))] = \
                    exact.get((grp, exe, int(found.get("mt", -1))), 0) + 1
                rows += 1
                continue
            exe = label2exe.get(r.get("rar_version") or "")
            if not exe:
                skipped += 1
                continue
            build[(grp, exe)] = build.get((grp, exe), 0) + 1
            rows += 1

        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        con = self._db()
        try:
            con.executemany(
                "INSERT INTO recipes(fmt, level, grp, exe, mt, hits, last_used)"
                " VALUES('',-1,?,?,?,?,?) ON CONFLICT(fmt, level, grp, exe, mt) "
                "DO UPDATE SET hits=excluded.hits, last_used=excluded.last_used",
                [(g, e, m, n, now) for (g, e, m), n in exact.items()])
            con.executemany(
                "INSERT INTO recipes(fmt, level, grp, exe, mt, hits, last_used)"
                " VALUES('',-1,?,?,-1,?,?) ON CONFLICT(fmt, level, grp, exe, mt)"
                " DO UPDATE SET hits=excluded.hits, last_used=excluded.last_used",
                [(g, e, n, now) for (g, e), n in build.items()])
            con.commit()
            groups = con.execute("SELECT COUNT(DISTINCT grp) FROM recipes "
                                 "WHERE grp<>''").fetchone()[0]
        finally:
            con.close()

        self._log(f"Imported {rows:,} measured result(s) from "
                  f"{path.name}: {len(exact)} exact (build + -mt), "
                  f"{len(build)} build-only.", "ok")
        self._log(f"  priors now cover {groups} group(s); "
                  f"{skipped:,} record(s) had no usable build.", "dim")
        return {"ok": True, "records": rows, "exact": len(exact),
                "build_only": len(build), "groups": groups, "skipped": skipped}

    def _db_forget(self, rel: str):
        """Drop any recorded miss — this release just captured cleanly."""
        try:
            con = self._db()
            try:
                con.execute("DELETE FROM misses WHERE release=?", (rel,))
                con.commit()
            finally:
                con.close()
        except Exception:
            pass

    @staticmethod
    def _order_sig(combos) -> str:
        """Fingerprint of a combo ORDER, so recorded progress is only reused
        against the sequence it was actually measured in."""
        h = hashlib.sha256()
        for ex, n in combos:
            h.update(f"{ex.name}:{n}\n".encode())
        return h.hexdigest()[:16]

    def _resume_point(self, rel: str, sig: str) -> tuple[int, str]:
        """(combos already tried, when) for a release a budget cut short.

        A parked release used to restart at combo 1, which meant that with a
        fixed budget it could NEVER finish — every run re-ground the identical
        prefix and parked in the same place. Resuming is only sound while the
        order is unchanged, hence the signature: priors shift as the corpus is
        learned, and an offset into a re-ordered list would skip combos that
        were never tried. A mismatch simply starts over, which is the safe
        direction to be wrong in."""
        if not rel or not self._db_path.is_file():
            return 0, ""
        try:
            con = self._db()
            try:
                r = con.execute(
                    "SELECT combos, order_sig, seen FROM misses WHERE "
                    "release=? AND kind='parked'", (rel,)).fetchone()
            finally:
                con.close()
        except Exception:
            return 0, ""
        if not r or not r[0]:
            return 0, ""
        if (r[1] or "") != sig:
            self._log("    (previous run's progress was measured in a "
                      "different combo order — starting over)", "dim")
            return 0, ""
        return int(r[0]), (r[2] or "")[:10]

    def _known_wall(self, rel: str) -> tuple | None:
        """(seen, builds) if this release has already been swept to exhaustion
        against a pack no larger than today's, else None."""
        if not self._db_path.is_file():
            return None
        try:
            con = self._db()
            try:
                r = con.execute("SELECT seen, builds FROM misses WHERE "
                                "release=? AND kind='wall'", (rel,)).fetchone()
            finally:
                con.close()
        except Exception:
            return None
        if not r:
            return None
        # A bigger build pack than the one that failed is new evidence, so the
        # old verdict no longer stands and the release is swept again.
        if r[1] and len(self._pack_exes()) > r[1]:
            return None
        return r

    def _hot_recipes(self, fmt: str, level: int, grp: str) -> list[tuple]:
        """Known-good (exe, mt) pairs, best bet first.

        Widening rings, sharpest first: this group at this compression level,
        then this group at any level, then the corpus at this level, then the
        format at large. The group rings are the ones that pay — a group is one
        person with one WinRAR install — and the wider rings only exist so a
        group never seen before still gets a sensible head start.

        Everything returned is also in the exhaustive sweep that follows; this
        only moves it earlier, never removes anything."""
        if not self._db_path.is_file():
            return []
        out, seen = [], set()
        rings = []
        if grp:
            rings += [("fmt=? AND level=? AND grp=?", (fmt, int(level), grp)),
                      ("fmt=? AND grp=?", (fmt, grp)),
                      # Imported srrdb priors: the group's build is known but
                      # not its thread count or the archive shape it came from,
                      # so they carry fmt='' / level=-1 / mt=-1 and are matched
                      # on the group alone. A build that cannot write this
                      # format is dropped later by _pack_args.
                      ("grp=? AND mt=-1", (grp,))]
        rings += [("fmt=? AND level=?", (fmt, int(level))), ("fmt=?", (fmt,))]
        try:
            con = self._db()
            try:
                for where, args in rings:
                    for exe, mt in con.execute(
                            f"SELECT exe, mt FROM recipes WHERE {where} "
                            "ORDER BY hits DESC, last_used DESC LIMIT 24", args):
                        if (exe, mt) not in seen:
                            seen.add((exe, mt))
                            out.append((exe, int(mt)))
            finally:
                con.close()
        except Exception:
            return out
        return out

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
            # Named columns, not positional: a bare VALUES(...) list silently
            # shifts every field the moment a column is added.
            con.execute(
                "INSERT INTO releases(name, rsr_path, created, format, sets, "
                "files, volumes, verified, verify, recipe_exe, recipe_version, "
                "mt, level, dict_kb, solid, extras, total_bytes, kind, "
                "tag) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (rel, str(rsr_path), manifest.get("created_utc"),
                 sets[0].get("format") if sets else "",
                 len(sets), nfiles, nvols, verified,
                 sets[0].get("verify") if sets else "",
                 recipe.get("exe", ""), recipe.get("version", ""),
                 recipe.get("mt", -1), recipe.get("level", -1),
                 recipe.get("dict_kb", 0), int(recipe.get("solid", False)),
                 nextra,
                 sum(v.get("size", 0) for s in sets for v in s.get("volumes", [])),
                 manifest.get("kind", "archive"), manifest.get("tag", "")))
            for s in sets:
                for f in s.get("files", []):
                    con.execute("INSERT INTO files VALUES(?,?,?,?,?,?,?,?,?)",
                                (rel, s.get("stem"), f["name"], f["size"],
                                 f["packed_size"], f["crc32"], f.get("sha256"),
                                 f["method"], f.get("source")))
            con.commit()
        finally:
            con.close()

    def reindex_store(self, store: str) -> dict:
        """Rebuild the index from what is actually on disk.

        The index is derived data: every row in it is reproducible from the
        manifest inside the .rsr it points at, so on any disagreement the DISK
        wins. That matters because a capture writes its .rsr before it records
        the row, and anything that interrupts the gap — a crash, a kill, or a
        stale process whose INSERT no longer matched a migrated table — leaves
        a perfectly good .rsr with no row. `skip_done` matches on the FILE, so
        a re-run then skips the release and it stays invisible for good. This
        heals all of that without recapturing anything.

        Three repairs, in order of how much they touch:
          * a .rsr with no row            -> indexed
          * a row pointing somewhere else -> re-pointed (the store was moved,
            or the release was re-filed, as the [NUKED] path fix did)
          * a row whose .rsr is gone      -> dropped, but ONLY when its path
            lies under the folder being reindexed. Reindexing one subtree must
            not delete rows for releases stored elsewhere."""
        path = (store or "").strip()
        if not path:
            return {"ok": False, "error": "no store folder given"}
        root = Path(path)
        if not root.is_dir():
            return {"ok": False, "error": f"store folder not found: {root}"}

        self._log(f"Reindexing {root} …", "info")
        found = sorted(root.rglob("*.rsr"))          # .rsr.tmp is not matched
        con = self._db()
        try:
            rows = {r[0]: r[1] or "" for r in
                    con.execute("SELECT name, rsr_path FROM releases")}
        finally:
            con.close()
        self._log(f"  {len(found):,} .rsr on disk · {len(rows):,} row(s) "
                  f"in the index", "dim")

        seen: dict[str, Path] = {}
        added = moved = intact = bad = dupes = 0
        for i, p in enumerate(found, 1):
            if i % 25 == 0 or i == len(found):
                self._progress(f"reindex {i}/{len(found)}")
            try:
                manifest, z = self.read_rsr(p)
                z.close()
            except Exception as e:
                bad += 1
                self._log(f"  ✗ unreadable, left alone: {p.name} — {e}", "err")
                continue
            # The manifest is the authority on the release name; the filename
            # is only a convention and a rename would otherwise fork the row.
            rel = manifest.get("release") or p.stem
            if rel in seen:
                # Two files claiming one release — exactly what re-filing a
                # release leaves behind. Index the newer and say where the
                # other is, rather than silently letting the walk order decide.
                dupes += 1
                other = seen[rel]
                keep = max(p, other, key=lambda x: x.stat().st_mtime)
                self._log(f"  ! {rel} is claimed by two .rsr — indexing the "
                          f"newer one, {keep}", "warn")
                self._log(f"      the other is still on disk: "
                          f"{other if keep is p else p}", "dim")
                if keep is other:
                    continue
            known = rows.get(rel)
            if known is None:
                added += 1
                why = "not indexed"
            elif Path(known) != p:
                moved += 1
                why = f"was {known}"
            else:
                intact += 1
                seen[rel] = p
                continue
            try:
                self._db_record(manifest, p)
                self._db_forget(rel)       # it exists; any recorded miss is stale
            except Exception as e:
                bad += 1
                self._log(f"  ✗ cannot index {p.name}: {e}", "err")
                continue
            seen[rel] = p
            self._log(f"  + {rel} ({why})", "ok")
        self._progress("")

        stale = []
        for name, rsr in rows.items():
            if name in seen or not rsr:
                continue
            q = Path(rsr)
            try:
                inside = q.is_relative_to(root)
            except ValueError:
                inside = False
            if inside and not q.is_file():
                stale.append(name)
        if stale:
            con = self._db()
            try:
                con.executemany("DELETE FROM releases WHERE name=?",
                                [(n,) for n in stale])
                con.executemany("DELETE FROM files WHERE release=?",
                                [(n,) for n in stale])
                con.commit()
            finally:
                con.close()
            for n in stale[:20]:
                self._log(f"  − {n} (indexed, but the .rsr is gone)", "warn")
            if len(stale) > 20:
                self._log(f"    …and {len(stale) - 20} more", "dim")

        parts = [f"{intact:,} already correct"]
        if added:
            parts.append(f"{added:,} indexed")
        if moved:
            parts.append(f"{moved:,} re-pointed")
        if stale:
            parts.append(f"{len(stale):,} stale row(s) dropped")
        if dupes:
            parts.append(f"{dupes:,} duplicate name(s)")
        if bad:
            parts.append(f"{bad:,} unreadable")
        self._log("Reindex complete — " + ", ".join(parts) + ".",
                  "ok" if not bad else "warn")
        return {"ok": True, "found": len(found), "added": added, "moved": moved,
                "intact": intact, "stale": len(stale), "dupes": dupes,
                "bad": bad}

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

    def release_info(self, key: str) -> dict:
        """Everything known about one captured release, for the detail popup.

        `key` is whatever the GUI has to hand — a release NAME from a queue row
        or a .rsr PATH from the database list — because the queue only ever
        knew the name, and requiring the caller to resolve it would duplicate
        the lookup in JavaScript."""
        key = (key or "").strip()
        if not key:
            return {"ok": False, "error": "no release given"}
        path = Path(key)
        if not (path.suffix.lower() == ".rsr" and path.is_file()):
            if not self._db_path.is_file():
                return {"ok": False, "error": "nothing captured yet"}
            con = self._db()
            try:
                row = con.execute("SELECT rsr_path FROM releases WHERE name=?",
                                  (key,)).fetchone()
                miss = con.execute(
                    "SELECT kind, reason, seen, combos, builds FROM misses "
                    "WHERE release=?", (key,)).fetchone()
            finally:
                con.close()
            if not row and miss:
                # Known, just not captured. "Not in the index" was technically
                # true and practically useless: the scanner has a great deal to
                # say about these — they are the releases worth reporting.
                kind, reason, seen, combos, builds = miss
                when = (seen or "")[:10]
                if kind == "wall":
                    what = (f"Swept to exhaustion on {when} against "
                            f"{builds} build(s) × every thread count, and "
                            f"nothing reproduced its streams. The build that "
                            f"packed it is not in the pack.")
                elif kind == "damaged":
                    what = (f"Checked against its own .sfv on {when} and found "
                            f"damaged.\n\n{_explain_error(reason or '')}")
                elif kind == "parked":
                    what = (f"Parked on {when} after {combos:,} of the "
                            f"{builds}-build sweep — the time budget ran out, "
                            f"not the search. The next run resumes from there.")
                else:
                    what = (f"Failed on {when} before a recipe could be "
                            f"proved.\n\n{_explain_error(reason or '')}")
                return {"ok": False, "known": True, "kind": kind,
                        "error": f"{key}\n\n{what}\n\nNo .rsr is written until "
                                 f"a recipe is proved, so there is nothing to "
                                 f"show here yet. Recorded as: "
                                 f"{reason or kind}."}
            if not row:
                return {"ok": False,
                        "error": f"'{key}' is not in the index — it may have "
                                 "been skipped, failed, or captured before the "
                                 "index existed."}
            path = Path(row[0])
        if not path.is_file():
            return {"ok": False,
                    "error": f"indexed, but the .rsr is gone from {path}"}

        try:
            manifest, z = self.read_rsr(path)
            entries = z.namelist()
            entries_info = {n: z.getinfo(n).file_size for n in entries}
            z.close()
        except Exception as e:
            return {"ok": False, "error": f"cannot read {path.name}: {e}"}

        sets = []
        for st in manifest.get("sets", []):
            rec = st.get("recipe") or {}
            vols = st.get("volumes", [])
            sets.append({
                "stem": st.get("stem", ""),
                "format": st.get("format", ""),
                "scheme": st.get("scheme", ""),
                "solid": bool(rec.get("solid")),
                "verify": st.get("verify", st.get("error", "—")),
                "recipe": (f"{rec.get('version', '?')} "
                           f"{_mt_label(rec.get('exe', ''), rec.get('mt', '?'))}"
                           if rec else "—"),
                "exe": rec.get("exe", ""),
                "settings": (f"-m{rec.get('level', '?')} "
                             f"-md{rec.get('dict_kb', '?')}KB "
                             f"{'-s' if rec.get('solid') else '-s-'}"
                             if rec else ""),
                "volumes": len(vols),
                "volume_bytes": sum(v.get("size", 0) for v in vols),
                "deltas": sum(1 for v in vols if v.get("delta")),
                # Size the patches from the container itself — the manifest
                # records which entry holds each delta, not how big it is, and
                # "9 volumes patched" reads very differently at 600 B than at
                # 600 KB (the latter would mean the replay is near-but-not-on
                # and the delta is hiding a stream difference).
                "delta_bytes": sum(
                    _zip_entry_size(entries_info, v["delta"])
                    for v in vols if v.get("delta")),
                "files": [{
                    "name": f.get("name", ""),
                    "size": f.get("size", 0),
                    "packed": f.get("packed_size", 0),
                    "crc32": "%08X" % (f.get("crc32") or 0),
                    "source": f.get("source", ""),
                } for f in st.get("files", [])],
            })
        return {"ok": True, "info": {
            "name": manifest.get("release", path.stem),
            "system": manifest.get("system", "—"),
            "year": manifest.get("year", "—"),
            "created": manifest.get("created_utc", ""),
            "tool": manifest.get("tool", ""),
            "source_folder": manifest.get("source_folder", ""),
            "path": str(path),
            "rsr_bytes": path.stat().st_size,
            "has_srr": "release.srr" in entries,
            "sets": sets,
            "sidecars": [{"name": s.get("name", ""), "size": s.get("size", 0)}
                         for s in manifest.get("sidecars", [])],
            "extras": [e for e in entries if e.startswith("extras/")],
        }}


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

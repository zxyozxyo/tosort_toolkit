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

# Serialises the on-disk log across capture threads (see _log_to_file).
_LOG_LOCK = threading.Lock()


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


# A loose file that IS the set's content is normally not a sidecar — it is the
# rom sitting unpacked beside its own rars, and carrying it would put back the
# very bytes the capture just took out. That reasoning is about big files. On a
# patch or trainer release the largest zip member is the nfo, so the nfo is the
# content, and the rule then dropped the loose nfo that every such release also
# ships beside its zip: the rebuild came out without it. Below this size the
# bytes are too cheap to reason about, so carry them.
LOOSE_CONTENT_MIN = 1 << 20

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

# Archive formats that are archives, just not ours. Kept apart from "this
# folder holds no archive" because the two need opposite reactions: a .lzh
# release is complete and correctly stored and simply cannot be described by a
# format that replays rar.exe, while a folder of loose files has lost the
# archive it came in.
# .lha/.lzh are NOT here — the scanner reads those now. .lzx stays: it is
# Amiga LZX, a different format that merely looks related, and the corpus holds
# exactly one (TB1SS.LZX, whose first bytes are "LZX" then a NUL).
_FOREIGN_ARCHIVE_EXT = {".lzx", ".arj", ".ace", ".7z", ".zoo",
                        ".arc", ".cab", ".tar", ".gz", ".bz2", ".xz", ".sit"}

# Guards the window in which _open_rar() swaps rarfile's comment decompressor
# out and back; two captures in one process must not interleave there.
_RAR_COMMENT_LOCK = threading.RLock()

# Least compressed output a prefix probe must see before its verdict counts.
# Under this the streams have barely diverged and an "agrees so far" means
# nothing, so the combo is packed in full instead.
PROBE_MIN_BYTES = 1 << 20

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


def volume_gap(folder: Path, vols: list) -> str:
    """A hole in a volume sequence that the release's own .sfv also has, or "".

    Street_Football-EXiMiUS ships .rar and .r01 through .r04 — no .r00 — and
    every one of the five matches its .sfv exactly. So nothing is wrong with
    the copy: a volume was never released, which is presumably why the release
    was nuked. rar cannot extract past the hole, and "extraction incomplete"
    reads as a bad download and invites a pointless re-fetch. Say which volume
    the release itself never had."""
    idx = [_classify_volume(p.name) for p in vols]
    nums = sorted(c[2] for c in idx if c)
    if not nums:
        return ""
    have = set(nums)
    gaps = [n for n in range(min(nums), max(nums) + 1) if n not in have]
    if not gaps:
        return ""                                # contiguous; nothing missing
    stem = next((c[0] for c in idx if c), "")
    want = sfv_expected(folder)
    named = [f"{stem}.r{n:02d}" for n in gaps]
    listed = [n for n in named if n.lower() in want]
    if listed:
        return (f"{', '.join(listed)} is in the .sfv but not on disk — that "
                f"volume is missing from your copy")
    return (f"the set jumps straight past {', '.join(named)}, and the "
            f"release's own .sfv does not list it either — a volume was never "
            f"released, so this archive cannot be extracted by anyone")


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
    # BELOW_NORMAL, so a scan can have the whole machine without owning it.
    # The sweep now runs dozens of rar.exe at once and will happily sit at
    # 100% CPU for hours; at normal priority that competes with whatever the
    # operator is doing on equal terms. Below normal, Windows hands the scan
    # every idle cycle on an empty machine — the throughput is unchanged when
    # nothing else wants the CPU — and preempts it the moment anything
    # interactive does. It makes a high core budget safe to leave set.
    return {"startupinfo": si,
            "creationflags": (getattr(subprocess, "CREATE_NO_WINDOW", 0)
                              | getattr(subprocess,
                                        "BELOW_NORMAL_PRIORITY_CLASS", 0))}


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

    # A file whose name says nothing and whose first bytes say RAR. SCZ shipped
    # the Unou_no_Tatsujin repack as SCZ-TSMMr.zip holding a RAR archive, so
    # the release routed to the ZIP path and died there as "unreadable zip" —
    # correctly, since it is not a zip. The marker decides format everywhere
    # else in here; let it decide membership too.
    for p in sorted(base.rglob("*")):
        if not p.is_file() or _classify_volume(p.name):
            continue
        if _rar_format(p):
            families[(str(p.parent), p.stem, "lone")] = [(0, p)]

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


# RAR4 main archive header flags (the byte pair at offset 10).
MHD_LOCK = 0x0004
MHD_SOLID = 0x0008


def main_flags(head: Path) -> int:
    """The RAR4 main archive header flags, or 0.

    Two of these bits are switches nothing else can tell you about, because
    they say something about the ARCHIVE and every other probe in here reads
    FILE headers:

      * MHD_SOLID — an archive packed with -s. With more than one file the
        file headers give it away too, since every file after the first
        carries its own solid bit. With ONE file they do not: `rar a -s` sets
        this flag and leaves the single file header looking exactly like -s-.
        Micronauts pack one .nds per archive, so the capture read "-s-", and
        every replay came out with the wrong main header.

      * MHD_LOCK — an archive packed with -k. Nothing in a file header
        records it at all.

    Between them they were the whole of the Lego_Batman and Nancy_Drew
    "replay unverified": the recovery record matched to the byte, the stream
    matched, nine of eleven volumes already had identical data offsets, and
    the two flags moved everything. With -s -k the set reproduces 11/11
    byte-identical."""
    if _rar_format(head) != "RAR4":
        return 0
    try:
        with open(head, "rb") as f:
            data = f.read(12)
    except OSError:
        return 0
    if len(data) < 12 or data[9] != 0x73:      # main header block type
        return 0
    return int.from_bytes(data[10:12], "little")


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


def _open_rar(head, info_callback=None):
    """rarfile.RarFile, except that a FILE comment can never stop the parse.

    rarfile decompresses a RAR3 per-file comment while parsing, and RAR3
    comment decompression is one of the things it shells out to an external
    unrar for. There is no unrar on this machine and there does not need to
    be — the build pack ships rar.exe, the packer, and every extract this tool
    does goes through that. So Micronauts' Lego_Batman and Nancy_Drew, which
    carry a file comment, came out of a scan as

        ERROR: Cannot find working tool
        rarfile.RarCannotExec: Cannot find working tool

    and a forty-line Python traceback, with no row, no recorded miss and no
    hint that the release was fine and the parser was not. (The pair had an
    older `replay unverified` against them from a run that got further, which
    is what made this look like a compression problem for so long.)

    Nothing in here ever reads a file comment. The ARCHIVE comment is a
    separate field, is captured from `rf.comment`, and on these two is None
    anyway. So when — and only when — the tool is what is missing, re-open
    with the comment decompressor stubbed and carry on with the block offsets
    and sizes that were the only thing wanted."""
    import rarfile
    try:
        return rarfile.RarFile(str(head), info_callback=info_callback)
    except rarfile.RarCannotExec:
        with _RAR_COMMENT_LOCK:
            orig = rarfile.rar3_decompress
            rarfile.rar3_decompress = lambda *a, **kw: b""
            try:
                rf = rarfile.RarFile(str(head), info_callback=info_callback)
            finally:
                rarfile.rar3_decompress = orig
        # The stub blanked the ARCHIVE comment too, and that one matters: it is
        # packed back with -z and it occupies real bytes in the first volume.
        # Flag it so the caller can go and fetch it properly.
        rf._rsr_comment_stubbed = True
        return rf


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

    rf = _open_rar(head, cb)
    rf.close()
    return blocks


# ══════════════════════════════════════════════════════════════════════════
#  LHA / LZH
# ══════════════════════════════════════════════════════════════════════════
#
# The 1996 PSX DOX scene packed in LHA, not RAR or ZIP: 329 releases of the
# corpus, the single largest thing the scanner could not read. The container
# yields to exactly the same skeleton trick as ZIP — headers carried verbatim,
# member data cut out — so nothing here has to model an LHA header field.
#
# What it does NOT yield is the compression. `-lh5-` is LZSS over an 8 KB
# window with per-block static Huffman, and reproducing a given encoder's
# output bit for bit means reimplementing that encoder's match finder. There
# is no lha binary on this machine and no preflate equivalent for LZH, so an
# `-lh5-` stream can only be carried, never derived. Measured over all 329:
# deflating an `-lh5-` stream returns 100.1% of it, so carrying every stream
# makes a .rsr the size of the archive — which is the one shape this format
# exists to avoid.
#
# `-lh0-` is stored, and a stored member IS its file. Where the biggest member
# is `-lh0-` the content can be supplied loose at rebuild exactly as it is for
# RAR and ZIP, and the capture is a real one. That is what this path does, and
# it declines the rest honestly rather than writing a copy of the archive.

LHA_METHODS = (b"-lh0-", b"-lh1-", b"-lh2-", b"-lh3-", b"-lh4-", b"-lh5-",
               b"-lh6-", b"-lh7-", b"-lzs-", b"-lz4-", b"-lz5-", b"-pm0-",
               b"-pm2-")
LHA_STORED = ("-lh0-", "-lz4-", "-pm0-")


def _u16(b: bytes, o: int) -> int:
    return int.from_bytes(b[o:o + 2], "little")


def lha_members(raw: bytes) -> list[dict] | None:
    """Every member of an LHA/LZH archive with the exact byte range its data
    occupies, or None if this is not one.

    Header levels 0, 1 and 2 all appear in the corpus (0 and 1 dominate, 2,005
    and 1,331 members). Level 1 is the one with a trap: its size field is a
    SKIP size covering the extended headers as well as the data, and the size
    of the first extended header is the last word of the BASE header — at
    `i + hs`, not after it. Reading it two bytes later reads compressed data as
    a header length, which walks off the end of the file; that alone accounted
    for 210 of 330 archives looking corrupt when they are all fine."""
    out: list[dict] = []
    i = 0
    n = len(raw)
    while i < n:
        if n - i < 22 or raw[i] == 0:
            break                              # 0 byte terminates the archive
        meth = raw[i + 2:i + 7]
        if meth not in LHA_METHODS:
            return None if not out else out
        csize, osize = struct.unpack("<II", raw[i + 7:i + 15])
        lvl = raw[i + 20]
        try:
            if lvl in (0, 1):
                hs = raw[i]
                nl = raw[i + 21]
                name = raw[i + 22:i + 22 + nl]
                crc = _u16(raw, i + 22 + nl)
                if lvl == 0:
                    data, dsize = i + 2 + hs, csize
                else:
                    # Level 1: walk the extended-header chain. Each header
                    # ends with the size of the next one; a size of 0 ends
                    # the chain and the data starts there.
                    esz = _u16(raw, i + hs)    # NOT i + hs + 2
                    j, ext = i + 2 + hs, 0
                    while esz:
                        ext += esz
                        if j + esz > n:
                            return out or None
                        nxt = _u16(raw, j + esz - 2)
                        j += esz
                        esz = nxt
                    data, dsize = j, csize - ext
            elif lvl == 2:
                hs = _u16(raw, i)
                crc = _u16(raw, i + 21)
                name = b""
                data, dsize = i + hs, csize
            else:
                return out or None
        except (IndexError, struct.error):
            return out or None
        if dsize < 0 or data + dsize > n:
            return out or None
        out.append({
            "name": name.decode("cp437", "replace").replace("\\", "/"),
            "method": meth.decode("ascii"),
            "level": lvl,
            "data_offset": data,
            "packed_size": dsize,
            "size": osize,
            "crc16": crc,
        })
        i = data + dsize
    return out or None


def lha_skeleton(raw: bytes, ents: list[dict]) -> tuple[bytes, list]:
    """The archive with every member's data cut out, plus where it was.

    Byte-for-byte the same idea as zip_skeleton: headers, the terminating 0
    and any padding are all carried, so a rebuild never has to write an LHA
    header field of its own."""
    holes = sorted((e["data_offset"], e["packed_size"]) for e in ents)
    skel = bytearray()
    pos = 0
    for off, ln in holes:
        skel += raw[pos:off]
        pos = off + ln
    skel += raw[pos:]
    return bytes(skel), [list(h) for h in holes]


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
    """Every entry, with the exact byte range its compressed data occupies.

    The central directory is the fast path, not the truth. The NUKED
    Urusei_Yatsura-SCZ carries one archive's members under a different
    archive's central directory: it lists five entries, the file holds three,
    and every offset in it points into the middle of a compressed stream. The
    members themselves are intact and decompress with the right CRC, so when
    the directory does not line up, read the local headers instead. A skeleton
    capture never needs the directory to be true — it is carried verbatim, and
    the rebuild reproduces the archive byte-exact including the lie."""
    ents = _zip_entries_central(path)
    if ents == "encrypted":
        return None
    return ents or _zip_entries_scan(path)


def _zip_entries_central(path: Path) -> list[dict] | str | None:
    """Entries as the central directory describes them, or None if it does
    not agree with the local headers."""
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
                return "encrypted"               # out of scope
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


def _zip_entries_scan(path: Path) -> list[dict] | None:
    """Walk the local headers in file order, keeping only members that prove
    themselves.

    PK\\x03\\x04 turns up inside compressed data by chance often enough that a
    candidate does not count until it decompresses to the length and the CRC
    its own header claims. Anything left over between members — a stale stream,
    padding, a whole obsolete central directory — is not our problem: the
    skeleton carries every byte we did not cut out."""
    raw = path.read_bytes()
    out = []
    i = raw.find(b"PK\x03\x04")
    while i >= 0:
        head = raw[i + 4:i + 30]
        if len(head) < 26:
            break
        _v, flag, meth, _t, _d, crc, cs, us, nl, el = struct.unpack(
            "<HHHHHIIIHH", head)
        if flag & 0x1:
            return None                          # encrypted; out of scope
        off = i + 30 + nl + el
        blob = raw[off:off + cs]
        ok = bool(nl) and meth in (0, 8) and not flag & 0x8 and len(blob) == cs
        if ok:
            try:
                plain = zlib.decompress(blob, -15) if meth else blob
            except zlib.error:
                ok = False
            else:
                ok = len(plain) == us and zlib.crc32(plain) == crc
        if not ok:
            i = raw.find(b"PK\x03\x04", i + 1)
            continue
        name = raw[i + 30:i + 30 + nl]
        out.append({
            "name": name.decode("utf-8" if flag & 0x800 else "cp437", "replace"),
            "size": us,
            "packed_size": cs,
            "crc32": crc,
            "method": meth,
            "data_offset": off,
        })
        i = raw.find(b"PK\x03\x04", off + cs)
    return out or None


def zip_truncated(path: Path) -> str:
    """Why a ZIP looks cut short, or "" if it does not.

    "cannot read" sends you looking for a parser bug when the answer is that
    the file is not all there. Baby_Pals-SirVG is 14,950,400 bytes — a round
    14,600 KB, the shape of an interrupted transfer — holding one member whose
    own header says its data ends at 37,880,850. Nothing can read that, and no
    amount of re-parsing will change it."""
    try:
        size = path.stat().st_size
        with open(path, "rb") as fh:
            raw = fh.read(1 << 16)
    except OSError:
        return ""
    if raw[:4] != b"PK\x03\x04":
        return ""
    with open(path, "rb") as fh:
        fh.seek(max(0, size - (1 << 16)))
        if b"PK\x05\x06" in fh.read(1 << 16):
            return ""                            # has an end record; not cut
    nl, el = struct.unpack("<HH", raw[26:30])
    cs, = struct.unpack("<I", raw[18:22])
    name = raw[30:30 + nl].decode("latin-1", "replace")
    end = 30 + nl + el + cs
    if end > size:
        return (f"the file is {size:,} B with no end-of-directory record, and "
                f"its first member {name} declares data ending at {end:,} B — "
                f"the archive is truncated, not unreadable")
    return ("there is no end-of-directory record — the archive is truncated or "
            "was never finished")


def zip_payload(raw: bytes, ent: dict) -> bytes:
    """One entry's expanded bytes, taken from where its local header says they
    are.

    Deliberately not ZipFile.read(): that goes through the central directory,
    which is not always describing this file, and it cannot tell two entries of
    the same name apart."""
    blob = raw[ent["data_offset"]:ent["data_offset"] + ent["packed_size"]]
    return zlib.decompress(blob, -15) if ent["method"] else blob


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


def end_block_sig(vol: Path) -> tuple | None:
    """(flags, header size) of a volume's end-of-archive block, or None.

    A matching compressed stream does NOT identify the build. Stored data is
    identical across every build, and RAR4 compression output is identical
    across much of the 3.x/4.x family — so the sweep matches a stream and then
    picks the first build that produced it, which may write its ARCHIVE
    differently from the one that made the original.

    Der_Schatz_der_Delfine-DNB is the clean example: our 4.10 writes an end
    block of 20 bytes with flags 0x0f (the volume-number field present), the
    original has 18 bytes and 0x07. Two bytes, at the end of every volume,
    shifting everything after them — a whole volume mismatching on a recipe
    whose stream was perfect.

    This is free to check: it reads the last few hundred bytes of a file we
    already have."""
    try:
        size = vol.stat().st_size
        with open(vol, "rb") as fh:
            fh.seek(max(0, size - 4096))
            tail = fh.read(4096)
    except OSError:
        return None
    # Walk backwards for the last 0x7b block header: crc(2) type(1) flags(2)
    # size(2), and the block must end exactly at the file end.
    for i in range(len(tail) - 7, -1, -1):
        if tail[i + 2] != 0x7B:
            continue
        flags = int.from_bytes(tail[i + 3:i + 5], "little")
        hsize = int.from_bytes(tail[i + 5:i + 7], "little")
        if 7 <= hsize <= 64 and (len(tail) - i) == hsize:
            return flags, hsize
    return None


def _rar4_file_headers(vol: Path, limit: int = 8) -> list[dict]:
    """The first few RAR4 file blocks of a volume: flags, header size, name.

    Only the head of the file is read — a file header sits at the front of its
    own block, so the first one is a few dozen bytes in."""
    try:
        with open(vol, "rb") as fh:
            raw = fh.read(1 << 16)
    except OSError:
        return []
    if raw[:7] != RAR4_SIG:
        return []
    out, pos = [], 7
    while pos + 32 <= len(raw) and len(out) < limit:
        typ = raw[pos + 2]
        flags = int.from_bytes(raw[pos + 3:pos + 5], "little")
        size = int.from_bytes(raw[pos + 5:pos + 7], "little")
        if size < 7:
            break
        add = 0
        if flags & 0x8000 and pos + 11 <= len(raw):
            add = int.from_bytes(raw[pos + 7:pos + 11], "little")
        if typ == 0x74:
            nlen = int.from_bytes(raw[pos + 26:pos + 28], "little")
            name = raw[pos + 32:pos + 32 + nlen]
            out.append({"flags": flags, "size": size,
                        "name": name.split(b"\0")[0].decode("latin-1", "replace")})
        pos += size + add
    return out


def rar4_vol_blocks(vol: Path) -> dict[str, tuple[int, int]]:
    """{packed name: (data offset, bytes IN THIS VOLUME)} for one RAR4 volume.

    packed_blocks() goes through rarfile, which walks the whole set and wants a
    proper end block. A volume left behind by a killed pack has neither, so the
    headers are walked by hand here instead: a file block carries its own size
    and its data length, which is all a prefix probe needs. Anything malformed
    just ends the walk — a truncated tail is the normal case here, not an
    error."""
    out: dict[str, tuple[int, int]] = {}
    try:
        size_on_disk = vol.stat().st_size
        with open(vol, "rb") as fh:
            if fh.read(7) != RAR4_SIG:
                return {}
            pos = 7
            while pos + 11 <= size_on_disk:
                fh.seek(pos)
                head = fh.read(32)
                if len(head) < 11:
                    break
                typ = head[2]
                flags = int.from_bytes(head[3:5], "little")
                hsize = int.from_bytes(head[5:7], "little")
                if hsize < 7:
                    break
                add = (int.from_bytes(head[7:11], "little")
                       if flags & 0x8000 else 0)
                if typ == 0x74:
                    fh.seek(pos)
                    full = fh.read(hsize)
                    if len(full) < 32:
                        break
                    nlen = int.from_bytes(full[26:28], "little")
                    name = full[32:32 + nlen].split(b"\0")[0]
                    if name:
                        out[name.decode("latin-1", "replace")] = (pos + hsize,
                                                                  add)
                pos += hsize + add
    except OSError:
        return {}
    return out


def header_exttime(vol: Path) -> bool | None:
    """Whether a volume's first file header carries the high-precision
    timestamp (LHD_EXTTIME, 0x1000), or None if there is no file header.

    The same lesson as end_block_sig, one block earlier. RAR4 gained the field
    in 3.20: measured across the pack, every build up to 3.11 writes a 54-byte
    header for a plain name and every build from 3.20 writes 59. The compressed
    stream is identical either side of that line, so the sweep matched a stream
    at 3.11 and replayed an archive five bytes short in every file header —
    reported as "replay unverified", with nothing to suggest the build was
    simply too old. Balls_Of_Fury-Micronauts is exactly that."""
    heads = _rar4_file_headers(vol, limit=1)
    return bool(heads[0]["flags"] & 0x1000) if heads else None


def unicode_named_ascii(vol: Path) -> list[str]:
    """Files whose header carries the Unicode-name flag for a plain ASCII name.

    No build reproduces this — measured across all 232 in the pack, none sets
    0x0200 for an ASCII name — so it is a property of the host that packed the
    release, not of the packer we can choose. In a FLAT archive it costs four
    bytes in one header and diff_bytes already patches it. In a VOLUMED set it
    pushes four bytes of data out of every volume, moving every split point, so
    no patch can express it and no sweep can find it. Say so instead of
    spending three hours proving it again."""
    out = []
    for h in _rar4_file_headers(vol):
        if h["flags"] & 0x0200 and h["name"].isascii():
            out.append(h["name"])
    return out


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
        rf = _open_rar(head, cb)
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
DELTA3_MAGIC = b"RSRD3\x00"


def _common_run(a: bytes, b: bytes, ai: int, bi: int) -> int:
    """How many bytes a[ai:] and b[bi:] share, compared in strides."""
    n = min(len(a) - ai, len(b) - bi)
    k, step = 0, 1 << 16
    while k < n:
        want = min(step, n - k)
        if a[ai + k:ai + k + want] != b[bi + k:bi + k + want]:
            for t in range(want):
                if a[ai + k + t] != b[bi + k + t]:
                    return k + t
            return k + want
        k += want
    return n


def _delta3(produced: bytes, original: bytes, max_ops: int = 64) -> bytes | None:
    """A copy/insert script: <u32 ops>, then per op <u64 at><u32 del><u32 ins>.

    diff_bytes handles one edit region, or one length change with matching
    ends. Balls_Of_Fury-Micronauts needs two: rar's authenticity block (`-av`,
    242 bytes of RSA signature nobody can regenerate) sits before the end
    block, AND the main header's PosAV field points at it, six bytes at offset
    seven. Either alone is a header residual. Together they defeat a single
    prefix/suffix split, and a perfect recipe was thrown away over 248 bytes.

    Resyncs by looking for the next 64 bytes of `produced` inside `original`,
    stepping the anchor forward so a substitution re-aligns as readily as an
    insertion."""
    la, lb = len(produced), len(original)
    ladder = list(range(0, 65)) + [128, 256, 512, 1024, 2048, 4096]
    window, anchor = 1 << 20, 64
    ops: list[tuple[int, int, bytes]] = []
    total = i = j = 0
    while True:
        k = _common_run(produced, original, i, j)
        i, j = i + k, j + k
        if i >= la or j >= lb:
            break
        hit = None
        for dp in ladder:
            if i + dp + anchor > la:
                break
            y = original.find(produced[i + dp:i + dp + anchor], j, j + window)
            if y >= 0:
                hit = (dp, y)
                break
        if hit is None:
            break                                # no resync: fall through
        dp, y = hit
        ops.append((i, dp, original[j:y]))
        total += y - j
        i, j = i + dp, y
        if total > DELTA_MAX_BYTES or len(ops) >= max_ops:
            return None
    if i < la or j < lb:
        ops.append((i, la - i, original[j:]))
        total += lb - j
    if total > DELTA_MAX_BYTES or not ops:
        return None
    out = bytearray(DELTA3_MAGIC + len(ops).to_bytes(4, "little"))
    for at, dl, ins in ops:
        out += (at.to_bytes(8, "little") + dl.to_bytes(4, "little")
                + len(ins).to_bytes(4, "little") + ins)
    return bytes(out)


# PPM candidates, appended AFTER the ordinary sweep (see _sweep_recipe).
# -mct+ forces the text/PPM coder; -mc<order>:<mem>t+ tunes its model order and
# memory. Measured: the coder is identical across 4.x and 5.x builds and
# differs only in the 3.x era, so this sweeps orders against a few build eras
# rather than the whole pack -- 3.x, 4.x and 5.x, which is every distinct PPM
# behaviour the pack contains.
PPM_ORDERS = (0, 63, 58, 40, 37, 34, 25, 20, 16, 12, 10, 8, 6, 4, 2,
              62, 61, 60, 59, 57, 56, 55, 54, 53, 52, 51, 50, 49, 48, 47,
              46, 45, 44, 43, 42, 41, 39, 38, 36, 35, 33, 32, 31, 30, 29,
              28, 27, 26, 24, 23, 22, 21, 19, 18, 17, 15, 14, 13, 11, 9,
              7, 5, 3)
PPM_MEMS = (64, 16, 4, 128, 256)


def _ppm_switches() -> list[tuple]:
    """Every -mc variant to try, cheapest and likeliest first."""
    out = [("-mct+",)]
    for order in PPM_ORDERS:
        if not order:
            continue
        for mem in PPM_MEMS:
            out.append((f"-mc{order}:{mem}t+",))
    return out


def diff_bytes(produced: bytes, original: bytes) -> bytes | None:
    """The smallest patch turning `produced` into `original`, or None.

    The simple forms are tried first because they are nearly free and cover
    almost everything. They are not always the best answer, though, and "fits
    under the cap" is not "small": Donkey_Kong_Jungle_Climber's last volume
    took a 870 KB middle when a copy/insert script expresses the same
    difference in 110 bytes. Anything fat gets a second opinion."""
    cand = _diff_simple(produced, original)
    if cand is not None and len(cand) <= 4096:
        return cand
    alt = _delta3(produced, original)
    if alt is None:
        return cand
    return alt if cand is None or len(alt) < len(cand) else cand


def _diff_simple(produced: bytes, original: bytes) -> bytes | None:
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


def diff_shape(produced: bytes, original: bytes) -> str:
    """Where two volumes stop agreeing, in one line.

    "replay unverified" only ever said the difference was too big to store as a
    patch. Whether the streams part at byte zero — a wrong build, wrong
    settings — or hold for thirty megabytes and then diverge is the entire
    question, and it costs one comparison pass to answer instead of a probe
    that has to guess which build the sweep chose."""
    la, lb = len(produced), len(original)
    n = min(la, lb)
    step = 1 << 20
    first = n
    for off in range(0, n, step):
        a, b = produced[off:off + step], original[off:off + step]
        if a != b:
            first = off + next(i for i in range(len(a)) if a[i] != b[i])
            break
    tail = 0
    limit = n - first
    while tail < limit:
        want = min(step, limit - tail)
        a = produced[la - tail - want:la - tail]
        b = original[lb - tail - want:lb - tail]
        if a != b:
            k = 0
            while k < want and a[want - 1 - k] == b[want - 1 - k]:
                k += 1
            tail += k
            break
        tail += want
    if first >= n and la == lb:
        return "identical"
    span = max(0, (lb - tail) - first)
    return (f"agrees for the first {first:,} B, differs over the next "
            f"{span:,} B, and the last {tail:,} B match"
            + (f" ({lb - la:+,} B of length)" if la != lb else ""))


def apply_delta(produced: bytes, patch: bytes) -> bytes:
    if patch.startswith(DELTA3_MAGIC):
        out = bytearray()
        i = len(DELTA3_MAGIC)
        n = int.from_bytes(patch[i:i + 4], "little")
        i += 4
        pos = 0
        for _ in range(n):
            at = int.from_bytes(patch[i:i + 8], "little")
            dl = int.from_bytes(patch[i + 8:i + 12], "little")
            il = int.from_bytes(patch[i + 12:i + 16], "little")
            i += 16
            out += produced[pos:at] + patch[i:i + il]
            i += il
            pos = at + dl
        out += produced[pos:]
        return bytes(out)
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

class _LockedConn:
    """A sqlite connection that owns the index lock until it is closed.

    Every DB helper in here already opens a connection, uses it, and closes it
    in a finally -- 21 of them. Rather than wrap all 21 in a lock and trust
    nobody to forget the 22nd, the lock is taken when the connection opens and
    released when it closes, so the try/finally that already exists does the
    unlocking."""

    def __init__(self, con, lock):
        self._con = con
        self._lock = lock
        self._closed = False

    def __getattr__(self, k):
        return getattr(self._con, k)

    def __enter__(self):
        return self._con.__enter__()

    def __exit__(self, *a):
        return self._con.__exit__(*a)

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            self._con.close()
        finally:
            self._lock.release()


class RsrToolAPI:
    def __init__(self):
        self._window = None
        self._stop = threading.Event()
        self._skip = threading.Event()
        self._running = False
        self._procs: set = set()
        self._proc_lock = threading.Lock()
        self._app_dir = Path(__file__).parent
        self._budget_min = 0
        self._content_root = None      # never delete the root itself
        # Per-CAPTURE state, kept per thread. With several releases in flight
        # at once these are the things that would otherwise collide: one
        # release's sweep deadline applied to another, one release's "budget
        # hit" relabelling the next as parked, one release's resume position
        # written against another's name. A thread-local slot gives each
        # capture its own without changing a single call signature.
        #
        # _consumed is HERE for the same reason, now that the rebuild also
        # runs several releases at once. It is what delete-source works from —
        # the last thing that should ever be shared or guessed at — so each
        # rebuild thread gets its own list and can never read another
        # release's sources and delete them. What may be deleted is still
        # decided centrally, against claims/built under the batch lock.
        # _content_root stays shared: it is one root for the whole batch.
        self._tl = threading.local()
        self._consumed = []            # content files a rebuild actually used
        # One writer at a time. Several captures now run at once and all of
        # them record rows, misses and learned recipes. sqlite would serialise
        # them anyway -- but by RAISING once its timeout expires, and _db_miss
        # and _db_forget swallow exceptions, so a contended write was a record
        # that vanished with nothing said. Re-entrant, because a couple of
        # helpers open a connection while already holding one.
        self._db_lock = threading.RLock()
        self._seeded = False           # recipe priors backfilled this process

    # ── plumbing ──────────────────────────────────────────────────────────

    # ── per-capture state, one slot per thread (see __init__) ────────────

    @property
    def _consumed(self) -> list:
        """Content files THIS thread's rebuild actually read (see __init__)."""
        v = getattr(self._tl, "consumed", None)
        if v is None:
            v = self._tl.consumed = []
        return v

    @_consumed.setter
    def _consumed(self, v):
        self._tl.consumed = list(v)

    @property
    def _deadline(self):
        return getattr(self._tl, "deadline", None)

    @_deadline.setter
    def _deadline(self, v):
        self._tl.deadline = v

    @property
    def _budget_hit(self):
        return getattr(self._tl, "budget_hit", False)

    @_budget_hit.setter
    def _budget_hit(self, v):
        self._tl.budget_hit = v

    @property
    def _budget_override(self):
        return getattr(self._tl, "budget_override", False)

    @_budget_override.setter
    def _budget_override(self, v):
        self._tl.budget_override = v

    @property
    def _sweep_pos(self):
        return getattr(self._tl, "sweep_pos", (0, ""))

    @_sweep_pos.setter
    def _sweep_pos(self, v):
        self._tl.sweep_pos = v

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
        # With several captures in flight the log interleaves, and a bare
        # "extracting 4,098 MB" belongs to no visible release. The tag is
        # thread-local, so each capture stamps its own lines and a single
        # capture prints exactly as it always did.
        tag = getattr(self._tl, "tag", "")
        if tag and msg.strip() and "══" not in msg:
            msg = f"{tag}{msg}"
        self._emit("log", {"msg": msg, "cls": cls})
        self._log_to_file(msg, cls)

    def _progress(self, msg: str):
        self._emit("progress", {"msg": msg})

    # ── the log on disk ─────────────────────────────────────────────────────
    # The log window is the only record a run leaves, and it lives in the
    # browser: close the window and a night of scanning goes with it. An
    # overnight run that errors on one group is exactly when the lines matter
    # and exactly when they are hardest to keep, so mirror every line to a
    # file that outlives the window and can be read back afterwards.
    _LOG_MAX_BYTES = 20 << 20        # roll at 20 MB, keep one previous file

    @property
    def _log_path(self) -> Path:
        return self._app_dir / "rsr_tool.log"

    def _log_to_file(self, msg: str, cls: str = "info"):
        with _LOG_LOCK:
            try:
                path = self._log_path
                if (path.exists()
                        and path.stat().st_size > self._LOG_MAX_BYTES):
                    prev = path.parent / (path.name + ".1")
                    try:
                        prev.unlink()
                    except OSError:
                        pass
                    path.rename(prev)
                stamp = time.strftime("%Y-%m-%d %H:%M:%S")
                with path.open("a", encoding="utf-8", errors="replace") as fh:
                    fh.write(f"{stamp} [{cls}] {msg}" + chr(10))
            except Exception:
                pass          # a log that breaks the run is worse than no log

    def log_path(self) -> str:
        """Where the run log is written (for the GUI's 'Open log' button)."""
        try:
            self._log_to_file("log path requested", "dim")
            return str(self._log_path)
        except Exception:
            return ""

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

    def _run_until(self, cmd: list, ready, timeout: int,
                   heartbeat: str = "", cwd=None) -> bool:
        """Run a pack and kill it the moment `ready()` says there is enough
        output to judge it on.

        The INPUT still has to be whole. Multithreaded rar derives its
        per-thread chunk boundaries from the total length of what it is given,
        so a shortened source produces different bytes and proves nothing —
        a mistake already made once in this project, and the reason a whole
        generation of srrdb verdicts had to be thrown away. What can be cut
        short is the OUTPUT: a wrong build diverges inside volume one and never
        recovers, so everything after volume one is work spent producing
        evidence nobody reads."""
        try:
            p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL,
                                 cwd=(str(cwd) if cwd else None),
                                 **_no_window())
        except Exception:
            return False
        with self._proc_lock:
            self._procs.add(p)
        try:
            deadline = time.monotonic() + timeout
            while True:
                if p.poll() is not None:
                    return p.returncode == 0
                if self._stop.is_set() or self._skip.is_set():
                    p.kill()
                    return False
                try:
                    if ready():
                        p.kill()
                        p.wait(timeout=30)
                        return True
                except OSError:
                    pass
                if time.monotonic() > deadline:
                    p.kill()
                    return False
                time.sleep(0.05)
        finally:
            with self._proc_lock:
                self._procs.discard(p)

    def _run(self, cmd: list, timeout: int, heartbeat: str = "",
             cwd=None) -> bool:
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
                                 stderr=subprocess.DEVNULL,
                                 cwd=(str(cwd) if cwd else None),
                                 **_no_window())
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
            # Extra roots scanned in the same run. `source` stays the first one
            # so a settings file written before today still loads, and so a
            # single folder needs no list at all.
            "sources": [str(p) for p in (cfg.get("sources") or []) if str(p).strip()],
            "store": cfg.get("store", str(self._app_dir / "rsr_store")),
            "max_mt": _num(cfg.get("max_mt"), 16, int),
            "embed_extras": bool(cfg.get("embed_extras", True)),
            "embed_max_mb": _num(cfg.get("embed_max_mb"), 16, int),
            "write_srr": bool(cfg.get("write_srr", True)),
            "skip_done": bool(cfg.get("skip_done", True)),
            # Off by default -- see _dict_candidates: 7,541 of 7,541.
            "dict_ladder": bool(cfg.get("dict_ladder", False)),
            "budget_min": _num(cfg.get("budget_min"), 45, int),
            "retry_walls": bool(cfg.get("retry_walls", False)),
            "small_first": bool(cfg.get("small_first", True)),
            # rar THREADS the sweep may use at once; 0 = auto (half the
            # machine, so it stays usable while a scan runs). See _cpu_budget.
            "workers": _num(cfg.get("workers"), 0, int),
            # Releases captured at once. See _job_slots.
            "jobs": max(1, min(16, _num(cfg.get("jobs"), 1, int))),
            # Releases REBUILT at once. Separate from `jobs` because a rebuild
            # replays one command at its recipe's own -mt. See _rebuild_slots.
            "rebuild_jobs": max(1, min(8, _num(cfg.get("rebuild_jobs"), 1, int))),
            # Not a setting — what the machine has, so the GUI can size its
            # slider and say what "auto" currently works out to.
            "cores": os.cpu_count() or 0,
        }

    def save_settings(self, s: dict) -> dict:
        cur = self.get_settings()
        s = s or {}
        out = {
            "source": (s.get("source") or cur["source"]).strip(),
            "sources": ([str(p).strip() for p in s["sources"] if str(p).strip()]
                        if isinstance(s.get("sources"), list) else cur["sources"]),
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
            "workers": max(0, min(256, _num(s.get("workers"),
                                            cur["workers"], int))),
            "jobs": max(1, min(16, _num(s.get("jobs"), cur["jobs"], int))),
        }
        # Keep anything already in the file that this method does not model.
        # It writes a fixed whitelist, so every save silently DROPPED the
        # priors-import bookkeeping (priors_imported_mtime / _records) that
        # import_srrdb_priors writes — which made the "there are new srrdb
        # results to import" reminder permanent, and made "when did I last
        # import?" unanswerable, however many times the import had run.
        try:
            raw = json.loads(self._config_path.read_text("utf-8"))
        except Exception:
            raw = {}
        if isinstance(raw, dict):
            for k, v in raw.items():
                out.setdefault(k, v)
        try:
            self._config_path.write_text(json.dumps(out, indent=2), "utf-8")
        except Exception as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, "settings": out}

    def store_status(self, path: str = "") -> dict:
        """Whether `path` is where the .rsr files this index knows about live.

        The Store box is one stray keystroke or one mis-click in a folder
        picker away from pointing somewhere else, and nothing about a run would
        look wrong: captures succeed, the index records them, and the corpus is
        quietly in two places. The index already knows the answer — every row
        carries the path its .rsr was written to — so ask it rather than
        remembering a setting that is itself the thing in doubt."""
        want = str(path or self.get_settings()["store"]).strip()
        try:
            con = sqlite3.connect(f"file:{self._db_path}?mode=ro", uri=True)
            try:
                rows = [r[0] for r in con.execute(
                    "SELECT rsr_path FROM releases WHERE rsr_path IS NOT NULL "
                    "AND rsr_path <> ''").fetchall()]
            finally:
                con.close()
        except Exception:
            return {"ok": True, "differs": False, "count": 0, "expected": ""}
        if not rows:
            return {"ok": True, "differs": False, "count": 0, "expected": ""}
        # The store root is the common ancestor of every .rsr in it: the layout
        # below it is SYSTEM/YEAR/RELEASE, so a few hundred rows converge on it.
        try:
            root = Path(os.path.commonpath([str(Path(r).parent) for r in rows]))
        except ValueError:
            return {"ok": True, "differs": False, "count": len(rows),
                    "expected": ""}
        # Walk up out of SYSTEM/YEAR/RELEASE if every row shares those levels.
        here = Path(want) if want else None
        same = False
        if here is not None:
            try:
                same = here.resolve() == root.resolve() \
                    or root.resolve().is_relative_to(here.resolve()) \
                    or here.resolve().is_relative_to(root.resolve())
            except OSError:
                same = str(here) == str(root)
        return {"ok": True, "differs": not same, "count": len(rows),
                "expected": str(root), "chosen": want}

    # ── the WinRAR build pack ─────────────────────────────────────────────

    def _comment_via_rar(self, head: Path) -> str:
        """The archive comment, read with rar.exe instead of rarfile.

        rarfile needs an unrar binary to decompress a RAR3 comment and there
        is none here; the build pack is rar.exe, the packer. But `rar cw`
        writes the comment out, and the packer does that perfectly well — so
        the one thing the stub in _open_rar() costs us is recoverable from the
        232 executables already sitting in the pack.

        This is not cosmetic. Micronauts' Lego_Batman carries WinRAR's own
        default comment, 56 bytes of it, and it occupies 97 bytes of the first
        volume. Without it the replay's first volume is 97 bytes short, every
        following byte shifts, and eleven volumes that are otherwise correct
        down to the recovery record come back as "replay unverified"."""
        exes = self._pack_exes()
        if not exes:
            return ""
        work = Path(tempfile.mkdtemp(prefix="rsr-cmt-", dir=self._work_root()))
        try:
            out = work / "comment.txt"
            for exe in reversed(exes[-4:]):        # newest first
                try:
                    subprocess.run([str(exe), "cw", str(head), str(out)],
                                   capture_output=True, timeout=120,
                                   cwd=str(work), **_no_window())
                except Exception:
                    continue
                if out.is_file() and out.stat().st_size:
                    raw = out.read_bytes()
                    for enc in ("utf-8", "cp437", "latin-1"):
                        try:
                            return raw.decode(enc)
                        except UnicodeDecodeError:
                            continue
                    return raw.decode("utf-8", "replace")
            return ""
        finally:
            _rmtree(work)

    @staticmethod
    def _work_root() -> Path:
        """One parent for every scratch folder this tool makes.

        They used to go straight into %TEMP% as rsr-XXXXXXXX, which works but
        makes them impossible to name: excluding them from a virus scanner
        would have meant excluding the whole of %TEMP%, and %TEMP% is where
        every installer and browser on the machine stages its downloads. That
        is far too much to give up for one release.

        And it does need excluding. A scene release that ships a TOOL —
        NINTENDO_DS_BETA_DUMPER-IND packs nds-dumper-beta.exe — gets its source
        quarantined between the extract and the read, and the capture fails
        with "Permission denied" on a file rar wrote seconds earlier. Nothing
        is wrong with the release or the tool; a 2008 homebrew dumper simply
        trips a heuristic.

        Still under %TEMP% rather than beside the code: these hold the whole
        extracted source, which is gigabytes for a 3DS release, and the system
        temp drive is the one with room for that."""
        root = Path(tempfile.gettempdir()) / "rsr-work"
        root.mkdir(parents=True, exist_ok=True)
        return root

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
        wanted = [s["source"]] + list(s["sources"])
        roots, seen = [], set()
        for w in wanted:
            w = str(w or "").strip()
            if not w:
                continue
            p = Path(w)
            key = str(p.resolve()).lower() if p.exists() else w.lower()
            if key not in seen:
                seen.add(key)
                roots.append(p)
        missing = [str(p) for p in roots if not p.is_dir()]
        if missing:
            return {"ok": False,
                    "error": f"Folder not found: {missing[0]}"
                             + (f" (and {len(missing) - 1} more)"
                                if len(missing) > 1 else "")}
        if not roots:
            return {"ok": False, "error": "Source folder not found"}

        def _bg():
            self._running = True
            self._stop.clear()
            self._skip.clear()
            try:
                self._scan_run(roots, Path(s["store"]), s)
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
        """A source may be one release folder, or a parent full of them — or,
        as a year-per-folder library is, a parent of parents.

        Archives sitting loose in `src` mean `src` IS the release. Otherwise
        its subfolders are examined the same way, because stopping after one
        level is what turned `NDS/NDS Scene 2005/<release>` into a single
        "NDS Scene 2005" release holding all 91 zips of the year: one .rsr
        that can only ever be rebuilt from one folder, and 91 real releases
        whose content it then shadowed in the index.

        A container is told from a release by its LOOSE files, not by its
        depth: a scene release folder always carries at least the nfo beside
        whatever it packs, and a year folder carries nothing at all. Depth is
        capped so a wrong root cannot turn into a full-disk walk."""

        def walk(d: Path, depth: int) -> list[Path]:
            try:
                kids = sorted(d.iterdir())
            except OSError:
                return [d]
            if any(_classify_volume(p.name) for p in kids if p.is_file()):
                return [d]
            subs = [p for p in kids if p.is_dir()]
            # Any loose file at all makes this a release — an nfo-only or
            # sfv-only folder is still a release and must not be descended
            # past. No loose files and no subfolders is an empty folder, which
            # the caller reports as such.
            if not subs or any(p.is_file() for p in kids) or depth <= 0:
                return [d]
            found = []
            for sub in subs:
                found.extend(walk(sub, depth - 1))
            return found

        return walk(src, 4) or [src]

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

    def _scan_run(self, roots, store: Path, s: dict):
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
        if isinstance(roots, (str, Path)):
            roots = [Path(roots)]
        # One list across every root, de-duplicated: nested or repeated roots
        # would otherwise capture the same release twice in one run.
        folders, seen = [], set()
        for root in roots:
            for f in self._release_folders(Path(root)):
                key = str(f.resolve()).lower()
                if key not in seen:
                    seen.add(key)
                    folders.append(f)
        # The store the index says it has been using. A run that quietly writes
        # somewhere else splits the corpus in two and nothing about it looks
        # wrong until a rebuild cannot find its .rsr.
        st = self.store_status(str(store))
        if st.get("differs") and st.get("count"):
            self._log(f"⚠ Store is {store}, but the {st['count']:,} release(s) "
                      f"already indexed live under {st['expected']}. Captures "
                      f"from this run will go somewhere else — stop now if that "
                      f"was not deliberate.", "warn")
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
        if len(roots) > 1:
            self._log(f"{len(folders)} release folder(s) across "
                      f"{len(roots)} folders:", "info")
            for root in roots:
                self._log(f"    {root}", "dim")
        else:
            self._log(f"{len(folders)} release folder(s) under {roots[0]}",
                      "info")
        self._log(f"Build pack: {len(exes)} exe(s)   ·   store: {store}", "dim")

        done = ok = failed = skipped = zips = parked = walls = meta = 0
        partial = broken = 0
        # ── capture, several releases at a time ──────────────────────────
        #
        # One release at a time left the machine at 15% CPU: a release that
        # lands on its group prior does ONE pack at -mt8, and around it sits a
        # single-threaded extract and a single-threaded hash of the original
        # streams. Parallelising the sweep does nothing for that, because such
        # a release barely sweeps. Releases are the unit with real work in
        # them, so they are what has to overlap.
        #
        # `jobs` is read afresh every time a slot frees, so the count can be
        # changed while a scan runs, exactly like the core budget. The core
        # budget is DIVIDED between whatever is in flight, so four releases do
        # not each ask for the whole machine.
        lock = threading.Lock()
        counters = {"done": 0, "ok": 0, "failed": 0, "skipped": 0, "zips": 0,
                    "parked": 0, "walls": 0, "meta": 0, "partial": 0}

        def _one(i, folder):
            rel_short = _release_name(folder)[:22]
            self._tl.tag = (f"<{rel_short}> "
                            if self._job_slots() > 1 else "")

            if self._stop.is_set():
                self._log("Stopped.", "warn")
                return
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
                with lock:
                    counters["skipped"] += 1
                return
            wall = None if s.get("retry_walls") else self._known_wall(rel)
            if wall:
                self._log(f"  Already swept to exhaustion on "
                          f"{(wall[0] or '')[:10]} against {wall[1]} build(s) — "
                          f"skipping (tick '{UI_RETRY_WALLS}' to redo).", "dim")
                self._emit("row", {"name": rel, "status": "skipped",
                                   "recipe": "known wall",
                                   "kind": "wall"})
                with lock:
                    counters["walls"] += 1
                return
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
                with lock:
                    counters["skipped"] += 1
                return
            if res.get("error") == "zip release":
                # Out of scope, not a failure. Counted apart so the summary's
                # "failed" figure stays a number worth reading.
                self._emit("row", {"name": rel, "status": "skipped",
                                   "recipe": "zip", "kind": "zip"})
                with lock:
                    counters["zips"] += 1
                return
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
                return
            if res.get("partial"):
                # Same reasoning: a fix release is complete in itself, it just
                # is not a set anyone can rebuild on its own.
                self._db_forget(rel)
                self._emit("row", {"name": rel, "status": "skipped",
                                   "recipe": "fix release — partial set",
                                   "kind": "partial"})
                with lock:
                    counters["partial"] += 1
                return
            if self._budget_hit and not res.get("ok"):
                # Not a wall and not a failure — an unfinished search. Kept
                # apart so a later re-run (with better priors) can be pointed
                # at exactly these, and so the failed count stays meaningful.
                pos, sig = getattr(self, "_sweep_pos", (0, ""))
                self._db_miss(rel, "parked", "time budget", len(exes),
                              combos=pos, order_sig=sig)
                # Its own status word, not "skipped". A parked release was
                # searched and ran out of time; a skipped one was never looked
                # at. Reading the same grey "skipped" for both hides the
                # unfinished work in among the two thousand deliberate passes.
                self._emit("row", {"name": rel, "status": "parked",
                                   "recipe": "parked — time budget",
                                   "kind": "parked"})
                with lock:
                    counters["parked"] += 1
                return
            with lock:
                counters["done"] += 1
            if res.get("metadata"):
                # A complete release that simply has no archive. Counted apart
                # so "verified" keeps meaning "a recipe was proved".
                with lock:
                    counters["meta"] += 1
                self._db_forget(rel)
                self._emit("row", {"name": rel, "status": "done",
                                   "recipe": res.get("recipe", "metadata only"),
                                   "kind": "metadata"})
                return
            if res.get("ok"):
                with lock:
                    counters["ok"] += 1
                self._db_forget(rel)          # it worked; the miss is stale
                self._emit("row", {"name": rel, "status": "done",
                                   "recipe": res.get("recipe", ""),
                                   "kind": "ok"})
            else:
                with lock:
                    counters["failed"] += 1
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


        # Keep `jobs` slots busy, re-reading the setting each time one frees.
        # The queue lives on self so that requeue() can add to a run that is
        # already going: on a long scan the useful moment to retry a parked
        # release is while you are watching it park, not tomorrow.
        pending = list(enumerate(folders, 1))
        self._queue = pending
        self._queue_lock = threading.Lock()
        self._queue_by_name = {_release_name(f): f for f in folders}
        self._queue_total = len(folders)
        live: list = []
        while pending or live:
            if self._stop.is_set():
                break
            want = self._job_slots()
            while len(live) < want:
                with self._queue_lock:
                    if not pending:
                        break
                    i, folder = pending.pop(0)
                th = threading.Thread(target=_one, args=(i, folder),
                                      daemon=True)
                th.start()
                live.append(th)
                self._live_jobs = len(live)
            if not live:
                break
            # Reap whatever has finished; a short join keeps the loop
            # responsive to a `jobs` change and to Stop.
            live[0].join(timeout=0.5)
            live = [th for th in live if th.is_alive()]
            self._live_jobs = max(1, len(live))
        for th in live:
            th.join()
        done = counters["done"]; ok = counters["ok"]
        failed = counters["failed"]; skipped = counters["skipped"]
        zips = counters["zips"]; parked = counters["parked"]
        walls = counters["walls"]; meta = counters["meta"]
        partial = counters["partial"]

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
            if kinds & {".lha", ".lzh"}:
                lhas = sorted(q for q in folder.rglob("*")
                              if q.is_file()
                              and q.suffix.lower() in (".lha", ".lzh"))
                return self._capture_lha(folder, store, s, rel, lhas)
            # A metadata-only fix (DIRFIX, NFOFIX, …) IS the release — it never
            # had an archive, so reporting "no archive set found" calls a
            # complete release a miss and buries it among the real ones. There
            # is still something worth keeping: the nfo is the entire artefact.
            files = [p for p in folder.rglob("*") if p.is_file()]
            if files and all(p.suffix.lower() in _SIDECAR_EXT for p in files):
                return self._capture_metadata(folder, store, s, rel, files)
            # "no archive" is the largest error class in the index — 338 of
            # 360 — and it was three completely different things wearing one
            # label, which is why it never got looked at. Say which.
            loose = sorted({p.suffix.lower() for p in files
                            if p.suffix.lower() not in _SIDECAR_EXT})
            foreign = [x for x in loose if x in _FOREIGN_ARCHIVE_EXT]
            if foreign:
                # An archive we do not read. 329 of those 338 are .lzh/.lha —
                # the 1996 PSX DOX scene packed in LHA, not RAR or ZIP — and
                # calling them "no archive" said the folder was empty of
                # archives when it is nothing but archive. Nothing here is
                # broken and nothing is missing; the format is simply outside
                # what this tool packs.
                self._log(f"  {', '.join(foreign)} archive — outside the "
                          f"RAR, ZIP and LHA formats this tool reads. Not a "
                          f"miss: there is no recipe to look for.", "warn")
                return {"ok": False,
                        "error": f"unsupported archive format "
                                 f"({', '.join(foreign)})"}
            if any(_classify_volume(p.name) for p in files):
                # Volume parts with nothing that carries a RAR marker: the
                # head of the set is missing, so there is no archive to read
                # even though the folder is full of one.
                self._log("  Volume parts with no readable head volume — the "
                          "first part of the set is missing, so there is "
                          "nothing to read the recipe out of.", "warn")
                return {"ok": False, "error": "incomplete set — head volume "
                                              "missing"}
            if loose:
                # An UNPACKED release: the content is sitting loose and the
                # archive that carried it is gone. A .rsr is a recipe for
                # reproducing an archive, and this folder has none, so no
                # re-run and no bigger build pack will change the answer.
                self._log(f"  Unpacked release — {', '.join(loose)} sitting "
                          f"loose with no archive around it. A .rsr describes "
                          f"how to rebuild an archive, so there is nothing "
                          f"here to capture; this will not change on a "
                          f"re-run.", "warn")
                return {"ok": False, "error": "unpacked — no archive to "
                                              "reproduce"}
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

        work = Path(tempfile.mkdtemp(prefix="rsr-", dir=self._work_root()))
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
        """Take the entry payloads back out of the .pcf.

        Same trick as the ZIP skeleton: what is left is headers and preflate's
        reconstruction data — 86 KB against 34 MB of rom on Dragon_Dance — and
        the content is supplied again at rebuild.

        `payloads` is (name, expanded, raw) per entry, and BOTH forms have to
        be looked for, because a .pcf is a mixture of the two. precomp expands
        the streams whose parameters preflate can derive and LEAVES THE REST
        COMPRESSED, silently — CYB_COV1 is 14 jpgs of which it expands 12 and
        skips BUSTMV2F and INTMO-F. Searching only for the expanded form found
        12 of 14 and reported "the content is not a contiguous run", which read
        as a preflate failure when preflate had in fact reconstructed the
        archive perfectly. That is why the many-file zips all failed together
        and the three-entry ones mostly passed: one skipped stream is enough,
        and the more streams an archive has the likelier one is skipped.

        The walk is forward-only, so the holes come out in ascending order and
        hole i is payload i — an invariant the caller and the manifest both
        rely on. Where a payload matches in more than one place it BACKTRACKS
        rather than committing to the first hit: identical members are legal
        (the 2005 LGC trainers ship bunzip2.exe and bzip2.exe byte-identical)
        and a two-byte member — CDRUTILS ships a 2 B FRL.NFO — matches almost
        anywhere, so the first hit is not always the one that lets the rest of
        the archive resolve."""
        n = len(payloads)
        for _nm, exp, rawb in payloads:
            if not exp and not rawb:
                return None                      # nothing to look for

        MAX_CAND = 64                            # per entry
        MAX_STEPS = 50_000                       # over the whole walk

        def cands(i: int, start: int) -> list:
            """Where payload i could sit at or after `start`, nearest first."""
            out, seen = [], set()
            exp, rawb = payloads[i][1], payloads[i][2]
            forms = [("expanded", exp)]
            if rawb and rawb != exp:
                forms.append(("raw", rawb))
            for form, data in forms:
                if not data:
                    continue
                probe = data[:1 << 16]
                j = pcf.find(probe, start)
                while j >= 0 and len(out) < MAX_CAND:
                    if j not in seen and pcf[j:j + len(data)] == data:
                        seen.add(j)
                        out.append((j, len(data), form))
                    j = pcf.find(probe, j + 1)
            out.sort()
            return out

        # Iterative, not recursive: a zip may hold thousands of members and
        # one frame per member would run into the interpreter's stack limit.
        holes: list = []
        levels: list = []
        i = steps = 0
        while i < n:
            if steps > MAX_STEPS:
                return None
            if i == len(levels):
                start = holes[-1][0] + holes[-1][1] if holes else 0
                levels.append([cands(i, start), 0])
            cs, k = levels[i]
            if k >= len(cs):
                levels.pop()
                i -= 1
                if i < 0:
                    return None                  # no arrangement works
                holes.pop()
                levels[i][1] += 1
                continue
            off, ln, form = cs[k]
            steps += 1
            holes.append([off, ln, payloads[i][0], form])
            i += 1

        skel = bytearray()
        pos = 0
        for off, ln, _nm, _form in holes:
            if off < pos:
                return None                      # overlapping payloads
            skel += pcf[pos:off]
            pos = off + ln
        skel += pcf[pos:]
        return bytes(skel), holes

    def _capture_lha(self, folder: Path, store: Path, s: dict, rel: str,
                     archives: list[Path]) -> dict:
        """Capture an LHA/LZH release: headers verbatim, stored members loose.

        The same contract as everywhere else — the .rsr is written only after
        it has been reassembled here and compared byte for byte with the
        original archive.

        The honest limit is stated in the LHA section above: an `-lh5-` stream
        can only be carried, never derived. So a capture comes out at one of
        two grades, and the manifest records which:

          * `skeleton` — the content member is stored, so it is supplied loose
            at rebuild exactly as a rom is, and the .rsr is a real recipe
            (~65% of the archive across the 42 that qualify);
          * `carried` — the content member is compressed, so every stream is
            carried and the .rsr weighs about what the archive does. It is a
            verified container rather than a recipe. Worth keeping, because a
            complete indexed corpus that rebuilds is worth more than the
            bytes — but labelled, so nothing downstream reads it as a recipe.
        """
        manifest = {
            "rsr_version": RSR_VERSION, "magic": RSR_MAGIC,
            "created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "tool": f"tosort_toolkit rsr_tool {RSR_VERSION}",
            "host": {"platform": platform.platform(),
                     "python": platform.python_version()},
            "release": rel, "system": _release_system(rel),
            "year": _release_year(folder, rel), "tag": _release_tag(folder.name),
            "kind": "lha", "source_folder": str(folder), "sets": [],
        }
        embedded: dict[str, bytes] = {}
        cap_b = max(0, int(s.get("embed_max_mb", 16))) * 1024 * 1024
        for ai, ap in enumerate(archives):
            raw = ap.read_bytes()
            ents = lha_members(raw)
            if not ents:
                self._log(f"  ✗ {ap.name}: not a readable LHA archive.", "err")
                return {"ok": False, "error": f"{ap.name}: unreadable LHA"}
            skel, holes = lha_skeleton(raw, ents)
            meths = ", ".join(sorted({e["method"] for e in ents}))
            self._log(f"  {ap.name}  ·  LHA  ·  {len(ents)} member(s)  ·  "
                      f"{meths}  ·  {len(skel):,} B of header carried verbatim",
                      "dim")
            biggest = max((e["size"] or 0) for e in ents)
            big_e = max(ents, key=lambda e: e["size"] or 0)
            # The grade of this set. A stored content member can be handed
            # back loose at rebuild and the capture is a recipe; a compressed
            # one cannot be, so everything has to be carried.
            derivable = big_e["method"] in LHA_STORED
            if not derivable:
                self._log(f"    the content member ({big_e['name']}) is "
                          f"{big_e['method']} — an LZH stream cannot be "
                          f"derived, so every member is carried and this is a "
                          f"container rather than a recipe.", "dim")
            files = []
            for idx, e in enumerate(ents):
                blob = raw[e["data_offset"]:
                           e["data_offset"] + e["packed_size"]]
                rec = dict(e)
                big = ((e["size"] or 0) >= biggest
                       or (cap_b and (e["size"] or 0) >= cap_b))
                stored = e["method"] in LHA_STORED
                loose = big and stored and derivable
                rec["source"] = "content" if loose else "extra"
                # A stored member IS its file, so its CRC32 is computable
                # and the index gets a real one. A compressed member's is not,
                # without an LZH decoder — it is carried verbatim and never
                # looked up by hash, so NULL is the honest value.
                rec["crc32"] = zlib.crc32(blob) if stored else None
                if loose:
                    # It can be matched back by hash and handed in loose at
                    # rebuild, same as a rom.
                    rec["sha256"] = _sha256(blob)
                    rec["recipe"] = {"impl": "stored", "label": "stored"}
                else:
                    key = f"lha/{ai}/{idx}.bin"
                    embedded[key] = blob
                    rec["stored"] = key
                    rec["recipe"] = {"impl": "verbatim", "label": "carried"}
                files.append(rec)

            # Prove it before anything is written: put every member back and
            # compare with the archive on disk.
            streams = {}
            for rec, e in zip(files, ents):
                off = e["data_offset"]
                streams[off] = (embedded[rec["stored"]] if rec.get("stored")
                                else raw[off:off + e["packed_size"]])
            if zip_assemble(skel, holes, streams) != raw:
                self._log(f"  ✗ {ap.name}: reassembly did not reproduce the "
                          f"archive byte-exact — refusing it.", "err")
                return {"ok": False,
                        "error": f"{ap.name}: reassembly not byte-exact"}

            key = f"lha/{ai}/skeleton.bin"
            embedded[key] = skel
            total = sum(len(zlib.compress(v, 9)) for k, v in embedded.items()
                        if k.startswith(f"lha/{ai}/"))
            # A container should weigh about what the archive does.
            # Meaningfully MORE is not a container, it is a bug — so this is a
            # sanity guard now, not a size policy.
            if total > len(raw) * 1.15:
                self._log(f"    ✗ the .rsr would be {total:,} B for a "
                          f"{len(raw):,} B archive — larger than the thing it "
                          f"describes, which should not happen. Refusing.",
                          "err")
                return {"ok": False,
                        "error": f"{ap.name}: capture larger than the archive"}
            n_extra = sum(1 for f in files if f["source"] == "extra")
            pct = total / max(len(raw), 1) * 100
            if derivable:
                self._log(f"    RECIPE — {n_extra} member(s) carried, content "
                          f"supplied at rebuild: {total:,} B against a "
                          f"{len(raw):,} B archive ({pct:.0f}%).", "ok")
            else:
                self._log(f"    CONTAINER — all {n_extra} member(s) carried: "
                          f"{total:,} B against a {len(raw):,} B archive "
                          f"({pct:.0f}%). Verified and rebuildable, but not a "
                          f"recipe.", "dim")
            manifest["sets"].append({
                "stem": ap.stem, "format": "LHA", "name": ap.name,
                "size": len(raw), "sha256": _sha256(raw),
                "method": "skeleton" if derivable else "carried",
                "skeleton": key,
                "holes": [[h[0], h[1]] for h in holes],
                "hole_names": [e["name"] for e in
                               sorted(ents, key=lambda x: x["data_offset"])],
                "files": files, "verify": "exact",
            })

        if not manifest["sets"]:
            return {"ok": False, "error": "no LHA archive captured"}
        manifest["sidecars"] = self._capture_sidecars(folder, s, embedded,
                                                      manifest)
        out_dir = self._store_dir(store, folder, rel)
        out_dir.mkdir(parents=True, exist_ok=True)
        rsr_path = out_dir / f"{rel}.rsr"
        self._write_rsr(rsr_path, manifest, embedded)
        self._db_record(manifest, rsr_path)
        self._log(f"  ✓ {rsr_path.name} written "
                  f"({rsr_path.stat().st_size:,} B) — VERIFIED", "ok")
        return {"ok": True, "recipe": "LHA skeleton"}

    def _capture_zip(self, folder: Path, store: Path, s: dict, rel: str,
                     zips: list[Path]) -> dict:
        """Capture a ZIP release: headers verbatim, streams by recipe.

        The same rule as everywhere else — the .rsr is written only after it
        has been reassembled here and compared byte for byte with the original
        archive."""
        work = Path(tempfile.mkdtemp(prefix="rsr-zip-", dir=self._work_root()))
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
                    cut = zip_truncated(zp)
                    if cut:
                        self._log(f"  ✗ {zp.name}: {cut}.", "err")
                        return {"ok": False,
                                "error": f"{zp.name}: truncated zip"}
                    self._log(f"  ✗ {zp.name}: cannot read — encrypted, or no "
                              "member survives a CRC check from either the "
                              "central directory or the local headers.", "err")
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
                bigs = [((e["size"] or 0) >= biggest
                         or (cap and (e["size"] or 0) >= cap))
                        for e in ents]
                prefer_pf = self._zip_prefers_preflate(_release_group(rel))
                for e in ents:
                    with open(zp, "rb") as fh:
                        fh.seek(e["data_offset"])
                        rawe = fh.read(e["packed_size"])
                    rec = dict(e)
                    big = ((e["size"] or 0) >= biggest
                           or (cap and (e["size"] or 0) >= cap))
                    rec["source"] = "content" if big else "extra"
                    # A rom is matched back to its release on size+CRC32, and a
                    # collision is settled on SHA-256. A RAR capture records
                    # one per file and a ZIP capture recorded none, so on the
                    # ZIP side there was nothing to settle WITH — and the
                    # tie-break treated "no sha" as "not this one", which is
                    # how a zip release lost its own rom to a later re-pre.
                    plain = None
                    if big:
                        try:
                            plain = (rawe if e["method"] == 0
                                     else zlib.decompress(rawe, -15))
                        except zlib.error:
                            plain = None
                        if plain is not None:
                            rec["sha256"] = _sha256(plain)
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
                        data = (plain if plain is not None
                                else zlib.decompress(rawe, -15))
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
                    # Both forms of every entry: a .pcf mixes expanded and
                    # still-compressed streams (see _pcf_cut).
                    payloads = [(e["name"], zip_payload(raw, e),
                                 raw[e["data_offset"]:
                                     e["data_offset"] + e["packed_size"]])
                                for e in ents]
                    cut = self._pcf_cut(pcf, payloads)
                    if cut is None:
                        self._log("    ✗ preflate ran, but the content is not "
                                  "a contiguous run in its output — it cannot "
                                  "be cut back out.", "err")
                        return {"ok": False,
                                "error": f"{zp.name}: preflate output could "
                                         f"not be separated from the content"}
                    skel, holes = cut
                    # Which form of each entry the .pcf actually holds. An
                    # entry precomp left compressed is fine as an EXTRA — the
                    # capture simply carries those bytes instead of the
                    # expanded ones, and carries fewer of them. It is fatal for
                    # CONTENT: content is supplied loose at rebuild, expanded,
                    # and putting it back compressed would mean re-deflating it
                    # exactly — the one thing preflate was called in to avoid.
                    pf_bytes = [payloads[i][1] if h[3] == "expanded"
                                else payloads[i][2]
                                for i, h in enumerate(holes)]
                    stuck = [h[2] for i, h in enumerate(holes)
                             if h[3] == "raw" and bigs[i]]
                    if stuck:
                        self._log(f"    ✗ preflate left the content stream "
                                  f"({', '.join(stuck[:3])}) compressed, so it "
                                  f"cannot be supplied from the loose file at "
                                  f"rebuild.", "err")
                        return {"ok": False,
                                "error": f"{zp.name}: preflate could not "
                                         f"expand the content stream"}
                    # Prove it here, exactly as a recipe is proved: put the
                    # content back, restore, and byte-compare.
                    # By position, not by name. `holes` is built by scanning
                    # forward through the .pcf in payload order, so hole i is
                    # payload i — whereas a name-keyed dict hands two
                    # same-named entries the same bytes, which the byte-compare
                    # below then rejects as a preflate failure it never was.
                    back = zip_assemble(skel, [[h[0], h[1]] for h in holes],
                                        {h[0]: pf_bytes[i]
                                         for i, h in enumerate(holes)})
                    restored = self._pcf_restore(back, work / "pf")
                    if restored != raw:
                        self._log("    ✗ preflate did not restore this archive "
                                  "byte-exact — refusing it.", "err")
                        return {"ok": False,
                                "error": f"{zp.name}: preflate did not restore "
                                         f"it byte-exact"}
                    key = f"zips/{zi_no}/preflate.bin"
                    # The sweep pass embedded each extra's ORIGINAL compressed
                    # stream before it gave up. preflate wants the expanded
                    # bytes instead, so those are dead weight now — and left in
                    # place they were counted a second time in the size test
                    # below, which is what refused both Ensata emulator
                    # releases for carrying nearly a megabyte they would never
                    # have carried.
                    for k in [k for k in embedded
                              if k.startswith(f"zips/{zi_no}/")]:
                        del embedded[k]
                    embedded[key] = skel
                    self._db_learn_zip(_release_group(rel),
                                       {"impl": "preflate",
                                        "label": self.PREFLATE_LABEL})
                    self._log(f"    ✓ preflate reconstructs this archive "
                              f"byte-exact — carrying {len(skel):,} B of "
                              f"reconstruction data instead of a recipe.", "ok")
                    files = []
                    for idx, e in enumerate(ents):
                        rec = dict(e)
                        big = ((e["size"] or 0) >= biggest
                               or (cap and (e["size"] or 0) >= cap))
                        rec["source"] = "content" if big else "extra"
                        rec["recipe"] = {"impl": "preflate", "label": "preflate"}
                        if big:
                            rec["sha256"] = _sha256(payloads[idx][1])
                        if not big:
                            k = f"zips/{zi_no}/pf/{len(files)}.bin"
                            # By position, not by name: two entries of the same
                            # name are legal in a ZIP and a dict silently keeps
                            # one of them. The bytes are whichever form the
                            # .pcf holds for this entry — the rebuild drops
                            # them straight back into the hole.
                            embedded[k] = pf_bytes[idx]
                            rec["stored"] = k
                        files.append(rec)
                    # Measure what the .rsr will actually cost, not what the
                    # payloads weigh loose: the container deflates everything
                    # it carries that is not already-compressed extras. Ensata
                    # is 1.9 MB of dlls and chm files expanded, 1.0 MB once
                    # written — comfortably under the 1.58 MB archive it
                    # replaces, where the raw total said give up.
                    total = sum(len(zlib.compress(v, 9))
                                for k, v in embedded.items()
                                if k.startswith(f"zips/{zi_no}/"))
                    if total >= len(raw):
                        # A release of nineteen similar jpgs has no small
                        # extras to carry cheaply — Contact-WTFE is exactly
                        # that. Promoting them to content was tried and is
                        # worse: it makes the .rsr a 59% copy of the archive
                        # AND demands all nineteen files back at rebuild. An
                        # honest refusal is the better answer; this shape is
                        # not what the format is for.
                        self._log(f"    ✗ preflate would carry {total:,} B "
                                  f"written for a {len(raw):,} B archive — "
                                  f"refusing, since that is no better than "
                                  f"keeping the archive.", "err")
                        return {"ok": False,
                                "error": f"{zp.name}: preflate data is larger "
                                         f"than the archive"}
                    n_extra = sum(1 for f in files if f["source"] == "extra")
                    self._log(f"    {n_extra} extra(s) carried alongside it: "
                              f"{total:,} B written against a "
                              f"{len(raw):,} B archive "
                              f"({total / len(raw) * 100:.0f}%).", "dim")
                    manifest["sets"].append({
                        "stem": zp.stem, "format": "ZIP", "name": zp.name,
                        "size": len(raw), "sha256": _sha256(raw),
                        "method": "preflate", "skeleton": key,
                        "holes": [[h[0], h[1]] for h in holes],
                        "hole_names": [h[2] for h in holes],
                        "hole_forms": [h[3] for h in holes],
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
            if (size, _file_crc32(p)) in content_ids and size >= LOOSE_CONTENT_MIN:
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
            rf = _open_rar(head, _end_cb)
        except Exception as e:
            if "first volume" in str(e).lower():
                # Started mid-set: the head volume is in another folder.
                return {"ok": False, "partial": True,
                        "error": "partial set: the first volume is missing"}
            raise
        try:
            infos = [i for i in rf.infolist() if i.is_file()]
            comment = rf.comment
            stubbed = getattr(rf, "_rsr_comment_stubbed", False)
        finally:
            rf.close()
        if stubbed and not comment:
            comment = self._comment_via_rar(head)
            if comment:
                self._log(f"    archive comment recovered with rar.exe "
                          f"({len(comment)} chars) — rarfile could not read it "
                          f"without an unrar binary.", "dim")
        if ends and ends[-1] & 0x0001:
            return {"ok": False, "partial": True,
                    "error": "partial set: the archive continues into a "
                             "volume that is not in this folder"}
        if not infos:
            return {"ok": False, "error": "no packed files"}

        meta = self._read_files(infos, st["format"])
        mflags = main_flags(head)
        # The main header is the authority on both of these; see main_flags().
        solid = any(f["solid"] for f in meta) or bool(mflags & MHD_SOLID)
        # NOT `locked` — that name is taken further down by the list of
        # source files antivirus grabbed after extraction, and a truthy empty
        # list quietly swallowed this flag once already.
        arch_locked = bool(mflags & MHD_LOCK)
        if arch_locked:
            self._log("    archive is LOCKED (-k) — the replay will lock too.",
                      "dim")
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
        # Where each packed file's FIRST volume slice lives in the original.
        # The sweep compares a candidate against these bytes and can stop as
        # soon as volume one is written, instead of compressing the entire
        # source to produce output nothing looks at.
        #
        # A single-volume original gets one too. The probe's command is OURS to
        # choose, so we can ask rar for volumes the original never had: cutting
        # the output does not change it (measured — a 40 MB source packed whole
        # and packed at -v1000000b give a byte-identical stream over the common
        # prefix). Only for sets that are actually COMPRESSED: rar decides
        # store-vs-compress differently when streaming to volumes, and a stored
        # set needs no probe anyway because storing is deterministic and the
        # sweep ends on its first combo.
        prefix = {}
        for f in meta:
            b = blocks.get(f["name"]) or []
            if b:
                vol, off, size = b[0]
                prefix[f["name"]] = (vol, off, size)
        probe_vol = 0
        if len(vols) == 1 and prefix:
            stored_only = all(int(f.get("method", 0) or 0) == 0 for f in meta)
            biggest = max((f.get("size") or 0) for f in meta)
            packed_total = sum(sz for _v, _o, sz in prefix.values())
            if (not stored_only and packed_total > 4 * PROBE_MIN_BYTES
                    and biggest):
                probe_vol = max(PROBE_MIN_BYTES, packed_total // 8)
                self._log(f"    single volume: probing on the first "
                          f"{probe_vol / (1 << 20):,.0f} MB of "
                          f"{packed_total / (1 << 20):,.0f} MB rather than "
                          "packing it all for every combo.", "dim")
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
        # Does this archive keep directories in its packed names? The `x`
        # above already extracted them that way, so the sources are laid out
        # correctly on disk; it is the PACK side that has to stop passing -ep.
        keep_paths = any(("/" in n or "\\" in n) for n in order)
        if keep_paths:
            deep = next(n for n in order if "/" in n or "\\" in n)
            self._log(f"    packed names carry directories ({deep[:56]}) — "
                      f"packing from the source root without -ep so the names "
                      f"match.", "dim")
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
            # Before blaming the download: a hole in the volume numbering that
            # the release's own .sfv shares is not our problem and never was.
            gap = volume_gap(folder, vols)
            if gap:
                self._log(f"    ✗ {gap}.", "err")
                return {"ok": False, "error": f"incomplete release: {gap}"}
            self._log(f"    ✗ extraction incomplete — {detail}", "err")
            return {"ok": False, "error": f"extraction incomplete ({detail})"}

        # rar wrote the sources; something else took one away. A release
        # carrying a tool — NINTENDO_DS_BETA_DUMPER-IND ships
        # nds-dumper-beta.exe — gets quarantined between the extract and the
        # first read, and the capture died on a raw errno 13 that named a
        # temp path and nothing else. Say what happened and whose it is.
        locked = []
        for p in src_files:
            try:
                with open(p, "rb") as fh:
                    fh.read(1)
            except OSError as e:
                locked.append(f"{p.name} ({e.strerror or e.errno})")
        if locked:
            self._log(f"    ✗ extracted but then unreadable: "
                      f"{', '.join(locked[:3])}. Antivirus quarantining a "
                      f"packed tool is the usual cause — exclude the temp "
                      f"folder to capture this one.", "err")
            return {"ok": False,
                    "error": f"source locked after extraction: {locked[0]}"}

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
            # The key has to be unique per DISTINCT file. Keyed on the
            # basename, a release carrying six different Config.cia under six
            # SDK-x.y.z directories (SDK.DevKit.Tools does) had the last one
            # overwrite the previous five: the manifest still named all six,
            # the bytes of five were gone, and the release could never rebuild.
            # The single `stem/base` fallback only ever resolved the FIRST
            # collision, so anything past the second silently clobbered.
            # Identical bytes still share one entry — that dedupe is the point
            # of comparing rather than always making a new key.
            relname = f["name"].replace("\\", "/").strip("/")
            key = None
            for cand in dict.fromkeys((f"extras/{base}",
                                       f"extras/{relname}",
                                       f"extras/{st['stem']}/{relname}")):
                if cand not in embedded or embedded[cand] == data:
                    key = cand
                    break
            if key is None:
                n = 2
                while True:
                    cand = f"extras/{st['stem']}/{n}/{relname}"
                    if cand not in embedded or embedded[cand] == data:
                        key = cand
                        break
                    n += 1
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
        # What the ORIGINAL's first volume ends with. Read before the sweep,
        # because it is one of the sweep's match conditions.
        want_end = end_block_sig(vols[0])
        want_ext = header_exttime(vols[0]) if st["format"] == "RAR4" else None
        # Four bytes of header no build in the pack writes. Worth saying out
        # loud, because in a volumed set it moves every split point and the
        # replay comes back different in every volume for no reason the recipe
        # can show — but it is a DELTA, not a wall, so do not refuse it.
        uni = unicode_named_ascii(vols[0])
        if uni:
            self._log(f"    {uni[0]} carries the Unicode-name flag for a plain "
                      f"ASCII name — four bytes no build reproduces, so expect "
                      f"a header patch here.", "dim")
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
                                        new_numbering=newnum, groups=mgroups,
                                        end_sig=want_end, hdr_ext=want_ext,
                                        rung=di, rungs=len(cands),
                                        base=srcdir if keep_paths else None,
                                        prefix=prefix,
                                        probe_vol=probe_vol,
                                        want_vols=len(vols))
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
                       "keep_paths": keep_paths,
                       "locked": arch_locked,
                       "comment": bool(comment)})
        if len(mgroups) > 1:
            # Only when it means something. A single-group recipe replays
            # through the identical code path with no `groups` key at all, so
            # every .rsr written before today still rebuilds unchanged.
            recipe["groups"] = [list(g) for g in mgroups]
        packed_dir = recipe.pop("_packed", None)
        verify, volmeta, deltas = self._verify_replay(
            recipe, src_files, vols, work, comment, st, si,
            base=srcdir if keep_paths else None, packed=packed_dir)
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
        that is the difference between one failed sweep and seven.

        DEFAULT OFF since 2026-09-04, on the evidence of the whole corpus:
        across ALL 7,541 verified sets the winning recipe used the dictionary
        the header declares -- 7,541 of 7,541, no exceptions. Which follows
        from the paragraph above rather than contradicting it: the header
        records the dictionary the compressor ACTUALLY used, WinRAR can only
        clamp a request down to that, and the effective window is what decides
        the output. So packing at the header value reproduces the original
        whatever the original command line asked for, and every rung above it
        is a full sweep that cannot match.

        It only ever ran when the first rung FAILED, so it never cost a hit
        anything -- it tripled the cost of every WALL, which is exactly where
        the hours go. Kept as a setting rather than deleted, in case a release
        ever turns up that needs it; nothing in this corpus does."""
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

    def _pack_cmds_extra(self, cmds, extra):
        """Splice extra switches into each command, after the -m/-md block.

        rar takes switches in any position before the archive name, and every
        command _pack_cmds builds starts [exe, "a", ...switches..., archive],
        so inserting at index 2 is always inside the switch run."""
        if not extra or cmds is None:
            return cmds
        return [list(c[:2]) + list(extra) + list(c[2:]) for c in cmds]

    def _pack_cmds(self, ex: Path, fmt: str, dict_kb: int, mt: int, solid: bool,
                   groups, srcs: list, target: Path, tail=(),
                   vol_args=(), base=None) -> list[list[str]] | None:
        """The command SEQUENCE that builds this archive — usually one command.

        `groups` is [(level, count)] over `srcs` in archive order. One entry is
        the ordinary case and produces exactly the command this used to build;
        more than one means the archive was assembled by successive `rar a`
        calls, which is the only way a set can hold files at different methods.

        Volume switches and the recovery record go on the LAST command only:
        -v cannot be combined with appending at all (rar refuses to modify a
        volume set), and an RR is written when the archive is finished.

        `base` is the source root for an archive that STORES PATHS. Plenty do:
        the PUSSYCAT header-fix collections pack
        `Tangled_EUR_NDS-RobotKillers/B6TPv00.ups`, EXPERiENCE packs
        `Ensata v1.4d/dlls/StrRes_eng.dll`, and XPA managed to pre two
        releases straight off the FTP and one off somebody's desktop
        (`home/glftpd/site/private/...`, `Documents and Settings/.../Desktop`).
        `-ep` throws all of that away, so the sweep packed flat names, the
        stream lookup — which is keyed on the name the ARCHIVE uses — never
        matched, and the release walled after an exhaustive search that had
        never once run the right command. Given a base, the sources are named
        relative to it and `-ep` is dropped; the caller runs the command with
        that base as its working directory, which is what makes rar store the
        same names the original does."""
        cmds = []
        at = 0
        for gi, (level, count) in enumerate(groups):
            pre = self._pack_args(ex, fmt, level, dict_kb, mt)
            if pre is None:
                return None
            cmd = pre + ["-s" if solid else "-s-", "-ds", "-o+", "-y", "-idcd"]
            if not base:
                cmd.append("-ep")
            if gi == len(groups) - 1:
                cmd += list(vol_args) + list(tail)
            names = []
            for q in srcs[at:at + count]:
                if base:
                    try:
                        names.append(os.path.relpath(str(q), str(base)))
                        continue
                    except ValueError:
                        pass
                names.append(str(q))
            cmd += [str(target)] + names
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
        # (build, -mt, extra switches). The third element carries the PPM
        # coder switches; it is () for an ordinary combo. Every append here
        # MUST produce three, including the priors path below — a 2-tuple
        # reaching the sweep is an unpack error on the first combo of every
        # release that has priors, which is nearly all of them.
        order: list[tuple[Path, int, tuple]] = []
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
                    order.append((ex, n, ()))
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
                        order.append((ex, mt, ()))
        for mt in mts:
            for ex in ranked:
                if (ex.name, mt) not in seen:
                    seen.add((ex.name, mt))
                    order.append((ex, mt, ()))
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

    def set_workers(self, n) -> dict:
        """Change the core budget, taking effect on the next chunk.

        Deliberately NOT part of save_settings: that is the whole-form save,
        and this has to work while a scan is running, when the rest of the
        form is disabled and re-saving it would be wrong."""
        n = max(0, min(256, _num(n, 0, int)))
        try:
            cfg = json.loads(self._config_path.read_text("utf-8"))
        except Exception:
            cfg = {}
        cfg["workers"] = n
        try:
            self._config_path.write_text(json.dumps(cfg, indent=2), "utf-8")
        except Exception as e:
            return {"ok": False, "error": str(e)}
        eff = self._cpu_budget()
        self._log(f"Core budget: {'auto' if not n else n} "
                  f"→ {eff} rar thread(s) of {os.cpu_count() or '?'}"
                  + (" — a running sweep picks this up at its next chunk."
                     if self._running else ""), "info")
        return {"ok": True, "workers": n, "effective": eff,
                "cores": os.cpu_count() or 0}

    def set_jobs(self, n) -> dict:
        """Change how many releases capture at once, from the next free slot.

        Same contract as set_workers: written straight to the config, read
        back by the scan loop, and therefore live."""
        n = max(1, min(16, _num(n, 1, int)))
        try:
            cfg = json.loads(self._config_path.read_text("utf-8"))
        except Exception:
            cfg = {}
        cfg["jobs"] = n
        try:
            self._config_path.write_text(json.dumps(cfg, indent=2), "utf-8")
        except Exception as e:
            return {"ok": False, "error": str(e)}
        # Work the share out from the number being SET, not from whatever
        # this instance currently has in flight — the GUI's call and the
        # running scan are different objects, and quoting _cpu_budget() here
        # said "each gets about 48 of the 48" no matter what was asked for.
        w = _num(self.get_settings().get("workers"), 0, int)
        total = max(1, min(w, 256)) if w > 0 else max(1, (os.cpu_count() or 4) // 2)
        self._log(f"Releases at once: {n}"
                  + (f" — about {max(1, total // n)} rar thread(s) each, "
                     f"of {total}." if n > 1 else " (one at a time).")
                  + (" A running scan applies this as slots free."
                     if self._running else ""), "info")
        return {"ok": True, "jobs": n}

    def set_rebuild_jobs(self, n) -> dict:
        """Change how many releases rebuild at once, from the next free slot.

        Same live contract as set_workers/set_jobs. Note there is deliberately
        no core slider for the rebuild: -mt is part of the recipe, and packing
        at any other thread count changes the bytes and fails verification."""
        n = max(1, min(8, _num(n, 1, int)))
        try:
            cfg = json.loads(self._config_path.read_text("utf-8"))
        except Exception:
            cfg = {}
        cfg["rebuild_jobs"] = n
        try:
            self._config_path.write_text(json.dumps(cfg, indent=2), "utf-8")
        except Exception as e:
            return {"ok": False, "error": str(e)}
        self._log(f"Rebuilds at once: {n}"
                  + ("" if n > 1 else " (one at a time)")
                  + (" — a running rebuild applies this as slots free."
                     if self._running else ""), "info")
        return {"ok": True, "rebuild_jobs": n}

    def requeue(self, name: str) -> dict:
        """Put a release back on the queue of the scan in progress.

        For the release that just parked on the budget, or errored on
        something you have since fixed — an antivirus exclusion, a file that
        was locked. It goes to the BACK of the queue, keeps whatever sweep
        position it had recorded, and gets the budget in force when it comes
        round, so retrying costs only the combos it has not tried yet.

        Only while a scan is running: outside one there is no queue to join,
        and scanning the folder again is the same thing with fewer surprises.
        """
        name = (name or "").strip()
        if not self._running:
            return {"ok": False, "error": "no scan is running"}
        folder = (getattr(self, "_queue_by_name", None) or {}).get(name)
        if folder is None:
            return {"ok": False,
                    "error": f"{name} is not part of the run in progress"}
        with getattr(self, "_queue_lock", threading.Lock()):
            q = getattr(self, "_queue", None)
            if q is None:
                return {"ok": False, "error": "no queue"}
            if any(f == folder for _i, f in q):
                return {"ok": False, "error": f"{name} is already queued"}
            self._queue_total = getattr(self, "_queue_total", 0) + 1
            q.append((self._queue_total, folder))
            ahead = len(q)
        self._log(f"↻ {name} put back on the queue — {ahead} ahead of it.",
                  "info")
        self._emit("row", {"name": name, "status": "queued",
                           "recipe": "waiting for another go",
                           "kind": "running"})
        return {"ok": True, "ahead": ahead}

    def _job_slots(self) -> int:
        """How many releases to capture at once, read fresh every time a slot
        frees so it can be changed while a scan runs."""
        return max(1, min(16, _num(self.get_settings().get("jobs"), 1, int)))

    def _rebuild_slots(self) -> int:
        """How many releases to REBUILD at once, re-read as each one ends.

        Separate from `jobs` because the two sides size differently: a capture
        sweeps many rar builds at once and is happy to take the machine, while
        a rebuild replays ONE command at the thread count its recipe recorded,
        so a single release often leaves most of the CPU idle."""
        return max(1, min(8, _num(
            self.get_settings().get("rebuild_jobs"), 1, int)))

    def _cpu_budget(self) -> int:
        """How many rar threads the sweep may use at once.

        A budget in THREADS, not workers, because `rar -mt8` already uses
        eight of them: eight workers at -mt8 would ask for 64 threads on a
        32-core machine and thrash. The sweep divides this by the thread count
        it is currently sweeping to decide how many combos to run together.

        0 means auto, and auto deliberately leaves half the machine alone —
        a scan runs for hours and the point is to be able to keep using the
        PC while it does."""
        want = _num(self.get_settings().get("workers"), 0, int)
        total = (max(1, min(want, 256)) if want > 0
                 else max(1, (os.cpu_count() or 4) // 2))
        # Shared between the captures actually in flight: four releases each
        # taking the whole budget would ask for four times the machine.
        return max(1, total // max(1, getattr(self, "_live_jobs", 1)))

    def _try_combo(self, ex: Path, n: int, wdir: Path, fmt: str, dict_kb: int,
                   solid: bool, groups, srcs, vol_args, base, prefix,
                   end_sig, hdr_ext, targets, extra=(), probe_vol=0,
                   want_vols=0, vol_first=0):
        """One (build, -mt) candidate, in its own directory. True if it is the
        recipe, False if not, None if the build cannot run this recipe at all.

        Everything here was the body of the serial loop; it is a function so
        that several can run at once. It touches no shared state except the
        process set, which is already locked."""
        cmds = self._pack_cmds(ex, fmt, dict_kb, n, solid, groups, srcs,
                               wdir / "probe.rar", vol_args=vol_args,
                               base=base)
        cmds = self._pack_cmds_extra(cmds, extra)
        if cmds is None:
            return None
        try:
            if wdir.exists():
                _rmtree(wdir)
            wdir.mkdir(parents=True, exist_ok=True)
        except OSError:
            return None

        # PREFIX PROBE — pack only until volume one is closed and judge on it.
        # A synthetic split, for an original that was one volume. The
        # end-of-archive and header checks are skipped for it: volume one of a
        # split is not the end of an archive and carries the volume flag, so
        # neither signature can match by construction.
        synthetic = bool(probe_vol) and not vol_args
        probe_cmds = cmds
        if synthetic:
            probe_cmds = self._pack_cmds_extra(
                cmds, (f"-v{probe_vol}b", "-vn"))
        if prefix and len(cmds) == 1 and (vol_args or synthetic):
            head = wdir / "probe.rar"

            def _closed():
                # rar has opened volume two, so volume one is complete and
                # flushed. Safer than watching volume one's size, which sits
                # at its final value for a moment before the file is closed.
                return any(q.name != "probe.rar" for q in wdir.iterdir())

            if not self._run_until(probe_cmds[0], _closed, timeout=900,
                                   heartbeat=f"probe -mt{n} "
                                             f"{_exe_label(ex.name)}",
                                   cwd=base):
                return False
            verdict = self._prefix_verdict(head, prefix)
            bad = (verdict is False
                   or (not synthetic and end_sig is not None
                       and end_block_sig(head) != end_sig)
                   or (not synthetic and hdr_ext is not None
                       and header_exttime(head) != hdr_ext))
            for junk in wdir.iterdir():
                try:
                    junk.unlink()
                except OSError:
                    pass
            if bad:
                return False

        if not all(self._run(c, timeout=900,
                             heartbeat=f"sweep -mt{n} {_exe_label(ex.name)}",
                             cwd=base)
                   for c in cmds):
            return False
        head = self._probe_head(wdir)
        if head is None:
            return False
        # Asked for volumes, so it has to make them. A build whose streams
        # match but which cannot split where the original splits did not make
        # this archive -- see the module note on RAR 2.x and 15,000,000 B.
        if vol_args and want_vols > 1:
            made = sorted(q for q in wdir.iterdir()
                          if q.is_file() and q.name.startswith(head.stem + "."))
            if len(made) != want_vols:
                return False
            if vol_first and head.stat().st_size != vol_first:
                return False
        if end_sig is not None and end_block_sig(head) != end_sig:
            return False
        if hdr_ext is not None and header_exttime(head) != hdr_ext:
            return False
        return bool(self._streams_match(head, targets))

    def _sweep_recipe(self, fmt, exes, level, dict_kb, solid, src_files,
                      targets, work, max_mt, year=0, grp="",
                      deadline=None, rel="", vol_bytes=0,
                      new_numbering=True, groups=None, end_sig=None,
                      hdr_ext=None, rung=0, rungs=1,
                      base=None, prefix=None, probe_vol=0,
                      want_vols=0) -> dict | None:
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
        # The PPM tail. -m5 can compress with LZSS or PPMd and rar chooses per
        # file; ask for the default only and an archive whose packer forced PPM
        # is unreachable at every build and thread count. These go LAST so a
        # release that matches normally never pays for them, and the sweep
        # returns on first match — the cost lands only on releases that have
        # already failed everything else, where the alternative is no capture
        # at all. One thread each: PPM ignores -mt.
        if fmt == "RAR4" and level == 5:
            ppm_exes = []
            for want in ("_rar5", "_rar4", "_rar3"):
                hit = next((e for e in exes if want in e.name), None)
                if hit is not None:
                    ppm_exes.append(hit)
            for sw in _ppm_switches():
                for ex in ppm_exes:
                    combos.append((ex, 1, sw))
            if ppm_exes:
                self._log(f"    {len(_ppm_switches()) * len(ppm_exes):,} PPM "
                          "(-mc) combo(s) queued behind the ordinary sweep — "
                          "tried only if every build and thread count fails.",
                          "dim")
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
        # What the original's volume structure has to come back as.
        vol_first = vol_bytes if (vol_bytes and want_vols > 1) else 0
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
        # ── the sweep, across as many cores as the budget allows ─────────
        #
        # Serially this left 31 of 32 cores idle: one rar.exe at a time, and
        # with the prefix probe each combo is about a second of work, so the
        # sweep had become mostly waiting. Combos are independent — each packs
        # the same sources into its own directory and is judged on its own
        # output — so they parallelise exactly.
        #
        # Determinism is preserved by evaluating each chunk IN ORDER: the
        # winner is the lowest-index combo that matched, exactly as the serial
        # sweep returned. Several builds of one family can match, and which one
        # you get must not depend on which core happened to finish first.
        idx = 0
        last_budget = 0
        while idx < len(combos):
            # Re-read every chunk rather than once per sweep. A sweep can run
            # for hours, and the whole point of the setting is to be able to
            # hand the machine back — turn it down at breakfast and the very
            # next chunk is smaller, without stopping the scan and losing the
            # sweep position.
            budget = self._cpu_budget()
            if budget != last_budget and last_budget:
                self._log(f"    core budget changed to {budget} thread(s) — "
                          f"applies from this chunk on.", "dim")
            last_budget = budget
            if self._stop.is_set() or self._skip.is_set():
                return None
            if (tried and deadline and not self._budget_override
                    and time.monotonic() > deadline):
                self._budget_hit = True
                self._sweep_pos = (start + tried, sig)
                self._log(f"    ⏱ time budget reached after {tried} combo(s) "
                          f"({start + tried:,} of {start + len(combos):,} "
                          "overall) — parking this release; the next run "
                          "resumes from here.", "warn")
                return None

            width = max(1, min(budget // max(1, combos[idx][1]),
                               len(combos) - idx))
            chunk = combos[idx:idx + width]
            results: list = [None] * len(chunk)

            def _slot(s: int):
                ex_, n_, *rest_ = chunk[s]
                extra_ = rest_[0] if rest_ else ()
                try:
                    results[s] = self._try_combo(
                        ex_, n_, probe_dir / f"w{s}", fmt, dict_kb, solid,
                        groups, srcs, vol_args, base, prefix, end_sig,
                        hdr_ext, targets, extra_, probe_vol,
                        want_vols, vol_first)
                except Exception:
                    results[s] = False

            if len(chunk) == 1:
                _slot(0)
            else:
                ths = [threading.Thread(target=_slot, args=(s,), daemon=True)
                       for s in range(len(chunk))]
                for th in ths:
                    th.start()
                for th in ths:
                    th.join()

            for s, res in enumerate(results):
                if res is True:
                    ex_, n_, *rest_ = chunk[s]
                    extra_ = rest_[0] if rest_ else ()
                    return {"exe": ex_.name, "version": _exe_label(ex_.name),
                            "mt": n_, "dict_kb": dict_kb,
                            # The coder switch, when this was a PPM combo.
                            # Without it the recipe names build and thread
                            # count, says nothing about the coder, and the
                            # replay packs with the default and diverges.
                            "mc": list(extra_) if extra_ else [],
                            "tried": tried + s + 1,
                            # Where this combo's volumes are. The replay can
                            # often use them as they stand instead of packing
                            # the whole archive a second time.
                            "_packed": str(probe_dir / f"w{s}")}
            tried += len(chunk)
            idx += len(chunk)

            now = time.monotonic()
            if tried <= len(chunk) and not rung:
                # Say up front what this release is going to cost, now that a
                # combo's cost and the number running together both matter.
                per = (now - t0) / max(tried, 1)
                worst = total * max(rungs, 1)
                allows = (int(max(deadline - t0, 0) / max(per, 1e-6))
                          if deadline else None)
                msg = (f"    ~{per:,.2f}s per combo at this size across "
                       f"{len(chunk)} core(s) — {worst:,} would take "
                       f"{worst * per / 3600:,.1f}h")
                if rungs > 1:
                    msg += (f" ({total:,} per dictionary × {rungs} on the "
                            f"ladder)")
                if allows is not None:
                    msg += f"; budget allows about {allows:,}"
                self._log(msg, "dim")
                if allows is not None and allows < worst:
                    self._log(f"    ⏱ budget covers {allows:,} of {worst:,} "
                              f"— {worst - allows:,} short "
                              f"(~{(worst - allows) * per / 60:,.0f} min more). "
                              f"Press '{UI_FINISH_ONE}' to lift it for this "
                              f"release.", "warn")
            elif now - last > 0.5:
                last = now
                rate = tried / max(now - t0, 1e-6)
                self._progress(
                    f"sweep {tried}/{total} · {len(chunk)} at a time"
                    + (f" · {rate * 60:,.0f}/min" if rate < 600 else "")
                    + (f" · {(deadline - now) / 60:,.0f} min left in budget"
                       if deadline else ""))
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
    @staticmethod
    def _prefix_verdict(probe_head: Path, prefix: dict):
        """True if volume one agrees with the original, False if it does not,
        None if there is not enough of it to say.

        Compared over the COMMON PREFIX of the two slices, never over their
        lengths. Two builds can write different-sized headers and so fit
        different amounts of stream into a fixed-size volume while producing
        the identical stream, and rejecting on length would throw away the
        build that is actually right."""
        got = rar4_vol_blocks(probe_head)
        if not got:
            return None
        seen = 0
        for name, (src_vol, src_off, src_len) in prefix.items():
            if name not in got:
                continue                       # not in volume one; no verdict
            off, size = got[name]
            n = min(size, src_len)
            if n <= 0:
                continue
            try:
                with open(probe_head, "rb") as a, open(src_vol, "rb") as b:
                    a.seek(off)
                    b.seek(src_off)
                    left = n
                    while left > 0:
                        step = min(1 << 20, left)
                        x, y = a.read(step), b.read(step)
                        if len(x) < step or len(y) < step:
                            return None
                        if x != y:
                            return False
                        left -= step
            except OSError:
                return None
            seen = max(seen, n)
        return True if seen >= PROBE_MIN_BYTES else None

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
                     comment_file: Path | None, fmt: str,
                     base=None) -> list[list[str]] | None:
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
        if recipe.get("locked"):
            # Tail, so it lands on the last command only — rar cannot append
            # to an archive it has already locked.
            tail.append("-k")
        cmds = self._pack_cmds(ex, fmt, recipe["dict_kb"], recipe["mt"],
                               recipe["solid"], groups, srcs, target,
                               tail=tail, vol_args=vol_args, base=base)
        return self._pack_cmds_extra(cmds, recipe.get("mc") or ())

    def _replay(self, recipe: dict, src_files, work: Path, comment,
                fmt: str, base=None) -> list[Path] | None:
        """Run the original command again and return the volumes it produced,
        in order."""
        out = work / "replay"
        if out.exists():
            _rmtree(out)
        out.mkdir(parents=True)
        cfile = None
        if comment:
            cfile = work / "comment.txt"
            # newline="" or Python translates the line endings on the way
            # out, and a comment that already holds CRLF — WinRAR's own
            # default one does — goes to disk as CR CR LF. Two extra bytes a
            # line is enough to move every byte of a 50 MB eleven-volume set
            # and lose the whole thing as "replay unverified".
            cfile.write_text(comment, encoding="utf-8", errors="replace",
                             newline="")
        cmds = self._replay_cmds(recipe, out / "replay.rar",
                                 [str(p) for p in src_files], cfile, fmt,
                                 base=base)
        if not cmds:
            return None
        mb = sum(p.stat().st_size for p in src_files if p.is_file()) / (1 << 20)
        for c in cmds:
            if not self._run(c, timeout=3600,
                             heartbeat=f"replaying {recipe['version']} "
                                       f"{_mt_label(recipe.get('exe', ''), recipe['mt'])}"
                                       f" over {mb:,.0f} MB",
                             cwd=base):
                return None
        made = sorted(p for p in out.iterdir() if p.is_file())
        if not made:
            return None
        ordered = [p for p in made if _classify_volume(p.name)]
        ordered.sort(key=lambda p: _classify_volume(p.name)[2])
        return ordered or made

    def _verify_replay(self, recipe, src_files, vols, work, comment, st, si=0,
                       base=None, packed=None):
        """The whole point of capture-time: don't claim the recipe works, run
        it and compare. Returns ('exact'|'delta'|'none', volume records,
        {path-in-rsr: patch bytes})."""
        produced = None
        # The sweep just packed this set to prove its streams. When the replay
        # command is the SAME command — no recovery record, no comment, no
        # lock, the same volume size — it would spend minutes reproducing
        # bytes that are already on disk. On a 4 GB 3DS release that is 35 s of
        # a 117 s capture, packed once to check the streams and again to check
        # the volumes.
        #
        # Re-running it proves nothing extra: rar is deterministic on identical
        # input, and the archive's own filename does not reach the bytes
        # (verified: probe.rar, replay.rar and some-other-name.rar produce
        # identical volumes). What the replay adds over the sweep is comparing
        # whole VOLUMES rather than streams, and that check runs the same
        # either way.
        if (packed and not recipe.get("rr_pct") and not comment
                and not recipe.get("locked")):
            try:
                made = [q for q in sorted(Path(packed).iterdir())
                        if q.is_file() and _classify_volume(q.name)]
                made.sort(key=lambda q: _classify_volume(q.name)[2])
            except OSError:
                made = []
            if made:
                produced = made
                self._log(f"    reusing the sweep's own pack — the replay "
                          f"command is identical, so the {len(made)} volume(s) "
                          f"are already on disk.", "dim")
        if produced is None:
            produced = self._replay(recipe, src_files, work, comment,
                                    st["format"], base=base)
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
            if not same and patch is None:
                self._log(f"      the join {diff_shape(got, joined)}.", "dim")
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
                    self._log(f"      {v.name} {diff_shape(got, orig)}.", "dim")
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

    def _match_all(self, p: Path) -> dict:
        """EVERY captured release this file is the content of.

        One rom is routinely the content of several real releases at once: the
        original pre, a PROPER or a RARFIX of it, a second group dumping the
        same cart, and years later a numbered re-release of the whole library.
        None of them is the "right" one — they are all releases, they are all
        rebuildable from this one file, and a rebuilder that keeps only one of
        them silently drops the rest on the floor."""
        if not self._db_path.is_file():
            return {"ok": False, "error": "no index yet"}
        size = p.stat().st_size
        crc = _file_crc32(p)
        con = self._db()
        try:
            rows = con.execute(
                "SELECT f.release, f.name, f.sha256, r.rsr_path, r.verified "
                "FROM files f JOIN releases r ON r.name = f.release "
                "WHERE f.size=? AND f.crc32=? AND f.source='content' "
                "ORDER BY f.release",
                (size, crc)).fetchall()
        finally:
            con.close()
        if not rows:
            return {"ok": False, "error": "no match",
                    "crc32": f"{crc:08X}", "size": size}
        if len(rows) > 1:
            # SHA-256 settles a genuine size+CRC32 collision — but only against
            # a release that HAS one. A ZIP capture records no sha for its
            # entries, so keeping the rows that compare equal threw away every
            # ZIP candidate and handed the rom to whichever RAR release shared
            # it: 8 of 8 2005 zip releases rebuilt as somebody else's re-pre,
            # and the tool called it a clean run. A missing sha is unknown, not
            # wrong; only a sha that is present AND different rules a release
            # out.
            sha = _file_sha256(p)
            keep = [r for r in rows if not r[2] or r[2] == sha]
            if keep:
                rows = keep
            # Confirmed first, unknown after — the caller that still wants one
            # answer should get the best-evidenced one.
            rows.sort(key=lambda r: 0 if r[2] == sha else 1)
        hits = [{"release": r[0], "packed_name": r[1], "rsr": r[3],
                 "verified": bool(r[4])} for r in rows]
        return {"ok": True, "crc32": f"{crc:08X}", "size": size,
                "releases": hits}

    def _match_content(self, p: Path) -> dict:
        """The single best release for this file — the shape the UI wants."""
        res = self._match_all(p)
        if not res.get("ok"):
            return res
        hits = res["releases"]
        return {"ok": True, "release": hits[0]["release"],
                "packed_name": hits[0]["packed_name"],
                "rsr": hits[0]["rsr"], "verified": hits[0]["verified"],
                "crc32": res["crc32"], "size": res["size"],
                "ambiguous": [h["release"] for h in hits[1:]]}

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
        # Default ON: without it these releases are simply unreachable from
        # this screen, and a set is not complete without them.
        with_meta = bool((cfg or {}).get("metadata_releases", True))

        def _bg():
            self._running = True
            self._stop.clear()
            self._size_map_cache = {}
            try:
                self._rebuild_batch_run(root, out, delete_content, with_meta)
            except Exception as e:
                self._log(f"Batch rebuild error: {e}", "err")
                self._log(traceback.format_exc(), "dim")
            finally:
                self._running = False
                self._progress("")
                self._emit("scan_done", {})

        threading.Thread(target=_bg, daemon=True).start()
        return {"ok": True, "started": True}

    def _content_sizes(self) -> set[int]:
        """Every file size the index knows as content."""
        con = self._db()
        try:
            return {int(r[0]) for r in con.execute(
                "SELECT DISTINCT size FROM files WHERE source='content'")}
        finally:
            con.close()

    def _rebuild_batch_run(self, root: Path, out: Path,
                           delete_content: bool = False,
                           with_meta: bool = True):
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
        # Nothing is excluded by extension. Skipping the usual sidecar types
        # here looked free and cost 51 releases: on a patch or trainer release
        # the largest zip member is the nfo, so the nfo IS the content, and a
        # scan that refuses to look at .nfo files can never match one. The
        # size test is the real filter and it is already free.
        for p in sorted(root.rglob("*")):
            if not p.is_file() or p.suffix.lower() == ".rsr":
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
        shared = 0
        for i, p in enumerate(cands, 1):
            if self._stop.is_set():
                self._log("Stopped.", "warn")
                return
            self._progress(f"hashing {i}/{len(cands)} · {p.name}")
            hit = self._match_all(p)
            if not hit.get("ok"):
                miss += 1
                self._emit("row", {"name": p.name, "status": "skipped",
                                   "recipe": hit.get("error", "no match"),
                                   "kind": "nomatch"})
                continue
            # EVERY release this rom belongs to, not just the best-evidenced
            # one. Building only the winner is what made the older zip
            # releases look unrebuildable: their rom is also in a later
            # re-pre, that re-pre won, and the zip release produced no
            # archive, no nfo and no extras — no output at all, which reads
            # from the outside like a failed rebuild rather than a release
            # that was never attempted.
            if len(hit["releases"]) > 1:
                shared += 1
            for h in hit["releases"]:
                rel = h["release"]
                if rel in matched:
                    continue                  # one release, one rebuild
                matched[rel] = (p, dict(h, crc32=hit["crc32"],
                                        size=hit["size"],
                                        others=[o["release"]
                                                for o in hit["releases"]
                                                if o["release"] != rel]))
        self._progress("")
        self._log(f"  {len(matched)} release(s) matched, {miss} file(s) with no "
                  "entry in the index.", "ok" if matched else "warn")
        if shared:
            self._log(f"  {shared} of those rom(s) are the content of more "
                      f"than one release — every one of them is queued.", "dim")

        # Which releases still need each source file. DELETE SOURCES must not
        # remove a rom the moment the first of its releases is built, or the
        # other four have nothing left to rebuild from.
        claims: dict = {}
        for rel, (p, _h) in matched.items():
            claims.setdefault(p, set()).add(rel)
        consumed_by: dict = {}
        failed_rels: set = set()
        built: set = set()          # releases rebuilt AND verified so far

        # Several releases at once (see _rebuild_slots). The bookkeeping that
        # decides what may be DELETED — claims, built, consumed_by — is shared,
        # so every read-and-act on it happens under one lock; the per-release
        # source list is thread-local and never crosses between them.
        book = threading.Lock()
        total = len(matched)

        def _rebuild_one(i, rel, p, hit):
            nonlocal done, failed, freed
            # Tag this thread's lines so parallel rebuilds stay readable, the
            # same way captures do.
            self._tl.tag = f"[{rel[:24]}] " if self._rebuild_slots() > 1 else ""
            self._log("", "")
            self._log(f"══ [{i}/{total}] {rel} ══", "info")
            self._log(f"  matched {p.name}  CRC={hit['crc32']}  "
                      f"{hit['size']:,} B", "dim")
            if hit.get("others"):
                self._log(f"  that content is also the release content of: "
                          f"{', '.join(hit['others'][:3])}"
                          + (f" (+{len(hit['others']) - 3} more)"
                             if len(hit["others"]) > 3 else "")
                          + " — those are queued too.", "dim")
            rsr = Path(hit["rsr"])
            if not rsr.is_file():
                self._log(f"  ✗ indexed .rsr is missing from the store: {rsr}",
                          "err")
                self._emit("row", {"name": rel, "status": "error",
                                   "recipe": ".rsr missing", "kind": "error"})
                with book:
                    failed += 1
                return
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
                mine = list(self._consumed)
                with book:
                    done += 1
                    built.add(rel)
                    ready = []
                    if delete_content:
                        for c in mine:
                            consumed_by.setdefault(c, set()).add(rel)
                            # Delete NOW if nothing else is still owed this
                            # file. Waiting for the whole batch was safe but
                            # could need the unpacked corpus and the rebuilt
                            # one on the disk at the same time — 1.4 TB of 3DS
                            # twice over. A source is freed the moment its
                            # LAST claimant has been rebuilt and hash-verified,
                            # which is exactly the test the end-of-run sweep
                            # applied, just applied sooner. A release still in
                            # flight has not been added to `built`, so its
                            # sources cannot be freed out from under it.
                            if not (claims.get(c, set()) - built):
                                ready.append(c)
                        for c in ready:
                            consumed_by.pop(c, None)
                if ready:
                    keep_all, self._consumed = self._consumed, ready
                    n = self._delete_consumed(out)
                    self._consumed = keep_all
                    with book:
                        freed += n
                self._emit("row", {"name": rel, "status": "done",
                                   "recipe": "rebuilt", "kind": "ok"})
            else:
                with book:
                    failed += 1
                    failed_rels.add(rel)
                self._emit("row", {"name": rel, "status": "error",
                                   "recipe": "rebuild failed", "kind": "error"})

        pending = list(enumerate(sorted(matched.items()), 1))
        live: list = []
        while pending or live:
            if self._stop.is_set():
                self._log("Stopped.", "warn")
                break
            want = self._rebuild_slots()
            while pending and len(live) < want:
                i, (rel, (p, hit)) = pending.pop(0)
                th = threading.Thread(target=_rebuild_one,
                                      args=(i, rel, p, hit), daemon=True)
                th.start()
                live.append(th)
            live = [th for th in live if th.is_alive()]
            if pending or live:
                time.sleep(0.2)
        for th in live:               # let what is in flight finish cleanly
            th.join()
        self._tl.tag = ""

        if delete_content and consumed_by:
            # Backstop. Most sources are freed as their last claimant finishes
            # (above); what reaches here is shared content whose other
            # claimants came later in the run. Same rule either way: a source
            # goes only once EVERY release that claimed it has been built and
            # hash-verified, so anything still owed to a release that failed,
            # or that the run never reached, stays where it is.
            held = 0
            keep = []
            for srcp, claimed_by in consumed_by.items():
                owed = claims.get(srcp, set())
                if owed - claimed_by:
                    held += 1
                    continue
                keep.append(srcp)
            self._consumed = keep
            freed += self._delete_consumed(out)
            if held:
                self._log(f"    {held} source(s) kept — another release still "
                          f"needs them.", "dim")

        if with_meta:
            done += self._rebuild_metadata(matched, out)

        self._log("", "")
        self._log(f"Batch rebuild complete — {done} rebuilt, {failed} failed, "
                  f"{miss} unmatched file(s)."
                  + (f" {_human_bytes(freed)} of unpacked sources deleted."
                     if freed else ""), "ok" if not failed else "warn")

    def _rebuild_metadata(self, matched: dict, out: Path) -> int:
        """Write the releases that are nothing but an nfo.

        A DIRFIX or NFOFIX release has no archive and therefore no content
        file, and this screen finds its work by hashing loose content and
        asking the index whose it is. There is nothing to hash for these, so
        nothing could ever match, so they were never even attempted — they
        captured fine and then quietly never came out again. They are not
        matched, they are enumerated.

        Scoped to the systems this run actually built, so pointing at a folder
        of NDS roms does not also write out every GBA nfo in the store. A
        release whose name carries no platform token is written whatever the
        run built — 'Unknown' means we could not attribute it, not that it
        belongs to some other system, and excluding it would strand it for
        good. When the run built nothing there is nothing to scope by, so
        everything is written."""
        systems = {_release_system(rel) for rel in matched}
        con = self._db()
        try:
            rows = con.execute(
                "SELECT name, rsr_path FROM releases WHERE kind='metadata'"
            ).fetchall()
        finally:
            con.close()
        todo = [(n, rp) for n, rp in rows
                if not systems
                or _release_system(n) in systems
                or _release_system(n) == "Unknown"]
        if not todo:
            return 0
        self._log("", "")
        self._log(f"══ {len(todo)} nfo-only release(s) ══", "info")
        self._log("  no content to match on, so these are written straight "
                  "from the store.", "dim")
        n = 0
        for rel, rp in sorted(todo):
            if self._stop.is_set():
                self._log("Stopped.", "warn")
                break
            dest = out / rel
            rsr = Path(rp)
            if not rsr.is_file():
                self._log(f"  ✗ {rel}: .rsr missing from the store.", "err")
                continue
            self._consumed = []
            try:
                res = self._rebuild_run(rsr, dest, dest)
            except Exception as e:
                self._log(f"  ✗ {rel}: {e}", "err")
                continue
            if res.get("ok"):
                n += 1
                self._emit("row", {"name": rel, "status": "done",
                                   "recipe": "nfo only", "kind": "ok"})
        self._log(f"  ✓ {n} of {len(todo)} written.", "ok" if n else "warn")
        return n

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
            # Delete-after-rebuild works off this list and nothing else, so a
            # path that resolves a source and forgets it is a source that can
            # never be deleted. Only the RAR path recorded them, which quietly
            # made the option a no-op for every ZIP release.
            self._consumed.append(src)
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
        # Two entries of the SAME name are legal in a ZIP, and capture stores
        # their payloads by POSITION for exactly that reason. Resolving them
        # back by name alone took the first record every time, so the second
        # occurrence replayed the first one's bytes: a same-length pair sailed
        # past the length check and only failed at the final hash, with nothing
        # in the log to say why. Consume occurrences in order instead, which
        # needs nothing new in the manifest and so works on already-captured
        # .rsr files.
        used: dict = {}
        for (off, ln), nm in zip(holes, names):
            seen_n = used.get(nm, 0)
            f = next((x for i, x in enumerate(st["files"])
                      if x["name"] == nm
                      and sum(1 for y in st["files"][:i] if y["name"] == nm)
                      == seen_n), None)
            used[nm] = seen_n + 1
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
            self._consumed.append(src)          # see _rebuild_zip
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

    def _rebuild_lha(self, st: dict, z, content: Path, out: Path) -> bool:
        """Put an LHA archive back together from the skeleton and its members.

        Simpler than the ZIP rebuild for the one reason that makes LHA worth
        so little as a recipe: nothing is derived. Every compressed member was
        carried, and the only thing asked of the caller is the stored content
        member, matched back by hash exactly as a rom is."""
        name = st["name"]
        holes = [list(h) for h in st.get("holes", [])]
        names = list(st.get("hole_names", []))
        streams = {}
        used: dict = {}
        for (off, ln), nm in zip(holes, names):
            seen_n = used.get(nm, 0)
            f = next((x for i, x in enumerate(st["files"])
                      if x["name"] == nm
                      and sum(1 for y in st["files"][:i] if y["name"] == nm)
                      == seen_n), None)
            used[nm] = seen_n + 1
            if f is None:
                self._log(f"    ✗ {nm}: not described in the manifest.", "err")
                return False
            if f.get("stored"):
                data = z.read(f["stored"])
            else:
                src = self._source_by_hash(content, f)
                if src is None:
                    self._log(f"    ✗ missing source: {nm} "
                              f"({f['size']:,} B)", "err")
                    return False
                self._consumed.append(src)
                data = src.read_bytes()
            if len(data) != ln:
                self._log(f"    ✗ {nm}: {len(data):,} B, expected {ln:,}.",
                          "err")
                return False
            streams[off] = data
        rebuilt = zip_assemble(z.read(st["skeleton"]), holes, streams)
        if _sha256(rebuilt) != st["sha256"]:
            self._log(f"    ✗ {name}: rebuilt archive does not match "
                      f"({len(rebuilt):,} vs {st['size']:,} B).", "err")
            return False
        dst = out / st.get("folder", "") / name
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(rebuilt)
        self._log(f"    ✓ {name}  {len(rebuilt):,} B — rebuilt from the "
                  f"skeleton, hash-exact.", "ok")
        return True

    def _rebuild_run(self, rsr: Path, content: Path, out: Path) -> dict:
        manifest, z = self.read_rsr(rsr)
        try:
            rel = manifest.get("release", rsr.stem)
            self._log(f"══ REBUILD {rel} ══", "info")
            self._log(f"  captured {manifest.get('created_utc')} by "
                      f"{manifest.get('tool')}", "dim")
            out.mkdir(parents=True, exist_ok=True)
            work = Path(tempfile.mkdtemp(prefix="rsr-rb-", dir=self._work_root()))
            ok_all = True
            try:
                for st in manifest.get("sets", []):
                    if self._stop.is_set():
                        self._log("Stopped.", "warn")
                        break
                    if st.get("format") == "ZIP":
                        ok_all &= self._rebuild_zip(st, z, content, out, work)
                        continue
                    if st.get("format") == "LHA":
                        ok_all &= self._rebuild_lha(st, z, content, out)
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
        # An archive that stores directories has to be staged under them, or
        # the replay packs the right bytes under the wrong names. The capture
        # recorded which shape this is; anything captured before that flag
        # existed is flat, as it always was.
        keep_paths = bool(recipe.get("keep_paths"))
        for f in sorted(st["files"], key=lambda x: x["order"]):
            base = Path(f["name"]).name
            rel_name = f["name"].replace("\\", "/") if keep_paths else base
            dst = srcdir / rel_name
            dst.parent.mkdir(parents=True, exist_ok=True)
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
        produced = self._replay(recipe, srcs, setwork, comment,
                                st["format"],
                                base=srcdir if keep_paths else None)
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
                    if p.is_file():
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

    # ══════════════════════════════════════════════════════════════════
    #  The blocked list — what cannot be captured, and how sure we are
    # ══════════════════════════════════════════════════════════════════
    #
    # Deliberately NOT the misses table and deliberately not automatic. Two
    # separate questions, and collapsing them would make the list worse than
    # nothing:
    #
    #   class  — WHY. "missing-volume" is a fact about the files (the .r00 is
    #            not on the disk and never will be). "unsolved" is a fact about
    #            US (every axis tried, nothing found, and that changes: 264
    #            releases were unrebuildable one morning and fine that
    #            afternoon once a stale-delta bug was found).
    #   status — HOW SURE. The scanner may only ever write `proposed`. Nothing
    #            counts as blocked until a person confirms it, so an
    #            in-progress corpus cannot silently populate the list.
    #
    # And entries expire: anything later captured flips to `resolved` by
    # itself, because a blocklist nobody re-checks becomes wrong quietly.
    BLOCKED_CLASSES = ("no-archive", "missing-volume", "damaged",
                       "superseded", "unsolved")

    @property
    def _blocked_path(self) -> Path:
        return self._app_dir / "rsr_blocked.db"

    def _blocked_db(self) -> sqlite3.Connection:
        con = sqlite3.connect(str(self._blocked_path), timeout=60)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("""CREATE TABLE IF NOT EXISTS blocked (
            release   TEXT PRIMARY KEY,
            system    TEXT,
            year      INTEGER,
            grp       TEXT,
            path      TEXT,
            class     TEXT,
            reason    TEXT,
            evidence  TEXT,
            status    TEXT,
            first_seen TEXT,
            decided    TEXT,
            decided_by TEXT)""")
        return con

    def blocked_add(self, rows: list, status: str = "proposed",
                    by: str = "") -> dict:
        """Record releases as blocked. `rows` are dicts with at least
        `release`, `class` and `reason`; `evidence` is what was actually
        checked, and is the field that makes an entry re-checkable later."""
        now = datetime.now().isoformat(timespec="seconds")
        status = status if status in ("proposed", "confirmed") else "proposed"
        n = 0
        con = self._blocked_db()
        try:
            for r in rows or []:
                rel = (r.get("release") or "").strip()
                if not rel:
                    continue
                cls = r.get("class") or "unsolved"
                if cls not in self.BLOCKED_CLASSES:
                    cls = "unsolved"
                con.execute(
                    """INSERT INTO blocked (release, system, year, grp, path,
                           class, reason, evidence, status, first_seen,
                           decided, decided_by)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(release) DO UPDATE SET
                           class=excluded.class, reason=excluded.reason,
                           evidence=excluded.evidence, status=excluded.status,
                           decided=excluded.decided,
                           decided_by=excluded.decided_by""",
                    (rel, r.get("system", ""), _num(r.get("year"), 0, int),
                     r.get("group", ""), r.get("path", ""), cls,
                     r.get("reason", ""), r.get("evidence", ""), status, now,
                     now, by or ("scanner" if status == "proposed" else "")))
                n += 1
            con.commit()
        finally:
            con.close()
        self._log(f"Blocked list: {n} release(s) recorded as {status}.", "info")
        return {"ok": True, "recorded": n}

    def blocked_confirm(self, releases: list, by: str = "operator") -> dict:
        """Promote proposals to confirmed — the only way anything counts."""
        now = datetime.now().isoformat(timespec="seconds")
        con = self._blocked_db()
        try:
            cur = con.executemany(
                "UPDATE blocked SET status='confirmed', decided=?, decided_by=?"
                " WHERE release=?", [(now, by, r) for r in releases or []])
            con.commit()
            n = cur.rowcount if cur.rowcount and cur.rowcount > 0 else len(releases or [])
        finally:
            con.close()
        return {"ok": True, "confirmed": n}

    def blocked_sync(self) -> dict:
        """Flip anything since captured to `resolved`.

        The whole risk of a list like this is that it goes stale and starts
        answering "impossible" for things that were fixed. Re-checking it
        against the index costs nothing and keeps it honest."""
        if not self._db_path.is_file():
            return {"ok": False, "error": "no index"}
        con = self._db()
        try:
            have = {n for (n,) in con.execute("SELECT name FROM releases")}
        finally:
            con.close()
        now = datetime.now().isoformat(timespec="seconds")
        b = self._blocked_db()
        try:
            rows = [r[0] for r in b.execute(
                "SELECT release FROM blocked WHERE status!='resolved'")]
            hit = [r for r in rows if r in have]
            for r in hit:
                b.execute("UPDATE blocked SET status='resolved', decided=?,"
                          " decided_by='sync' WHERE release=?", (now, r))
            b.commit()
        finally:
            b.close()
        if hit:
            self._log(f"Blocked list: {len(hit)} entr(y/ies) have since been "
                      "captured — marked resolved.", "ok")
        return {"ok": True, "resolved": len(hit), "names": hit[:20]}

    def blocked_list(self, status: str = "") -> list:
        """Everything on the list, newest decision first."""
        con = self._blocked_db()
        try:
            q = ("SELECT release, system, year, grp, class, reason, evidence,"
                 " status, first_seen, decided, decided_by, path FROM blocked")
            args: tuple = ()
            if status:
                q += " WHERE status=?"
                args = (status,)
            q += " ORDER BY class, release"
            cols = ("release", "system", "year", "group", "class", "reason",
                    "evidence", "status", "first_seen", "decided",
                    "decided_by", "path")
            return [dict(zip(cols, r)) for r in con.execute(q, args)]
        finally:
            con.close()

    def blocked_export(self, dest: str = "", fmt: str = "both") -> dict:
        """Write the list somewhere a person can read it.

        `txt` is grouped by class with the reason and the evidence spelled
        out, because "it does not work" is useless six months later — the
        evidence is what lets you tell a permanent fact about the files from
        something we simply had not solved yet. `csv` is for a spreadsheet."""
        self.blocked_sync()
        rows = self.blocked_list()
        out = Path(dest) if dest else (self._app_dir / "rsr_blocked")
        made = []
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        if fmt in ("csv", "both"):
            f = out.with_suffix(".csv")
            with f.open("w", encoding="utf-8-sig", newline="") as fh:
                w = csv.writer(fh)
                w.writerow(["release", "system", "year", "group", "class",
                            "reason", "evidence", "status", "first_seen",
                            "decided", "decided_by", "path"])
                for r in rows:
                    w.writerow([r[k] for k in
                                ("release", "system", "year", "group", "class",
                                 "reason", "evidence", "status", "first_seen",
                                 "decided", "decided_by", "path")])
            made.append(str(f))
        if fmt in ("txt", "both"):
            f = out.with_suffix(".txt")
            by_class: dict = {}
            for r in rows:
                by_class.setdefault(r["class"], []).append(r)
            L = [f"RSR — releases this tool cannot capture", f"generated {stamp}",
                 "", f"{len(rows)} entr(y/ies): "
                 + ", ".join(f"{s}={sum(1 for r in rows if r['status'] == s)}"
                             for s in ("confirmed", "proposed", "resolved")),
                 "",
                 "confirmed = checked and agreed.  proposed = the scanner's",
                 "suggestion, NOT yet agreed.  resolved = captured since, and",
                 "kept only as a record that the list was once wrong about it.",
                 ""]
            for cls in sorted(by_class):
                items = by_class[cls]
                L.append(f"── {cls}  ({len(items)}) " + "─" * max(0, 50 - len(cls)))
                for r in items:
                    L.append(f"  {r['release']}")
                    L.append(f"      status   : {r['status']}"
                             + (f"  (by {r['decided_by']}, {r['decided']})"
                                if r["decided_by"] else ""))
                    L.append(f"      reason   : {r['reason']}")
                    if r["evidence"]:
                        L.append(f"      evidence : {r['evidence']}")
                    if r["path"]:
                        L.append(f"      path     : {r['path']}")
                    L.append("")
                L.append("")
            f.write_text("\n".join(L), encoding="utf-8")
            made.append(str(f))
        self._log("Blocked list exported: " + ", ".join(made), "ok")
        return {"ok": True, "files": made, "rows": len(rows)}

    @property
    def _db_path(self) -> Path:
        return self._app_dir / DB_NAME

    def _db(self) -> sqlite3.Connection:
        self._db_lock.acquire()
        try:
            return self._open_db()
        except Exception:
            self._db_lock.release()
            raise

    def _open_db(self) -> sqlite3.Connection:
        # 60s rather than sqlite's 5s default: belt and braces behind the lock,
        # for when something OUTSIDE this process holds the file -- the srrdb
        # tool reads this index live.
        con = sqlite3.connect(str(self._db_path), timeout=60)
        try:
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("PRAGMA synchronous=NORMAL")
        except Exception:
            pass
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
        return _LockedConn(con, self._db_lock)

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
        try:
            cfg = json.loads(self._config_path.read_text("utf-8"))
        except Exception:
            cfg = {}
        cfg["priors_imported_mtime"] = int(path.stat().st_mtime)
        cfg["priors_imported_records"] = rows
        try:
            self._config_path.write_text(json.dumps(cfg, indent=2), "utf-8")
        except Exception:
            pass
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
        for ex, n, *rest in combos:
            sw = ",".join(rest[0]) if rest and rest[0] else ""
            h.update(f"{ex.name}:{n}:{sw}\n".encode())
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

    def priors_status(self) -> dict:
        """Is there anything new in srrdb_results.json to import?

        A reminder every N starts would nag when nothing has changed and stay
        silent when everything has. The file's own timestamp answers the real
        question: the srrdb tool rewrites it after every batch, so if it is
        newer than the last import there ARE new measured rebuilds waiting,
        and if it is not there is nothing to do."""
        path = self._app_dir / "srrdb_results.json"
        if not path.is_file():
            return {"ok": True, "stale": False}
        try:
            cfg = json.loads(self._config_path.read_text("utf-8"))
        except Exception:
            cfg = {}
        done = int(cfg.get("priors_imported_mtime") or 0)
        mtime = int(path.stat().st_mtime)
        try:
            n = len(json.loads(path.read_text("utf-8")))
        except Exception:
            n = 0
        return {"ok": True, "stale": mtime > done, "never": not done,
                "records": n,
                "since": int(cfg.get("priors_imported_records") or 0),
                "when": datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")}

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

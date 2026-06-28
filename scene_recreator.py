"""
Scene ZIP Recreator — matches old scene .zip releases against DAT-listed
target CRC32/MD5/SHA1 by attempting a sequence of known, non-destructive
repair techniques (byte-level ZIP surgery, line-ending fixes, junk-file
removal) without ever touching the original file.

Techniques attempted, in order, each re-checked against the target hash.
Steps 3, 4, 6, 7, and 8 each search the FULL packaging space — every
entry ordering combined with every compression level (0-9) and zlib
strategy, plus any learned group fingerprints — rather than stopping
after one default rebuild, since the correct fix is often a structural
change AND a specific compression setting together, not either alone:
  1. EOCD comment strip/change (grow-append topsite tagline removal)
  2. FAT-front / Unix-tail grow-append truncation + EOCD rebuild
  3. Line-ending normalisation (CRLF<->LF) on .nfo/.diz entries
  4. Junk-file removal (entries added by a grow-append, re-zipped)
  5. Compression-setting variations (STORE, DEFLATE levels 0-9) on full
     rebuild
  6. Faithful rebuild — reuses every original header field (version
     needed/made-by, flag bits, DOS timestamp, internal/external
     attributes) from the file's own central directory, and searches
     across entry ordering (original/alphabetical/reverse/size-based)
     combined with every compression level (0-9) and zlib strategy
     (default/filtered/huffman-only/rle/fixed) — covers cases where a
     different original packer used different settings but the same
     underlying metadata
  7. Heuristic junk removal — flags entries that look like injected
     FTP-script adverts/courier tags/topsite signatures (by name pattern,
     including folder names like "adverts/", independent of the
     FAT/Unix split used by technique 4), removes every non-empty subset
     of flagged candidates, and re-runs the full search on the survivors
  8. Strip to essentials (last resort) — keeps only the single largest
     entry (assumed to be the main ROM/content file) plus any .nfo/.diz
     entries, discarding everything else regardless of name, then tries
     both line-ending variants on the survivors across the full search.
     Useful when junk has no recognisable name pattern at all.
  9. Foreign-packer diagnosis (no fix attempted) — checks whether every
     entry's stored CRC32 matches its actual decompressed content; if so,
     the structure is internally clean and the most likely explanation is
     that the original release was packed with a different ZIP tool whose
     exact compressed-byte output cannot be reproduced by Python's zlib.
     Reported distinctly from genuine content/structure problems so you
     can tell "probably fine, can't byte-match" apart from "actually
     broken."

Optional reference-folder fingerprinting: if one or more folders of
known-good, DAT-verified scene zips are supplied, each is matched
against the DAT (same 3-way name matching as the main scan),
CRC/MD5/SHA1-verified, then analysed to learn that release group's
actual packer fingerprint (compression level/strategy, entry order
convention, header metadata, AND the exact set of entry names present).
Reference files are always copied to a temp dir before inspection and
are never opened/modified in place. When repairing a broken zip, the
learned fingerprint(s) for its release group are tried first — usually
1-4 attempts instead of the dozens needed by the blind search — before
falling through to the general techniques above. A second reference-
based pass also catches junk by direct comparison: any entry in a
broken zip that isn't one of the per-release-unique files (rom/nfo/diz)
and doesn't appear in ANY verified reference for that group is treated
as a strong junk candidate, regardless of what it's named — this catches
injected files that name-pattern heuristics would miss entirely.

Original files are never modified. All work happens on a copy in a
separate output folder.
"""

import os
import re
import json
import zlib
import struct
import shutil
import hashlib
import threading
import zipfile
from pathlib import Path
from dataclasses import dataclass, field

import webview

try:
    from dat_merger import parse_dat_file
except Exception:
    parse_dat_file = None


# ═══════════════════════════════════════════════════════════════════════
# ZIP STRUCTURE PARSING (raw byte level)
# ═══════════════════════════════════════════════════════════════════════

EOCD_SIG = b'PK\x05\x06'
CD_SIG = b'PK\x01\x02'
LOCAL_SIG = b'PK\x03\x04'


@dataclass
class ZipCDRecord:
    """A parsed central directory record, with its raw bytes preserved."""
    name: str
    crc32: int
    comp_size: int
    uncomp_size: int
    local_offset: int
    version_made_by: int
    raw: bytes          # the exact original CD record bytes
    extra: bytes
    # Extra metadata captured for faithful rebuilds (technique_faithful_rebuild)
    version_needed: int = 0
    flag_bits: int = 0
    compress_method: int = 0
    dos_time: int = 0
    dos_date: int = 0
    internal_attr: int = 0
    external_attr: int = 0
    is_unix: bool = field(init=False)

    def __post_init__(self):
        # version_made_by high byte: 0 = FAT/DOS, 3 = Unix
        self.is_unix = (self.version_made_by >> 8) == 3


def find_eocd(data: bytes):
    """Locate EOCD record, scanning backward (handles trailing comment)."""
    idx = data.rfind(EOCD_SIG)
    if idx == -1:
        return None
    return idx


def parse_central_directory(data: bytes) -> list:
    """Parse all central directory records into ZipCDRecord objects."""
    eocd_idx = find_eocd(data)
    if eocd_idx is None:
        raise ValueError("No EOCD record found — not a valid ZIP")

    eocd = data[eocd_idx:eocd_idx + 22]
    n_entries = struct.unpack('<H', eocd[10:12])[0]
    cd_size = struct.unpack('<I', eocd[12:16])[0]
    cd_offset = struct.unpack('<I', eocd[16:20])[0]

    records = []
    pos = cd_offset
    for _ in range(n_entries):
        if data[pos:pos + 4] != CD_SIG:
            break
        version_made_by = struct.unpack('<H', data[pos + 4:pos + 6])[0]
        version_needed = struct.unpack('<H', data[pos + 6:pos + 8])[0]
        flag_bits = struct.unpack('<H', data[pos + 8:pos + 10])[0]
        compress_method = struct.unpack('<H', data[pos + 10:pos + 12])[0]
        dos_time = struct.unpack('<H', data[pos + 12:pos + 14])[0]
        dos_date = struct.unpack('<H', data[pos + 14:pos + 16])[0]
        crc32 = struct.unpack('<I', data[pos + 16:pos + 20])[0]
        comp_size = struct.unpack('<I', data[pos + 20:pos + 24])[0]
        uncomp_size = struct.unpack('<I', data[pos + 24:pos + 28])[0]
        name_len = struct.unpack('<H', data[pos + 28:pos + 30])[0]
        extra_len = struct.unpack('<H', data[pos + 30:pos + 32])[0]
        comment_len = struct.unpack('<H', data[pos + 32:pos + 34])[0]
        internal_attr = struct.unpack('<H', data[pos + 36:pos + 38])[0]
        external_attr = struct.unpack('<I', data[pos + 38:pos + 42])[0]
        local_offset = struct.unpack('<I', data[pos + 42:pos + 46])[0]

        rec_len = 46 + name_len + extra_len + comment_len
        raw = data[pos:pos + rec_len]
        name = data[pos + 46:pos + 46 + name_len].decode('utf-8', 'replace')
        extra = data[pos + 46 + name_len:pos + 46 + name_len + extra_len]

        records.append(ZipCDRecord(
            name=name, crc32=crc32, comp_size=comp_size,
            uncomp_size=uncomp_size, local_offset=local_offset,
            version_made_by=version_made_by, raw=raw, extra=extra,
            version_needed=version_needed, flag_bits=flag_bits,
            compress_method=compress_method, dos_time=dos_time,
            dos_date=dos_date, internal_attr=internal_attr,
            external_attr=external_attr,
        ))
        pos += rec_len

    return records


def build_eocd(n_entries: int, cd_size: int, cd_offset: int,
                comment: bytes = b'') -> bytes:
    """Build a fresh EOCD record."""
    return (
        EOCD_SIG +
        struct.pack('<HHHHIIH',
                    0, 0,            # disk number, cd start disk
                    n_entries, n_entries,
                    cd_size, cd_offset,
                    len(comment)) +
        comment
    )


def crc32_of(data: bytes) -> str:
    return f"{zlib.crc32(data) & 0xFFFFFFFF:08x}"


def md5_of(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def sha1_of(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


# ═══════════════════════════════════════════════════════════════════════
# REPAIR TECHNIQUES
# ═══════════════════════════════════════════════════════════════════════

class RepairAttempt:
    """Result of a single repair technique attempt."""
    def __init__(self, technique: str, success: bool, data: bytes = None,
                 detail: str = "", diagnosis: str = ""):
        self.technique = technique
        self.success = success
        self.data = data
        self.detail = detail
        # Optional extra diagnostic note shown even on failure — used to
        # explain *why* a technique couldn't fix it (e.g. "content is
        # almost certainly correct, but original packer differs")
        self.diagnosis = diagnosis


def technique_eocd_comment_strip(data: bytes, target_crc: str) -> RepairAttempt:
    """Try removing or blanking the EOCD comment (common topsite tagline)."""
    eocd_idx = find_eocd(data)
    if eocd_idx is None:
        return RepairAttempt("eocd_comment_strip", False, detail="no EOCD found")

    comment_len = struct.unpack('<H', data[eocd_idx + 20:eocd_idx + 22])[0]
    if comment_len == 0:
        return RepairAttempt("eocd_comment_strip", False, detail="no comment present")

    # Try with comment stripped entirely
    no_comment_eocd = data[eocd_idx:eocd_idx + 20] + struct.pack('<H', 0)
    candidate = data[:eocd_idx] + no_comment_eocd
    if crc32_of(candidate) == target_crc:
        return RepairAttempt(
            "eocd_comment_strip", True, candidate,
            f"removed {comment_len}-byte EOCD comment"
        )
    return RepairAttempt(
        "eocd_comment_strip", False,
        detail=f"tried removing {comment_len}-byte comment, no match"
    )


def technique_grow_append_truncate(data: bytes, target_crc: str) -> RepairAttempt:
    """
    Detect FAT-front / Unix-tail grow-append pattern and truncate at the
    first foreign (Unix) local header, rebuilding CD + EOCD from the
    surviving FAT-origin records.
    """
    try:
        records = parse_central_directory(data)
    except Exception as e:
        return RepairAttempt("grow_append_truncate", False, detail=f"parse error: {e}")

    if len(records) < 2:
        return RepairAttempt("grow_append_truncate", False, detail="too few entries")

    # Find first record that is Unix-origin, while earlier ones are FAT
    first_unix_idx = None
    for i, rec in enumerate(records):
        if rec.is_unix:
            first_unix_idx = i
            break

    if first_unix_idx is None or first_unix_idx == 0:
        return RepairAttempt(
            "grow_append_truncate", False,
            detail="no FAT-front/Unix-tail split detected"
        )

    fat_records = records[:first_unix_idx]
    cut_offset = records[first_unix_idx].local_offset

    part1 = data[:cut_offset]
    cd_bytes = b''.join(r.raw for r in fat_records)

    # Try both with and without a comment (empty comment first — most common)
    for comment in (b'', None):
        if comment is None:
            continue
        eocd = build_eocd(len(fat_records), len(cd_bytes), cut_offset, comment)
        candidate = part1 + cd_bytes + eocd
        if crc32_of(candidate) == target_crc:
            return RepairAttempt(
                "grow_append_truncate", True, candidate,
                f"truncated at offset {cut_offset}, kept {len(fat_records)} "
                f"FAT entries, removed {len(records) - len(fat_records)} "
                f"Unix-grafted entries"
            )

    return RepairAttempt(
        "grow_append_truncate", False,
        detail=f"detected split at entry {first_unix_idx} (offset {cut_offset}) "
               f"but rebuilt EOCD did not match"
    )


def technique_line_ending_fix(data: bytes, target_crc: str, fingerprints: list = None) -> RepairAttempt:
    """
    Try normalising line endings (CRLF<->LF) on .nfo/.diz entries, then
    search the FULL packaging space (entry order x compression level x
    zlib strategy, plus any known group fingerprints) for each line-
    ending variant — since the correct fix is often "different line
    endings AND a different compression setting", not just one or the
    other.
    """
    try:
        records = parse_central_directory(data)
    except Exception as e:
        return RepairAttempt("line_ending_fix", False, detail=f"parse error: {e}")

    if not records:
        return RepairAttempt("line_ending_fix", False, detail="no entries to inspect")

    text_names = {r.name for r in records if r.name.lower().endswith(('.nfo', '.diz'))}
    if not text_names:
        return RepairAttempt("line_ending_fix", False, detail="no .nfo/.diz entries")

    try:
        import io
        orig_contents = {}
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for rec in records:
                orig_contents[rec.name] = zf.read(rec.name)
    except Exception as e:
        return RepairAttempt("line_ending_fix", False, detail=f"could not read entries: {e}")

    for mode in ('crlf', 'lf'):
        contents = dict(orig_contents)
        for name in text_names:
            if mode == 'crlf':
                contents[name] = contents[name].replace(b'\r\n', b'\n').replace(b'\n', b'\r\n')
            else:
                contents[name] = contents[name].replace(b'\r\n', b'\n')

        result = _search_rebuild_combinations(
            records, contents, target_crc, fingerprints=fingerprints,
            technique_name="line_ending_fix",
            extra_detail=f"normalised {mode.upper()} on {', '.join(text_names)}, "
        )
        if result.success:
            return result

    return RepairAttempt(
        "line_ending_fix", False,
        detail=f"tried CRLF/LF on {', '.join(text_names)} across full "
               f"order/compression/fingerprint search — no match"
    )

    return RepairAttempt("line_ending_fix", False, detail="tried CRLF/LF, no match")


def technique_junk_removal(data: bytes, target_crc: str, fingerprints: list = None) -> RepairAttempt:
    """
    Try removing entries that look like grow-append junk (Unix-origin
    entries appended after FAT entries) entirely, then search the FULL
    packaging space (entry order x compression level x zlib strategy,
    plus any known group fingerprints) on the survivors.
    """
    try:
        records = parse_central_directory(data)
    except Exception as e:
        return RepairAttempt("junk_removal", False, detail=f"parse error: {e}")

    unix_records = [r for r in records if r.is_unix]
    if not unix_records:
        return RepairAttempt("junk_removal", False, detail="no candidate junk entries")

    fat_records = [r for r in records if not r.is_unix]
    if not fat_records:
        return RepairAttempt("junk_removal", False, detail="no FAT entries remain")

    try:
        import io
        contents = {}
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for rec in fat_records:
                contents[rec.name] = zf.read(rec.name)
    except Exception as e:
        return RepairAttempt("junk_removal", False, detail=f"could not read entries: {e}")

    removed_names = [r.name for r in unix_records]
    result = _search_rebuild_combinations(
        fat_records, contents, target_crc, fingerprints=fingerprints,
        technique_name="junk_removal",
        extra_detail=f"removed junk entries ({', '.join(removed_names)}), "
    )
    if result.success:
        return result

    return RepairAttempt(
        "junk_removal", False,
        detail=f"removed candidates ({', '.join(removed_names)}), tried full "
               f"order/compression/fingerprint search — no match"
    )


def technique_compression_variants(data: bytes, target_crc: str) -> RepairAttempt:
    """
    Rebuild fresh trying STORE vs DEFLATE, and for DEFLATE, every zlib
    compression level (0-9) plus the available zlib strategies, since
    different original zip tools (PKZIP, Info-ZIP, WinZip, etc.) and
    settings produce different compressed bytes for identical content.
    """
    try:
        import io
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = zf.namelist()
            contents = {n: zf.read(n) for n in names}

            # STORE — no compression, bytes are deterministic regardless of tool
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, 'w', zipfile.ZIP_STORED) as out_zf:
                for n in names:
                    out_zf.writestr(n, contents[n])
            if crc32_of(buf.getvalue()) == target_crc:
                return RepairAttempt(
                    "compression_variants", True, buf.getvalue(),
                    "rebuilt fresh using STORE (no compression)"
                )

            # DEFLATE at every zlib level — Python's zipfile only exposes
            # compresslevel via the ZipFile constructor (3.7+)
            for level in range(0, 10):
                buf = io.BytesIO()
                with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED,
                                      compresslevel=level) as out_zf:
                    for n in names:
                        out_zf.writestr(n, contents[n])
                candidate = buf.getvalue()
                if crc32_of(candidate) == target_crc:
                    return RepairAttempt(
                        "compression_variants", True, candidate,
                        f"rebuilt fresh using DEFLATE level {level}"
                    )
    except Exception as e:
        return RepairAttempt("compression_variants", False, detail=f"error: {e}")

    return RepairAttempt(
        "compression_variants", False,
        detail="tried STORE and DEFLATE levels 0-9, no match"
    )


def _likely_text_content(data: bytes) -> bool:
    """
    Heuristic matching what real zip tools use to set the internal_attr
    'file is ASCII/text' bit: no null bytes, and the vast majority of
    bytes are printable ASCII or common whitespace. Used to derive a
    correct per-entry internal_attr when header_overrides are in play
    (since internal_attr is content-dependent, not a group-wide packer
    setting, so a single override value would be wrong — see
    PackerFingerprint.as_header_overrides).
    """
    if not data:
        return True
    if b'\x00' in data:
        return False
    sample = data[:4096]
    text_chars = sum(1 for b in sample if b in (9, 10, 13) or 32 <= b <= 126)
    return (text_chars / len(sample)) > 0.95


def _rebuild_zip_faithful(records: list, contents: dict, order: list,
                            compress_method: int, zlib_level: int = None,
                            zlib_strategy: int = None,
                            header_overrides: dict = None) -> bytes:
    """
    Manually rebuild a ZIP container using each entry's ORIGINAL metadata
    (version needed/made-by, flag bits, DOS time/date, internal/external
    attributes, extra field) from the parsed central directory, but with
    freshly compressed data. Only the entry order and compression
    settings are varied — everything else is carried over verbatim from
    the existing file's own headers, UNLESS header_overrides is given.

    `order` is a list of indices into `records` specifying the entry
    sequence to write (different packers add files in different orders).
    `compress_method` is 0 (stored) or 8 (deflate).

    `header_overrides`, if given, is a dict that can contain any of
    'version_made_by', 'version_needed', 'flag_bits', 'internal_attr',
    'external_attr' — these values are used for EVERY entry instead of
    each record's own value. This matters because the broken file being
    repaired may itself have had these fields altered (e.g. by whatever
    re-zipped it), so its own headers are not always trustworthy — a
    verified reference's fingerprint can supply the correct values
    instead. Fields not present in the dict still fall back to each
    record's own value as before.
    """
    by_name = {r.name: r for r in records}
    local_parts = []
    cd_parts = []
    offset = 0
    local_offsets = {}
    ov = header_overrides or {}

    for idx in order:
        rec = records[idx]
        raw_content = contents[rec.name]

        if compress_method == 0:
            comp_data = raw_content
        else:
            if zlib_strategy is not None:
                co = zlib.compressobj(
                    zlib_level if zlib_level is not None else 6,
                    zlib.DEFLATED, -15, 8, zlib_strategy
                )
            else:
                co = zlib.compressobj(
                    zlib_level if zlib_level is not None else 6,
                    zlib.DEFLATED, -15
                )
            comp_data = co.compress(raw_content) + co.flush()

        name_bytes = rec.name.encode('utf-8')
        crc = zlib.crc32(raw_content) & 0xFFFFFFFF

        version_needed = ov.get('version_needed', rec.version_needed)
        flag_bits = ov.get('flag_bits', rec.flag_bits)

        # Local file header — reuse original version_needed/flag_bits/
        # dos_time/dos_date verbatim (or overridden values); method/
        # sizes/crc reflect the rebuild
        local_header = (
            LOCAL_SIG +
            struct.pack('<HHHHHIIIHH',
                        version_needed, flag_bits, compress_method,
                        rec.dos_time, rec.dos_date,
                        crc, len(comp_data), len(raw_content),
                        len(name_bytes), 0)  # extra_len = 0 in local header
        ) + name_bytes

        local_offsets[rec.name] = offset
        local_parts.append(local_header + comp_data)
        offset += len(local_header) + len(comp_data)

    # Central directory — same field reuse, pointing at the new local offsets
    for idx in order:
        rec = records[idx]
        raw_content = contents[rec.name]
        crc = zlib.crc32(raw_content) & 0xFFFFFFFF
        name_bytes = rec.name.encode('utf-8')

        if compress_method == 0:
            comp_size = len(raw_content)
        else:
            # Recompute using same settings as above for accurate size
            if zlib_strategy is not None:
                co = zlib.compressobj(
                    zlib_level if zlib_level is not None else 6,
                    zlib.DEFLATED, -15, 8, zlib_strategy
                )
            else:
                co = zlib.compressobj(
                    zlib_level if zlib_level is not None else 6,
                    zlib.DEFLATED, -15
                )
            comp_size = len(co.compress(raw_content) + co.flush())

        version_made_by = ov.get('version_made_by', rec.version_made_by)
        version_needed = ov.get('version_needed', rec.version_needed)
        flag_bits = ov.get('flag_bits', rec.flag_bits)
        external_attr = ov.get('external_attr', rec.external_attr)
        if header_overrides is not None:
            # Overrides active means we don't trust the broken file's
            # own per-entry internal_attr either — derive it correctly
            # from this entry's actual content instead (text vs binary),
            # matching what real zip tools do automatically.
            internal_attr = 1 if _likely_text_content(raw_content) else 0
        else:
            internal_attr = rec.internal_attr

        cd_record = (
            CD_SIG +
            struct.pack('<HHHHHHIIIHHHHHII',
                        version_made_by, version_needed,
                        flag_bits, compress_method,
                        rec.dos_time, rec.dos_date,
                        crc, comp_size, len(raw_content),
                        len(name_bytes), 0, 0,  # extra_len, comment_len = 0
                        0, internal_attr, external_attr,
                        local_offsets[rec.name])
        ) + name_bytes
        cd_parts.append(cd_record)

    local_blob = b''.join(local_parts)
    cd_blob = b''.join(cd_parts)
    eocd = build_eocd(len(order), len(cd_blob), len(local_blob))

    return local_blob + cd_blob + eocd


def _search_rebuild_combinations(records: list, contents: dict, target_crc: str,
                                    fingerprints: list = None,
                                    technique_name: str = "rebuild",
                                    extra_detail: str = "") -> 'RepairAttempt':
    """
    Shared exhaustive search used by every structural technique (line-
    ending fix, junk removal, heuristic junk removal, last-resort strip)
    so that whenever a structural change is made, it's tried against the
    FULL compression search space — not just one default rebuild. This
    is what makes "fix X" and "find the right compression" actually
    cross-multiply, instead of each technique guessing one fixed
    compression setting and giving up.

    Search order, fastest/most-likely first:
      1. Any known group fingerprints (order + compression learned from
         verified references) — usually 1-4 attempts, tried first since
         they're most likely correct for this group
      2. STORE, in every entry ordering
      3. DEFLATE at levels 1/6/9 x 5 zlib strategies, in every entry
         ordering (original/alphabetical/reverse/size-ascending)

    `records`/`contents` describe the STRUCTURE AFTER the structural
    change has already been applied (e.g. junk entries already removed,
    or .nfo/.diz content already line-ending-normalised) — this function
    only varies packaging (order + compression), not content.
    """
    n = len(records)
    if n == 0:
        return RepairAttempt(technique_name, False, detail="no entries remain after structural change")

    orig_order = list(range(n))
    alpha_order = sorted(orig_order, key=lambda i: records[i].name.lower())
    alpha_rev_order = list(reversed(alpha_order))
    size_order = sorted(orig_order, key=lambda i: records[i].uncomp_size)
    orderings = [
        ("original order", orig_order),
        ("alphabetical order", alpha_order),
        ("reverse alphabetical order", alpha_rev_order),
        ("size-ascending order", size_order),
    ]
    order_by_name = {
        'original': orig_order,
        'alphabetical': alpha_order,
        'reverse_alphabetical': alpha_rev_order,
        'size_ascending': size_order,
    }

    tried = 0

    # 1. Known group fingerprints first — fastest path if available.
    # Try WITH the fingerprint's header overrides (version_made_by,
    # flag_bits, internal/external_attr from the verified reference)
    # first, since the broken file's own headers may themselves have
    # been altered by whatever process broke it — then fall back to
    # using the broken file's own headers in case those happen to
    # already be correct and overriding would be wrong.
    if fingerprints:
        for fp in fingerprints:
            fp_order = _order_indices_for_kind(records, fp.order_kind)
            for use_overrides in (True, False):
                try:
                    candidate = _rebuild_zip_faithful(
                        records, contents, fp_order,
                        compress_method=fp.compress_method,
                        zlib_level=fp.zlib_level if fp.compress_method == 8 else None,
                        zlib_strategy=fp.zlib_strategy if fp.compress_method == 8 else None,
                        header_overrides=fp.as_header_overrides() if use_overrides else None,
                    )
                    tried += 1
                    if crc32_of(candidate) == target_crc:
                        override_note = " (using reference's header metadata)" if use_overrides else ""
                        return RepairAttempt(
                            technique_name, True, candidate,
                            f"{extra_detail}using group '{fp.group}' fingerprint from "
                            f"{fp.source_file} (order={fp.order_kind}, "
                            f"method={'STORE' if fp.compress_method == 0 else 'DEFLATE'}"
                            f"{f', level={fp.zlib_level}' if fp.compress_method == 8 else ''})"
                            f"{override_note}"
                        )
                except Exception:
                    continue

    # 2 & 3. Blind search across orderings x compression settings
    strategies = [
        ("default", None), ("filtered", zlib.Z_FILTERED),
        ("huffman-only", zlib.Z_HUFFMAN_ONLY), ("rle", zlib.Z_RLE),
        ("fixed", zlib.Z_FIXED),
    ]
    for order_name, order in orderings:
        try:
            candidate = _rebuild_zip_faithful(records, contents, order, compress_method=0)
            tried += 1
            if crc32_of(candidate) == target_crc:
                return RepairAttempt(
                    technique_name, True, candidate,
                    f"{extra_detail}rebuilt with {order_name}, STORE"
                )
        except Exception:
            pass

        for level in range(1, 10):
            for strat_name, strat in strategies:
                try:
                    candidate = _rebuild_zip_faithful(
                        records, contents, order, compress_method=8,
                        zlib_level=level, zlib_strategy=strat
                    )
                    tried += 1
                    if crc32_of(candidate) == target_crc:
                        return RepairAttempt(
                            technique_name, True, candidate,
                            f"{extra_detail}rebuilt with {order_name}, DEFLATE "
                            f"level {level} strategy={strat_name}"
                        )
                except Exception:
                    continue

    return RepairAttempt(
        technique_name, False,
        detail=f"{extra_detail}tried {tried} order/compression/fingerprint "
               f"combinations after structural change — no match"
    )


def technique_faithful_rebuild(data: bytes, target_crc: str) -> RepairAttempt:
    """
    More thorough rebuild than technique_compression_variants: reuses
    every original header field (version needed/made-by, flag bits, DOS
    timestamp, internal/external attributes) from the existing file's own
    central directory — since those are already known-correct — and only
    varies what differs between packers: entry order, compression method,
    zlib level, and zlib strategy.

    Tries, in order: original entry order, alphabetical order, reverse
    alphabetical order, and size-ascending order; for each, STORE then
    DEFLATE at levels 1/6/9 with default strategy plus FILTERED/RLE/
    FIXED/HUFFMAN_ONLY strategies at level 6.
    """
    try:
        records = parse_central_directory(data)
    except Exception as e:
        return RepairAttempt("faithful_rebuild", False, detail=f"parse error: {e}")

    if not records:
        return RepairAttempt("faithful_rebuild", False, detail="no entries to rebuild")

    try:
        import io
        contents = {}
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for rec in records:
                contents[rec.name] = zf.read(rec.name)
    except Exception as e:
        return RepairAttempt("faithful_rebuild", False, detail=f"could not read entries: {e}")

    n = len(records)
    orig_order = list(range(n))
    alpha_order = sorted(orig_order, key=lambda i: records[i].name.lower())
    alpha_rev_order = list(reversed(alpha_order))
    size_order = sorted(orig_order, key=lambda i: records[i].uncomp_size)

    orderings = [
        ("original order", orig_order),
        ("alphabetical order", alpha_order),
        ("reverse alphabetical order", alpha_rev_order),
        ("size-ascending order", size_order),
    ]

    strategies = [
        ("default", None),
        ("filtered", zlib.Z_FILTERED),
        ("huffman-only", zlib.Z_HUFFMAN_ONLY),
        ("rle", zlib.Z_RLE),
        ("fixed", zlib.Z_FIXED),
    ]

    tried = 0
    for order_name, order in orderings:
        # STORE first — order is the only variable, no compression settings
        try:
            candidate = _rebuild_zip_faithful(records, contents, order, compress_method=0)
            tried += 1
            if crc32_of(candidate) == target_crc:
                return RepairAttempt(
                    "faithful_rebuild", True, candidate,
                    f"rebuilt with original headers, {order_name}, STORE"
                )
        except Exception:
            pass

        for level in range(1, 10):
            for strat_name, strat in strategies:
                try:
                    candidate = _rebuild_zip_faithful(
                        records, contents, order, compress_method=8,
                        zlib_level=level, zlib_strategy=strat
                    )
                    tried += 1
                    if crc32_of(candidate) == target_crc:
                        return RepairAttempt(
                            "faithful_rebuild", True, candidate,
                            f"rebuilt with original headers, {order_name}, "
                            f"DEFLATE level {level} strategy={strat_name}"
                        )
                except Exception:
                    continue

    return RepairAttempt(
        "faithful_rebuild", False,
        detail=f"tried {tried} combinations of entry order, compression "
               f"level and zlib strategy with original header metadata "
               f"preserved — no match"
    )


# Filenames commonly injected by old FTP/topsite scripts as adverts,
# courier signatures, or site taglines — usually tiny files with no
# game-relevant content. Matched case-insensitively, with or without
# extension, since these vary a lot site to site.
_JUNK_NAME_HINTS = (
    'advert', 'adverts', 'banner', 'site.nfo', 'sitenfo', '.vert',
    'courier', 'group.nfo', 'top10', 'topsite', 'affil', 'ftp_data',
    'ftp-data', '0day',
)

# Standard scene-release sidecar names that should NEVER be treated as
# junk, even though they're small text/info files too.
_KNOWN_GOOD_SIDECARS = ('file_id.diz', 'nfo')


def _looks_like_junk_entry(name: str, uncomp_size: int, total_entries: int) -> bool:
    """
    Heuristic check for whether a ZIP entry is likely an injected
    advert/courier/tagline file rather than legitimate release content.
    Deliberately conservative on NAME matching (avoiding false positives
    on real release files), but size is treated as a weak signal only —
    name pattern is what actually drives the decision, since junk/advert
    files vary wildly in size (some are padded with ANSI art, ASCII
    banners, etc. and can run to several KB).
    """
    lower = name.lower()
    parts = lower.split('/')
    base = parts[-1]  # the filename itself
    folder_parts = parts[:-1]  # any containing folder names, e.g. "adverts"

    # Never flag the standard .nfo (proper release nfo) or file_id.diz —
    # but ONLY if they're not sitting inside a junk-named folder, since a
    # real-looking filename inside an "adverts/" folder is still junk
    if base == 'file_id.diz' and not any(
        any(hint in fp for hint in _JUNK_NAME_HINTS) for fp in folder_parts
    ):
        return False
    if base.endswith('.nfo') and not any(h in base for h in _JUNK_NAME_HINTS) and not any(
        any(hint in fp for hint in _JUNK_NAME_HINTS) for fp in folder_parts
    ):
        # A .nfo file not matching junk hints and not inside a junk
        # folder — could still be a generic short name like "-MI-" with
        # no extension, handled below instead
        return False

    # Explicit junk-name hints anywhere in the filename OR in any
    # containing folder name — this is the fix: a file living inside
    # "adverts/" should be flagged even if its own filename looks benign
    if any(hint in base for hint in _JUNK_NAME_HINTS):
        return True
    if any(any(hint in fp for hint in _JUNK_NAME_HINTS) for fp in folder_parts):
        return True

    # Cryptic short all-caps/dashes names with no extension — a classic
    # courier/topsite tag pattern, e.g. "-MI-", "-XYZ-", "-TRSI-", "-Z9-".
    # Name pattern alone is the signal here; size is not checked since
    # these files vary considerably (a few bytes to several KB of ANSI/
    # ASCII art or padding). Digits ARE allowed in the tag body (e.g.
    # "-Z9-", "-FTP9-") since that's a common real-world pattern —
    # excluding them entirely missed genuine junk tags.
    is_short_tag_pattern = (
        len(base) <= 12 and
        ('-' in base or (base.isupper() and base.isalpha())) and
        '.' not in base  # no file extension at all
    )
    if is_short_tag_pattern:
        return True

    return False


def technique_heuristic_junk_removal(data: bytes, target_crc: str, fingerprints: list = None) -> RepairAttempt:
    """
    Removes entries that LOOK like injected adverts/courier tags/topsite
    signatures based on name/size heuristics (not just the FAT/Unix
    version-made-by split used by technique_junk_removal), then searches
    the FULL packaging space (entry order x compression level x zlib
    strategy, plus any known group fingerprints) on the survivors for
    every non-empty subset of flagged candidates.

    This catches cases like a single-origin zip (no grow-append split)
    that still has a junk file like "-MI-" sitting alongside the real
    release content — common with old FTP scripts that auto-injected
    site adverts into every upload.
    """
    try:
        records = parse_central_directory(data)
    except Exception as e:
        return RepairAttempt("heuristic_junk_removal", False, detail=f"parse error: {e}")

    if len(records) < 2:
        return RepairAttempt("heuristic_junk_removal", False, detail="too few entries to consider removal")

    # Combine reference-based detection (entries absent from this
    # group's known-good references) with name-pattern heuristics —
    # NOT mutually exclusive, since a file can have one junk entry that
    # happens to coincide with a name in an imperfect reference set
    # (missed by reference-based detection alone) alongside a separate
    # entry that IS pattern-recognisable junk, or vice versa. Using
    # only one detection method risks only ever removing half the
    # actual junk in a file, so the combination search below would
    # never reach the truly-correct subset.
    candidates_to_remove = [
        r for r in records
        if _looks_like_junk_entry(r.name, r.uncomp_size, len(records))
    ]
    if fingerprints:
        known_names = set()
        for fp in fingerprints:
            known_names |= set(fp.known_entry_names)
        if known_names:
            for r in records:
                if r.name.lower() not in known_names and r not in candidates_to_remove:
                    candidates_to_remove.append(r)

    # Image files (cover art etc.) — see the matching comment in
    # technique_surgical_delete for the full rationale. Added here too
    # so this technique catches the same cases when surgical_delete
    # itself doesn't apply (e.g. survivors were also recompressed).
    for r in records:
        if r.name.lower().rsplit('/', 1)[-1].endswith(('.jpg', '.jpeg', '.png', '.gif', '.bmp')):
            if r not in candidates_to_remove:
                candidates_to_remove.append(r)

    if not candidates_to_remove:
        return RepairAttempt("heuristic_junk_removal", False, detail="no suspicious junk-named entries found")

    junk_names = [r.name for r in candidates_to_remove]

    try:
        import io
        contents = {}
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for rec in records:
                contents[rec.name] = zf.read(rec.name)
    except Exception as e:
        return RepairAttempt("heuristic_junk_removal", False, detail=f"could not read entries: {e}")

    # Try every non-empty subset of the flagged candidates — with usually
    # only 1-3 suspicious entries this stays cheap, and covers cases
    # where multiple junk files were injected but the DAT target only
    # has some of them removed (e.g. one advert added later than another)
    from itertools import combinations
    removal_sets = []
    for r in range(1, len(candidates_to_remove) + 1):
        removal_sets.extend(combinations(candidates_to_remove, r))

    total_tried_label = []
    for remove_set in removal_sets:
        remove_names = {r.name for r in remove_set}
        surviving = [r for r in records if r.name not in remove_names]
        if not surviving:
            continue

        removed_str = ', '.join(r.name for r in remove_set)
        result = _search_rebuild_combinations(
            surviving, contents, target_crc, fingerprints=fingerprints,
            technique_name="heuristic_junk_removal",
            extra_detail=f"removed suspected junk ({removed_str}), "
        )
        if result.success:
            return result

    return RepairAttempt(
        "heuristic_junk_removal", False,
        detail=f"flagged {len(candidates_to_remove)} suspicious entr"
               f"{'y' if len(candidates_to_remove)==1 else 'ies'} "
               f"({', '.join(junk_names)}) and tried full order/compression/"
               f"fingerprint search across {len(removal_sets)} removal "
               f"combination(s) — no match"
    )


def _extract_local_entry_verbatim(data: bytes, rec: 'ZipCDRecord', fix_descriptors: bool = False) -> bytes:
    """
    Extract the COMPLETE local file entry — local header, filename,
    extra field, and compressed data — as one exact, untouched byte
    block straight from the original file. Used for true surgical
    deletion (matching what Info-ZIP's `zip -d` actually does): when
    removing a junk entry, every surviving entry's bytes are carried
    over byte-for-byte rather than being decompressed and recompressed.
    This guarantees the surviving entries' compressed bytes can never
    be wrong, since they're never touched at all — only the entries
    being deleted are ever removed, and only the central directory is
    rewritten to drop their records and fix up offsets.

    If fix_descriptors is True, also applies
    _strip_data_descriptor_if_redundant() to this entry — correcting a
    streaming-style/redundant-descriptor local header to a fully
    populated one with the flag bit cleared and the descriptor dropped,
    using only verified values from the central directory record. This
    only ever changes header bytes; the compressed data itself is
    untouched either way. Defaults to False so existing callers (and
    the existing regression-tested behaviour of plain deletion without
    any header changes) are unaffected unless explicitly requested.
    """
    pos = rec.local_offset
    if data[pos:pos + 4] != LOCAL_SIG:
        raise ValueError(f"no local header at offset {pos} for {rec.name}")
    name_len = struct.unpack('<H', data[pos + 26:pos + 28])[0]
    extra_len = struct.unpack('<H', data[pos + 28:pos + 30])[0]
    entry_end = pos + 30 + name_len + extra_len + rec.comp_size

    if fix_descriptors and (rec.flag_bits & 0x08):
        desc_len, new_header = _strip_data_descriptor_if_redundant(data, rec)
        if desc_len:
            # Corrected header + filename/extra + compressed data,
            # with the trailing descriptor dropped entirely
            return new_header + data[pos + 30:entry_end]

    return data[pos:entry_end]


def _rebuild_zip_surgical(records: list, data: bytes, keep_indices: list, fix_descriptors: bool = False) -> bytes:
    """
    Rebuild a zip by keeping ONLY the listed entries (by index into
    `records`), using each kept entry's EXACT original local-file-entry
    bytes verbatim (no decompression, no recompression — true surgical
    deletion, matching Info-ZIP's `zip -d file.zip junk_entry` command,
    which removes an entry in-place without touching any other entry's
    compressed data at all). The entries are kept in their existing
    relative order; only their byte offsets shift since earlier entries
    may have been removed. The central directory records are rebuilt
    from each entry's existing CD record bytes (rec.raw) with only the
    local_offset field patched, NOT recompressed metadata — every other
    header field (version_made_by, flag_bits, dos_time, internal/
    external_attr, extra field) is carried over byte-for-byte, UNLESS
    fix_descriptors corrects flag_bits for an entry with a redundant or
    backfillable data descriptor (see _strip_data_descriptor_if_redundant).

    fix_descriptors defaults to False, so plain deletion behaviour is
    completely unchanged unless a caller explicitly opts into also
    correcting descriptor-related header issues on the survivors —
    useful when a junk entry and a separate descriptor-flag corruption
    both need fixing together in the same rebuild.
    """
    local_parts = []
    cd_parts = []
    offset = 0
    new_offsets = {}
    fixed_flags = {}  # idx -> corrected flag_bits, only populated when fix_descriptors actually changed something

    for idx in keep_indices:
        rec = records[idx]
        if fix_descriptors and (rec.flag_bits & 0x08):
            desc_len, new_header = _strip_data_descriptor_if_redundant(data, rec)
            if desc_len:
                fixed_flags[idx] = rec.flag_bits & ~0x08
        entry_bytes = _extract_local_entry_verbatim(data, rec, fix_descriptors=fix_descriptors)
        new_offsets[idx] = offset
        local_parts.append(entry_bytes)
        offset += len(entry_bytes)

    for idx in keep_indices:
        rec = records[idx]
        # rec.raw is the original CD record's exact bytes (signature
        # through comment, but NOT including the filename/extra/comment
        # which follow it in the original parse) — patch the local
        # header offset field, and ALSO flag_bits if this entry's
        # descriptor was corrected, so the central directory stays
        # consistent with the corrected local header
        if idx in fixed_flags:
            cd_record = (
                rec.raw[:8] + struct.pack('<H', fixed_flags[idx]) + rec.raw[10:42]
                + struct.pack('<I', new_offsets[idx]) + rec.raw[46:]
            )
        else:
            cd_record = rec.raw[:42] + struct.pack('<I', new_offsets[idx]) + rec.raw[46:]
        cd_parts.append(cd_record)

    local_blob = b''.join(local_parts)
    cd_blob = b''.join(cd_parts)
    eocd = build_eocd(len(keep_indices), len(cd_blob), len(local_blob))

    return local_blob + cd_blob + eocd


def technique_surgical_delete(data: bytes, target_crc: str,
                                fingerprints: list = None) -> RepairAttempt:
    """
    True surgical deletion of suspected junk entries, matching exactly
    what Info-ZIP's `zip -d file.zip junk_entry` does: removes entries
    WITHOUT touching any other entry's compressed bytes at all — no
    decompression, no recompression, nothing. Every surviving entry's
    local header and compressed data is copied byte-for-byte verbatim
    from the original file. Only the central directory is rewritten to
    drop the removed entries' records and patch local-offset fields.

    This is the most reliable junk-removal technique available, since
    it can NEVER introduce a compression mismatch on the surviving
    entries (there's no recompression step to get wrong) — it only
    fails if the WRONG entries are identified as junk, or if the
    original packer didn't simply append/insert extra files (e.g. if it
    also reordered or touched the survivors in some other way).

    Junk candidates are identified the same way as the other junk-
    removal techniques: entries absent from this group's verified
    references (if fingerprints/known_entry_names are available), or
    failing that, name-pattern heuristics (junk-style folders/names).
    Tries removing each plausible non-empty subset, smallest first.
    """
    try:
        records = parse_central_directory(data)
    except Exception as e:
        return RepairAttempt("surgical_delete", False, detail=f"parse error: {e}")

    if len(records) < 2:
        return RepairAttempt("surgical_delete", False, detail="too few entries to consider deletion")

    # Identify candidate junk entries from BOTH detection methods and
    # combine them — these are NOT mutually exclusive. A file can have
    # one entry that's absent from references (caught by reference-
    # based detection) AND a separate entry that just happens to share
    # a name with something in an imperfect/incomplete reference set
    # but is still junk by pattern (caught by heuristic detection). If
    # only one detection method is consulted, a file with BOTH kinds of
    # junk in it will only ever get one of them removed, and the
    # combination search will never reach the actually-correct subset.
    candidates = []
    if fingerprints:
        known_names = set()
        for fp in fingerprints:
            known_names |= set(fp.known_entry_names)
        if known_names:
            candidates.extend(r for r in records if r.name.lower() not in known_names)

    heuristic_candidates = [r for r in records if _looks_like_junk_entry(r.name, r.uncomp_size, len(records))]
    for r in heuristic_candidates:
        if r not in candidates:
            candidates.append(r)

    # Image files (cover art etc.) are a recurring "maybe legitimate,
    # maybe extra" pattern in scene releases — some groups' DAT entries
    # include them, some don't, and unlike junk tags they're typically
    # named after the release itself (e.g. "Lunar Lancer.jpg" alongside
    # "Lunar Lancer.gb"), so neither name-pattern heuristics nor
    # reference-based "absent from known names" can reliably tell
    # whether a given .jpg belongs. Adding them as a candidate here is
    # safe regardless: the combinatorial search below only succeeds if
    # removing it actually produces the correct CRC, so a genuinely
    # required .jpg is never wrongly stripped — it just won't match
    # without it, and the search naturally falls through to keeping it.
    image_candidates = [r for r in records if r.name.lower().rsplit('/', 1)[-1].endswith(
        ('.jpg', '.jpeg', '.png', '.gif', '.bmp')
    )]
    for r in image_candidates:
        if r not in candidates:
            candidates.append(r)

    if not candidates:
        return RepairAttempt("surgical_delete", False, detail="no candidate junk entries identified")

    # Try removing each non-empty subset of candidates, smallest first
    # (most likely real-world case is removing exactly the junk files
    # and nothing else) — capped to keep this fast for larger candidate
    # counts, since the common case is 1-3 junk entries
    from itertools import combinations
    tried = 0
    all_indices = list(range(len(records)))
    candidate_indices = [i for i, r in enumerate(records) if r in candidates]

    for r in range(1, min(len(candidate_indices), 4) + 1):
        for remove_combo in combinations(candidate_indices, r):
            keep_indices = [i for i in all_indices if i not in remove_combo]

            # Try plain deletion first (matches existing tested
            # behaviour exactly, no header changes at all beyond the
            # offset patch every deletion already requires)
            try:
                candidate_zip = _rebuild_zip_surgical(records, data, keep_indices)
            except Exception:
                candidate_zip = None
            tried += 1
            if candidate_zip is not None and crc32_of(candidate_zip) == target_crc:
                removed_names = ', '.join(records[i].name for i in remove_combo)
                return RepairAttempt(
                    "surgical_delete", True, candidate_zip,
                    f"surgically removed entr{'y' if len(remove_combo)==1 else 'ies'} "
                    f"({removed_names}) — all surviving entries' compressed bytes "
                    f"carried over byte-for-byte, unchanged (no recompression)"
                )

            # If plain deletion didn't match, also try the same
            # combination WITH descriptor correction on survivors —
            # covers files where a junk entry AND a separate redundant/
            # streaming-style data-descriptor issue both need fixing in
            # the same rebuild (the deletion alone gets the entry count
            # right but the leftover descriptor bytes/flag still throw
            # the CRC off). Only attempted as a fallback, and only ever
            # changes header bytes on survivors — never their
            # compressed data.
            try:
                candidate_zip_fixed = _rebuild_zip_surgical(records, data, keep_indices, fix_descriptors=True)
            except Exception:
                continue
            tried += 1
            if crc32_of(candidate_zip_fixed) == target_crc:
                removed_names = ', '.join(records[i].name for i in remove_combo)
                return RepairAttempt(
                    "surgical_delete", True, candidate_zip_fixed,
                    f"surgically removed entr{'y' if len(remove_combo)==1 else 'ies'} "
                    f"({removed_names}) and corrected redundant/streaming-style data "
                    f"descriptor header(s) on survivors — compressed bytes carried over "
                    f"byte-for-byte, unchanged (no recompression)"
                )

    return RepairAttempt(
        "surgical_delete", False,
        detail=f"tried {tried} surgical-deletion combination(s) of candidate junk "
               f"entries ({', '.join(r.name for r in candidates)}) — no match"
    )


DATA_DESC_SIG = b'PK\x07\x08'


def _strip_data_descriptor_if_redundant(data: bytes, rec: 'ZipCDRecord') -> tuple:
    """
    For a single entry whose flag_bits has bit 3 set ("data descriptor
    present after the compressed data"), check for a genuine trailing
    descriptor and handle TWO distinct real-world corruption patterns,
    both of which end up needing the same fix — a fully-populated local
    header with flag bit 3 cleared and the descriptor removed, with the
    compressed bytes themselves never touched:

    1. REDUNDANT DESCRIPTOR: the local header already has the correct
       CRC/sizes (matching the central directory), but a descriptor was
       still added (and the flag set) despite never being needed — seen
       in releases re-touched by a tool that defaults to streaming mode.
       Here we simply clear the flag and drop the descriptor; the header
       is already correct.

    2. STREAMING-STYLE HEADER NEEDING BACKFILL: the local header has
       zeros for CRC/comp_size (genuinely relying on the descriptor and
       central directory for the real values) — but the known-good
       version of this exact release has those values filled in
       directly with no descriptor at all. Since the central directory
       record (and/or the descriptor) already has the correct values,
       we can safely backfill the header with them, matching what the
       correct version looks like, then drop the descriptor.

    Either way, this never guesses: the values written into the
    corrected header always come from the verified central directory
    record (rec.crc32/comp_size/uncomp_size), and a genuine descriptor
    with matching values must actually be found immediately after the
    compressed data before anything is changed. If the local header has
    zeros AND no matching descriptor is found, this is left completely
    alone — we don't have a verified source for the real values in that
    case, so guessing would be unsafe.

    Returns (descriptor_length, new_local_header_bytes) if applicable,
    or (0, None) if this entry should be left alone. new_local_header_
    bytes is the entry's local header with flag_bits bit 3 cleared and
    CRC/comp_size/uncomp_size set to the verified central directory
    values; every other byte (version_needed, method, dates, filename
    length fields, and all compressed data) is untouched.
    """
    if not (rec.flag_bits & 0x08):
        return 0, None  # bit not set — nothing to do for this entry

    pos = rec.local_offset
    if data[pos:pos + 4] != LOCAL_SIG:
        return 0, None
    local_flag = struct.unpack('<H', data[pos + 6:pos + 8])[0]
    local_crc = struct.unpack('<I', data[pos + 14:pos + 18])[0]
    local_comp_size = struct.unpack('<I', data[pos + 18:pos + 22])[0]
    local_uncomp_size = struct.unpack('<I', data[pos + 22:pos + 26])[0]
    name_len = struct.unpack('<H', data[pos + 26:pos + 28])[0]
    extra_len = struct.unpack('<H', data[pos + 28:pos + 30])[0]

    header_already_correct = (
        local_crc == rec.crc32 and local_comp_size == rec.comp_size
        and local_uncomp_size == rec.uncomp_size
    )

    # The compressed data's true length always comes from the central
    # directory record (rec.comp_size), since that's independently
    # verified — never from the local header, which may legitimately
    # be zero in the streaming-style case being handled here.
    data_start = pos + 30 + name_len + extra_len
    data_end = data_start + rec.comp_size

    found_valid_descriptor = False
    desc_len = 0
    for try_len, has_sig in ((16, True), (12, False)):
        desc = data[data_end:data_end + try_len]
        if len(desc) < try_len:
            continue
        if has_sig:
            if desc[:4] != DATA_DESC_SIG:
                continue
            desc_crc = struct.unpack('<I', desc[4:8])[0]
            desc_comp = struct.unpack('<I', desc[8:12])[0]
            desc_uncomp = struct.unpack('<I', desc[12:16])[0]
        else:
            desc_crc = struct.unpack('<I', desc[0:4])[0]
            desc_comp = struct.unpack('<I', desc[4:8])[0]
            desc_uncomp = struct.unpack('<I', desc[8:12])[0]
        if desc_crc == rec.crc32 and desc_comp == rec.comp_size and desc_uncomp == rec.uncomp_size:
            found_valid_descriptor = True
            desc_len = try_len
            break

    if header_already_correct:
        if not found_valid_descriptor:
            return 0, None  # nothing to strip — header's fine and no descriptor exists
        # Case 1: redundant descriptor, header unchanged, just clear the flag
        new_flag = local_flag & ~0x08
        new_header = data[pos:pos + 6] + struct.pack('<H', new_flag) + data[pos + 8:pos + 30]
        return desc_len, new_header

    # Header has zeros (or something other than the verified values) —
    # only proceed if we found a genuine matching descriptor, confirming
    # the central directory's values are independently corroborated by
    # the file itself rather than just trusted blindly
    if not found_valid_descriptor:
        return 0, None  # streaming-style header with no descriptor to confirm against — leave alone

    # Case 2: backfill the header with the verified CRC/sizes and clear
    # the flag — matches what the known-good version of this entry
    # looks like (fully populated header, no descriptor needed)
    new_flag = local_flag & ~0x08
    new_header = (
        data[pos:pos + 6] + struct.pack('<H', new_flag) + data[pos + 8:pos + 14]
        + struct.pack('<III', rec.crc32, rec.comp_size, rec.uncomp_size)
        + data[pos + 26:pos + 30]
    )
    return desc_len, new_header


def technique_data_descriptor_strip(data: bytes, target_crc: str,
                                      fingerprints: list = None) -> RepairAttempt:
    """
    Fixes a specific, narrow corruption pattern: one or more entries
    have flag_bits bit 3 set ("data descriptor follows") AND a genuine
    trailing data descriptor after their compressed data, but the
    local header ALREADY has the real CRC/sizes filled in — meaning
    the descriptor is entirely redundant (some tool added it, and set
    the flag, without it ever being structurally necessary). This is
    NOT a content or compression difference at all: every entry's
    compressed bytes are carried over completely untouched, only the
    flag bit is cleared and the redundant descriptor bytes are
    dropped, exactly mirroring what the verified-good original looks
    like in this scenario.

    Genuine streaming-zip entries (local header legitimately has zero
    CRC/sizes, with the real values only in the descriptor) are left
    completely alone — stripping those would be destructive, since the
    local header has no other source for those values.
    """
    try:
        records = parse_central_directory(data)
    except Exception as e:
        return RepairAttempt("data_descriptor_strip", False, detail=f"parse error: {e}")

    strippable = []
    for rec in records:
        desc_len, new_header = _strip_data_descriptor_if_redundant(data, rec)
        if desc_len:
            strippable.append((rec, desc_len, new_header))

    if not strippable:
        return RepairAttempt(
            "data_descriptor_strip", False,
            detail="no entries with a redundant trailing data descriptor found"
        )

    # Rebuild: walk every record in original order, copying each
    # entry's bytes verbatim EXCEPT for the strippable ones, where the
    # corrected (flag-cleared) header replaces the original header and
    # the trailing descriptor bytes are dropped entirely. Compressed
    # data itself is never touched for ANY entry.
    strip_map = {id(rec): (desc_len, new_header) for rec, desc_len, new_header in strippable}
    local_parts = []
    cd_parts = []
    offset = 0
    new_offsets = {}

    for rec in records:
        pos = rec.local_offset
        name_len = struct.unpack('<H', data[pos + 26:pos + 28])[0]
        extra_len = struct.unpack('<H', data[pos + 28:pos + 30])[0]
        data_start = pos + 30 + name_len + extra_len
        data_end = data_start + rec.comp_size

        if id(rec) in strip_map:
            desc_len, new_header = strip_map[id(rec)]
            entry_bytes = new_header + data[pos + 30:data_end]  # corrected header + filename/extra/compressed data, descriptor dropped
        else:
            entry_bytes = data[pos:data_end]  # untouched, no descriptor to begin with on this entry

        new_offsets[id(rec)] = offset
        local_parts.append(entry_bytes)
        offset += len(entry_bytes)

    for rec in records:
        if id(rec) in strip_map:
            # Patch flag_bits (offset 8) AND local_offset (offset 42) in
            # the central directory record; every other byte unchanged
            new_flag = rec.flag_bits & ~0x08
            cd_record = (
                rec.raw[:8] + struct.pack('<H', new_flag) + rec.raw[10:42]
                + struct.pack('<I', new_offsets[id(rec)]) + rec.raw[46:]
            )
        else:
            cd_record = rec.raw[:42] + struct.pack('<I', new_offsets[id(rec)]) + rec.raw[46:]
        cd_parts.append(cd_record)

    local_blob = b''.join(local_parts)
    cd_blob = b''.join(cd_parts)
    eocd = build_eocd(len(records), len(cd_blob), len(local_blob))
    candidate_zip = local_blob + cd_blob + eocd

    if crc32_of(candidate_zip) == target_crc:
        stripped_names = ', '.join(rec.name for rec, _, _ in strippable)
        return RepairAttempt(
            "data_descriptor_strip", True, candidate_zip,
            f"stripped redundant data descriptor(s) and cleared flag bit on "
            f"({stripped_names}) — all compressed bytes carried over byte-for-byte, "
            f"unchanged (no recompression)"
        )

    return RepairAttempt(
        "data_descriptor_strip", False,
        detail=f"found {len(strippable)} entr{'y' if len(strippable)==1 else 'ies'} with "
               f"redundant data descriptor(s) "
               f"({', '.join(rec.name for rec, _, _ in strippable)}) and stripped them — "
               f"still no match (something else differs too)"
    )

def technique_strip_to_essentials(data: bytes, target_crc: str, fingerprints: list = None) -> RepairAttempt:
    """
    Last-resort technique: strips EVERYTHING except the single largest
    entry (assumed to be the actual ROM/content file) plus any .nfo and
    .diz entries, discarding every other entry regardless of whether it
    looked suspicious by name — then tries both line-ending variants on
    the surviving .nfo/.diz AND the full order/compression/fingerprint
    search on top of that.

    This is intentionally more aggressive than heuristic_junk_removal:
    it doesn't try to recognise what's junk by name, it just assumes
    everything except the main content file and the two standard
    sidecar types is disposable scaffolding. Useful when a release has
    junk files with no recognisable name pattern and no reference
    available to compare against.
    """
    try:
        records = parse_central_directory(data)
    except Exception as e:
        return RepairAttempt("strip_to_essentials", False, detail=f"parse error: {e}")

    if len(records) < 2:
        return RepairAttempt("strip_to_essentials", False, detail="too few entries to strip")

    main_rec = max(records, key=lambda r: r.uncomp_size)
    sidecar_recs = [
        r for r in records
        if r is not main_rec and r.name.lower().endswith(('.nfo', '.diz'))
    ]
    survivors = [main_rec] + sidecar_recs

    if len(survivors) == len(records):
        return RepairAttempt(
            "strip_to_essentials", False,
            detail="nothing to strip — every entry is already the main "
                   "file or a .nfo/.diz sidecar"
        )

    stripped_names = [r.name for r in records if r not in survivors]

    try:
        import io
        orig_contents = {}
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for rec in survivors:
                orig_contents[rec.name] = zf.read(rec.name)
    except Exception as e:
        return RepairAttempt("strip_to_essentials", False, detail=f"could not read entries: {e}")

    stripped_str = ', '.join(stripped_names)

    # Try as-is first (no line-ending change), then both CRLF/LF variants
    # on the surviving sidecars — each against the full search space
    for mode in (None, 'crlf', 'lf'):
        contents = dict(orig_contents)
        if mode is not None:
            for rec in sidecar_recs:
                if mode == 'crlf':
                    contents[rec.name] = contents[rec.name].replace(b'\r\n', b'\n').replace(b'\n', b'\r\n')
                else:
                    contents[rec.name] = contents[rec.name].replace(b'\r\n', b'\n')

        mode_label = f", normalised {mode.upper()} on sidecars" if mode else ""
        result = _search_rebuild_combinations(
            survivors, contents, target_crc, fingerprints=fingerprints,
            technique_name="strip_to_essentials",
            extra_detail=f"stripped down to main file + .nfo/.diz only "
                         f"(removed: {stripped_str}){mode_label}, "
        )
        if result.success:
            return result

    return RepairAttempt(
        "strip_to_essentials", False,
        detail=f"stripped to main file + .nfo/.diz (removed: {stripped_str}), "
               f"tried both line-ending variants across full order/"
               f"compression/fingerprint search — no match"
    )


def technique_local_header_reconstruct(data: bytes, target_crc: str,
                                          fingerprints: list = None) -> RepairAttempt:
    """
    Fixes localized corruption confined to one or more LOCAL file
    headers — e.g. the very first few bytes of a file got scrambled
    (bad sector, partial overwrite, truncated/garbled transfer) while
    everything else (filename, compressed data, and critically the
    CENTRAL DIRECTORY) remains completely intact. Since the central
    directory independently records every field a local header has
    (flag_bits, compress_method, dos_time/date, crc32, comp_size,
    uncomp_size) except the constant 4-byte signature, a corrupted
    local header can be safely rebuilt from the verified CD record
    alone — no recompression, no guessing.

    This is deliberately self-verifying rather than blindly trusting
    the CD: for each entry, the reconstructed header is only used if
    (a) the filename bytes immediately following where the header
    would end still spell out the CD record's own name, and (b) the
    bytes at the position the CD's comp_size implies for the END of
    the compressed data are followed by either the next entry's local
    signature or the start of the central directory — i.e. the data
    region implied by the CD record's sizes lines up with real
    structure either side of it. If those checks fail for an entry,
    it's left untouched; this technique only ever fixes headers it can
    independently confirm are reconstructable, never ones it has to
    guess at.
    """
    try:
        records = parse_central_directory(data)
    except Exception as e:
        return RepairAttempt("local_header_reconstruct", False, detail=f"parse error: {e}")

    if not records:
        return RepairAttempt("local_header_reconstruct", False, detail="no entries found")

    eocd_idx = find_eocd(data)
    if eocd_idx is None:
        return RepairAttempt("local_header_reconstruct", False, detail="no EOCD found")
    cd_offset = struct.unpack('<I', data[eocd_idx + 16:eocd_idx + 20])[0]

    reconstructed = []  # (rec, new_header_bytes) for entries that needed and passed reconstruction
    for i, rec in enumerate(records):
        pos = rec.local_offset
        if data[pos:pos + 4] == LOCAL_SIG:
            continue  # this entry's header signature is intact — nothing to do here

        # Header signature is wrong/corrupted for this entry. Build the
        # CORRECT header purely from the CD record's own verified
        # fields (never from whatever garbled bytes are currently at
        # this position), using the CD record's exact name/extra
        # lengths so the byte layout matches what filename bytes
        # should follow.
        name_bytes = rec.name.encode('utf-8', errors='surrogateescape')
        extra_len = len(rec.extra)
        new_header = (
            LOCAL_SIG
            + struct.pack('<H', rec.version_needed)
            + struct.pack('<H', rec.flag_bits)
            + struct.pack('<H', rec.compress_method)
            + struct.pack('<H', rec.dos_time)
            + struct.pack('<H', rec.dos_date)
            + struct.pack('<I', rec.crc32)
            + struct.pack('<I', rec.comp_size)
            + struct.pack('<I', rec.uncomp_size)
            + struct.pack('<H', len(name_bytes))
            + struct.pack('<H', extra_len)
        )

        # Self-verification: does the filename right after our
        # reconstructed header actually match the CD record's name?
        name_start = pos + 30
        name_end = name_start + len(name_bytes)
        if data[name_start:name_end] != name_bytes:
            continue  # filename doesn't line up — don't guess, leave this entry alone

        # Self-verification: does the region the CD record's comp_size
        # implies for "end of this entry's data" land on either the
        # next entry's genuine local header signature, or the start of
        # the central directory (if this is the last entry)? This
        # confirms comp_size is trustworthy for THIS file, not just
        # generically plausible.
        data_end = name_end + extra_len + rec.comp_size
        if i + 1 < len(records):
            next_rec = records[i + 1]
            boundary_ok = (data_end == next_rec.local_offset)
        else:
            boundary_ok = (data_end == cd_offset)
        if not boundary_ok:
            continue  # sizes don't line up with real structure — leave alone

        reconstructed.append((rec, new_header))

    if not reconstructed:
        return RepairAttempt(
            "local_header_reconstruct", False,
            detail="no entries had a corrupted-but-reconstructable local header"
        )

    # Rebuild: every entry's bytes carried over verbatim EXCEPT the
    # reconstructed headers replace the corrupted ones at the same
    # position — filename, extra field, and ALL compressed data for
    # every entry (including the ones being fixed) are completely
    # untouched. The central directory and EOCD are also untouched,
    # since they were already correct and consistent.
    fix_map = {id(rec): new_header for rec, new_header in reconstructed}
    out = bytearray(data)
    for rec, new_header in reconstructed:
        pos = rec.local_offset
        out[pos:pos + 30] = new_header

    candidate_zip = bytes(out)
    if crc32_of(candidate_zip) == target_crc:
        fixed_names = ', '.join(rec.name for rec, _ in reconstructed)
        return RepairAttempt(
            "local_header_reconstruct", True, candidate_zip,
            f"reconstructed corrupted local header(s) for ({fixed_names}) from the "
            f"verified central directory record — filename, extra field, and all "
            f"compressed data for every entry left completely untouched"
        )

    return RepairAttempt(
        "local_header_reconstruct", False,
        detail=f"reconstructed {len(reconstructed)} corrupted local header(s) "
               f"({', '.join(rec.name for rec, _ in reconstructed)}) — still no match "
               f"(something else differs too)"
    )


def technique_sidecar_content_substitute(data: bytes, target_crc: str,
                                           replacement_lookup: dict) -> RepairAttempt:
    """
    Fixes the case where one entry inside the zip (typically a .nfo or
    .diz sidecar) contains genuinely WRONG content — not corrupted, not
    misordered, just the wrong file entirely, e.g. a different
    release's file_id.diz ending up bundled into this zip by mistake
    during scene packaging or a later re-archiving step. Unlike every
    other technique here, this doesn't try to derive the correct bytes
    from anything already present in the broken zip — there's nothing
    to derive, since the correct content simply isn't anywhere in this
    file. Instead, it substitutes in known-correct replacement content
    supplied by the caller (replacement_lookup), which is expected to
    have already been verified against the DAT's own hash entry for
    that exact filename — e.g. a loose .nfo/.diz sitting in the scan
    folder whose CRC/MD5/SHA1 matches what the DAT lists for it.

    replacement_lookup: dict of lowercased entry name -> raw replacement
    bytes (the verified-correct PLAINTEXT content, not compressed).

    For every entry whose CRC doesn't match the CRC the verified
    replacement content would produce AND whose lowercased name is in
    replacement_lookup, this:

    1. Determines this zip's actual compression fingerprint by testing
       every OTHER deflate-compressed entry's known plaintext (decompressed
       from its own verified-correct compressed bytes) against every
       zlib level/strategy combination — i.e. works out exactly which
       settings THIS zip was built with, using entries we already know
       are correct as the reference.
    2. Recompresses the replacement content using that exact fingerprint.
    3. Rebuilds the zip with the replacement entry's compressed bytes
       and corrected CRC/sizes substituted in (filename, extra field,
       and the local headers/compressed data of every OTHER entry are
       carried over completely untouched, true verbatim style).
    4. Only returns success if the rebuilt zip's CRC32 matches target_crc
       exactly — if the fingerprint can't be determined, or the
       resulting CRC still doesn't match, nothing is changed and this
       reports failure rather than guessing.
    """
    try:
        records = parse_central_directory(data)
    except Exception as e:
        return RepairAttempt("sidecar_content_substitute", False, detail=f"parse error: {e}")

    if not records or not replacement_lookup:
        return RepairAttempt("sidecar_content_substitute", False, detail="nothing to substitute")

    # Find entries whose CRC doesn't match what the verified replacement
    # content would produce — this catches "wrong but otherwise valid"
    # entries (a different release's file correctly compressed and
    # internally self-consistent, just not the right content), which
    # an internal-consistency check alone would never flag, since
    # there's nothing actually corrupted about them.
    #
    # Also catches entries whose local header/data is itself unreadable
    # (e.g. a scrambled local header signature, or corruption bleeding
    # a few bytes into the start of the compressed stream) — if a
    # verified replacement is available for that exact filename, it's
    # still a valid substitution candidate; failing to even read it is
    # if anything a STRONGER signal something is wrong with it, not a
    # reason to silently skip considering it.
    wrong_entries = []
    correct_entries = []  # (plaintext, comp_data, method) for fingerprinting
    for rec in records:
        try:
            comp_data = _read_local_comp_data(data, rec)
            if rec.compress_method == 8:
                plaintext = zlib.decompress(comp_data, -15)
            elif rec.compress_method == 0:
                plaintext = comp_data
            else:
                continue  # unsupported method — can't verify or use for fingerprinting
        except Exception:
            # Couldn't even read this entry's local header/data — if a
            # verified replacement exists for this exact name, it's
            # still worth trying as a substitution candidate, since
            # being unreadable is consistent with (and arguably
            # stronger evidence for) something being wrong with it
            if rec.name.lower() in replacement_lookup:
                wrong_entries.append(rec)
            continue

        replacement = replacement_lookup.get(rec.name.lower())
        if replacement is not None:
            replacement_crc = zlib.crc32(replacement) & 0xFFFFFFFF
            if rec.crc32 != replacement_crc:
                wrong_entries.append(rec)
                continue  # don't also treat this as a "correct" fingerprinting source

        # Entries not flagged as wrong are assumed correct as-is and
        # usable for determining this zip's compression fingerprint —
        # this includes entries with no replacement available at all
        # (nothing to compare against) and ones that already match
        actual_crc = zlib.crc32(plaintext) & 0xFFFFFFFF
        if actual_crc == rec.crc32:
            correct_entries.append((plaintext, comp_data, rec.compress_method))

    if not wrong_entries:
        return RepairAttempt(
            "sidecar_content_substitute", False,
            detail="no entries found with both a CRC mismatch and an available verified replacement"
        )

    if not correct_entries:
        return RepairAttempt(
            "sidecar_content_substitute", False,
            detail="no other correct entries available to determine this zip's compression fingerprint"
        )

    # Determine the fingerprint by trying every correct, deflate-
    # compressed entry until one actually yields a match — NOT just the
    # largest, since a large binary entry (e.g. a ROM) may have been
    # compressed by a tool/settings combination plain zlib can't
    # reproduce even when a small text entry compressed by the SAME
    # zip tool reproduces perfectly fine (compressors can behave very
    # differently depending on input characteristics. Smaller text-like
    # entries are tried first since they're cheaper to test and, in
    # practice, are exactly the kind of entry (.nfo/.diz) this
    # technique exists to fix in the first place.
    deflate_correct = [c for c in correct_entries if c[2] == 8]
    deflate_correct.sort(key=lambda c: len(c[0]))
    fingerprint = None
    for plaintext, comp_data, _ in deflate_correct:
        candidates = _detect_compression_fingerprint(plaintext, comp_data, 8)
        if candidates:
            fingerprint = candidates[0]
            break

    replacements = {}  # local_offset -> (new_comp_data, new_crc, new_uncomp_size, rec)
    for rec in wrong_entries:
        new_plain = replacement_lookup[rec.name.lower()]
        new_crc = zlib.crc32(new_plain) & 0xFFFFFFFF
        if rec.compress_method == 0:
            new_comp = new_plain
        elif rec.compress_method == 8 and fingerprint:
            level, strat = fingerprint
            if strat is not None:
                co = zlib.compressobj(level, zlib.DEFLATED, -15, 8, strat)
            else:
                co = zlib.compressobj(level, zlib.DEFLATED, -15)
            new_comp = co.compress(new_plain) + co.flush()
        else:
            continue  # can't determine how to compress this — skip, don't guess
        replacements[rec.local_offset] = (new_comp, new_crc, len(new_plain), rec)

    if not replacements:
        return RepairAttempt(
            "sidecar_content_substitute", False,
            detail="could not determine compression settings to rebuild the replacement content"
        )

    eocd_idx = find_eocd(data)
    if eocd_idx is None:
        return RepairAttempt("sidecar_content_substitute", False, detail="no EOCD found")

    def _rebuild_with_timestamps(ts_overrides: dict) -> bytes:
        """
        Rebuild the zip with the substituted entries using the given
        per-entry (dos_time, dos_date) override (falling back to the
        WRONG entry's own existing timestamp if not overridden for a
        given local_offset) — every other entry carried over verbatim,
        completely unaffected either way.
        """
        local_parts = []
        cd_parts = []
        offset = 0
        new_offsets = {}

        for rec in records:
            if rec.local_offset in replacements:
                new_comp, new_crc, new_uncomp, _ = replacements[rec.local_offset]
                dos_time, dos_date = ts_overrides.get(rec.local_offset, (rec.dos_time, rec.dos_date))
                name_bytes = rec.name.encode('utf-8', errors='surrogateescape')
                extra_len = len(rec.extra)
                new_header = (
                    LOCAL_SIG
                    + struct.pack('<H', rec.version_needed)
                    + struct.pack('<H', rec.flag_bits & ~0x08)  # never needs a descriptor — sizes known upfront
                    + struct.pack('<H', rec.compress_method)
                    + struct.pack('<H', dos_time)
                    + struct.pack('<H', dos_date)
                    + struct.pack('<I', new_crc)
                    + struct.pack('<I', len(new_comp))
                    + struct.pack('<I', new_uncomp)
                    + struct.pack('<H', len(name_bytes))
                    + struct.pack('<H', extra_len)
                )
                entry_bytes = new_header + name_bytes + rec.extra + new_comp
            else:
                entry_bytes = _extract_local_entry_verbatim(data, rec)
            new_offsets[rec.local_offset] = offset
            local_parts.append(entry_bytes)
            offset += len(entry_bytes)

        for rec in records:
            if rec.local_offset in replacements:
                new_comp, new_crc, new_uncomp, _ = replacements[rec.local_offset]
                dos_time, dos_date = ts_overrides.get(rec.local_offset, (rec.dos_time, rec.dos_date))
                new_flag = rec.flag_bits & ~0x08
                # CD record layout: sig(0-4) ver_made_by(4-6) ver_needed(6-8)
                # flag(8-10) method(10-12) dos_time(12-14) dos_date(14-16)
                # crc(16-20) comp_size(20-24) uncomp_size(24-28) name_len(28-30)
                # extra_len(30-32) comment_len(32-34) disk(34-36) internal(36-38)
                # external(38-42) local_offset(42-46)
                cd_record = (
                    rec.raw[:8] + struct.pack('<H', new_flag)
                    + struct.pack('<H', rec.compress_method)
                    + struct.pack('<H', dos_time) + struct.pack('<H', dos_date)
                    + struct.pack('<III', new_crc, len(new_comp), new_uncomp)
                    + rec.raw[28:42] + struct.pack('<I', new_offsets[rec.local_offset]) + rec.raw[46:]
                )
            else:
                cd_record = rec.raw[:42] + struct.pack('<I', new_offsets[rec.local_offset]) + rec.raw[46:]
            cd_parts.append(cd_record)

        local_blob = b''.join(local_parts)
        cd_blob = b''.join(cd_parts)
        eocd = build_eocd(len(records), len(cd_blob), len(local_blob))
        return local_blob + cd_blob + eocd

    names = ', '.join(rec.name for rec in wrong_entries if rec.local_offset in replacements)
    wrong_offsets = list(replacements.keys())

    # TIER 1 (cheapest): keep each wrong entry's OWN existing timestamp
    # unchanged — covers the case where the substitution doesn't need a
    # timestamp fix at all (e.g. the wrong content happened to be
    # packed at the same moment, or the DAT/zip tool doesn't encode a
    # meaningfully different one)
    candidate_zip = _rebuild_with_timestamps({})
    if crc32_of(candidate_zip) == target_crc:
        return RepairAttempt(
            "sidecar_content_substitute", True, candidate_zip,
            f"substituted verified-correct content for ({names}) using its own existing "
            f"timestamp — this entry's content was simply wrong (a different release's "
            f"file mixed in), not corrupted; every other entry's compressed bytes carried "
            f"over byte-for-byte, unchanged"
        )

    # TIER 2: try every OTHER entry's timestamp from the SAME zip —
    # scene packers commonly stamp every entry in one release within
    # the same packing session, so another entry's timestamp is a
    # strong, cheap-to-try candidate before resorting to a broader search
    other_timestamps = sorted(set(
        (r.dos_time, r.dos_date) for r in records if r.local_offset not in wrong_offsets
    ))
    for dos_time, dos_date in other_timestamps:
        ts_overrides = {off: (dos_time, dos_date) for off in wrong_offsets}
        candidate_zip = _rebuild_with_timestamps(ts_overrides)
        if crc32_of(candidate_zip) == target_crc:
            return RepairAttempt(
                "sidecar_content_substitute", True, candidate_zip,
                f"substituted verified-correct content for ({names}), reusing another "
                f"entry's timestamp from the same zip — this entry's content was simply "
                f"wrong (a different release's file mixed in), not corrupted; every other "
                f"entry's compressed bytes carried over byte-for-byte, unchanged"
            )

    # TIER 3: bounded search across a plausible time window — only
    # reached if neither cheap tier worked. Rather than a blind nested
    # loop (which can bury the most plausible candidates deep in the
    # search order behind many implausible ones), candidates are
    # generated in priority order: same-date entries' times first (an
    # entry added on the same day as another in this release is far
    # more likely to share a close time), small time deltas before
    # large ones, and date_delta=0 before drifting into adjacent days.
    # This makes the cap meaningful — the search stops at a sensible
    # point having already tried the most plausible candidates, rather
    # than exhausting an arbitrary slice of a much larger space that
    # happens to come first in nested-loop order.
    wrong_date = wrong_entries[0].dos_date
    wrong_time = wrong_entries[0].dos_time
    same_date_times = sorted({dt for dt, dd in other_timestamps if dd == wrong_date})
    other_date_times = sorted({dt for dt, dd in other_timestamps if dd != wrong_date})
    base_times_priority = same_date_times + other_date_times + [wrong_time]
    base_dates_priority = [wrong_date] + sorted({dd for _, dd in other_timestamps if dd != wrong_date})

    TIME_WINDOW = 1200  # ~±1200 DOS time-units (~40 minutes either side)
    MAX_TIER3_ATTEMPTS = 20000
    tried = 0
    seen_combos = set()

    def _gen_candidates():
        # date_delta=0 (this release's actual known dates) exhausted
        # FIRST across all base times, before drifting to adjacent days
        for date_delta in (0, 1, -1, 2, -2):
            for base_date in base_dates_priority:
                dos_date = base_date + date_delta
                if not (0 <= dos_date <= 0xFFFF):
                    continue
                for base_time in base_times_priority:
                    # Small deltas first: 0, +1, -1, +2, -2, ...
                    for time_delta in range(0, TIME_WINDOW + 1):
                        for sign in ((1,) if time_delta == 0 else (1, -1)):
                            dos_time = base_time + sign * time_delta
                            if not (0 <= dos_time <= 0xFFFF):
                                continue
                            yield dos_time, dos_date

    for dos_time, dos_date in _gen_candidates():
        if tried >= MAX_TIER3_ATTEMPTS:
            break
        combo = (dos_time, dos_date)
        if combo in seen_combos:
            continue
        seen_combos.add(combo)
        tried += 1
        ts_overrides = {off: (dos_time, dos_date) for off in wrong_offsets}
        candidate_zip = _rebuild_with_timestamps(ts_overrides)
        if crc32_of(candidate_zip) == target_crc:
            return RepairAttempt(
                "sidecar_content_substitute", True, candidate_zip,
                f"substituted verified-correct content for ({names}), found via "
                f"bounded timestamp search ({tried} attempt(s)) — this entry's "
                f"content was simply wrong (a different release's file mixed "
                f"in), not corrupted; every other entry's compressed bytes "
                f"carried over byte-for-byte, unchanged"
            )

    return RepairAttempt(
        "sidecar_content_substitute", False,
        detail=f"substituted content for ({names}) using this zip's detected compression "
               f"fingerprint, tried own/shared/bounded-search timestamps ({tried} extra "
               f"attempt(s)) — still no match (fingerprint or timestamp assumptions may be "
               f"wrong, or something else differs too)"
    )


_SIDECAR_ONLY_EXTENSIONS = {
    '.nfo', '.diz', '.txt', '.md',
    '.pdf', '.jpg', '.jpeg', '.png', '.gif', '.bmp',
}


_SIDECAR_RENAME_PATTERNS = [
    '{stem}_{n}{ext}',
    '{stem} ({n}){ext}',
    '{stem}-{n}{ext}',
    '{stem}({n}){ext}',
]


def _candidate_loose_sidecar_paths(loose_sidecar_index: dict, expected_name: str,
                                     near_folder: Path = None, max_n: int = 10) -> list:
    """
    Finds candidate loose-file paths for a sidecar expected to be named
    `expected_name`, searching across the WHOLE pre-built recursive
    index (loose_sidecar_index: lowercased stem -> list[Path], built
    once per run over the entire scan folder tree — see _run) rather
    than being limited to the zip's own immediate folder. This matters
    because loose .nfo/.diz files commonly end up scattered across
    subfolders relative to the zips they belong with, or accumulate
    duplicate-renamed copies in a shared main folder when many releases
    ship a same-named file (e.g. every release's NFO being called
    "mirage.nfo").

    Widens the match beyond the exact stem to also cover common
    duplicate-renaming patterns (e.g. looking for "file_id.diz" also
    matches "file_id_1.diz", "file_id (1).diz", "file_id-1.diz", for
    several different numbers) — purely to decide which files are
    WORTH checking; this never substitutes a name-based guess without
    independently verifying the content against the DAT's own hash
    afterward (see the caller).

    Returns a list of Path objects, ordered with exact-stem matches in
    the SAME folder as the zip first (most likely to be the right one
    when multiple matches exist across the tree), then exact-stem
    matches elsewhere, then rename-pattern matches near the zip, then
    rename-pattern matches elsewhere — so a correct nearby match is
    always tried before a same-named but more distant one.
    """
    p = Path(expected_name)
    stem, ext = p.stem.lower(), p.suffix

    candidate_stems = [stem] + [
        pattern.format(stem=stem, n=n, ext='')
        for n in range(1, max_n + 1) for pattern in _SIDECAR_RENAME_PATTERNS
    ]

    near_first = []
    rest = []
    seen = set()
    for cstem in candidate_stems:
        for path in loose_sidecar_index.get(cstem, []):
            if path in seen:
                continue
            seen.add(path)
            if near_folder is not None and path.parent == near_folder:
                near_first.append(path)
            else:
                rest.append(path)
    return near_first + rest


def detect_leftovers_only(data: bytes, dat_size: int) -> dict:
    """
    Detects a zip that ISN'T corrupted or wrong at all — it genuinely
    only contains sidecar/documentation-type files (.nfo, .diz, .pdf,
    .jpg, etc.) with the actual release content (ROM, executable,
    whatever the real payload is) already missing entirely. This
    happens when someone has previously stripped a release down on
    purpose (e.g. extracting and discarding the ROM, keeping only the
    NFO as a record) — there's nothing here to repair, since the
    content that would need restoring isn't present anywhere in the
    file, and running the full technique stack against it would only
    waste time without any possibility of success.

    PRIMARY SIGNAL (sufficient on its own): every single entry's
    extension is in the sidecar-only set. A genuine release always has
    at least one entry that ISN'T a sidecar type (the ROM/EXE/whatever
    the platform's actual content format is) — if literally everything
    inside is sidecar-typed, with no exceptions, that alone reliably
    means the main content is missing. This does NOT also require the
    zip to be small — a leftovers-only bundle can still be sizeable if
    it has several .nfo/.diz/.pdf/image files bundled together (e.g. 4
    or 5 sidecar files easily add up to more than half of what a small
    original release would have totalled), so gating on size as well
    would let genuine leftovers slip through undetected for exactly
    that reason.

    SECONDARY CHECK (informational only, included in the log line when
    available, never required): if the DAT lists an expected size for
    this release, it's reported alongside the detection for extra
    context — but a missing or inconclusive DAT size never prevents
    detection from firing when the extension signal alone is clear.

    Returns a dict with 'is_leftovers_only' (bool) and, if true,
    'entries' (list of entry names found) and 'detail' (human-readable
    explanation) for logging/reporting. Returns
    {'is_leftovers_only': False} on any parse error or if the extension
    signal doesn't hold — never raises.
    """
    try:
        records = parse_central_directory(data)
    except Exception:
        return {'is_leftovers_only': False}

    if not records:
        return {'is_leftovers_only': False}

    all_sidecar_type = all(
        Path(r.name).suffix.lower() in _SIDECAR_ONLY_EXTENSIONS for r in records
    )
    if not all_sidecar_type:
        return {'is_leftovers_only': False}

    try:
        dat_size = int(dat_size) if dat_size is not None else 0
    except (TypeError, ValueError):
        dat_size = 0

    actual_size = len(data)
    entry_names = [r.name for r in records]

    size_note = ""
    if dat_size > 0:
        size_note = (
            f", and is {'far ' if actual_size < dat_size * 0.5 else ''}smaller "
            f"({actual_size:,} bytes) than the DAT's expected size ({dat_size:,} bytes) "
            f"for the complete release"
        )

    return {
        'is_leftovers_only': True,
        'entries': entry_names,
        'detail': (
            f"contains only sidecar-type file(s) ({', '.join(entry_names)}) with no "
            f"main content file present{size_note} — this looks like a zip that's "
            f"already had its main content removed/stripped, not a corrupted or wrong "
            f"file. Skipping repair attempts since there's "
            f"nothing here that could reconstruct the missing content."
        )
    }


def technique_foreign_packer_diagnosis(data: bytes, target_crc: str) -> RepairAttempt:
    """
    Final diagnostic pass — does NOT attempt a fix. Checks whether the
    ZIP's internal structure is fully self-consistent (every entry's
    stored CRC32 matches its actual decompressed content, no structural
    anomalies) despite the container CRC32 not matching the DAT target.

    If everything inside checks out cleanly, the most likely explanation
    is that the original release was packed with a different ZIP tool
    (old PKZIP/Info-ZIP/WinZip etc.) whose exact compressed-byte output
    for DEFLATE cannot be reproduced by Python's zlib, even though the
    decompressed content is correct. This is reported as a diagnosis
    rather than a fix, so it can be distinguished from genuinely
    unrecoverable / wrong-content files.
    """
    try:
        records = parse_central_directory(data)
    except Exception as e:
        return RepairAttempt(
            "foreign_packer_diagnosis", False,
            detail=f"could not parse central directory: {e}"
        )

    if not records:
        return RepairAttempt(
            "foreign_packer_diagnosis", False,
            detail="no entries found — cannot diagnose"
        )

    try:
        import io
        bad_entries = []
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for rec in records:
                try:
                    actual = zf.read(rec.name)
                except Exception as e:
                    bad_entries.append(f"{rec.name} (unreadable: {e})")
                    continue
                actual_crc = zlib.crc32(actual) & 0xFFFFFFFF
                if actual_crc != rec.crc32:
                    bad_entries.append(
                        f"{rec.name} (stored CRC {rec.crc32:08x} != "
                        f"actual content CRC {actual_crc:08x})"
                    )
    except Exception as e:
        return RepairAttempt(
            "foreign_packer_diagnosis", False,
            detail=f"could not open/read entries: {e}"
        )

    if bad_entries:
        # Genuine content problem inside the zip itself — not just a
        # repacking/tool-fingerprint issue
        return RepairAttempt(
            "foreign_packer_diagnosis", False,
            detail=f"{len(bad_entries)} entr(y/ies) failed internal CRC "
                   f"check: {'; '.join(bad_entries[:3])}"
                   f"{' ...' if len(bad_entries) > 3 else ''}",
            diagnosis="content integrity issue — at least one file inside "
                      "the zip does not match its own stored CRC32, "
                      "independent of the container hash"
        )

    # Every entry's content is internally consistent. Container CRC still
    # doesn't match the DAT target — almost certainly a different
    # original packer/tool producing different compressed bytes for the
    # same (correct) content.
    n_entries = len(records)
    names_preview = ', '.join(r.name for r in records[:5])
    return RepairAttempt(
        "foreign_packer_diagnosis", False,
        detail=f"all {n_entries} entr{'y' if n_entries == 1 else 'ies'} "
               f"internally consistent ({names_preview}"
               f"{', ...' if n_entries > 5 else ''}) — content is very "
               f"likely correct",
        diagnosis="LIKELY FOREIGN PACKER: zip structure and all internal "
                  "file CRCs check out cleanly, but the container's "
                  "compressed bytes don't match any rebuild Python can "
                  "produce. This usually means the original release was "
                  "packed with a different zip tool (PKZIP/Info-ZIP/etc.) "
                  "whose exact DEFLATE output cannot be reproduced — the "
                  "content itself is probably correct, but byte-perfect "
                  "container reconstruction isn't possible with current "
                  "techniques."
    )


def repair_loose_text_file(data: bytes, target_crc: str) -> RepairAttempt:
    """
    Repair a loose (not-in-ZIP) .nfo/.diz text file by trying line-ending
    normalisation against a target CRC32. Used for sidecar files sitting
    next to a scene .zip on disk, separate from the in-ZIP technique.
    """
    if crc32_of(data) == target_crc:
        return RepairAttempt("already_match", True, data, "already matches")

    for mode in ('crlf', 'lf'):
        if mode == 'crlf':
            candidate = data.replace(b'\r\n', b'\n').replace(b'\n', b'\r\n')
        else:
            candidate = data.replace(b'\r\n', b'\n')
        if crc32_of(candidate) == target_crc:
            return RepairAttempt(
                "line_ending_fix", True, candidate,
                f"normalised to {mode.upper()} line endings"
            )

    return RepairAttempt("line_ending_fix", False, detail="tried CRLF/LF, no match")


TECHNIQUES = [
    technique_surgical_delete,
    technique_data_descriptor_strip,
    technique_local_header_reconstruct,
    technique_eocd_comment_strip,
    technique_grow_append_truncate,
    technique_line_ending_fix,
    technique_junk_removal,
    technique_compression_variants,
    technique_faithful_rebuild,
    technique_heuristic_junk_removal,
    technique_strip_to_essentials,
    technique_foreign_packer_diagnosis,
]


# ═══════════════════════════════════════════════════════════════════════
# DAT INDEX
# ═══════════════════════════════════════════════════════════════════════

# Matches common TOSEC/scene dated-prefix conventions at the start of a
# release/game name, e.g.:
#   2002-10-25-Barbies_Secret_Agent_EURO_GBA-LIGHTFORCE
#   20021025-Some_Release-GROUP
#   2002.10.25-Some_Release-GROUP
_DATE_PREFIX_RE = re.compile(
    r'^(?:\d{4}-\d{2}-\d{2}|\d{8}|\d{4}\.\d{2}\.\d{2})[-_.]'
)


def strip_date_prefix(name: str) -> str:
    """Strip a leading YYYY-MM-DD- / YYYYMMDD- / YYYY.MM.DD- style date
    prefix from a release name, if present. Returns the name unchanged
    if no such prefix is found."""
    return _DATE_PREFIX_RE.sub('', name)


def extract_release_group(game_name: str) -> str:
    """
    Extract the scene release-group tag from a DAT release name, e.g.
    "Mission_Cleanup_SMD-bADkARMA" -> "bADkARMA"
    "2002-10-25-Barbies_Secret_Agent_EURO_GBA-LIGHTFORCE" -> "LIGHTFORCE"
    Convention: the group tag is whatever follows the LAST hyphen in the
    release name (date prefixes use hyphens too, so strip those first).
    Comparison/storage is lowercased so group matching is case-insensitive
    (per-user note: same group, just upper/lower case drift in DAT naming).
    Falls back to the full name if no hyphen is present.
    """
    stripped = strip_date_prefix(game_name)
    if '-' not in stripped:
        return stripped.lower()
    return stripped.rsplit('-', 1)[-1].strip().lower()


def build_dat_index(dat_folder: Path) -> dict:
    """
    Parse every DAT in dat_folder and build an index:
      { filename_lower: [ {crc, md5, sha1, size, source_dat, game_name}, ... ] }
    Multiple candidates per filename are kept (for ambiguous/try-all cases).

    Indexes by THREE keys per ROM entry, since scene ZIPs on disk don't
    always match the DAT's literal name:
      1. The literal ROM filename (rom name="...")
      2. The release/game name (game name="...") — covers ZIPs renamed on
         disk to the release name instead of the internal ROM name, e.g.
         "bk-miscu.zip" found on disk as "Mission_Cleanup_SMD-bADkARMA.zip"
      3. The release/game name with a leading date prefix stripped, e.g.
         "2002-10-25-Barbies_Secret_Agent_EURO_GBA-LIGHTFORCE" also
         indexed as "Barbies_Secret_Agent_EURO_GBA-LIGHTFORCE" — covers
         TOSEC-dated DAT entries where the on-disk file has no date prefix.
    """
    index = {}
    if parse_dat_file is None:
        return index

    def _add(key_name: str, candidate: dict):
        if not key_name:
            return
        key = key_name.strip().lower()
        if not key.endswith('.zip'):
            key = key + '.zip'
        # Avoid storing the exact same candidate twice under the same key
        bucket = index.setdefault(key, [])
        if candidate not in bucket:
            bucket.append(candidate)

    dat_files = list(dat_folder.rglob('*.dat')) + list(dat_folder.rglob('*.xml'))
    for dat_path in dat_files:
        try:
            header, entries = parse_dat_file(dat_path)
        except Exception:
            continue
        for entry in entries:
            for rom in entry.roms:
                rname = (rom.get('name') or '').strip()
                if not rname.lower().endswith('.zip'):
                    continue
                candidate = {
                    'crc': (rom.get('crc') or '').lower(),
                    'md5': (rom.get('md5') or '').lower(),
                    'sha1': (rom.get('sha1') or '').lower(),
                    'size': rom.get('size'),
                    'source_dat': dat_path.name,
                    'game_name': entry.name,
                    'rom_name': rname,
                    # Full list of ROM entries from the same <game> block —
                    # used to look up sidecar .nfo/.diz hash entries that
                    # belong alongside this ZIP (loose files next to it on
                    # disk, not inside the ZIP).
                    'sibling_roms': entry.roms,
                }
                # Index 1: literal ROM filename as listed in the DAT
                _add(rname, candidate)
                # Index 2: the release/game name — covers ZIPs renamed on
                # disk to the release name instead of the internal ROM name
                _add(entry.name, candidate)
                # Index 3: release name with leading date prefix stripped —
                # covers dated TOSEC-style DAT entries vs undated on-disk files
                stripped = strip_date_prefix(entry.name)
                if stripped != entry.name:
                    _add(stripped, candidate)
    return index


# ═══════════════════════════════════════════════════════════════════════
# REFERENCE-FOLDER FINGERPRINTING
# ═══════════════════════════════════════════════════════════════════════
#
# If the user has a folder of known-good (DAT-verified) scene zips,
# analyse them per release-group to learn the actual packer settings
# used (compression level/strategy, entry order convention, header
# metadata) instead of blindly searching the whole combinatorial space
# for every broken file. Reference files are NEVER opened in place —
# they're copied to a temp working dir first, since "never altered" is
# a hard requirement.

@dataclass
class PackerFingerprint:
    """A learned set of packer characteristics for a release group,
    derived from one verified-good reference zip."""
    group: str
    source_file: str          # which reference zip this came from
    order_kind: str           # 'original' | 'alphabetical' | 'reverse_alphabetical' | 'size_ascending' | 'unknown'
    compress_method: int      # 0 = store, 8 = deflate
    zlib_level: int           # best-guess level (1/6/9), only meaningful if compress_method == 8
    zlib_strategy: int        # None or one of zlib.Z_*
    version_made_by: int
    version_needed: int
    flag_bits: int
    internal_attr: int = 0
    external_attr: int = 0
    # The exact set of entry names present in this verified reference —
    # used to detect junk by direct comparison: any entry in a broken
    # zip from the same group that ISN'T in this set is almost certainly
    # an injected file (advert/courier/etc), since the reference is
    # known-good per the DAT. Far more reliable than name-pattern
    # guessing when a reference happens to be available.
    known_entry_names: frozenset = field(default_factory=frozenset)

    def as_header_overrides(self) -> dict:
        """Return this fingerprint's header fields as an overrides dict
        for _rebuild_zip_faithful — using the REFERENCE's known-correct
        metadata instead of trusting whatever's in the broken file being
        repaired (which may itself have had these fields altered by
        whatever process broke it).

        internal_attr is deliberately NOT included here: it's a per-
        entry content-dependent flag (bit 0 = "this file is ASCII/text"),
        not a group-wide packer setting — a binary ROM and a text .nfo
        in the SAME zip from the SAME group legitimately have different
        internal_attr values. Forcing one fixed value across every entry
        would be wrong; _rebuild_zip_faithful derives it per-entry from
        each entry's own content instead (see _looks_like_text_content).
        """
        return {
            'version_made_by': self.version_made_by,
            'version_needed': self.version_needed,
            'flag_bits': self.flag_bits,
            'external_attr': self.external_attr,
        }

    def to_dict(self) -> dict:
        """Serialize for storage in the reference fingerprint database
        (see reference_db module functions). frozenset isn't natively
        JSON-serializable, so known_entry_names becomes a sorted list."""
        return {
            'group': self.group,
            'source_file': self.source_file,
            'order_kind': self.order_kind,
            'compress_method': self.compress_method,
            'zlib_level': self.zlib_level,
            'zlib_strategy': self.zlib_strategy,
            'version_made_by': self.version_made_by,
            'version_needed': self.version_needed,
            'flag_bits': self.flag_bits,
            'internal_attr': self.internal_attr,
            'external_attr': self.external_attr,
            'known_entry_names': sorted(self.known_entry_names),
        }

    @classmethod
    def from_dict(cls, d: dict) -> 'PackerFingerprint':
        """Reconstruct from a dict produced by to_dict()."""
        return cls(
            group=d['group'],
            source_file=d['source_file'],
            order_kind=d['order_kind'],
            compress_method=d['compress_method'],
            zlib_level=d['zlib_level'],
            zlib_strategy=d['zlib_strategy'],
            version_made_by=d['version_made_by'],
            version_needed=d['version_needed'],
            flag_bits=d['flag_bits'],
            internal_attr=d.get('internal_attr', 0),
            external_attr=d.get('external_attr', 0),
            known_entry_names=frozenset(d.get('known_entry_names', [])),
        )


def _entry_role(name: str) -> str:
    """Classify an entry by its likely ROLE in a release (nfo / diz /
    other-content), independent of the exact filename — since the ROM
    filename differs release to release but the role-based ordering
    convention (e.g. 'nfo always first, diz second, content last') is
    often consistent across an entire release group."""
    lower = name.lower().rsplit('/', 1)[-1]
    if lower == 'file_id.diz':
        return 'diz'
    if lower.endswith('.nfo'):
        return 'nfo'
    return 'content'


def _detect_order_kind(records: list) -> str:
    """Classify the entry order of a parsed central directory against
    the common conventions we already search blindly elsewhere.

    'role_based' is checked FIRST and preferred over the literal
    alphabetical/size orderings where it also happens to apply, because
    it's the most TRANSFERABLE convention across different releases
    from the same group: the actual ROM filename varies release to
    release, but role order (e.g. nfo, then diz, then content) is
    typically dictated by the packer/FTP script and stays constant.
    A literal 'original'/alphabetical/size order detected from one
    specific reference file's filenames doesn't generalise to a
    different release with different filenames, but role order does.
    """
    roles = [_entry_role(r.name) for r in records]
    # Recognise it as role-based only if there's more than one distinct
    # role present (otherwise "role order" is indistinguishable from
    # just "original order" and provides no extra transferable info)
    if len(set(roles)) > 1:
        return 'role:' + ','.join(roles)

    names = [r.name for r in records]
    if names == sorted(names, key=str.lower):
        return 'alphabetical'
    if names == sorted(names, key=str.lower, reverse=True):
        return 'reverse_alphabetical'
    sizes = [r.uncomp_size for r in records]
    if sizes == sorted(sizes):
        return 'size_ascending'
    return 'original'  # treat whatever order is present as the "original" convention


def _order_indices_for_kind(records: list, order_kind: str) -> list:
    """
    Given a list of records and an order_kind (as produced by
    _detect_order_kind), return the list of indices into `records` that
    achieves that ordering. Handles the 'role:...' family generically —
    role order transfers correctly across different releases from the
    same group even though the exact filenames differ, unlike a literal
    name-based 'original' order which only matches the specific file it
    was detected from.
    """
    n = len(records)
    if order_kind == 'alphabetical':
        return sorted(range(n), key=lambda i: records[i].name.lower())
    if order_kind == 'reverse_alphabetical':
        return sorted(range(n), key=lambda i: records[i].name.lower(), reverse=True)
    if order_kind == 'size_ascending':
        return sorted(range(n), key=lambda i: records[i].uncomp_size)
    if order_kind.startswith('role:'):
        wanted_roles = order_kind[len('role:'):].split(',')
        # Group current indices by role, preserving each role-group's
        # existing relative order for any ties (e.g. multiple 'content'
        # entries keep their existing relative sequence)
        role_buckets = {}
        for i in range(n):
            role_buckets.setdefault(_entry_role(records[i].name), []).append(i)
        result = []
        for role in wanted_roles:
            if role in role_buckets and role_buckets[role]:
                result.append(role_buckets[role].pop(0))
        # Any leftover entries (role counts didn't match exactly, e.g.
        # extra content files) get appended in their existing order
        leftovers = [i for bucket in role_buckets.values() for i in bucket]
        result.extend(sorted(leftovers))
        # Safety: if something went wrong and we don't have a full
        # permutation, fall back to original order rather than return
        # a malformed/incomplete list
        if sorted(result) != list(range(n)):
            return list(range(n))
        return result
    # 'original' or unrecognised — use whatever order is already present
    return list(range(n))


def _order_kind_display(order_kind: str, max_roles: int = 8) -> str:
    """
    Compact, human-readable form of an order_kind string for logging.
    Role-based orders (order_kind == 'role:nfo,diz,content,content,...')
    can run to hundreds of entries on large archives — logging the full
    sequence is just noise at that point, so anything beyond max_roles
    entries gets summarized as counts instead (e.g. 'role:nfo,diz,
    +312 content (314 entries total)').
    """
    if not order_kind.startswith('role:'):
        return order_kind
    roles = order_kind[len('role:'):].split(',')
    if len(roles) <= max_roles:
        return order_kind
    from collections import Counter
    counts = Counter(roles)
    content_count = counts.get('content', 0)
    other_roles_in_order = []
    seen = set()
    for role in roles:
        if role != 'content' and role not in seen:
            other_roles_in_order.append(role)
            seen.add(role)
    parts = other_roles_in_order + ([f'+{content_count} content'] if content_count else [])
    return 'role:' + ','.join(parts) + f' ({len(roles)} entries total)'


def _detect_compression_fingerprint(raw_content: bytes, comp_data: bytes,
                                      compress_method: int) -> list:
    """
    Given one entry's known plaintext and its actual compressed bytes
    from a verified-good reference zip, work out which zlib level(s) and
    strategy(ies) reproduce those exact compressed bytes. Returns a list
    of (level, strategy) tuples — often just one, but small/simple
    content can compress identically at several levels (e.g. levels 5-9
    all happen to produce the same output for some files), so ALL
    matches are returned rather than just the first found. The caller
    can disambiguate using a second entry if the result is ambiguous, or
    fall back to trying every returned candidate.

    Returns an empty list if no combination matches at all (e.g. the
    original packer wasn't zlib/Python-compatible, which itself is
    useful to know — the group simply can't be fingerprinted this way).
    """
    if compress_method == 0:
        return [(0, None)]  # STORE — no level/strategy applies

    strategies = [None, zlib.Z_FILTERED, zlib.Z_HUFFMAN_ONLY, zlib.Z_RLE, zlib.Z_FIXED]
    matches = []
    for level in range(1, 10):
        for strat in strategies:
            try:
                if strat is not None:
                    co = zlib.compressobj(level, zlib.DEFLATED, -15, 8, strat)
                else:
                    co = zlib.compressobj(level, zlib.DEFLATED, -15)
                candidate = co.compress(raw_content) + co.flush()
            except Exception:
                continue
            if candidate == comp_data:
                matches.append((level, strat))
    return matches



def _read_local_comp_data(data: bytes, rec: 'ZipCDRecord') -> bytes:
    """Extract the raw compressed bytes for one entry directly from its
    local file header (not via zipfile, since we need the exact
    compressed bytes, not the decompressed content)."""
    pos = rec.local_offset
    if data[pos:pos + 4] != LOCAL_SIG:
        raise ValueError(f"no local header at offset {pos} for {rec.name}")
    name_len = struct.unpack('<H', data[pos + 26:pos + 28])[0]
    extra_len = struct.unpack('<H', data[pos + 28:pos + 30])[0]
    data_start = pos + 30 + name_len + extra_len
    return data[data_start:data_start + rec.comp_size]


def load_fingerprint_db(db_path: Path) -> dict:
    """
    Load the reference fingerprint database from disk. Returns a dict
    of group_lower -> list[PackerFingerprint], same shape as what
    build_reference_fingerprints() produces, so it can be merged with
    or substituted for a fresh scan's results directly.

    The database stores fingerprints PER GROUP (e.g. one entry covers
    every "mirage" release), not per individual reference file — once a
    group's packer convention is known, every other reference from that
    same group is redundant to re-scan, since groups are assumed to use
    a consistent convention across their whole catalogue. Returns an
    empty dict if the file doesn't exist or can't be read/parsed (never
    raises — a missing/corrupt database just means "start fresh").
    """
    if not db_path or not db_path.is_file():
        return {}
    try:
        raw = json.loads(db_path.read_text(encoding='utf-8'))
    except Exception:
        return {}

    result = {}
    for group, fp_dicts in raw.get('groups', {}).items():
        try:
            result[group] = [PackerFingerprint.from_dict(d) for d in fp_dicts]
        except Exception:
            continue  # skip a corrupted group entry rather than failing the whole load
    return result


def save_fingerprint_db(db_path: Path, fingerprints: dict) -> bool:
    """
    Save (group_lower -> list[PackerFingerprint]) to the database file,
    merging with whatever's already there rather than overwriting other
    groups that aren't part of this run's `fingerprints` dict — so
    scanning one reference folder doesn't wipe out groups learned from
    a different folder in an earlier run.

    Returns True on success, False on failure (logged by the caller —
    this never raises, since a failed database save should never block
    or break the actual repair workflow).
    """
    try:
        existing = {}
        if db_path.is_file():
            try:
                existing = json.loads(db_path.read_text(encoding='utf-8')).get('groups', {})
            except Exception:
                existing = {}

        for group, fps in fingerprints.items():
            existing[group] = [fp.to_dict() for fp in fps]

        db_path.parent.mkdir(parents=True, exist_ok=True)
        db_path.write_text(
            json.dumps({'version': 1, 'groups': existing}, indent=2),
            encoding='utf-8'
        )
        return True
    except Exception:
        return False


def build_reference_fingerprints(ref_folder: Path, dat_index: dict,
                                   log_fn=None, known_group_fps: dict = None,
                                   excluded_refs: set = None) -> tuple:
    """
    Scan ref_folder recursively for .zip files, match them against the
    DAT index (same 3-way name matching as the main scan: literal rom
    name, release name, date-stripped release name — case-insensitive),
    verify each candidate's CRC32 (and MD5/SHA1 if the DAT provides them)
    to confirm it's genuinely a correct reference, then extract a
    PackerFingerprint per matched release group.

    If known_group_fps is given (group_lower -> list[PackerFingerprint],
    typically loaded from the on-disk database via load_fingerprint_db),
    any reference file whose EXACT filename has already contributed a
    fingerprint before skips the expensive byte-level extraction —
    its existing fingerprint(s) are reused as-is.

    excluded_refs, if given, is a set of lowercased filenames to skip
    entirely — never even hash-verified or fingerprinted. This exists
    for the rare case where a reference file genuinely matches its
    DAT-listed CRC/MD5/SHA1 (so the toolkit's automatic verification
    can't reject it) but the release itself is known to already
    contain injected junk that happens not to be heuristically
    detectable by name pattern (e.g. a randomly-named .nfo file),
    which would otherwise silently poison the whole group's known-good
    name set. Manually excluding it here removes it from consideration
    as a reference without needing it removed from the reference
    folder itself.

    Reference files are NEVER opened for writing. Each one is opened in
    strict read-only mode ('rb'), its bytes copied into a separate temp
    directory, and a tripwire check confirms the original's size and
    modification time are unchanged immediately after reading — if
    anything ever did touch the original, this is detected and that file
    is skipped with a loud error rather than silently trusted. All
    parsing/CRC-checking/fingerprinting happens only on the temp copy.
    The temp directory is deleted again once analysis finishes.

    Returns: (fingerprints_dict, n_verified, n_rejected) — single-folder
    use is normally via build_reference_fingerprints_multi() below, which
    handles combining results across several chosen folders.
    """
    def _log(msg, cls='dim'):
        if log_fn:
            log_fn(msg, cls)

    fingerprints = {}  # group_lower -> list[PackerFingerprint]

    if not ref_folder or not ref_folder.is_dir():
        return fingerprints, 0, 0

    excluded_refs = excluded_refs or set()

    ref_zips = sorted(
        p for p in ref_folder.rglob('*') if p.is_file() and p.suffix.lower() == '.zip'
    )
    _log(f'Reference folder: scanning {len(ref_zips)} .zip file(s) across all subfolders', 'dim')

    import tempfile
    tmp_root = Path(tempfile.mkdtemp(prefix='scene_recreator_refs_'))

    n_verified = 0
    n_rejected = 0

    try:
        for ref_path in ref_zips:
            if ref_path.name.lower() in excluded_refs:
                _log(f'  ⊘ Reference {ref_path.name} — manually excluded, skipping entirely', 'warn')
                continue
            key = ref_path.name.lower()
            candidates = dat_index.get(key)
            if not candidates:
                continue  # not a known DAT filename — skip silently, huge ref folders are expected

            # Record original size/mtime BEFORE touching anything, as a
            # tripwire — if these differ after we're done, something
            # touched the original and we want to know immediately.
            try:
                orig_stat = ref_path.stat()
                orig_size, orig_mtime = orig_stat.st_size, orig_stat.st_mtime
            except Exception as e:
                _log(f'  Reference {ref_path.name}: could not stat original: {e}', 'warn')
                continue

            # Read the original strictly read-only (mode 'rb') and copy
            # its bytes to a fresh file in the temp dir. We never open
            # the original in a write-capable mode, never call write/
            # unlink/rename on ref_path, and only ever pass safe_copy
            # (the temp copy) onward for inspection.
            safe_copy = tmp_root / f'{n_verified + n_rejected}_{ref_path.name}'
            try:
                with open(ref_path, 'rb') as f_src:
                    ref_data = f_src.read()
                safe_copy.write_bytes(ref_data)
            except Exception as e:
                _log(f'  Reference {ref_path.name}: could not copy for inspection: {e}', 'warn')
                continue

            # Tripwire: confirm the original is untouched after reading it
            try:
                post_stat = ref_path.stat()
                if post_stat.st_size != orig_size or post_stat.st_mtime != orig_mtime:
                    _log(
                        f'  ⚠ SAFETY CHECK FAILED for {ref_path.name}: size/mtime '
                        f'changed after read — skipping this file and treating as '
                        f'untrustworthy. This should never happen; please report it.',
                        'err'
                    )
                    continue
            except Exception:
                pass  # if we can't re-stat, don't block on it — copy already succeeded

            ref_crc = crc32_of(ref_data)

            matched_cand = None
            for cand in candidates:
                if cand['crc'] and ref_crc == cand['crc']:
                    matched_cand = cand
                    break

            if not matched_cand:
                n_rejected += 1
                continue  # filename matched but content doesn't — not a usable reference

            # Extra verification: MD5/SHA1 if the DAT provides them, to
            # guard against a CRC32 coincidence poisoning the fingerprint
            ref_md5 = md5_of(ref_data)
            ref_sha1 = sha1_of(ref_data)
            md5_ok = (not matched_cand['md5']) or (ref_md5 == matched_cand['md5'])
            sha1_ok = (not matched_cand['sha1']) or (ref_sha1 == matched_cand['sha1'])
            if not (md5_ok and sha1_ok):
                n_rejected += 1
                _log(f'  Reference {ref_path.name}: CRC matched but MD5/SHA1 '
                     f'did not — rejecting as unreliable reference', 'warn')
                continue

            group = extract_release_group(matched_cand['game_name'])

            # Skip re-scanning ONLY if THIS EXACT reference filename has
            # already contributed a fingerprint to this group before —
            # never just because the group has *some* fingerprint at
            # all. A single group name (e.g. "mirage") can legitimately
            # span many structurally different release layouts across
            # different platforms or multi-file releases; skipping by
            # group name alone meant the very first reference scanned
            # for a group silently prevented every other, structurally
            # different reference in that same group from EVER being
            # scanned in subsequent runs — including ones whose extra
            # files (e.g. a bundled README.md, or a release-specific
            # sidecar name) were never learned, breaking junk detection
            # for that release's actual layout. Keying on the source
            # filename instead means re-encountering the SAME reference
            # file still skips (the whole point of the optimisation),
            # while a never-before-seen reference always gets scanned
            # at least once, regardless of how many other fingerprints
            # already exist for the same group name.
            existing_for_group = known_group_fps.get(group) if known_group_fps else None
            already_seen_this_file = existing_for_group and any(
                fp.source_file.lower() == ref_path.name.lower() for fp in existing_for_group
            )
            if already_seen_this_file:
                if group not in fingerprints:
                    fingerprints[group] = list(existing_for_group)
                    n_verified += 1
                _log(
                    f'  ⏭ Reference {ref_path.name} — already scanned previously '
                    f'(group "{group}", {len(existing_for_group)} stored fingerprint(s) '
                    f'total for this group) — skipping re-scan of this same file', 'dim'
                )
                continue

            try:
                records = parse_central_directory(ref_data)
                if not records:
                    continue
                order_kind = _detect_order_kind(records)

                import io
                with zipfile.ZipFile(io.BytesIO(ref_data)) as zf:
                    ref_contents = {r.name: zf.read(r.name) for r in records}

                # Fingerprint using EVERY deflate-compressed entry, not
                # just the largest — small/simple content can compress
                # identically at several levels (e.g. levels 5-9 all
                # producing the same bytes), so a single entry's match
                # set can be ambiguous. Intersecting across multiple
                # entries narrows this down; if more than one entry is
                # available, only candidates consistent with ALL of them
                # survive, which is far more reliable than trusting one
                # entry's possibly-ambiguous result.
                deflate_records = [r for r in records if r.compress_method == 8]
                candidate_set = None
                compress_method_for_fp = records[0].compress_method
                if deflate_records:
                    compress_method_for_fp = 8
                    for rec in deflate_records:
                        comp_data = _read_local_comp_data(ref_data, rec)
                        matches = set(_detect_compression_fingerprint(
                            ref_contents[rec.name], comp_data, 8
                        ))
                        candidate_set = matches if candidate_set is None else (candidate_set & matches)
                        if candidate_set is not None and len(candidate_set) <= 1:
                            break  # fully disambiguated, no need to check more entries
                else:
                    candidate_set = {(0, None)}

                if not candidate_set:
                    # Entries individually matched something, but no
                    # single (level, strategy) combo satisfies ALL of
                    # them — genuinely inconsistent/foreign packer for
                    # this reference, or our entries used different
                    # settings from each other (unusual but possible)
                    _log(
                        f'  Reference {ref_path.name}: no single compression '
                        f'level/strategy reproduces every entry — skipping '
                        f'compression fingerprint for this reference (other '
                        f'metadata still used)', 'warn'
                    )
                    candidate_set = {(6, None)}  # harmless default fallback

                main_rec = records[0]
                # Use the largest record purely for header metadata
                # (version_made_by/flag_bits/attrs), which IS expected
                # to be uniform across entries in the same archive
                largest_rec = max(records, key=lambda r: r.uncomp_size)

                # Filter out junk-pattern names before they ever become
                # part of this group's "known good" name set. A
                # reference can correctly verify against the DAT (its
                # CRC/MD5/SHA1 genuinely match what the DAT lists) while
                # STILL containing entries that look like injected junk
                # by name — this happens when whoever built the DAT
                # captured a release that already had junk baked into
                # it, making that junk part of the "official" hash.
                # Treating such names as "known good" for the whole
                # group would mask the exact same junk pattern in every
                # OTHER release of this group, defeating the entire
                # point of reference-based junk detection. The genuine
                # content/order/compression metadata from this
                # reference is still fully trustworthy and used as
                # normal — only the suspicious-LOOKING filenames are
                # excluded from the known-names set.
                suspicious_names = [
                    r.name for r in records
                    if _looks_like_junk_entry(r.name, r.uncomp_size, len(records))
                ]
                clean_names = frozenset(
                    r.name.lower() for r in records if r.name not in suspicious_names
                )
                if suspicious_names:
                    _log(
                        f'  ⚠ Reference {ref_path.name}: contains junk-pattern '
                        f'entr{"y" if len(suspicious_names)==1 else "ies"} '
                        f'({", ".join(suspicious_names)}) despite verifying against the '
                        f'DAT — excluding {"it" if len(suspicious_names)==1 else "them"} from '
                        f'this group\'s known-good name set (the DAT\'s own listed hash for '
                        f'this release apparently already includes this junk)', 'warn'
                    )

                made_fps = []
                for level, strat in sorted(candidate_set, key=lambda t: (t[0] != 6, t[0])):
                    # Sort so level 6 (the overwhelmingly common real-
                    # world default) is tried first among ties, purely
                    # as a sensible tie-break ordering — every candidate
                    # is still included, none are discarded
                    fp = PackerFingerprint(
                        group=group,
                        source_file=ref_path.name,
                        order_kind=order_kind,
                        compress_method=compress_method_for_fp,
                        zlib_level=level,
                        zlib_strategy=strat,
                        version_made_by=largest_rec.version_made_by,
                        version_needed=largest_rec.version_needed,
                        flag_bits=largest_rec.flag_bits,
                        internal_attr=largest_rec.internal_attr,
                        external_attr=largest_rec.external_attr,
                        known_entry_names=clean_names,
                    )
                    fingerprints.setdefault(group, []).append(fp)
                    made_fps.append(fp)

                n_verified += 1
                level_summary = ', '.join(
                    f"level={fp.zlib_level}/{fp.zlib_strategy or 'default'}" for fp in made_fps
                )
                _log(
                    f'  ✓ Reference {ref_path.name} verified — group "{group}": '
                    f'order={_order_kind_display(order_kind)}, method={"STORE" if compress_method_for_fp==0 else "DEFLATE"}, '
                    f'{len(made_fps)} candidate setting(s) ({level_summary})', 'ok'
                )
            except Exception as e:
                _log(f'  Reference {ref_path.name}: fingerprint extraction failed: {e}', 'warn')
                continue
    finally:
        try:
            shutil.rmtree(tmp_root, ignore_errors=True)
        except Exception:
            pass

    return fingerprints, n_verified, n_rejected


def build_reference_fingerprints_multi(ref_folders: list, dat_index: dict,
                                         log_fn=None, known_group_fps: dict = None,
                                         excluded_refs: set = None) -> dict:
    """
    Run build_reference_fingerprints() across multiple reference folders
    (e.g. several chosen subfolders instead of one huge parent folder,
    for speed) and merge the results. Fingerprints for the same release
    group found in different folders are combined into one list, so a
    group's full set of observed packer conventions is available
    regardless of which folder each reference happened to live in.

    known_group_fps (group_lower -> list[PackerFingerprint]), if given,
    is consulted to skip re-scanning any group already known — typically
    loaded from the on-disk database via load_fingerprint_db(). As
    folders are processed, groups newly found in an earlier folder this
    run are folded into the skip-set too, so folder 2 doesn't re-scan a
    group already confirmed from folder 1 in this same run.

    excluded_refs (set of lowercased filenames), if given, is passed
    through to every folder scanned — a manually-flagged bad reference
    is excluded everywhere, regardless of which folder it's found in.
    """
    def _log(msg, cls='dim'):
        if log_fn:
            log_fn(msg, cls)

    combined = {}
    total_verified = 0
    total_rejected = 0
    skip_set = dict(known_group_fps) if known_group_fps else {}

    for folder_str in ref_folders:
        folder_str = (folder_str or '').strip()
        if not folder_str:
            continue
        folder = Path(folder_str).expanduser()
        if not folder.is_dir():
            _log(f'Reference folder not found, skipping: {folder}', 'warn')
            continue

        _log(f'Analysing reference folder: {folder}', 'info')
        fps, n_verified, n_rejected = build_reference_fingerprints(
            folder, dat_index, log_fn=log_fn, known_group_fps=skip_set,
            excluded_refs=excluded_refs
        )
        total_verified += n_verified
        total_rejected += n_rejected
        for group, fp_list in fps.items():
            combined.setdefault(group, []).extend(fp_list)
            # Merge into skip_set (not setdefault) — a group already
            # present from an earlier folder this run must still pick
            # up newly-discovered fingerprints from THIS folder, or a
            # later folder's references for the same group would keep
            # comparing against a stale, incomplete fingerprint list
            # and lose the benefit of every layout learned so far.
            existing = skip_set.get(group, [])
            existing_sources = {fp.source_file.lower() for fp in existing}
            merged = list(existing) + [fp for fp in fp_list if fp.source_file.lower() not in existing_sources]
            skip_set[group] = merged

    n_groups = len(combined)
    if ref_folders:
        _log(
            f'Reference analysis complete: {total_verified} verified reference(s) '
            f'across {n_groups} release group(s) from {len([f for f in ref_folders if (f or "").strip()])} '
            f'folder(s) ({total_rejected} rejected — name matched but content/hash did not)',
            'info'
        )
    return combined


def technique_fingerprint_rebuild(data: bytes, target_crc: str,
                                    fingerprints: list) -> RepairAttempt:
    """
    Fast-path rebuild using one or more PackerFingerprints learned from
    verified-good reference zips of the same release group. Tried before
    falling back to the blind faithful_rebuild search — if the group's
    packer convention is known, this needs only a handful of attempts
    instead of dozens.
    """
    if not fingerprints:
        return RepairAttempt("fingerprint_rebuild", False, detail="no reference fingerprints available for this group")

    try:
        records = parse_central_directory(data)
    except Exception as e:
        return RepairAttempt("fingerprint_rebuild", False, detail=f"parse error: {e}")

    if not records:
        return RepairAttempt("fingerprint_rebuild", False, detail="no entries to rebuild")

    try:
        import io
        contents = {}
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for rec in records:
                contents[rec.name] = zf.read(rec.name)
    except Exception as e:
        return RepairAttempt("fingerprint_rebuild", False, detail=f"could not read entries: {e}")

    n = len(records)
    tried = 0
    for fp in fingerprints:
        order = _order_indices_for_kind(records, fp.order_kind)
        for use_overrides in (True, False):
            try:
                candidate = _rebuild_zip_faithful(
                    records, contents, order,
                    compress_method=fp.compress_method,
                    zlib_level=fp.zlib_level if fp.compress_method == 8 else None,
                    zlib_strategy=fp.zlib_strategy if fp.compress_method == 8 else None,
                    header_overrides=fp.as_header_overrides() if use_overrides else None,
                )
                tried += 1
                if crc32_of(candidate) == target_crc:
                    override_note = " (using reference's header metadata)" if use_overrides else ""
                    return RepairAttempt(
                        "fingerprint_rebuild", True, candidate,
                        f"matched group '{fp.group}' fingerprint from "
                        f"{fp.source_file} (order={fp.order_kind}, "
                        f"method={'STORE' if fp.compress_method==0 else 'DEFLATE'}"
                        f"{f', level={fp.zlib_level}' if fp.compress_method==8 else ''})"
                        f"{override_note}"
                    )
            except Exception:
                continue

    return RepairAttempt(
        "fingerprint_rebuild", False,
        detail=f"tried {tried} known fingerprint(s) for this group — no match"
    )


# Filenames that are virtually always unique PER RELEASE (the actual ROM,
# the per-release nfo/diz containing that release's own description) —
# these should never be flagged as "missing from reference" junk, since
# they legitimately differ release to release even within the same group.
# Junk/scaffolding files injected by an automated FTP script, by
# contrast, tend to be IDENTICAL across every release from that group
# (same advert, same courier tag, same folder structure) — those are
# exactly what direct comparison against a reference can catch reliably.
def _is_likely_per_release_unique(name: str) -> bool:
    lower = name.lower()
    base = lower.rsplit('/', 1)[-1]
    if base == 'file_id.diz':
        return True
    if base.endswith('.nfo'):
        return True
    # Anything that looks like the actual game/rom/media content (has a
    # "real" extension and isn't a known scaffolding pattern) is treated
    # as per-release-unique too, conservatively, so we never risk
    # stripping real content based on a name not appearing in some other
    # release's reference.
    return False


def technique_reference_junk_removal(data: bytes, target_crc: str,
                                       fingerprints: list) -> RepairAttempt:
    """
    Uses verified-good reference zips from the SAME release group to
    identify junk by direct comparison rather than name-pattern
    guessing: any entry in the broken zip that is NOT one of the
    "per-release-unique" files (rom/nfo/diz) AND does not appear in any
    reference's known_entry_names is treated as a strong junk candidate
    — since the reference is independently DAT-verified as correct, any
    extra scaffolding file not present there was very likely injected
    by an automated script (advert, courier tag, topsite banner, etc.)
    rather than being legitimate release content.

    This is more reliable than technique_heuristic_junk_removal's name
    pattern matching whenever a reference happens to be available, since
    it doesn't depend on guessing what junk filenames look like — it
    just compares against what's actually been seen in verified releases
    from this exact group.
    """
    if not fingerprints:
        return RepairAttempt("reference_junk_removal", False, detail="no reference fingerprints available for this group")

    try:
        records = parse_central_directory(data)
    except Exception as e:
        return RepairAttempt("reference_junk_removal", False, detail=f"parse error: {e}")

    if len(records) < 2:
        return RepairAttempt("reference_junk_removal", False, detail="too few entries to consider removal")

    # Union of every entry name ever seen across all known-good
    # references for this group — an entry not in this set, and not
    # itself per-release-unique, is a junk candidate
    known_names = set()
    for fp in fingerprints:
        known_names |= fp.known_entry_names

    junk_candidates = [
        r for r in records
        if r.name.lower() not in known_names and not _is_likely_per_release_unique(r.name)
    ]

    if not junk_candidates:
        return RepairAttempt(
            "reference_junk_removal", False,
            detail="no entries found that are absent from this group's known-good references"
        )

    junk_names = [r.name for r in junk_candidates]

    try:
        import io
        contents = {}
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for rec in records:
                contents[rec.name] = zf.read(rec.name)
    except Exception as e:
        return RepairAttempt("reference_junk_removal", False, detail=f"could not read entries: {e}")

    # Try removing all reference-absent candidates together first (the
    # common case — one automated script injects the same set of junk
    # files every time), then fall back to subsets if that doesn't hit
    from itertools import combinations
    removal_sets = [tuple(junk_candidates)]
    if len(junk_candidates) > 1:
        for r in range(1, len(junk_candidates)):
            removal_sets.extend(combinations(junk_candidates, r))

    strategies = [None, zlib.Z_FILTERED, zlib.Z_HUFFMAN_ONLY, zlib.Z_RLE, zlib.Z_FIXED]
    tried = 0

    for remove_set in removal_sets:
        remove_names = {r.name for r in remove_set}
        surviving = [r for r in records if r.name not in remove_names]
        if not surviving:
            continue
        order = list(range(len(surviving)))

        # Try every fingerprint's order/compression convention for this
        # group on the surviving entries, STORE first then DEFLATE
        for fp in fingerprints:
            fp_order = _order_indices_for_kind(surviving, fp.order_kind)
            for use_overrides in (True, False):
                try:
                    candidate = _rebuild_zip_faithful(
                        surviving, contents, fp_order,
                        compress_method=fp.compress_method,
                        zlib_level=fp.zlib_level if fp.compress_method == 8 else None,
                        zlib_strategy=fp.zlib_strategy if fp.compress_method == 8 else None,
                        header_overrides=fp.as_header_overrides() if use_overrides else None,
                    )
                    tried += 1
                    if crc32_of(candidate) == target_crc:
                        removed_str = ', '.join(r.name for r in remove_set)
                        override_note = " (using reference's header metadata)" if use_overrides else ""
                        return RepairAttempt(
                            "reference_junk_removal", True, candidate,
                            f"removed entr{'y' if len(remove_set)==1 else 'ies'} absent "
                            f"from group's known-good references ({removed_str}), "
                            f"rebuilt using group fingerprint from {fp.source_file}"
                            f"{override_note}"
                        )
                except Exception:
                    continue

        # Also try the generic STORE + level/strategy search as a
        # fallback for groups with only weak/no compression fingerprint
        try:
            candidate = _rebuild_zip_faithful(surviving, contents, order, compress_method=0)
            tried += 1
            if crc32_of(candidate) == target_crc:
                removed_str = ', '.join(r.name for r in remove_set)
                return RepairAttempt(
                    "reference_junk_removal", True, candidate,
                    f"removed entr{'y' if len(remove_set)==1 else 'ies'} absent "
                    f"from group's known-good references ({removed_str}), "
                    f"rebuilt using STORE"
                )
        except Exception:
            pass

    return RepairAttempt(
        "reference_junk_removal", False,
        detail=f"found {len(junk_candidates)} entr{'y' if len(junk_candidates)==1 else 'ies'} "
               f"absent from references ({', '.join(junk_names)}) and tried {tried} "
               f"rebuild combinations after removal — no match"
    )


# ═══════════════════════════════════════════════════════════════════════
# MAIN API
# ═══════════════════════════════════════════════════════════════════════

class SceneRecreatorAPI:
    def __init__(self):
        self._window = None
        self._running = False
        self._stop_flag = threading.Event()
        self._skip_flag = threading.Event()

    def set_window(self, w):
        self._window = w

    def _emit(self, event: str, data: dict):
        if not self._window:
            return
        try:
            import json as _j, base64 as _b64
            b64 = _b64.b64encode(
                _j.dumps(data, ensure_ascii=False).encode('utf-8')
            ).decode('ascii')
            js = "(function(){var d=JSON.parse(atob('"+b64+"'));window.srEvent('"+event+"',d);})()"
            self._window.evaluate_js(js)
        except Exception:
            pass

    def _log(self, msg: str, cls: str = 'info'):
        self._emit('log', {'msg': msg, 'cls': cls})

    def _progress(self, pct: int, label: str = ''):
        self._emit('progress', {'pct': pct, 'label': label})

    def save_config(self, cfg: dict) -> bool:
        try:
            p = Path(__file__).parent / 'scene_recreator.json'
            with open(p, 'w') as f:
                json.dump(cfg, f, indent=2)
            return True
        except Exception:
            return False

    def load_config(self) -> dict:
        try:
            p = Path(__file__).parent / 'scene_recreator.json'
            if p.exists():
                with open(p) as f:
                    return json.load(f)
        except Exception:
            pass
        return {}

    def browse_folder(self) -> str:
        try:
            import tkinter as tk
            from tkinter import filedialog
            root = tk.Tk()
            root.withdraw()
            root.attributes('-topmost', True)
            path = filedialog.askdirectory()
            root.destroy()
            return path if path else ''
        except Exception:
            return ''

    def start_run(self, config: dict) -> bool:
        if self._running:
            self._log('Already running.', 'warn')
            return False
        self._stop_flag.clear()
        t = threading.Thread(target=self._run, args=(config,), daemon=True)
        t.start()
        return True

    def stop_run(self):
        self._stop_flag.set()
        self._log('Stopping...', 'warn')

    def skip_current_file(self):
        """Abandon repair attempts on whichever file is currently being
        processed and move straight to the next one — unlike stop_run(),
        this does NOT end the whole run."""
        self._skip_flag.set()
        self._log('Skipping current file...', 'warn')

    def reset_fingerprint_db(self, db_path: str = '') -> bool:
        """
        Delete the reference fingerprint database file so the next
        reference scan starts completely fresh, with no previously
        learned groups skipped. Uses the same default path as _run()
        (next to this script) if db_path isn't given, so this matches
        whatever the actual scan would use without the caller having
        to duplicate that logic. Returns True if a file was deleted or
        none existed to begin with (both are a successful "now fresh"
        state); False only on a genuine deletion error.
        """
        path = Path(db_path) if db_path else (Path(__file__).parent / 'reference_fingerprint_db.json')
        try:
            if path.is_file():
                path.unlink()
                self._log(f'Fingerprint database reset: {path.name} deleted.', 'warn')
            else:
                self._log('Fingerprint database already empty — nothing to reset.', 'dim')
            return True
        except Exception as e:
            self._log(f'Could not reset fingerprint database: {e}', 'err')
            return False

    def exclude_reference(self, filename: str, excluded_refs_path: str = '') -> bool:
        """
        Manually flag a reference filename so it's never used as a
        reference again, even if it genuinely verifies against the
        DAT's listed CRC/MD5/SHA1 — for the rare case where the DAT's
        own hash for a release already includes injected junk that
        isn't heuristically distinguishable by name pattern (e.g. a
        randomly-named extra .nfo), which would otherwise silently
        poison the whole release group's known-good name set.

        Appends the lowercased filename to a plain text file (one per
        line, default next to this script), creating it if needed.
        Does nothing if the filename is already listed. Returns True
        on success, False on a genuine write error.
        """
        path = Path(excluded_refs_path) if excluded_refs_path else (
            Path(__file__).parent / 'excluded_references.txt'
        )
        name = filename.strip().lower()
        if not name:
            return False
        try:
            existing = set()
            if path.is_file():
                existing = {
                    line.strip().lower() for line in path.read_text(encoding='utf-8').splitlines()
                    if line.strip() and not line.strip().startswith('#')
                }
            if name in existing:
                self._log(f'{filename} is already excluded.', 'dim')
                return True
            with open(path, 'a', encoding='utf-8') as f:
                f.write(name + '\n')
            self._log(f'Excluded {filename} from future reference scans.', 'warn')
            return True
        except Exception as e:
            self._log(f'Could not exclude reference: {e}', 'err')
            return False

    def list_excluded_references(self, excluded_refs_path: str = '') -> list:
        """Return the current list of manually-excluded reference filenames."""
        path = Path(excluded_refs_path) if excluded_refs_path else (
            Path(__file__).parent / 'excluded_references.txt'
        )
        if not path.is_file():
            return []
        try:
            return [
                line.strip() for line in path.read_text(encoding='utf-8').splitlines()
                if line.strip() and not line.strip().startswith('#')
            ]
        except Exception:
            return []

    def unexclude_reference(self, filename: str, excluded_refs_path: str = '') -> bool:
        """Remove a filename from the manually-excluded references list."""
        path = Path(excluded_refs_path) if excluded_refs_path else (
            Path(__file__).parent / 'excluded_references.txt'
        )
        name = filename.strip().lower()
        if not path.is_file():
            return True
        try:
            lines = path.read_text(encoding='utf-8').splitlines()
            kept = [line for line in lines if line.strip().lower() != name]
            path.write_text('\n'.join(kept) + ('\n' if kept else ''), encoding='utf-8')
            self._log(f'Removed {filename} from excluded references.', 'dim')
            return True
        except Exception as e:
            self._log(f'Could not update excluded references: {e}', 'err')
            return False

    def relocate_files(self, file_paths: list, dest_folder: str, move: bool = True) -> dict:
        """
        Move or copy a list of original files (by full path, as stored
        in a result category's detail entries — e.g. 'leftovers only'
        files the person wants set aside for manual review) into a
        separate destination folder. Unlike _place_file, this never
        rewrites file content — it relocates the files exactly as they
        are on disk, since these aren't being repaired, just sorted out
        of the main scan folder.

        Used by the GUI's "Move to folder" / "Copy to folder" action in
        a result category's detail panel — the person picks a
        destination, and every file currently listed for that category
        gets relocated there in one action.

        Returns {'moved': int, 'copied': int, 'failed': list of
        {file, error}} so the GUI can report exactly what happened,
        including any individual failures (e.g. a file already removed
        from disk, or a permissions issue) without the whole batch
        aborting partway through.
        """
        dest = Path(dest_folder).expanduser()
        try:
            dest.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            self._log(f'Could not create destination folder {dest_folder}: {e}', 'err')
            return {'moved': 0, 'copied': 0, 'failed': [{'file': p, 'error': str(e)} for p in file_paths]}

        moved = 0
        copied = 0
        failed = []
        for p in file_paths:
            src = Path(p)
            if not src.is_file():
                failed.append({'file': src.name, 'error': 'file no longer exists at the recorded path'})
                continue
            out_path = dest / src.name
            try:
                if move:
                    shutil.move(str(src), str(out_path))
                    moved += 1
                else:
                    shutil.copy2(str(src), str(out_path))
                    copied += 1
            except Exception as e:
                failed.append({'file': src.name, 'error': str(e)})

        verb = 'Moved' if move else 'Copied'
        self._log(
            f'{verb} {moved or copied} file(s) to {dest}'
            + (f' — {len(failed)} failed' if failed else ''),
            'ok' if not failed else 'warn'
        )
        for f in failed:
            self._log(f'    {f["file"]}: {f["error"]}', 'err')

        return {'moved': moved, 'copied': copied, 'failed': failed}

    def _run(self, config: dict):
        self._running = True
        self._emit('status', {'state': 'running'})

        src_dir = Path(config.get('src', '')).expanduser()
        dat_dir = Path(config.get('dat_dir', '')).expanduser()
        dst_dir = Path(config.get('dst', '')).expanduser()
        dry_run = bool(config.get('dry_run', False))
        move_mode = bool(config.get('move_mode', False))  # False = copy (default)

        if not src_dir.is_dir():
            self._log(f'ERROR: Source folder not found: {src_dir}', 'err')
            self._finish_error()
            return
        if not dat_dir.is_dir():
            self._log(f'ERROR: DAT folder not found: {dat_dir}', 'err')
            self._finish_error()
            return
        if not dry_run and not dst_dir:
            self._log('ERROR: Output folder required (unless dry-run).', 'err')
            self._finish_error()
            return

        if parse_dat_file is None:
            self._log('ERROR: dat_merger module not available for DAT parsing.', 'err')
            self._finish_error()
            return

        self._log(f'Parsing DAT file(s) in: {dat_dir}', 'info')
        dat_index = build_dat_index(dat_dir)
        self._log(f'Indexed {len(dat_index)} unique scene filename(s) across all DATs', 'dim')

        if not dat_index:
            self._log('No usable DAT entries found.', 'warn')
            self._finish_done()
            return

        # Optional reference folder(s) — known-good DAT-verified zips used
        # to learn per-release-group packer fingerprints (compression
        # level/strategy, entry order). Originals are never opened in
        # place; they're copied to a temp dir before any inspection.
        # Multiple folders can be supplied (e.g. specific subfolders
        # rather than one huge parent folder, for speed).
        #
        # The fingerprint DATABASE (separate from the in-memory
        # group_fingerprints dict built fresh each run) persists learned
        # per-group conventions across runs, keyed by group name. Once a
        # group's convention is known, subsequent reference scans skip
        # the expensive byte-level extraction for every other file from
        # that same group — large reference folders with hundreds of
        # files per group otherwise re-derive the same answer every time.
        # use_fingerprint_db defaults to True; set False in config to
        # force a full fresh scan, ignoring (but not erasing) the database.
        group_fingerprints = {}
        ref_dirs_list = config.get('ref_dirs', [])
        if not ref_dirs_list and config.get('ref_dir'):
            # Backwards compatibility with the older single ref_dir key
            ref_dirs_list = [config['ref_dir']]
        if ref_dirs_list:
            use_db = config.get('use_fingerprint_db', True)
            db_path = Path(config.get('fingerprint_db_path') or
                            (Path(__file__).parent / 'reference_fingerprint_db.json'))
            known_group_fps = {}
            if use_db:
                known_group_fps = load_fingerprint_db(db_path)
                if known_group_fps:
                    self._log(
                        f'Fingerprint database: {len(known_group_fps)} group(s) already '
                        f'known from previous scans (from {db_path.name})', 'dim'
                    )

            # Manually-excluded reference filenames — a plain text
            # file, one filename per line (blank lines and lines
            # starting with # are ignored), for the rare case where a
            # reference genuinely matches its DAT-listed CRC/MD5/SHA1
            # but is known to already contain junk that isn't
            # heuristically detectable by name pattern. Defaults to a
            # file next to this script, same pattern as the
            # fingerprint database, but can be overridden in config.
            excluded_refs_path = Path(config.get('excluded_refs_path') or
                                        (Path(__file__).parent / 'excluded_references.txt'))
            excluded_refs = set()
            if excluded_refs_path.is_file():
                try:
                    for line in excluded_refs_path.read_text(encoding='utf-8').splitlines():
                        line = line.strip()
                        if line and not line.startswith('#'):
                            excluded_refs.add(line.lower())
                    if excluded_refs:
                        self._log(
                            f'Excluded references: {len(excluded_refs)} filename(s) loaded '
                            f'from {excluded_refs_path.name} — these will never be used as '
                            f'references, even if they verify against the DAT', 'dim'
                        )
                except Exception as e:
                    self._log(f'Could not read excluded references file: {e}', 'warn')

            group_fingerprints = build_reference_fingerprints_multi(
                ref_dirs_list, dat_index, log_fn=self._log,
                known_group_fps=known_group_fps if use_db else None,
                excluded_refs=excluded_refs
            )
            if use_db and group_fingerprints:
                if save_fingerprint_db(db_path, group_fingerprints):
                    self._log(f'Fingerprint database updated: {db_path.name}', 'dim')
                else:
                    self._log(f'Warning: could not save fingerprint database to {db_path}', 'warn')

        self._log(f'Scanning: {src_dir}', 'info')
        zip_files = sorted(
            p for p in src_dir.rglob('*') if p.is_file() and p.suffix.lower() == '.zip'
        )
        self._log(f'Found {len(zip_files)} .zip file(s) to check', 'dim')
        self._log(f'Mode: {"MOVE" if move_mode else "COPY"} confirmed files to output', 'dim')

        # One-time recursive index of every loose .nfo/.diz anywhere
        # under the scan folder (not just alongside each zip) — built
        # once here rather than re-walking the tree per file, since a
        # large collection can have hundreds of zips to check against.
        # Indexed by lowercased stem (filename without extension) so
        # both exact names and rename-pattern variants (e.g.
        # "file_id_1.diz" found while looking for "file_id.diz") are
        # reachable via the same lookup, regardless of which subfolder
        # they happen to live in.
        loose_sidecar_index = {}  # lowercased stem -> list[Path]
        for p in src_dir.rglob('*'):
            if p.is_file() and p.suffix.lower() in ('.nfo', '.diz'):
                loose_sidecar_index.setdefault(p.stem.lower(), []).append(p)
        if loose_sidecar_index:
            total_loose = sum(len(v) for v in loose_sidecar_index.values())
            self._log(
                f'Indexed {total_loose} loose .nfo/.diz file(s) across the scan folder '
                f'for sidecar matching', 'dim'
            )

        if not dry_run:
            dst_dir.mkdir(parents=True, exist_ok=True)

        stats = {
            'already_match': 0,
            'fixed': {},      # technique -> count
            'unresolved': 0,
            'no_dat_entry': 0,
            'leftovers_only': 0,
            'sidecar_matched': 0,
            'sidecar_fixed': 0,
            'sidecar_no_hash': 0,
            'skipped': 0,
        }
        # Per-category filename lists, so the GUI can show exactly which
        # files fall into each summary bucket when the user clicks on
        # one — separate from near_misses, which carries the FULL
        # diagnostic detail (attempt logs etc.) only for unresolved/
        # foreign-packer/skipped files specifically.
        details = {
            'already_match': [],
            'fixed': [],          # list of {file, technique, game_name}
            'unresolved': [],
            'foreign_packer': [],
            'no_dat_entry': [],
            'leftovers_only': [],  # list of {file, entries, game_name}
            'sidecar_matched': [],
            'sidecar_fixed': [],
            'sidecar_no_hash': [],
            'skipped': [],
        }
        stats['sidecar_details'] = details  # _handle_sidecars appends directly into this
        near_misses = []

        total = len(zip_files)
        for idx, zpath in enumerate(zip_files):
            if self._stop_flag.is_set():
                self._log('Stopped by user.', 'warn')
                break
            self._skip_flag.clear()  # skip only ever applies to ONE file

            pct = int((idx / total) * 100) if total else 100
            self._progress(pct, zpath.name)

            key = zpath.name.lower()
            candidates = dat_index.get(key)

            if not candidates:
                stats['no_dat_entry'] += 1
                details['no_dat_entry'].append({'file': zpath.name})
                self._log(f'— {zpath.name}: no DAT entry found', 'dim')
                continue

            # Note how the match was found — by literal ROM filename,
            # release/game name (renamed-zip case), or release name with
            # a date prefix stripped (TOSEC-dated DAT vs undated on-disk file)
            match_kinds = set()
            for c in candidates:
                if c.get('rom_name', '').lower() == key:
                    match_kinds.add('rom filename')
                game_name_l = c.get('game_name', '').lower()
                if game_name_l + '.zip' == key:
                    match_kinds.add('release name')
                stripped_l = strip_date_prefix(c.get('game_name', '')).lower()
                if stripped_l != game_name_l and stripped_l + '.zip' == key:
                    match_kinds.add('release name (date prefix stripped)')
            match_via = ' / '.join(sorted(match_kinds)) or 'name'
            self._log(
                f'  matched via {match_via}  '
                f'({len(candidates)} DAT candidate(s))', 'dim'
            )

            try:
                data = zpath.read_bytes()
            except Exception as e:
                self._log(f'✗ {zpath.name}: cannot read file: {e}', 'err')
                continue

            current_crc = crc32_of(data)

            # Try every candidate target in turn (handles same-name-in-multiple-DATs)
            matched_candidate = None
            for cand in candidates:
                if cand['crc'] and current_crc == cand['crc']:
                    matched_candidate = cand
                    break

            confirmed_candidate = None  # set once we know which DAT game this is
            confirmed_bytes = None      # the final, verified-correct ZIP bytes — used for extracting in-ZIP sidecars

            if matched_candidate:
                stats['already_match'] += 1
                details['already_match'].append({
                    'file': zpath.name, 'game_name': matched_candidate.get('game_name', '')
                })
                self._log(f'✓ {zpath.name}: already matches DAT CRC ({current_crc})', 'ok')
                confirmed_candidate = matched_candidate
                confirmed_bytes = data
                if not dry_run:
                    self._place_file(zpath, data, confirmed_candidate, dst_dir, move_mode)
            else:
                # Cheap, early check: is this zip already known to be
                # just leftover sidecar files with the main content
                # already stripped out, rather than corrupted/wrong?
                # If so, skip the entire repair stack — there's nothing
                # in the file that could ever reconstruct the missing
                # content, so attempting repair would only waste time.
                leftovers_check = detect_leftovers_only(data, candidates[0].get('size'))
                if leftovers_check['is_leftovers_only']:
                    stats['leftovers_only'] = stats.get('leftovers_only', 0) + 1
                    details.setdefault('leftovers_only', []).append({
                        'file': zpath.name,
                        'path': str(zpath),
                        'entries': leftovers_check['entries'],
                        'game_name': candidates[0]['game_name'],
                    })
                    self._log(
                        f'○ {zpath.name}: leftovers only, not attempting repair — '
                        f'{leftovers_check["detail"]}', 'warn'
                    )
                    continue

                # Needs repair — try each candidate target with each technique
                self._log(f'⚙ {zpath.name}: CRC mismatch (have {current_crc}, '
                           f'{len(candidates)} target(s) in DAT) — attempting repair', 'warn')

                fixed = False
                attempt_log = []
                last_diagnosis = ""
                for cand in candidates:
                    if self._stop_flag.is_set() or self._skip_flag.is_set():
                        break
                    target_crc = cand['crc']
                    if not target_crc:
                        continue

                    # Cheap early attempt: if the source folder has a
                    # loose .nfo/.diz whose CRC/MD5/SHA1 matches the
                    # DAT's own hash entry for that exact filename (the
                    # same trust model _handle_sidecars already uses
                    # for loose sidecars after a fix is confirmed), use
                    # its content to fix an entry INSIDE this zip that
                    # has the wrong content — e.g. a different
                    # release's file_id.diz mixed in by mistake. Only
                    # tried if there's a genuine DAT-verified candidate
                    # to substitute; otherwise this is a no-op.
                    #
                    # Loose .nfo/.diz files sitting in the scan folder
                    # may have different line endings than what's
                    # actually baked into the zip (CRLF vs LF) even
                    # when the underlying text is genuinely correct —
                    # repair_loose_text_file() tries an exact match
                    # first, then both line-ending normalisations
                    # against the DAT's CRC, the same way the existing
                    # post-fix sidecar handling already does, so a
                    # loose file isn't wrongly rejected just because of
                    # how its line endings happen to be stored on disk.
                    replacement_lookup = {}
                    for rom in (cand.get('sibling_roms') or []):
                        rn = (rom.get('name') or '').strip()
                        if not rn.lower().endswith(('.nfo', '.diz')):
                            continue
                        want_crc = (rom.get('crc') or '').lower()
                        if not want_crc:
                            continue
                        want_md5 = (rom.get('md5') or '').lower()
                        want_sha1 = (rom.get('sha1') or '').lower()

                        for loose_path in _candidate_loose_sidecar_paths(loose_sidecar_index, rn, near_folder=zpath.parent):
                            if not loose_path.is_file():
                                continue
                            try:
                                loose_data = loose_path.read_bytes()
                            except Exception:
                                continue
                            loose_result = repair_loose_text_file(loose_data, want_crc)
                            if not loose_result.success:
                                continue
                            verified_data = loose_result.data
                            if want_md5 and md5_of(verified_data) != want_md5:
                                continue
                            if want_sha1 and sha1_of(verified_data) != want_sha1:
                                continue
                            if loose_path.name.lower() != rn.lower():
                                self._log(
                                    f'    found verified match for {rn} under a different '
                                    f'name on disk: {loose_path.name}', 'dim'
                                )
                            replacement_lookup[rn.lower()] = verified_data
                            break  # first verified match wins — no need to keep checking other candidates

                    if replacement_lookup:
                        sub_result = technique_sidecar_content_substitute(data, target_crc, replacement_lookup)
                        attempt_log.append(
                            f"{sub_result.technique}: "
                            f"{'MATCH' if sub_result.success else 'no match'} — {sub_result.detail}"
                        )
                        self._log(f'    [{sub_result.technique}] '
                                   f'{"✓ MATCH" if sub_result.success else "no match"} '
                                   f'— {sub_result.detail}',
                                   'ok' if sub_result.success else 'dim')
                        if sub_result.success:
                            final_md5 = md5_of(sub_result.data)
                            final_sha1 = sha1_of(sub_result.data)
                            md5_ok = (not cand.get('md5')) or (final_md5 == cand['md5'])
                            sha1_ok = (not cand.get('sha1')) or (final_sha1 == cand['sha1'])
                            if md5_ok and sha1_ok:
                                self._log(
                                    f'  ✓✓ {zpath.name}: FIXED via {sub_result.technique} '
                                    f'(matched {cand["game_name"]} in {cand["source_dat"]})',
                                    'ok'
                                )
                                stats['fixed'][sub_result.technique] = stats['fixed'].get(sub_result.technique, 0) + 1
                                details['fixed'].append({
                                    'file': zpath.name, 'technique': sub_result.technique,
                                    'game_name': cand['game_name']
                                })
                                confirmed_candidate = cand
                                confirmed_bytes = sub_result.data
                                if not dry_run:
                                    self._place_file(zpath, sub_result.data, confirmed_candidate, dst_dir, move_mode)
                                fixed = True
                                break

                    # Fast path: if we have learned packer fingerprints for
                    # this release group from verified reference zips, try
                    # those first — usually 1-4 attempts instead of dozens
                    group = extract_release_group(cand['game_name'])
                    group_fps = group_fingerprints.get(group)
                    if group_fps:
                        for fast_fn, fast_args in (
                            (technique_surgical_delete, (data, target_crc, group_fps)),
                            (technique_data_descriptor_strip, (data, target_crc, group_fps)),
                            (technique_local_header_reconstruct, (data, target_crc, group_fps)),
                            (technique_fingerprint_rebuild, (data, target_crc, group_fps)),
                            (technique_reference_junk_removal, (data, target_crc, group_fps)),
                        ):
                            fp_result = fast_fn(*fast_args)
                            attempt_log.append(
                                f"{fp_result.technique}: "
                                f"{'MATCH' if fp_result.success else 'no match'} — {fp_result.detail}"
                            )
                            self._log(f'    [{fp_result.technique}] '
                                       f'{"✓ MATCH" if fp_result.success else "no match"} '
                                       f'— {fp_result.detail}',
                                       'ok' if fp_result.success else 'dim')
                            if fp_result.success:
                                final_md5 = md5_of(fp_result.data)
                                final_sha1 = sha1_of(fp_result.data)
                                md5_ok = (not cand['md5']) or (final_md5 == cand['md5'])
                                sha1_ok = (not cand['sha1']) or (final_sha1 == cand['sha1'])
                                if md5_ok and sha1_ok:
                                    self._log(
                                        f'  ✓✓ {zpath.name}: FIXED via {fp_result.technique} '
                                        f'(matched {cand["game_name"]} in {cand["source_dat"]})',
                                        'ok'
                                    )
                                    stats['fixed'][fp_result.technique] = stats['fixed'].get(fp_result.technique, 0) + 1
                                    details['fixed'].append({
                                        'file': zpath.name, 'technique': fp_result.technique,
                                        'game_name': cand['game_name']
                                    })
                                    confirmed_candidate = cand
                                    confirmed_bytes = fp_result.data
                                    if not dry_run:
                                        self._place_file(zpath, fp_result.data, confirmed_candidate, dst_dir, move_mode)
                                    fixed = True
                                    break
                                else:
                                    self._log(
                                        f'    [{fp_result.technique}] CRC matched but MD5/SHA1 '
                                        f'verification failed — rejecting (hash collision avoided)',
                                        'err'
                                    )
                                    attempt_log.append(f"{fp_result.technique}: CRC matched but MD5/SHA1 failed verification")
                        if fixed:
                            break

                    for technique_fn in TECHNIQUES:
                        if self._stop_flag.is_set() or self._skip_flag.is_set():
                            break
                        if 'fingerprints' in technique_fn.__code__.co_varnames[:technique_fn.__code__.co_argcount]:
                            result = technique_fn(data, target_crc, fingerprints=group_fps)
                        else:
                            result = technique_fn(data, target_crc)
                        attempt_log.append(
                            f"{result.technique}: "
                            f"{'MATCH' if result.success else 'no match'} — {result.detail}"
                        )
                        self._log(f'    [{result.technique}] '
                                   f'{"✓ MATCH" if result.success else "no match"} '
                                   f'— {result.detail}',
                                   'ok' if result.success else 'dim')

                        if result.diagnosis:
                            last_diagnosis = result.diagnosis
                            self._log(f'    ⓘ {result.diagnosis}', 'warn')
                            attempt_log.append(f"DIAGNOSIS: {result.diagnosis}")

                        if result.success:
                            # Verify MD5/SHA1 too if DAT provides them
                            final_md5 = md5_of(result.data)
                            final_sha1 = sha1_of(result.data)
                            md5_ok = (not cand['md5']) or (final_md5 == cand['md5'])
                            sha1_ok = (not cand['sha1']) or (final_sha1 == cand['sha1'])

                            if md5_ok and sha1_ok:
                                self._log(
                                    f'  ✓✓ {zpath.name}: FIXED via {result.technique} '
                                    f'(matched {cand["game_name"]} in {cand["source_dat"]})',
                                    'ok'
                                )
                                stats['fixed'][result.technique] = stats['fixed'].get(result.technique, 0) + 1
                                details['fixed'].append({
                                    'file': zpath.name, 'technique': result.technique,
                                    'game_name': cand['game_name']
                                })
                                confirmed_candidate = cand
                                confirmed_bytes = result.data
                                if not dry_run:
                                    self._place_file(zpath, result.data, confirmed_candidate, dst_dir, move_mode)
                                fixed = True
                            else:
                                self._log(
                                    f'    [{result.technique}] CRC matched but MD5/SHA1 '
                                    f'verification failed — rejecting (hash collision avoided)',
                                    'err'
                                )
                                attempt_log.append(f"{result.technique}: CRC matched but MD5/SHA1 failed verification")
                            break
                    if fixed:
                        break

                if not fixed:
                    if self._skip_flag.is_set():
                        stats['skipped'] = stats.get('skipped', 0) + 1
                        details['skipped'].append({'file': zpath.name})
                        self._log(f'⏭ {zpath.name}: skipped by user', 'warn')
                    elif 'LIKELY FOREIGN PACKER' in last_diagnosis:
                        stats['foreign_packer'] = stats.get('foreign_packer', 0) + 1
                        details['foreign_packer'].append({'file': zpath.name, 'diagnosis': last_diagnosis})
                        self._log(
                            f'✗ {zpath.name}: unresolved — likely foreign packer '
                            f'(content probably correct, see diagnosis above)', 'warn'
                        )
                    else:
                        stats['unresolved'] += 1
                        details['unresolved'].append({'file': zpath.name})
                        self._log(f'✗ {zpath.name}: unresolved — no technique matched target CRC', 'err')
                    near_misses.append({
                        'file': zpath.name,
                        'current_crc': current_crc,
                        'targets': [c['crc'] for c in candidates if c['crc']],
                        'diagnosis': last_diagnosis,
                        'attempts': attempt_log,
                        'skipped': self._skip_flag.is_set(),
                        '_retry_zpath': zpath,
                        '_retry_candidates': candidates,
                        '_retry_dst_dir': dst_dir,
                        '_retry_move_mode': move_mode,
                    })

            # ── Sidecar .nfo/.diz handling ──────────────────────────────
            # Only attempted once we know which DAT <game> block this ZIP
            # belongs to (i.e. it was confirmed matching or successfully
            # fixed). Looks for .nfo/.diz files sitting next to the ZIP
            # on disk, and checks them against any sibling ROM entries
            # in the same DAT game block that have a hash for that exact
            # sidecar filename.
            if confirmed_candidate and not self._stop_flag.is_set():
                self._handle_sidecars(
                    zpath, confirmed_candidate, dst_dir, move_mode, dry_run, stats,
                    zip_bytes=confirmed_bytes, loose_sidecar_index=loose_sidecar_index
                )

        # Auto-retry pass: for files that stayed unresolved or were
        # diagnosed as "likely foreign packer", check whether any of
        # the references that contributed to their release group's
        # known_entry_names might be a poisoned reference (one whose
        # own DAT-verified hash already contains injected junk that
        # the auto-detect filter couldn't catch by name pattern alone
        # — e.g. a randomly-named extra .nfo). For each candidate
        # suspect reference, exclude just that one reference and retry
        # only the reference-dependent techniques; stop at the first
        # exclusion that produces a match, and report exactly which
        # reference was responsible so it can be permanently excluded
        # going forward rather than needing this retry every time.
        retryable = [nm for nm in near_misses if not nm.get('skipped') and nm.get('_retry_zpath')]
        if retryable and config.get('auto_retry_excluding_suspect_refs', True) and group_fingerprints:
            self._log('', '')
            self._log(f'═══ AUTO-RETRY: checking {len(retryable)} unresolved file(s) for '
                       f'poisoned references ═══', 'info')
            still_unresolved = []
            retry_total = len(retryable)
            for retry_idx, nm in enumerate(retryable):
                if self._stop_flag.is_set():
                    self._log('  Stopped by user — remaining files left as unresolved.', 'warn')
                    still_unresolved.extend(retryable[retry_idx:])
                    break

                self._progress(
                    int((retry_idx / retry_total) * 100) if retry_total else 100,
                    f'auto-retry: {nm["file"]}'
                )

                zpath = nm['_retry_zpath']
                candidates_for_file = nm['_retry_candidates']
                try:
                    data = zpath.read_bytes()
                except Exception:
                    still_unresolved.append(nm)
                    continue

                resolved_this_file = False
                for cand in candidates_for_file:
                    target_crc = cand.get('crc')
                    if not target_crc:
                        continue
                    group = extract_release_group(cand['game_name'])
                    group_fps = group_fingerprints.get(group)
                    if not group_fps:
                        continue

                    try:
                        records = parse_central_directory(data)
                        file_names = {r.name.lower() for r in records}
                    except Exception:
                        continue

                    # A reference is only a plausible suspect if it
                    # shares an UNUSUAL entry name with this file — one
                    # that only a small minority of the group's other
                    # references also have. Common boilerplate names
                    # (e.g. "file_id.diz", "mirage.nfo") appear in
                    # nearly every reference for a group and say
                    # nothing about which specific reference might be
                    # poisoned; matching on those alone would flag most
                    # of the group as "suspect" and make this pass as
                    # expensive as just trying every fingerprint
                    # individually. A name shared by only a handful of
                    # references is a far stronger, cheaper signal.
                    name_doc_count = {}
                    for fp in group_fps:
                        for n in fp.known_entry_names:
                            name_doc_count[n] = name_doc_count.get(n, 0) + 1
                    rarity_cutoff = max(1, len(group_fps) // 4)  # name appears in at most 25% of refs
                    unusual_shared_names = {
                        n for n in file_names
                        if name_doc_count.get(n, 0) and name_doc_count[n] <= rarity_cutoff
                    }

                    suspects = []
                    if unusual_shared_names:
                        for fp in group_fps:
                            if unusual_shared_names & set(fp.known_entry_names):
                                suspects.append(fp.source_file)
                    suspects = sorted(set(s.lower() for s in suspects))

                    # Hard cap on how many suspects get tried per file —
                    # protects against a pathological case (e.g. a
                    # genuinely common rare-ish name) still producing a
                    # large suspect list and silently consuming a huge
                    # amount of time with no further feedback
                    MAX_SUSPECTS_PER_FILE = 15
                    if len(suspects) > MAX_SUSPECTS_PER_FILE:
                        self._log(
                            f'    {zpath.name}: {len(suspects)} suspect reference(s) found, '
                            f'checking the first {MAX_SUSPECTS_PER_FILE} only', 'dim'
                        )
                        suspects = suspects[:MAX_SUSPECTS_PER_FILE]
                    elif suspects:
                        self._log(
                            f'    {zpath.name}: checking {len(suspects)} suspect reference(s)...',
                            'dim'
                        )

                    for suspect in suspects:
                        if self._stop_flag.is_set():
                            break
                        trimmed_fps = [fp for fp in group_fps if fp.source_file.lower() != suspect]
                        if not trimmed_fps:
                            continue
                        for fast_fn in (technique_surgical_delete, technique_data_descriptor_strip,
                                        technique_local_header_reconstruct, technique_reference_junk_removal):
                            # Note: technique_fingerprint_rebuild deliberately
                            # excluded here — it internally tries every
                            # compression/order combination per fingerprint,
                            # which is the single most expensive technique
                            # and rarely the one that actually needs a
                            # specific reference excluded (a poisoned
                            # reference's effect is on known_entry_names,
                            # which only the deletion/junk-removal
                            # techniques consult)
                            result = fast_fn(data, target_crc, fingerprints=trimmed_fps)
                            if not result.success:
                                continue
                            final_md5 = md5_of(result.data)
                            final_sha1 = sha1_of(result.data)
                            md5_ok = (not cand.get('md5')) or (final_md5 == cand['md5'])
                            sha1_ok = (not cand.get('sha1')) or (final_sha1 == cand['sha1'])
                            if not (md5_ok and sha1_ok):
                                continue
                            self._log(
                                f'  ✓✓ {zpath.name}: FIXED via {result.technique} after excluding '
                                f'suspect reference "{suspect}" (matched {cand["game_name"]}) — '
                                f'consider adding "{suspect}" to excluded_references.txt permanently',
                                'ok'
                            )
                            stats['fixed'][result.technique] = stats['fixed'].get(result.technique, 0) + 1
                            details['fixed'].append({
                                'file': zpath.name, 'technique': result.technique,
                                'game_name': cand['game_name'],
                                'diagnosis': f'fixed by excluding suspect reference "{suspect}"'
                            })
                            if nm.get('diagnosis', '').startswith('LIKELY FOREIGN PACKER'):
                                stats['foreign_packer'] = max(0, stats.get('foreign_packer', 0) - 1)
                                details['foreign_packer'] = [
                                    d for d in details['foreign_packer'] if d.get('file') != zpath.name
                                ]
                            else:
                                stats['unresolved'] = max(0, stats.get('unresolved', 0) - 1)
                                details['unresolved'] = [
                                    d for d in details['unresolved'] if d.get('file') != zpath.name
                                ]
                            if not dry_run:
                                self._place_file(zpath, result.data, cand, nm['_retry_dst_dir'], nm['_retry_move_mode'])
                            resolved_this_file = True
                            break
                        if resolved_this_file:
                            break
                    if resolved_this_file:
                        break

                if not resolved_this_file:
                    still_unresolved.append(nm)

            self._progress(100, 'auto-retry complete')
            recovered_files = {nm['file'] for nm in retryable} - {nm['file'] for nm in still_unresolved}
            if recovered_files:
                near_misses = [nm for nm in near_misses if nm['file'] not in recovered_files]

            n_recovered = len(retryable) - len(still_unresolved)
            if n_recovered:
                self._log(f'  Auto-retry recovered {n_recovered} file(s) by excluding a suspect reference.', 'ok')
            else:
                self._log('  Auto-retry found no suspect references that resolved any remaining file.', 'dim')

        # Strip internal retry-context keys (Path objects, dicts) before
        # serializing the near-miss report — these were only needed
        # in-memory for the auto-retry pass above
        near_misses = [
            {k: v for k, v in nm.items() if not k.startswith('_retry_')}
            for nm in near_misses
        ]

        # Write near-miss report
        if near_misses and not dry_run:
            report_path = dst_dir / '_near_miss_report.json'
            try:
                report_path.write_text(json.dumps(near_misses, indent=2), encoding='utf-8')
                self._log(f'Near-miss report written: {report_path.name}', 'dim')
            except Exception:
                pass

        # Summary
        n_fixed_total = sum(stats['fixed'].values())
        n_foreign = stats.get('foreign_packer', 0)
        self._log('', '')
        self._log('═══ SUMMARY ═══', 'info')
        self._log(f'  Already matching DAT:  {stats["already_match"]}', 'ok')
        self._log(f'  Fixed:                 {n_fixed_total}', 'ok' if n_fixed_total else 'dim')
        for tech, count in stats['fixed'].items():
            self._log(f'    via {tech}: {count}', 'dim')
        self._log(f'  Unresolved:            {stats["unresolved"]}', 'warn' if stats['unresolved'] else 'dim')
        self._log(f'  Likely foreign packer: {n_foreign}  '
                   f'(structure clean, content likely correct — see report)',
                   'warn' if n_foreign else 'dim')
        self._log(f'  No DAT entry found:    {stats["no_dat_entry"]}', 'dim')
        self._log(f'  Leftovers only (no repair attempted): {stats.get("leftovers_only", 0)}',
                   'warn' if stats.get('leftovers_only') else 'dim')
        self._log(f'  Skipped by user:       {stats.get("skipped", 0)}', 'warn' if stats.get('skipped') else 'dim')
        self._log('  --- Sidecar .nfo/.diz files ---', 'dim')
        self._log(f'  Sidecar matched:       {stats["sidecar_matched"]}', 'ok' if stats['sidecar_matched'] else 'dim')
        self._log(f'  Sidecar fixed:         {stats["sidecar_fixed"]}', 'ok' if stats['sidecar_fixed'] else 'dim')
        self._log(f'  Sidecar no DAT hash:   {stats["sidecar_no_hash"]}', 'dim')
        if dry_run:
            self._log('  (DRY RUN — no files were written)', 'warn')

        # Fold the sidecar handler's per-file detail lists (populated
        # via stats['sidecar_details']) into the same `details` dict
        # used for every other category, so the GUI has one consistent
        # structure to pull from regardless of category.
        sidecar_details = stats.get('sidecar_details', {})
        for key in ('sidecar_matched', 'sidecar_fixed', 'sidecar_no_hash'):
            details[key] = sidecar_details.get(key, [])

        self._progress(100, 'Complete')
        self._running = False
        self._emit('status', {'state': 'done', 'stats': {
            'already_match': stats['already_match'],
            'fixed': n_fixed_total,
            'unresolved': stats['unresolved'],
            'foreign_packer': n_foreign,
            'no_dat_entry': stats['no_dat_entry'],
            'leftovers_only': stats.get('leftovers_only', 0),
            'sidecar_matched': stats['sidecar_matched'],
            'sidecar_fixed': stats['sidecar_fixed'],
            'sidecar_no_hash': stats['sidecar_no_hash'],
            'skipped': stats.get('skipped', 0),
        }, 'details': details})

    def _handle_sidecars(self, zpath: Path, candidate: dict, dst_dir: Path,
                          move_mode: bool, dry_run: bool, stats: dict,
                          zip_bytes: bytes = None, loose_sidecar_index: dict = None):
        """
        Ensure any .nfo/.diz the DAT's game block expects ends up sitting
        alongside the ZIP in the output folder. Checks two places, in
        order:

        1. LOOSE FILES anywhere in the scan tree (via loose_sidecar_index,
           a pre-built recursive index — not just next to zpath on disk,
           since loose sidecars commonly end up scattered across
           subfolders or accumulate renamed duplicates in a shared main
           folder) — if the DAT has a hash entry for that exact filename
           (or a recognized rename-pattern variant of it), verify/fix
           via line-ending normalisation and place it alongside the
           ZIP in the output.

        2. INSIDE THE ZIP ITSELF — many scene releases bundle their
           .nfo/.diz as entries INSIDE the zip rather than as loose
           files next to it. For any sidecar hash entry not already
           satisfied by a loose file, this looks for a same-named entry
           inside zip_bytes (the final, confirmed-correct ZIP — already
           verified/repaired by this point), extracts its content, and
           tries the same CRC/line-ending repair. The .nfo/.diz inside
           the ZIP is left exactly as-is (untouched, since it's part of
           what makes the ZIP's own hash correct) — this only writes an
           ADDITIONAL standalone copy into the output folder, it never
           modifies or removes anything from inside the zip.

        If no DAT hash entry exists for a given sidecar filename at
        all, it's left untouched wherever it is found.
        """
        sibling_roms = candidate.get('sibling_roms') or []
        # Map of sidecar filename (lower) -> hash info, from the same
        # <game> block as the matched ZIP
        sidecar_hashes = {}
        for rom in sibling_roms:
            rn = (rom.get('name') or '').strip()
            if rn.lower().endswith(('.nfo', '.diz')):
                sidecar_hashes[rn.lower()] = {
                    'crc': (rom.get('crc') or '').lower(),
                    'md5': (rom.get('md5') or '').lower(),
                    'sha1': (rom.get('sha1') or '').lower(),
                }

        if not sidecar_hashes:
            return  # nothing to do — no NFO/DIZ entries in this game block

        satisfied = set()  # filenames (lower) already handled via a loose file
        sd = stats.get('sidecar_details', {})

        # ── Pass 1: loose files anywhere in the scan tree ────────────
        loose_sidecar_index = loose_sidecar_index or {}
        checked_paths = set()
        for ext in ('.nfo', '.diz'):
            # Build the candidate list for every expected sidecar name
            # this DAT game block has a hash for (not just ones already
            # named exactly right on disk) — this is the same widened,
            # index-based, near-folder-first search used by the in-zip
            # substitution technique, applied here for the standalone
            # loose-copy pass too, so both paths behave consistently.
            for expected_name, hash_info in sidecar_hashes.items():
                if not expected_name.lower().endswith(ext):
                    continue
                for sidecar_path in _candidate_loose_sidecar_paths(
                    loose_sidecar_index, expected_name, near_folder=zpath.parent
                ):
                    if sidecar_path in checked_paths or not sidecar_path.is_file():
                        continue
                    checked_paths.add(sidecar_path)
                    matched_expected_name = expected_name.lower()

                    if not hash_info or not hash_info['crc']:
                        stats['sidecar_no_hash'] += 1
                        sd.setdefault('sidecar_no_hash', []).append({'file': sidecar_path.name})
                        continue  # no DAT hash entry for this sidecar — leave alone

                    try:
                        sdata = sidecar_path.read_bytes()
                    except Exception as e:
                        self._log(f'    sidecar {sidecar_path.name}: cannot read: {e}', 'err')
                        continue

                    result = repair_loose_text_file(sdata, hash_info['crc'])
                    if not result.success:
                        self._log(
                            f'    sidecar {sidecar_path.name}: CRC mismatch, '
                            f'line-ending fix did not resolve — leaving in place', 'warn'
                        )
                        continue

                    final_md5 = md5_of(result.data)
                    final_sha1 = sha1_of(result.data)
                    md5_ok = (not hash_info['md5']) or (final_md5 == hash_info['md5'])
                    sha1_ok = (not hash_info['sha1']) or (final_sha1 == hash_info['sha1'])
                    if not (md5_ok and sha1_ok):
                        self._log(
                            f'    sidecar {sidecar_path.name}: CRC matched but '
                            f'MD5/SHA1 verification failed — rejecting', 'err'
                        )
                        continue

                    if matched_expected_name != sidecar_path.name.lower():
                        self._log(
                            f'    sidecar {sidecar_path.name}: verified as a match for '
                            f'"{matched_expected_name}" despite the different filename on disk', 'dim'
                        )

                    if result.technique == 'already_match':
                        stats['sidecar_matched'] += 1
                        sd.setdefault('sidecar_matched', []).append({'file': sidecar_path.name})
                        self._log(f'    sidecar {sidecar_path.name}: already matches DAT CRC', 'ok')
                    else:
                        stats['sidecar_fixed'] += 1
                        sd.setdefault('sidecar_fixed', []).append({'file': sidecar_path.name, 'technique': result.technique})
                        self._log(
                            f'    sidecar {sidecar_path.name}: FIXED via {result.technique}', 'ok'
                        )

                    if not dry_run:
                        self._place_file(sidecar_path, result.data, candidate, dst_dir, move_mode)
                    # Mark the EXPECTED name as satisfied (not the loose
                    # file's own on-disk name, which may be a renamed
                    # variant) — Pass 2 checks against expected DAT
                    # sidecar names, so a renamed loose match needs to
                    # correctly prevent a redundant in-zip extraction
                    # for the name the DAT actually expects.
                    satisfied.add(matched_expected_name)
                    break  # first verified match for this expected name wins

        # ── Pass 2: entries bundled inside the ZIP itself ───────────
        # For any sidecar the DAT expects that wasn't already satisfied
        # by a loose file, check if the (confirmed-correct) ZIP has a
        # same-named entry and extract+repair a standalone copy from it.
        remaining = {name: info for name, info in sidecar_hashes.items() if name not in satisfied}
        if not remaining or not zip_bytes:
            return

        try:
            import io
            with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
                zip_entry_names = {n.lower(): n for n in zf.namelist()}
                for sidecar_name, hash_info in remaining.items():
                    # Match by exact filename only (not by folder path)
                    # since the DAT lists bare filenames like
                    # "file_id.diz", but inside the zip it might sit at
                    # the top level or, occasionally, in a subfolder
                    matched_entry = None
                    for entry_lower, entry_actual in zip_entry_names.items():
                        if entry_lower.rsplit('/', 1)[-1] == sidecar_name:
                            matched_entry = entry_actual
                            break
                    if not matched_entry:
                        continue  # this sidecar isn't loose AND isn't in the zip — nothing to extract
                    if not hash_info['crc']:
                        continue  # no usable hash to verify against

                    try:
                        edata = zf.read(matched_entry)
                    except Exception as e:
                        self._log(f'    sidecar {sidecar_name}: could not read from inside ZIP: {e}', 'err')
                        continue

                    result = repair_loose_text_file(edata, hash_info['crc'])
                    if not result.success:
                        self._log(
                            f'    sidecar {sidecar_name}: found inside ZIP but CRC mismatch, '
                            f'line-ending fix did not resolve — not extracted', 'warn'
                        )
                        continue

                    final_md5 = md5_of(result.data)
                    final_sha1 = sha1_of(result.data)
                    md5_ok = (not hash_info['md5']) or (final_md5 == hash_info['md5'])
                    sha1_ok = (not hash_info['sha1']) or (final_sha1 == hash_info['sha1'])
                    if not (md5_ok and sha1_ok):
                        self._log(
                            f'    sidecar {sidecar_name}: CRC matched but MD5/SHA1 '
                            f'verification failed — not extracted (hash collision avoided)', 'err'
                        )
                        continue

                    if result.technique == 'already_match':
                        stats['sidecar_matched'] += 1
                        sd.setdefault('sidecar_matched', []).append({'file': sidecar_name, 'in_zip': True})
                        self._log(
                            f'    sidecar {sidecar_name}: extracted from inside ZIP, '
                            f'already matches DAT CRC', 'ok'
                        )
                    else:
                        stats['sidecar_fixed'] += 1
                        sd.setdefault('sidecar_fixed', []).append({'file': sidecar_name, 'technique': result.technique, 'in_zip': True})
                        self._log(
                            f'    sidecar {sidecar_name}: extracted from inside ZIP, '
                            f'FIXED via {result.technique}', 'ok'
                        )

                    if not dry_run:
                        out_dir = self._game_output_dir(candidate, dst_dir)
                        out_dir.mkdir(parents=True, exist_ok=True)
                        out_path = out_dir / sidecar_name
                        try:
                            out_path.write_bytes(result.data)
                        except Exception as e:
                            self._log(f'    ERROR writing extracted sidecar {sidecar_name}: {e}', 'err')
        except Exception as e:
            self._log(f'    could not inspect ZIP for in-archive sidecars: {e}', 'err')

    def _game_output_dir(self, candidate: dict, dst_dir: Path) -> Path:
        """Output folder for a confirmed file: dst_dir / <DAT game name>."""
        game_name = candidate.get('game_name') or 'Unknown'
        # Sanitise for filesystem safety (strip characters illegal on Windows)
        safe_name = re.sub(r'[<>:"/\\|?*]', '_', game_name).strip()
        return dst_dir / safe_name

    def _place_file(self, src_path: Path, data: bytes, candidate: dict,
                     dst_dir: Path, move_mode: bool):
        """Write `data` (the confirmed-good bytes) to the DAT-game-named
        output folder, then move or copy metadata/remove the original
        per the move_mode toggle. Only ever called for hash-confirmed
        files (already-matching or successfully-repaired-and-verified)."""
        out_dir = self._game_output_dir(candidate, dst_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / src_path.name
        try:
            out_path.write_bytes(data)
            if move_mode:
                try:
                    src_path.unlink()
                except Exception as e:
                    self._log(f'    Warning: could not remove original {src_path.name}: {e}', 'warn')
        except Exception as e:
            self._log(f'    ERROR writing output for {src_path.name}: {e}', 'err')

    def _finish_error(self):
        self._running = False
        self._emit('status', {'state': 'error'})

    def _finish_done(self):
        self._running = False
        self._emit('status', {'state': 'done', 'stats': {}})


def main():
    api = SceneRecreatorAPI()
    window = webview.create_window(
        title='Scene ZIP Recreator — ToSort Toolkit',
        url=str(Path(__file__).parent / 'gui' / 'scene_recreator.html'),
        js_api=api,
        width=900,
        height=900,
        min_size=(700, 600),
        background_color='#0d0f12',
    )
    api.set_window(window)
    webview.start(debug=False)


if __name__ == '__main__':
    main()

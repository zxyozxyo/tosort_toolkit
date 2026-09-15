"""
ROM Manipulation Backend
========================
Detects ROM/disc-image formats by CONTENT (magic numbers and header structure),
not by file extension, and offers the conversions each detected format supports.

Design notes
------------
* Detection reads a 64 KiB head block per file and nothing more. The library
  this runs against is ~250 TB; full-file hashing during a scan would take days.
  Targeted seeks are used only where a header points outside the head block.
* Extensions are a HINT that breaks ties, never the primary signal. A .bin that
  is really a byteswapped N64 ROM is identified as such.
* Folder names (NDS/3DS/WII/GC/...) are the last-resort backstop, used only for
  formats that genuinely have no signature (headerless NES).

Conversion engines live in the sibling sections below; see ENGINE_NATIVE vs
ENGINE_EXTERNAL. Only the GameCube/Wii NKit tier needs external executables.
"""

import os
import re
import json
import time
import struct
import binascii
from pathlib import Path

# ── low-level readers ─────────────────────────────────────────────────────────

def _u16le(b, o): return struct.unpack_from('<H', b, o)[0]
def _u32le(b, o): return struct.unpack_from('<I', b, o)[0]
def _u32be(b, o): return struct.unpack_from('>I', b, o)[0]
def _u64le(b, o): return struct.unpack_from('<Q', b, o)[0]


def _have(buf, offset, length):
    """True when buf actually contains the requested slice.

    Guards every header probe: a truncated or tiny file must fall through to
    the next detector, not raise out of the whole scan."""
    return len(buf) >= offset + length


def _at(buf, offset, length):
    return buf[offset:offset + length] if _have(buf, offset, length) else b''



def _clean(raw, limit=64):
    """Printable ASCII from a raw header field.

    Header text is frequently padded with 0xFF, Shift-JIS, or plain garbage on
    homebrew. These strings end up in the GUI and the log, so anything not
    printable is dropped rather than turned into replacement characters."""
    if isinstance(raw, str):
        raw = raw.encode('utf-8', 'ignore')
    out = ''.join(chr(c) for c in raw if 0x20 <= c <= 0x7E)
    return out.strip()[:limit]


HEAD_BYTES = 0x10000          # 64 KiB covers every signature we probe for


# ── result shape ──────────────────────────────────────────────────────────────

def _result(system, fmt, variant=None, confidence='high', detail='',
            conversions=None, notes=None, meta=None):
    """One detected file.

    system      — console family, e.g. 'NDS', 'GC', 'N64'
    fmt         — container/format, e.g. 'CCI', 'RVZ', 'iNES'
    variant     — the axis the user converts ALONG, e.g. 'encrypted'
    confidence  — 'high' (magic matched), 'medium' (structure only),
                  'low' (extension/folder inference)
    conversions — list of conversion ids this file can feed
    """
    return {
        'system': system,
        'format': fmt,
        'variant': variant,
        'confidence': confidence,
        'detail': detail,
        'conversions': conversions or [],
        'notes': notes or [],
        'meta': meta or {},
    }


# ══════════════════════════════════════════════════════════════════════════════
#  DETECTORS
#  Each takes (buf, path, fh) and returns a _result() or None. They are tried in
#  SIGNATURE_ORDER; the first hit wins, so put the unambiguous magics first.
# ══════════════════════════════════════════════════════════════════════════════

# ── Nintendo 64 ───────────────────────────────────────────────────────────────
# The first word of every N64 ROM is the PI bus config 0x80371240. Which byte
# order it arrives in IS the format, so the magic identifies the variant too.

N64_ORDERS = {
    b'\x80\x37\x12\x40': ('z64', 'big-endian',   'native cart order'),
    b'\x37\x80\x40\x12': ('v64', 'byteswapped',  '16-bit byteswapped (Doctor V64)'),
    b'\x40\x12\x37\x80': ('n64', 'little-endian', '32-bit little-endian (Mr. Backup)'),
    b'\x12\x40\x80\x37': ('wordswapped', 'wordswapped', 'rare 16-bit wordswap'),
}


def _detect_n64(buf, path, fh):
    sig = _at(buf, 0, 4)
    if sig not in N64_ORDERS:
        return None
    ext, variant, human = N64_ORDERS[sig]
    # Internal name lives at 0x20 (20 bytes), in the same byte order as the ROM.
    name = _n64_internal_name(buf, sig)
    others = [v for k, (e, v, h) in N64_ORDERS.items() if k != sig and v != 'wordswapped']
    return _result(
        'N64', ext.upper(), variant,
        detail=f'{human}' + (f' — "{name}"' if name else ''),
        conversions=[f'n64:{variant}->{t}' for t in others],
        meta={'order': variant, 'internal_name': name},
    )


def _n64_internal_name(buf, sig):
    """Game title from 0x20, unswapped back to big-endian first."""
    raw = _at(buf, 0x20, 20)
    if not raw:
        return ''
    raw = _n64_to_big(raw, sig)
    try:
        return raw.decode('shift_jis', 'replace').strip('\x00 ').strip()
    except Exception:
        return ''


def _n64_to_big(data, sig):
    if sig == b'\x37\x80\x40\x12':                       # v64 → z64
        return _swap16(data)
    if sig == b'\x40\x12\x37\x80':                       # n64 → z64
        return _swap32(data)
    if sig == b'\x12\x40\x80\x37':                       # wordswapped → z64
        return _swapword(data)
    return data


def _swap16(data):
    """Swap every adjacent byte pair. Odd tail byte is passed through."""
    b = bytearray(data)
    n = len(b) & ~1
    b[0:n:2], b[1:n:2] = b[1:n:2], b[0:n:2]
    return bytes(b)


def _swap32(data):
    b = bytearray(data)
    n = len(b) & ~3
    for i in range(0, n, 4):
        b[i:i + 4] = b[i:i + 4][::-1]
    return bytes(b)


def _swapword(data):
    b = bytearray(data)
    n = len(b) & ~3
    for i in range(0, n, 4):
        b[i:i + 4] = b[i + 2:i + 4] + b[i:i + 2]
    return bytes(b)


# ── NES / Famicom ─────────────────────────────────────────────────────────────

def _detect_nes(buf, path, fh):
    if _at(buf, 0, 4) == b'NES\x1a':
        b7 = buf[7] if _have(buf, 7, 1) else 0
        is_nes2 = (b7 & 0x0C) == 0x08
        prg = buf[4] if _have(buf, 4, 1) else 0
        chr_ = buf[5] if _have(buf, 5, 1) else 0
        mapper = ((buf[6] >> 4) | (b7 & 0xF0)) if _have(buf, 7, 1) else 0
        return _result(
            'NES', 'NES 2.0' if is_nes2 else 'iNES', 'headered',
            detail=f'mapper {mapper}, PRG {prg*16} KiB, CHR {chr_*8} KiB',
            conversions=['nes:headered->headerless'],
            meta={'mapper': mapper, 'prg16k': prg, 'chr8k': chr_,
                  'header': bytes(_at(buf, 0, 16)).hex()},
        )
    if _at(buf, 0, 4) == b'FDS\x1a':
        return _result('NES', 'FDS', 'headered', detail='Famicom Disk System, headered',
                       conversions=['nes:fds-headered->headerless'])
    if _at(buf, 0, 4) == b'UNIF':
        return _result('NES', 'UNIF', None, detail='UNIF container',
                       conversions=['nes:unif->nes'])
    return None


def _detect_nes_headerless(buf, path, fh, folder_hint=None):
    """Headerless NES has NO signature. Only claimed on a strong external hint.

    Deliberately conservative: a headerless NES ROM is just raw 6502 code, and
    guessing wrong here means writing a bogus 16-byte header onto something that
    was never a NES ROM. Requires .nes extension or an explicit folder hint."""
    ext = path.suffix.lower()
    size = fh['size']
    hinted = ext in ('.nes', '.unh') or         (folder_hint or '').upper() in ('NES', 'FAMICOM')
    if not hinted:
        return None
    # PRG banks are 16 KiB, CHR banks 8 KiB; a headerless dump is a clean sum.
    if size == 0 or size % 8192 != 0:
        return None
    return _result(
        'NES', 'raw', 'headerless', confidence='low',
        detail=f'{size // 1024} KiB, no iNES header — identified by '
               + ('extension' if ext in ('.nes', '.unh') else 'folder'),
        conversions=['nes:headerless->headered'],
        notes=['Re-heading needs a DAT match; the header cannot be computed '
               'from the data.'],
    )


# ── Nintendo DS ───────────────────────────────────────────────────────────────
# Identity comes from the cartridge header: the Nintendo logo at 0xC0 has a
# fixed CRC16 of 0xCF56 on every licensed release. The encrypted/decrypted axis
# is read from the Secure Area, the 16 KiB at arm9_rom_offset — when decrypted
# its first 8 bytes read 'encryObj', when encrypted they are KEY1 ciphertext.

NDS_SECURE_MAGIC = b'encryObj'
# What a decrypted dump actually carries. After decryption proves the key by
# yielding 'encryObj', ndstool - and therefore every No-Intro "(Decrypted)" dump -
# overwrites those 8 bytes with this marker (0xE7FFDEFF twice). Writing
# 'encryObj' instead produces a ROM that round-trips but matches no DAT.
NDS_DECRYPTED_MARKER = bytes.fromhex('ffdeffe7ffdeffe7')


def _detect_nds(buf, path, fh):
    if not _have(buf, 0x160, 0):
        return None
    logo_crc = _u16le(buf, 0x15C) if _have(buf, 0x15C, 2) else 0
    gamecode = _at(buf, 0x0C, 4)
    title = _at(buf, 0x00, 12).split(b'\x00')[0]
    unitcode = buf[0x12] if _have(buf, 0x12, 1) else 0

    licensed = logo_crc == 0xCF56
    # An alphanumeric gamecode alone is 4 bytes of luck: two headerless NES ROMs
    # in the test set passed on it. Unlicensed images must also carry a valid
    # header CRC16 (0x15E, over 0x000-0x15D), which ndstool always writes.
    plausible = bool(gamecode) and gamecode.isalnum() and         _u16le(buf, 0x15E) == _crc16_modbus(buf[:0x15E])
    if not licensed and not plausible:
        return None

    arm9_off = _u32le(buf, 0x20) if _have(buf, 0x20, 4) else 0
    conf = 'high' if licensed else 'medium'
    system = 'DSi' if unitcode == 0x03 else 'NDS'

    variant, note = _nds_secure_state(buf, fh, arm9_off)
    convs = []
    if variant == 'encrypted':
        convs = ['nds:encrypted->decrypted']
    elif variant == 'decrypted':
        convs = ['nds:decrypted->encrypted']

    # Trim state is an independent axis from encryption: a ROM can be either,
    # both, or neither, so it adds its own conversion rather than replacing one.
    used = _nds_used_size(buf)
    capacity = (128 * 1024 << buf[0x14]) if _have(buf, 0x15, 1) and buf[0x14] < 16 else 0
    trim_note = ''
    if used and fh['size'] > used:
        convs.append('nds:untrimmed->trimmed')
        waste = fh['size'] - used
        trim_note = (f'untrimmed - {waste // 1024:,} KiB of padding past the '
                     f'{used:,}-byte ROM')
    elif used and fh['size'] == used and capacity > used:
        convs.append('nds:trimmed->untrimmed')
        trim_note = f'trimmed to {used:,} bytes (cart holds {capacity:,})'

    try:
        title_s = _clean(title, 12)
    except Exception:
        title_s = ''
    return _result(
        system, 'NDS ROM', variant, confidence=conf,
        detail=f'{title_s} [{gamecode.decode("ascii", "replace")}] — '
               f'secure area {variant or "n/a"}',
        conversions=convs,
        notes=[n for n in (note, trim_note) if n],
        meta={'gamecode': _clean(gamecode, 4), 'used_rom_size': used,
              'capacity': capacity,
              'arm9_rom_offset': arm9_off, 'logo_crc_ok': licensed},
    )


def _crc16_modbus(data, crc=0xFFFF):
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def _nds_secure_state(buf, fh, arm9_off):
    """Classify the Secure Area. Returns (variant, note)."""
    # Only ARM9 offsets inside 0x4000..0x7FFF carry a Secure Area at all.
    if not (0x4000 <= arm9_off < 0x8000):
        return None, ('No Secure Area (ARM9 binary starts at '
                      f'0x{arm9_off:X}) — homebrew or already stripped.')
    head = _read_at(buf, fh, arm9_off, 8)
    if not head:
        return None, 'Secure Area offset lies past the end of the file.'
    if head in (NDS_SECURE_MAGIC, NDS_DECRYPTED_MARKER):
        return 'decrypted', ''
    if head == b'\x00' * 8:
        return None, 'Secure Area is zeroed — nothing to encrypt or decrypt.'
    return 'encrypted', ''


# ── Nintendo 3DS ──────────────────────────────────────────────────────────────
# NCSD (.3ds/.cci) wraps one or more NCCH partitions. The encryption axis is
# NCCH flags byte 7: bit 0x04 (NoCrypto) set means the partition is decrypted.

NCCH_FLAG_OFFSET = 0x188        # relative to the start of an NCCH
NCCH_NOCRYPTO = 0x04
NCCH_FIXEDKEY = 0x01
MEDIA_UNIT = 0x200


def _detect_3ds(buf, path, fh):
    if _at(buf, 0x100, 4) == b'NCSD':
        return _detect_ncsd(buf, path, fh)
    if _at(buf, 0x100, 4) == b'NCCH':
        return _detect_ncch(buf, path, fh, 0, 'NCCH')
    if _detect_cia_header(buf):
        return _detect_cia(buf, path, fh)
    return None


def _detect_ncsd(buf, path, fh):
    # Partition table at 0x120: eight (offset, length) pairs in media units.
    first_off = _u32le(buf, 0x120) * MEDIA_UNIT if _have(buf, 0x124, 0) else 0
    media_id = _u64le(buf, 0x108) if _have(buf, 0x108, 8) else 0
    if media_id == 0:
        return _result('3DS', 'NAND', None, confidence='medium',
                       detail='NCSD with no media ID — NAND image, not a game',
                       conversions=[])
    inner = _detect_ncch(buf, path, fh, first_off, 'CCI')
    if inner:
        return inner
    return _result('3DS', 'CCI', None, confidence='medium',
                   detail='NCSD cartridge image; first partition unreadable',
                   conversions=[])


def _detect_ncch(buf, path, fh, base, fmt):
    magic = _read_at(buf, fh, base + 0x100, 4)
    if magic != b'NCCH':
        return None
    flags = _read_at(buf, fh, base + NCCH_FLAG_OFFSET, 8)
    if len(flags) < 8:
        return None
    crypto_method, f7 = flags[3], flags[7]
    if f7 & NCCH_NOCRYPTO:
        variant, convs = 'decrypted', ['3ds:decrypted->encrypted']
        how = 'NoCrypto flag set'
    else:
        variant, convs = 'encrypted', ['3ds:encrypted->decrypted']
        how = {0x00: 'standard key', 0x01: 'key 7.x',
               0x0A: 'key 9.3', 0x0B: 'key 9.6'}.get(crypto_method,
                                                     f'method 0x{crypto_method:02X}')
        if f7 & NCCH_FIXEDKEY:
            how = 'fixed (zero) key'
    product = _read_at(buf, fh, base + 0x150, 16).split(b'\x00')[0]
    return _result(
        '3DS', fmt, variant, confidence='high',
        detail=f'{product.decode("ascii", "replace")} — {how}',
        conversions=convs,
        meta={'crypto_method': crypto_method, 'flags7': f7,
              'fixed_key': bool(f7 & NCCH_FIXEDKEY), 'ncch_base': base},
    )


def _detect_cia_header(buf):
    """CIA has no ASCII magic: it is a 0x2020-byte header, type 0, version 0."""
    if not _have(buf, 0x08, 0):
        return False
    return (_u32le(buf, 0x00) == 0x2020
            and _u16le(buf, 0x04) == 0x0000
            and _u16le(buf, 0x06) == 0x0000)


# ── GameCube / Wii and their containers ───────────────────────────────────────

GC_MAGIC   = 0xC2339F3D        # at 0x1C of a GameCube disc image
WII_MAGIC  = 0x5D1C9EA3        # at 0x18 of a Wii disc image
NKIT_TAG   = b'NKIT'           # NKit stamps this at 0x200 of its ISO output


def _detect_disc(buf, path, fh):
    """Raw (or NKit-processed) GameCube/Wii disc images."""
    wii = _have(buf, 0x18, 4) and _u32be(buf, 0x18) == WII_MAGIC
    gc  = _have(buf, 0x1C, 4) and _u32be(buf, 0x1C) == GC_MAGIC
    if not (wii or gc):
        return None
    system = 'WII' if wii else 'GC'
    gameid = _clean(_at(buf, 0x00, 6), 6)
    nkit = _at(buf, 0x200, 4) == NKIT_TAG
    fmt = 'NKit ISO' if nkit else 'ISO'
    notes = []
    if nkit:
        notes.append('Carries NKit recovery data - conversions preserve it.')
    return _result(
        system, fmt, 'nkit' if nkit else 'plain',
        detail=f'[{gameid}] {"NKit-processed" if nkit else "raw"} disc image',
        conversions=_disc_conversions(system, 'iso'),
        notes=notes,
        meta={'gameid': gameid, 'nkit': nkit},
    )


def _detect_wbfs(buf, path, fh):
    if _at(buf, 0, 4) != b'WBFS':
        return None
    nkit = NKIT_TAG in _at(buf, 0x200, 0x400)
    return _result('WII', 'WBFS', 'nkit' if nkit else 'plain',
                   detail='WBFS scrubbed Wii image',
                   conversions=_disc_conversions('WII', 'wbfs'))


# WIA/RVZ header is BIG-endian. The WIADisc struct follows the 0x48-byte file
# header: compression at 0x4C, level at 0x50, chunk size at 0x54, and a verbatim
# copy of the first 0x80 disc bytes at 0x58 - which is why the platform magic is
# readable without decompressing anything.
RVZ_COMPRESSION = {0: 'none', 1: 'purge', 2: 'bzip2', 3: 'lzma', 4: 'lzma2',
                   5: 'zstd'}
TARGET_RVZ = ('zstd', 19, 131072)      # the config this module converts INTO


def _rvz_params(buf):
    if not _have(buf, 0x58, 0):
        return None, None, None
    comp = RVZ_COMPRESSION.get(_u32be(buf, 0x4C), f'0x{_u32be(buf, 0x4C):X}')
    return comp, _u32be(buf, 0x50), _u32be(buf, 0x54)


def _detect_rvz(buf, path, fh):
    sig = _at(buf, 0, 4)
    if sig in (b'RVZ\x01', b'WIA\x01'):
        kind = 'RVZ' if sig == b'RVZ\x01' else 'WIA'
        system = _rvz_system(buf, fh)
        comp, level, chunk = _rvz_params(buf)
        at_target = (comp, level, chunk) == TARGET_RVZ
        chunk_k = f'{chunk // 1024}k' if chunk else '?'
        convs = _disc_conversions(system, 'rvz')
        notes = []
        if at_target:
            notes.append('Already at the target config - recompressing would '
                         'only cost time.')
        else:
            convs = convs + [f'disc:{system.lower()}:rvz->rvz']
        return _result(
            system, kind, f'{comp}-{level}-{chunk_k}' if comp else None,
            detail=f'Dolphin {kind}, {comp} level {level}, {chunk_k} blocks'
                   + (' - MATCHES target' if at_target else ''),
            conversions=convs, notes=notes,
            meta={'compression': comp, 'level': level, 'chunk_size': chunk,
                  'at_target': at_target},
        )
    if _have(buf, 0, 4) and _u32le(buf, 0) == 0xB10BC001:
        return _result('GC', 'GCZ', None, detail='GCZ compressed image',
                       conversions=_disc_conversions('GC', 'gcz'))
    return None


def _rvz_system(buf, fh):
    """RVZ/WIA copy the first disc bytes into their header, so the platform
    magic is still readable without decompressing anything."""
    head = _at(buf, 0x58, 0x80)
    if len(head) >= 0x20:
        if _u32be(head, 0x18) == WII_MAGIC:
            return 'WII'
        if _u32be(head, 0x1C) == GC_MAGIC:
            return 'GC'
    return 'GC/WII'


def _detect_ciso(buf, path, fh):
    """CISO is used by TWO unrelated formats: PSP CSO and GameCube CISO.

    They are told apart by the field at offset 4 - PSP stores a header size of
    0x18 there, GameCube stores the block size (a power of two, >= 0x8000)."""
    if _at(buf, 0, 4) != b'CISO':
        return None
    field = _u32le(buf, 4) if _have(buf, 4, 4) else 0
    if field == 0x18:
        total = _u64le(buf, 8) if _have(buf, 8, 8) else 0
        block = _u32le(buf, 16) if _have(buf, 16, 4) else 0
        return _result('PSP', 'CSO', 'compressed',
                       detail=f'PSP CSO - {total // (1024*1024)} MiB uncompressed, '
                              f'{block} B blocks',
                       conversions=['psp:cso->iso'])
    return _result('GC', 'CISO', 'nkit',
                   detail=f'GameCube CISO, {field} B blocks',
                   conversions=_disc_conversions('GC', 'ciso'))


DISC_TARGETS = {
    'GC':  ['iso', 'ciso', 'rvz'],
    'WII': ['iso', 'wbfs', 'rvz'],
}


def _disc_conversions(system, current):
    targets = DISC_TARGETS.get(system, [])
    return [f'disc:{system.lower()}:{current}->{t}' for t in targets if t != current]


# ── Sony PSP ──────────────────────────────────────────────────────────────────

def _detect_psp(buf, path, fh):
    sig4 = _at(buf, 0, 4)
    if sig4 == b'\x00PBP':
        return _detect_pbp(buf, path, fh)
    if sig4 == b'\x7fPKG':
        # byte 7 is the package platform (1 = PS3, 2 = PSP/PS1 classic);
        # key type 2/3 (and 4 on a non-PS3 package) is PS Vita / PSM
        platform = buf[7] if _have(buf, 8, 0) else 0
        key_type = buf[0xE7] & 7 if _have(buf, 0xE8, 0) else 0
        content = _clean(_at(buf, 0x30, 36), 36)
        if key_type in (2, 3) or (key_type == 4 and platform != 1):
            return _result('VITA', 'PKG', 'encrypted', confidence='high',
                           detail=f'PS Vita / PSM package {content}',
                           conversions=['vita:pkg->decrypted', 'vita:pkg->nonpdrm'])
        system = 'PS3' if platform == 1 else 'PSP'
        return _result(system, 'PKG', 'encrypted', confidence='high',
                       detail=f'PSN package {content}',
                       conversions=['psp:pkg->decrypted'])
    if sig4 == b'DAX\x00':
        return _result('PSP', 'DAX', 'compressed', detail='DAX compressed ISO',
                       conversions=['psp:dax->iso'])
    if sig4 == b'JISO':
        return _result('PSP', 'JSO', 'compressed', detail='JSO compressed ISO',
                       conversions=['psp:jso->iso'])
    # Plain UMD ISO: ISO9660 descriptor at 0x8001, PSP volume name in the PVD.
    if _read_at(buf, fh, 0x8001, 5) == b'CD001':
        vol = _read_at(buf, fh, 0x8028, 32).split(b'\x00')[0].strip()
        up = vol.upper()
        # Every PSP disc sets the system identifier (0x8008) to "PSP GAME";
        # the volume name is the game's own label ("SCEE", "FULL_AUTO_2", or
        # blank), so checking only that missed all 8 PSN ISOs in testing.
        system_id = _read_at(buf, fh, 0x8008, 32).upper()
        is_psp = b'PSP GAME' in system_id or b'PSP' in up or b'UMD' in up
        return _result('PSP' if is_psp else 'ISO9660', 'ISO',
                       'plain' if is_psp else None,
                       confidence='high' if is_psp else 'medium',
                       detail=f'ISO9660 - volume "{_clean(vol, 32)}"',
                       conversions=_iso_conversions(is_psp, _ps3_iso(buf, fh)))
    return None


def _detect_pbp(buf, path, fh):
    """PBP is a table of eight section offsets starting at 0x08. The PSAR
    section tells an encrypted PSN game from a plain homebrew EBOOT."""
    if not _have(buf, 0x28, 4):
        return None
    psar_off = _u32le(buf, 0x24)
    kind = _read_at(buf, fh, psar_off, 8)
    if kind == b'NPUMDIMG':
        return _result('PSP', 'EBOOT.PBP', 'encrypted', confidence='high',
                       detail='PSN PSP game (NPUMDIMG)',
                       conversions=['psp:pbp->iso'])
    if kind == b'PSISOIMG':
        return _result('PSX', 'EBOOT.PBP', 'encrypted', confidence='high',
                       detail='PS one Classic (PSISOIMG)',
                       conversions=['psx:pbp->bin'])
    if kind == b'PSTITLEI':
        return _result('PSX', 'EBOOT.PBP', 'encrypted', confidence='high',
                       detail='PS one Classic, multi-disc (PSTITLEIMG)',
                       conversions=['psx:pbp->bin'])
    return _result('PSP', 'PBP', 'plain', confidence='medium',
                   detail='PBP container (homebrew or unencrypted)',
                   conversions=[])


# ── archives (recognised, deferred) ───────────────────────────────────────────

ARCHIVE_SIGS = [
    (b'PK\x03\x04', 'ZIP'), (b'PK\x05\x06', 'ZIP'),
    (b'7z\xbc\xaf\x27\x1c', '7z'), (b'Rar!\x1a\x07', 'RAR'),
    (b'\xfd7zXZ', 'XZ'), (b'\x1f\x8b', 'GZIP'),
]


def _detect_archive(buf, path, fh):
    for sig, name in ARCHIVE_SIGS:
        if buf.startswith(sig):
            return _result('ARCHIVE', name, None, confidence='high',
                           detail=f'{name} archive - contents not inspected',
                           conversions=[],
                           notes=['Looking inside archives is planned; for now, '
                                  'extract first.'])
    return None


# ══════════════════════════════════════════════════════════════════════════════
#  SCAN DRIVER
# ══════════════════════════════════════════════════════════════════════════════

def _read_at(buf, fh, offset, length):
    """Bytes at an absolute offset, served from the head block when possible.

    Falls back to a seek only when the header pointed outside the 64 KiB we
    already hold, which keeps a scan to roughly one read per file."""
    if offset < 0 or length <= 0:
        return b''
    if _have(buf, offset, length):
        return buf[offset:offset + length]
    if offset + length > fh['size']:
        return b''
    fp = fh.get('fp')
    if fp is None:
        return b''
    try:
        fp.seek(offset)
        return fp.read(length)
    except Exception:
        return b''


# Order matters: unambiguous magics first, structural guesses last.
DETECTORS = [
    _detect_archive,
    _detect_rvz,
    _detect_wbfs,
    _detect_ciso,
    _detect_n64,
    _detect_nes,
    _detect_3ds,
    _detect_disc,
    _detect_nds,
    _detect_psp,
]

# Folders whose name tells us what the contents are, for the formats that
# genuinely have no signature. Matched case-insensitively against any parent.
FOLDER_HINTS = {
    'NES': 'NES', 'FAMICOM': 'NES', 'FDS': 'FDS',
    'NDS': 'NDS', 'DS': 'NDS', '3DS': '3DS',
    'WII': 'WII', 'GC': 'GC', 'GAMECUBE': 'GC', 'NGC': 'GC',
    'N64': 'N64', 'PSP': 'PSP',
}


# Descriptive folder names (No-Intro / Redump style) name the system inside a
# longer string. Checked in order, so SUPER NINTENDO wins before the plain NES
# phrase it contains.
FOLDER_PHRASES = (
    ('SUPER NINTENDO', 'SNES'), ('SUPER FAMICOM', 'SNES'),
    ('DISK SYSTEM', 'FDS'), ('NINTENDO ENTERTAINMENT SYSTEM', 'NES'),
    ('FAMILY COMPUTER', 'NES'),
    ('FAMICOM', 'NES'),
)


def folder_hint(path):
    """Nearest parent folder that names a system, or None."""
    for parent in path.parents:
        name = parent.name.strip().upper()
        hint = FOLDER_HINTS.get(name)
        if hint:
            return hint
        for phrase, system in FOLDER_PHRASES:
            if phrase in name:
                return system
    return None


def identify(path):
    """Identify one file by content. Never raises; unreadable files come back
    as a result with system 'ERROR' so a scan of 250 TB cannot be derailed by
    one bad sector or a locked file."""
    path = Path(path)
    try:
        size = path.stat().st_size
    except OSError as e:
        return _unknown(path, 0, f'cannot stat: {e}', system='ERROR')
    if size == 0:
        return _unknown(path, 0, 'empty file')

    try:
        with open(path, 'rb') as fp:
            buf = fp.read(HEAD_BYTES)
            fh = {'size': size, 'fp': fp, 'path': path}
            for detector in DETECTORS:
                try:
                    hit = detector(buf, path, fh)
                except Exception as e:
                    hit = None
                    _detector_failed(detector, path, e)
                if hit:
                    return _finish(hit, path, size)
            # No signature matched. Only now may a hint speak.
            hint = folder_hint(path)
            for loose in (_detect_snes_loose, _detect_nes_headerless):
                hit = loose(buf, path, fh, hint)
                if hit:
                    return _finish(hit, path, size)
    except OSError as e:
        return _unknown(path, size, f'cannot read: {e}', system='ERROR')

    return _unknown(path, size, 'no known signature')


def _finish(hit, path, size):
    hit['path'] = str(path)
    hit['name'] = path.name
    hit['size'] = size
    return hit


def _unknown(path, size, why, system='UNKNOWN'):
    r = _result(system, 'unknown', None, confidence='none', detail=why)
    return _finish(r, Path(path), size)


_DETECTOR_FAILURES = []


def _detector_failed(detector, path, exc):
    """Record, do not raise. A detector bug must degrade to 'unknown' for that
    one file rather than abort the scan, but it must not vanish silently."""
    _DETECTOR_FAILURES.append({
        'detector': getattr(detector, '__name__', str(detector)),
        'path': str(path), 'error': f'{type(exc).__name__}: {exc}',
    })


def detector_failures():
    return list(_DETECTOR_FAILURES)


def scan_folder(folder, recursive=True, progress=None, should_stop=None,
                skip_exts=None):
    """Identify every file under folder. Returns a list of results.

    progress(done, total, current_path) is called as it goes; should_stop() is
    polled so the GUI can cancel a long scan."""
    root = Path(folder)
    if not root.is_dir():
        raise NotADirectoryError(folder)
    skip = {e.lower() for e in (skip_exts or [])}

    files = []
    walker = os.walk(root) if recursive else [(str(root), [], os.listdir(root))]
    for dirpath, _dirs, names in walker:
        for n in names:
            p = Path(dirpath) / n
            if skip and p.suffix.lower() in skip:
                continue
            files.append(p)
        if should_stop and should_stop():
            break

    out = []
    total = len(files)
    for i, p in enumerate(files, 1):
        if should_stop and should_stop():
            break
        if p.is_file():
            out.append(identify(p))
        if progress:
            progress(i, total, str(p))
    return out


# ══════════════════════════════════════════════════════════════════════════════
#  CONVERSION ENGINES
#
#  Every conversion declares which engine backs it:
#    native   - implemented here, no external dependency
#    keyed    - implemented here, but blocked until the user supplies a key file
#    external - needs an executable in apps/romtools/
#
#  A conversion whose engine is unavailable is still LISTED, with the reason, so
#  the GUI can show why it is greyed out rather than silently omitting it.
# ══════════════════════════════════════════════════════════════════════════════

import zlib
import hashlib
import tempfile

CHUNK = 8 * 1024 * 1024

ENGINE_NATIVE   = 'native'
ENGINE_KEYED    = 'keyed'
ENGINE_EXTERNAL = 'external'


class ConversionError(Exception):
    pass


def _sha1_file(path, progress=None, label=''):
    h = hashlib.sha1()
    total = os.path.getsize(path)
    done = 0
    with open(path, 'rb') as f:
        while True:
            b = f.read(CHUNK)
            if not b:
                break
            h.update(b)
            done += len(b)
            if progress:
                progress(done, total, label)
    return h.hexdigest()


# ── N64 byte-order ────────────────────────────────────────────────────────────
# All three orders are reversible permutations of the same bytes, so every pair
# is supported and a round trip is exact by construction.

_N64_SWAPPERS = {
    ('big-endian', 'byteswapped'):   _swap16,
    ('byteswapped', 'big-endian'):   _swap16,
    ('big-endian', 'little-endian'): _swap32,
    ('little-endian', 'big-endian'): _swap32,
    ('byteswapped', 'little-endian'): lambda d: _swap32(_swap16(d)),
    ('little-endian', 'byteswapped'): lambda d: _swap16(_swap32(d)),
}

_N64_EXT = {'big-endian': '.z64', 'byteswapped': '.v64', 'little-endian': '.n64'}


def convert_n64(src, dst, src_order, dst_order, progress=None):
    """Stream a byte-order change in 8 MiB chunks.

    Chunks are aligned to 4 bytes so a swap never straddles a boundary; an N64
    ROM whose length is not a multiple of 4 keeps its tail bytes unswapped,
    which is what every other N64 tool does."""
    fn = _N64_SWAPPERS.get((src_order, dst_order))
    if fn is None:
        raise ConversionError(f'no N64 path from {src_order} to {dst_order}')
    total = os.path.getsize(src)
    done = 0
    with open(src, 'rb') as fi, open(dst, 'wb') as fo:
        while True:
            b = fi.read(CHUNK)
            if not b:
                break
            fo.write(fn(b))
            done += len(b)
            if progress:
                progress(done, total, 'converting')
    return dst


# ── NES header ────────────────────────────────────────────────────────────────

def strip_nes_header(src, dst, progress=None):
    """Drop the 16-byte iNES header. The header itself is returned so a caller
    can put it back byte-for-byte during verification."""
    total = os.path.getsize(src)
    with open(src, 'rb') as fi, open(dst, 'wb') as fo:
        header = fi.read(16)
        if header[:4] != b'NES\x1a':
            raise ConversionError('not an iNES file')
        done = 0
        while True:
            b = fi.read(CHUNK)
            if not b:
                break
            fo.write(b)
            done += len(b)
            if progress:
                progress(done, total, 'stripping')
    return header


def add_nes_header(src, dst, header, progress=None):
    """Prepend a KNOWN-GOOD 16-byte header. Never synthesises one: mapper,
    mirroring and PRG/CHR split are not derivable from the ROM body, so the
    header must come from a DAT match or from a strip we performed ourselves."""
    if len(header) != 16 or header[:4] != b'NES\x1a':
        raise ConversionError('refusing to write an invalid iNES header')
    total = os.path.getsize(src)
    with open(src, 'rb') as fi, open(dst, 'wb') as fo:
        fo.write(header)
        done = 0
        while True:
            b = fi.read(CHUNK)
            if not b:
                break
            fo.write(b)
            done += len(b)
            if progress:
                progress(done, total, 'adding header')
    return dst


# ── NES 2.0 header database ───────────────────────────────────────────────────
# nes20db.xml (NewRisingSun) records, for every known dump, the SHA-1 of the
# headerless ROM plus every field an NES 2.0 header carries. That is exactly the
# information a headerless dump lacks, so it turns "add header" from impossible
# into a lookup. No-Intro's headered set uses the same database, which is what
# lets the output be checked against the Headered DAT.

_NES20DB = {}


def load_nes20db(path=None):
    """{rom sha1 (lower): <game> element}. Cached per path+mtime."""
    import xml.etree.ElementTree as ET
    path = Path(path) if path else _key_path('nes20db')
    if not path or not Path(path).exists():
        raise ConversionError(
            f'adding an NES header needs nes20db.xml in {KEYS_DIR}')
    key = (str(path), Path(path).stat().st_mtime)
    if key not in _NES20DB:
        games = {}
        for game in ET.parse(path).getroot().iter('game'):
            rom = game.find('rom')
            if rom is not None and rom.get('sha1'):
                games[rom.get('sha1').lower()] = game
        _NES20DB.clear()
        _NES20DB[key] = games
    return _NES20DB[key]


def _nes2_rom_size_bytes(size, unit):
    """(lsb, msb_nibble) for a PRG/CHR ROM size, using exponent-multiplier
    notation when the size is not a whole number of units that fits 12 bits."""
    if size % unit == 0 and size // unit <= 0xEFF:
        n = size // unit
        return n & 0xFF, n >> 8
    for mm in range(4):
        odd = 2 * mm + 1
        if size % odd == 0:
            e = (size // odd).bit_length() - 1
            if (1 << e) * odd == size and e < 64:
                return (e << 2) | mm, 0xF
    raise ConversionError(f'ROM size {size} cannot be expressed in an NES 2.0 header')


def _nes2_shift(size):
    """RAM/NVRAM size nibble: 0 for none, else log2(size / 64)."""
    if not size:
        return 0
    shift = (size // 64).bit_length() - 1
    if 64 << shift != size or not 1 <= shift <= 15:
        raise ConversionError(f'RAM size {size} is not 64 << n')
    return shift


def build_nes20_header(game):
    """The 16-byte NES 2.0 header described by one nes20db <game>."""
    def size(tag):
        el = game.find(tag)
        return int(el.get('size')) if el is not None else 0

    pcb = game.find('pcb').attrib
    console = game.find('console').attrib
    mapper, sub = int(pcb.get('mapper', 0)), int(pcb.get('submapper', 0))
    ctype = int(console.get('type', 0))
    prg_lsb, prg_msb = _nes2_rom_size_bytes(size('prgrom'), 16384)
    chr_lsb, chr_msb = _nes2_rom_size_bytes(size('chrrom'), 8192)

    mirroring = pcb.get('mirroring', 'H')
    flags6 = (mapper & 0x0F) << 4
    flags6 |= {'H': 0x00, 'V': 0x01, '4': 0x08, '1': 0x08}.get(mirroring, 0)
    if mirroring == '1':            # four-screen with the vertical bit also set
        flags6 |= 0x01
    if pcb.get('battery') == '1':
        flags6 |= 0x02
    if game.find('trainer') is not None:
        flags6 |= 0x04

    flags7 = ((mapper >> 4) & 0x0F) << 4 | 0x08 | (ctype if ctype < 3 else 3)
    byte13 = 0
    vs = game.find('vs')
    if ctype == 1 and vs is not None:
        byte13 = int(vs.get('ppu', 0)) & 0x0F | (int(vs.get('hardware', 0)) & 0x0F) << 4
    elif ctype >= 3:
        byte13 = ctype & 0x0F
    misc = game.find('miscrom')

    return bytes([
        0x4E, 0x45, 0x53, 0x1A, prg_lsb, chr_lsb, flags6, flags7,
        (mapper >> 8) & 0x0F | (sub & 0x0F) << 4,
        prg_msb | chr_msb << 4,
        _nes2_shift(size('prgram')) | _nes2_shift(size('prgnvram')) << 4,
        _nes2_shift(size('chrram')) | _nes2_shift(size('chrnvram')) << 4,
        int(console.get('region', 0)) & 0x03,
        byte13,
        int(misc.get('number', 0)) & 0x03 if misc is not None else 0,
        int(game.find('expansion').get('type', 0)) & 0x3F,
    ])


# No-Intro's (Headered) NES DATs carry each dump's 16-byte header as a
# header="4E 45 53 1A ..." attribute, alongside the SHA-1 of the whole headered
# file. That is a far more current source than nes20db (last public release
# 2021-12-25), and it is self-verifying: a candidate header is only accepted
# when SHA-1(header + ROM body) equals the DAT's hash, so a wrong header can
# never be written.
NES_HEADER_DAT_DIR = None       # default: KEYS_DIR / 'nes_header_dats'
NES_HEADER_DATS = []          # extra DAT paths (tests / GUI may add to this)
_NES_DAT_CACHE = {}


def load_nes_header_dats(paths=None):
    """{body size: {header bytes: {sha1 of headered file: game name}}}"""
    import xml.etree.ElementTree as ET
    if paths is None:
        folder = NES_HEADER_DAT_DIR or KEYS_DIR / 'nes_header_dats'
        paths = sorted(folder.glob('*.dat')) if folder.is_dir() else []
        paths += [Path(x) for x in NES_HEADER_DATS]
    paths = [Path(x) for x in paths if Path(x).is_file()]
    key = tuple((str(x), x.stat().st_mtime) for x in paths)
    if key not in _NES_DAT_CACHE:
        table = {}
        for path in paths:
            game = None
            for event, el in ET.iterparse(path, events=('start', 'end')):
                if event == 'start' and el.tag == 'game':
                    game = el.get('name')
                elif event == 'end' and el.tag == 'rom':
                    header, sha1, size = el.get('header'), el.get('sha1'), el.get('size')
                    if header and sha1 and size:
                        h = bytes.fromhex(header.replace(' ', ''))
                        if len(h) == 16 and h[:4] == b'NES\x1a':
                            table.setdefault(int(size) - 16, {}).setdefault(h, {})[
                                sha1.lower()] = game
                elif event == 'end' and el.tag == 'game':
                    el.clear()
        _NES_DAT_CACHE.clear()
        _NES_DAT_CACHE[key] = table
    return _NES_DAT_CACHE[key]


def _nes_header_from_dats(src, table):
    """(header, game) proven by the headered file's SHA-1, or (None, None)."""
    size = os.path.getsize(src)
    candidates = table.get(size)
    if not candidates:
        return None, None
    body = Path(src).read_bytes()
    for header, hashes in candidates.items():
        digest = hashlib.sha1(header + body).hexdigest()
        if digest in hashes:
            return header, hashes[digest]
    return None, None


def nes_header_from_db(src, dst, progress=None, db=None):
    """Headerless NES -> headered. No-Intro DAT headers first (current and
    hash-verified), nes20db as the fallback for dumps no DAT lists."""
    table = load_nes_header_dats()
    if table:
        header, game = _nes_header_from_dats(src, table)
        if header:
            add_nes_header(src, dst, header, progress)
            return {'header': header.hex(), 'source': f'No-Intro DAT: {game}'}
    if db is None:
        try:
            db = load_nes20db()
        except ConversionError:
            db = None
    game = db.get(_sha1_file(src, progress, 'hashing')) if db else None
    if game is None:
        raise ConversionError(
            'this ROM is in neither the No-Intro headered DATs nor nes20db.xml, '
            'so its header cannot be known')
    header = build_nes20_header(game)
    add_nes_header(src, dst, header, progress)
    return {'header': header.hex(), 'source': 'nes20db.xml (2021 data - not DAT verified)'}


# ── PSP CSO (compressed ISO) ──────────────────────────────────────────────────
# CSO is an index of raw-deflate blocks. Lossless and fully reversible, and it
# needs no keys at all - which is why it works today while the PSN crypto tiers
# wait on key material.

CSO_MAGIC = b'CISO'
CSO_HEADER_SIZE = 0x18


def _pack_blocks(src, dst, magic, block_size, compress, progress=None,
                 label='compressing'):
    """Write a CSO/ZSO-style container: header, block index, then blocks.

    A block whose compressed form is no smaller is stored raw and flagged with
    the index high bit, so the container never grows a block."""
    total = os.path.getsize(src)
    blocks = (total + block_size - 1) // block_size
    # Index entries are 31-bit offsets shifted left by `align`; an image whose
    # container could pass 2 GiB needs a shift, and every block then starts on
    # a multiple of 1 << align (the format's rule, as maxcso writes it).
    worst = CSO_HEADER_SIZE + 4 * (blocks + 1) + total + blocks
    align = 0
    while (worst >> align) >= 0x80000000:
        align += 1
    index = []
    with open(src, 'rb') as fi, open(dst, 'wb') as fo:
        fo.write(magic)
        fo.write(struct.pack('<I', CSO_HEADER_SIZE))
        fo.write(struct.pack('<Q', total))
        fo.write(struct.pack('<I', block_size))
        fo.write(bytes([1, align, 0, 0]))             # version 1, index shift
        fo.write(bytes(4 * (blocks + 1)))            # index placeholder

        def here():
            pos = fo.tell()
            pad = -pos % (1 << align)
            if pad:
                fo.write(bytes(pad))
            return (pos + pad) >> align

        for i in range(blocks):
            raw = fi.read(block_size)
            if not raw:
                break
            packed = compress(raw)
            pos = here()
            if len(packed) >= len(raw):
                index.append(pos | 0x80000000)
                fo.write(raw)
            else:
                index.append(pos)
                fo.write(packed)
            if progress and (i % 512 == 0):
                progress(i * block_size, total, label)
        index.append(here())
        fo.seek(CSO_HEADER_SIZE)
        fo.write(struct.pack(f'<{len(index)}I', *index))
    return dst


def _unpack_blocks(src, dst, magic, decompress, progress=None,
                   label='decompressing'):
    with open(src, 'rb') as fi:
        head = fi.read(CSO_HEADER_SIZE)
        if head[:4] != magic:
            raise ConversionError(
                f'not a {magic.decode()} file (found {head[:4]!r})')
        total = struct.unpack_from('<Q', head, 8)[0]
        block_size = struct.unpack_from('<I', head, 16)[0]
        align = head[21]
        if block_size == 0:
            raise ConversionError('header declares a zero block size')
        blocks = (total + block_size - 1) // block_size
        # Check the index fits before unpacking it, so a truncated or
        # mislabelled file gives a clear message rather than a struct error.
        need = CSO_HEADER_SIZE + 4 * (blocks + 1)
        have = os.path.getsize(src)
        if have < need:
            raise ConversionError(
                f'index is truncated: header declares {total:,} bytes '
                f'({blocks:,} blocks, needing {need:,} bytes of index) but the '
                f'file is only {have:,} bytes')
        index = struct.unpack(f'<{blocks + 1}I', fi.read(4 * (blocks + 1)))
        with open(dst, 'wb') as fo:
            written = 0
            for i in range(blocks):
                entry = index[i]
                plain = bool(entry & 0x80000000)
                start = (entry & 0x7FFFFFFF) << align
                stop = (index[i + 1] & 0x7FFFFFFF) << align
                fi.seek(start)
                chunk = fi.read(max(stop - start, 0) or block_size)
                want = min(block_size, total - written)
                out = chunk[:block_size] if plain else decompress(chunk, want)
                # The last block is padded inside the container; the image must
                # stop at the recorded total or the round trip differs.
                if written + len(out) > total:
                    out = out[:total - written]
                fo.write(out)
                written += len(out)
                if progress and (i % 512 == 0):
                    progress(written, total, label)
    return dst


def iso_to_cso(src, dst, block_size=2048, level=9, progress=None):
    return _pack_blocks(src, dst, CSO_MAGIC, block_size,
                        lambda raw: zlib.compress(raw, level)[2:-4], progress)


def cso_to_iso(src, dst, progress=None):
    return _unpack_blocks(src, dst, CSO_MAGIC,
                          lambda blob, n: zlib.decompress(blob, -15), progress)


# ══════════════════════════════════════════════════════════════════════════════
#  CONVERSION REGISTRY
# ══════════════════════════════════════════════════════════════════════════════

HERE = Path(__file__).resolve().parent
ROMTOOLS_DIR = HERE / 'apps' / 'romtools'
KEYS_DIR = ROMTOOLS_DIR / 'keys'

# Key files the keyed engines need. These are console dumps and are NOT
# redistributable - the user supplies them once and they live here.
KEY_FILES = {
    'boot9':    (KEYS_DIR / 'boot9.bin',
                 '3DS - boot9.bin (or boot9_prot.bin), dumped from a 3DS'),
    'nds_blow': (KEYS_DIR / 'bios7.bin',
                 'NDS - the DS ARM7 BIOS (bios7.bin); the KEY1 table is read '
                 'from offset 0x30 automatically'),
    'aes_keys': (KEYS_DIR / 'aes_keys.txt',
                 '3DS - aes_keys.txt; supplies the 7.x (0x25) and New 3DS '
                 '(0x18, 0x1B) KeyX that boot9 does not contain'),
    'nes20db':  (KEYS_DIR / 'nes20db.xml',
                 'NES - nes20db.xml (NES 2.0 header database); lets a '
                 'headerless ROM get its real header back'),
    'seeddb':   (KEYS_DIR / 'seeddb.bin',
                 '3DS - seeddb.bin; only titles that use seed crypto need it'),
}

EXTERNAL_TOOLS = {
    'dolphintool': (ROMTOOLS_DIR / 'DolphinTool.exe',
                    'DolphinTool.exe, from the official Dolphin release archive'),
    'nkit':        (ROMTOOLS_DIR / 'nkit.exe',
                    'nkit.exe, from the official NKit release archive'),
}

# What each tool ACTUALLY accepts, read off its own CLI rather than assumed:
#   DolphinTool convert -f : iso, gcz, wia, rvz      (no wbfs, no ciso)
#   NKit        task=convert: rvz, wbfs, wux, deciso (no ciso; CISO was dropped
#                             in NKit 2.x), task=expand restores a full ISO.
# CISO is therefore handled natively below - it is a simple block-map container
# and needs no external tool at all.
DOLPHIN_FORMATS = ('iso', 'gcz', 'wia', 'rvz')
NKIT_FORMATS = ('rvz', 'wbfs', 'iso')


def _key_path(name):
    p = KEY_FILES[name][0]
    return p if p.exists() else None


def _tool_path(name):
    p = EXTERNAL_TOOLS[name][0]
    return p if p.exists() else None


def _label_n64(a, b):
    return f'N64: {a} -> {b}'


def _build_registry():
    """id -> spec. Built rather than typed out because the N64 and disc families
    are complete graphs and writing them by hand invites a wrong pair."""
    reg = {}

    for a in ('big-endian', 'byteswapped', 'little-endian'):
        for b in ('big-endian', 'byteswapped', 'little-endian'):
            if a == b:
                continue
            reg[f'n64:{a}->{b}'] = {
                'label': _label_n64(a, b), 'engine': ENGINE_NATIVE,
                'system': 'N64', 'ext': _N64_EXT[b],
                'fn': lambda s, d, p=None, _a=a, _b=b: convert_n64(s, d, _a, _b, p),
                'inverse': f'n64:{b}->{a}',
            }

    reg['nes:headered->headerless'] = {
        'label': 'NES: strip iNES header', 'engine': ENGINE_NATIVE,
        'system': 'NES', 'ext': '.nes',
        'fn': lambda s, d, p=None: strip_nes_header(s, d, p),
        'inverse': None,      # verified against the header we just removed
        'rebuild': lambda out, tmp, extra, p=None: add_nes_header(
            out, tmp, bytes(extra), p),
    }
    reg['nes:headerless->headered'] = {
        'label': 'NES: add header (No-Intro DAT, else nes20db)',
        'engine': ENGINE_NATIVE, 'system': 'NES', 'ext': '.nes',
        'fn': lambda s, d, p=None: nes_header_from_db(s, d, p),
        'inverse': 'nes:headered->headerless',
        'why': 'The 16-byte header is not derivable from the ROM body; the '
               'No-Intro headered DATs in keys/nes_header_dats (or nes20db.xml) '
               'supply it by hash.',
    }

    reg['psp:iso->cso'] = {
        'label': 'PSP: ISO -> CSO (compress)', 'engine': ENGINE_NATIVE,
        'system': 'PSP', 'ext': '.cso',
        'fn': lambda s, d, p=None: iso_to_cso(s, d, progress=p),
        'inverse': 'psp:cso->iso',
    }
    reg['psp:cso->iso'] = {
        'label': 'PSP: CSO -> ISO (decompress)', 'engine': ENGINE_NATIVE,
        'system': 'PSP', 'ext': '.iso',
        'fn': lambda s, d, p=None: cso_to_iso(s, d, p),
        'inverse': 'psp:iso->cso',
    }

    for cid, label, need in (
        ('nds:encrypted->decrypted', 'NDS: decrypt Secure Area', 'nds_blow'),
        ('nds:decrypted->encrypted', 'NDS: encrypt Secure Area', 'nds_blow'),
        ('3ds:encrypted->decrypted', '3DS: decrypt NCCH', 'boot9'),
        ('3ds:decrypted->encrypted', '3DS: encrypt NCCH', 'boot9'),
    ):
        reg[cid] = {
            'label': label, 'engine': ENGINE_KEYED,
            'system': cid.split(':')[0].upper(), 'ext': '.nds' if 'nds' in cid
                      else None, 'fn': None,
            'requires': need, 'why': KEY_FILES[need][1],
        }

    for system in ('gc', 'wii'):
        for a in DISC_TARGETS[system.upper()]:
            for b in DISC_TARGETS[system.upper()]:
                if a == b:
                    continue
                reg[f'disc:{system}:{a}->{b}'] = _disc_spec(system, a, b)
        reg[f'disc:{system}:rvz->rvz'] = _disc_spec(system, 'rvz', 'rvz',
                                                    recompress=True)
    return reg


def _disc_spec(system, a, b, recompress=False):
    tool = 'dolphintool' if 'rvz' in (a, b) else 'nkit'
    verb = 'recompress to zstd-19-128k' if recompress else f'{a.upper()} -> {b.upper()}'
    return {
        'label': f'{system.upper()}: {verb}',
        'engine': ENGINE_EXTERNAL, 'system': system.upper(), 'ext': f'.{b}',
        'fn': None, 'requires': tool, 'why': EXTERNAL_TOOLS[tool][1],
        'inverse': f'disc:{system}:{b}->{a}' if not recompress else None,
    }


CONVERSIONS = _build_registry()


def conversion_status(conv_id):
    """Whether a conversion can run right now, and if not, why not."""
    spec = CONVERSIONS.get(conv_id)
    if not spec:
        return {'available': False, 'reason': 'unknown conversion'}
    # A present key is not enough: the engine behind it has to exist too, or a
    # conversion would advertise itself and then crash when run.
    if spec.get('fn') is None:
        need = spec.get('requires')
        have = (need in KEY_FILES and _key_path(need)) or \
               (need in EXTERNAL_TOOLS and _tool_path(need))
        # Distinguish "you are missing something" from "I have not written this
        # yet" - blaming a key the user has already supplied is worse than
        # saying nothing.
        return {'available': False,
                'reason': ('engine not implemented yet' if have
                           else spec.get('why', 'not implemented yet'))}
    engine = spec['engine']
    if engine == ENGINE_NATIVE:
        return {'available': True, 'reason': ''}
    need = spec.get('requires')
    if engine == ENGINE_EXTERNAL:
        ok = _tool_path(need) is not None
        return {'available': ok,
                'reason': '' if ok else f'missing tool: {spec["why"]}'}
    if need in KEY_FILES:
        ok = _key_path(need) is not None
        return {'available': ok,
                'reason': '' if ok else f'missing key: {spec["why"]}'}
    return {'available': False, 'reason': spec.get('why', 'not implemented')}


def describe_conversions(conv_ids):
    """Decorate a detector's conversion ids with label and availability."""
    out = []
    for cid in conv_ids:
        spec = CONVERSIONS.get(cid)
        st = conversion_status(cid)
        out.append({
            'id': cid,
            'label': spec['label'] if spec else cid,
            'engine': spec['engine'] if spec else 'unknown',
            'available': st['available'],
            'reason': st['reason'],
        })
    return out


# ══════════════════════════════════════════════════════════════════════════════
#  RUNNER
# ══════════════════════════════════════════════════════════════════════════════

def output_path(src, conv_id, src_root, out_root):
    """Mirror the source tree under out_root. Sources are NEVER written to."""
    src, src_root, out_root = Path(src), Path(src_root), Path(out_root)
    try:
        rel = src.relative_to(src_root)
    except ValueError:
        rel = Path(src.name)
    ext = (CONVERSIONS.get(conv_id) or {}).get('ext')
    dest = out_root / rel
    if ext:
        dest = dest.with_suffix(ext)
    if dest.resolve() == src.resolve():
        dest = dest.with_name(dest.stem + '_converted' + dest.suffix)
    return dest


def run_conversion(item, conv_id, src_root, out_root, verify=True,
                   progress=None, overwrite=False):
    """Convert one detected file. Returns a result dict; never raises.

    Guarantees: the source file is opened read-only and never deleted; output is
    written to a .part file and renamed only after conversion (and verification)
    succeed, so an interrupted run cannot leave a plausible-looking half file."""
    spec = CONVERSIONS.get(conv_id)
    src = Path(item['path'])
    started = time.time()
    base = {'path': str(src), 'conversion': conv_id,
            'label': spec['label'] if spec else conv_id}

    st = conversion_status(conv_id)
    if not st['available']:
        return dict(base, ok=False, skipped=True, error=st['reason'])

    dest = output_path(src, conv_id, src_root, out_root)
    if dest.exists() and not overwrite:
        return dict(base, ok=False, skipped=True, output=str(dest),
                    error='output already exists')
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + '.part')

    try:
        extra = spec['fn'](src, part, progress)
        result = dict(base, output=str(dest))
        if verify:
            vr = _verify(src, part, conv_id, extra, progress)
            result.update(vr)
            # None means "nothing could check this", which is not a failure:
            # deleting on it threw away every 3DS decrypt and NDS untrim, even
            # though those engines run their own plaintext checks.
            if vr['verified'] is False:
                part.unlink(missing_ok=True)
                return dict(result, ok=False,
                            error=f'verification failed: {vr["detail"]}')
        if dest.exists() and overwrite:
            dest.unlink()
        part.rename(dest)
        # A conversion that emits companion files (chdman writes a sheet plus
        # its track binary) reports them so the log can name what was produced.
        if isinstance(extra, dict) and extra.get('sidecars'):
            result['sidecars'] = extra['sidecars']
        return dict(result, ok=True, seconds=round(time.time() - started, 2),
                    out_size=dest.stat().st_size)
    except Exception as e:
        part.unlink(missing_ok=True)
        return dict(base, ok=False, error=f'{type(e).__name__}: {e}')


def _verify(src, out, conv_id, extra, progress=None):
    """Round-trip check: convert the output back and compare to the source.

    Where a conversion is not invertible on its own (NES header strip), the
    missing piece we removed is fed back in, which is still a true byte-for-byte
    comparison against the original."""
    spec = CONVERSIONS[conv_id]

    # Some containers cannot be compared byte-for-byte after a round trip (a
    # rebuilt CHD differs from the original even though its payload matches),
    # so those declare verify_mode='tool' and are checked by their own verifier.
    mode = spec.get('verify_mode')
    if mode == 'none':
        return {'verified': None, 'detail': spec.get(
            'note', 'this conversion has no exact inverse to check against')}
    if mode == 'verifier':
        ok, why = spec['verifier'](src, out, progress)
        return {'verified': ok, 'detail': why}      # ok may be None: unprovable
    if mode == 'payload':
        work = Path(tempfile.mkdtemp(prefix='rompayload_'))
        try:
            a_iso, b_iso = work / 'source.iso', work / 'output.iso'
            CONVERSIONS[spec['payload_src']]['fn'](src, a_iso, progress)
            CONVERSIONS[spec['payload_out']]['fn'](out, b_iso, progress)
            ha = _sha1_file(a_iso, progress, 'hashing decoded source')
            hb = _sha1_file(b_iso, progress, 'hashing decoded output')
            return {'verified': ha == hb, 'payload_sha1': ha,
                    'detail': 'decoded disc data identical' if ha == hb
                              else 'decoded disc data differs'}
        finally:
            shutil.rmtree(work, ignore_errors=True)
    if mode == 'tool':
        ok, why = chdman_verify(out, progress)
        return {'verified': ok, 'detail': why}
    if mode == 'tool_source':
        # Extracting produces a cue/iso, which chdman cannot verify. Checking
        # the SOURCE container instead proves its hunks and CRCs are intact,
        # which is what makes the extracted payload trustworthy.
        ok, why = chdman_verify(src, progress)
        return {'verified': ok,
                'detail': ('source CHD integrity verified' if ok else why)}

    src_hash = _sha1_file(src, progress, 'verifying source')

    inv = spec.get('inverse')
    tmp = Path(tempfile.mkdtemp(prefix='romverify_')) / 'back.bin'
    try:
        rebuild = spec.get('rebuild')
        if rebuild is not None and extra is not None:
            rebuild(out, tmp, extra, progress)
        elif inv and CONVERSIONS.get(inv, {}).get('fn'):
            CONVERSIONS[inv]['fn'](out, tmp, progress)
        else:
            return {'verified': None, 'detail': 'no inverse available',
                    'src_sha1': src_hash}
        back_hash = _sha1_file(tmp, progress, 'verifying round trip')
        ok = back_hash == src_hash
        return {'verified': ok, 'src_sha1': src_hash, 'roundtrip_sha1': back_hash,
                'detail': 'byte-exact' if ok else 'round trip differs from source'}
    finally:
        try:
            tmp.unlink(missing_ok=True)
            tmp.parent.rmdir()
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════════════════
#  GUI API
# ══════════════════════════════════════════════════════════════════════════════

import threading

CONFIG_PATH = HERE / 'rom_tools.json'

DEFAULT_CONFIG = {
    'source': '',
    'output': '',
    'recursive': True,
    'verify': True,
    'overwrite': False,
    'skip_exts': ['.txt', '.nfo', '.jpg', '.png', '.sfv', '.dat', '.xml',
                  '.log', '.db', '.ini'],
}


class RomToolsAPI:
    """Backend for the ROM Manipulation window.

    Scanning and converting each run on their own worker thread so the window
    stays responsive; both poll a stop flag so a run over a large tree can be
    cancelled without leaving partial output behind."""

    def __init__(self):
        self._window = None
        self._stop = threading.Event()
        self._busy = False
        self._items = []

    def set_window(self, w):
        self._window = w

    # ── plumbing ──────────────────────────────────────────────────────────────

    def _emit(self, event, data):
        if not self._window:
            return
        try:
            payload = (json.dumps(data, ensure_ascii=True)
                       .replace('\\', '\\\\').replace("'", "\\'"))
            self._window.evaluate_js(
                f"window.romEvent('{event}', JSON.parse('{payload}'))")
        except Exception:
            pass

    def _log(self, msg, cls='info'):
        self._emit('log', {'msg': str(msg), 'cls': cls})

    def browse_folder(self):
        try:
            import tkinter as tk
            from tkinter import filedialog
            root = tk.Tk(); root.withdraw()
            root.attributes('-topmost', True)
            path = filedialog.askdirectory()
            root.destroy()
            return path or ''
        except Exception:
            return ''

    # ── config ────────────────────────────────────────────────────────────────

    def get_config(self):
        cfg = dict(DEFAULT_CONFIG)
        try:
            if CONFIG_PATH.exists():
                cfg.update(json.loads(CONFIG_PATH.read_text(encoding='utf-8')))
        except Exception:
            pass
        return cfg

    def save_config(self, cfg):
        merged = self.get_config()
        merged.update(cfg or {})
        try:
            CONFIG_PATH.write_text(json.dumps(merged, indent=2), encoding='utf-8')
            return {'ok': True}
        except Exception as e:
            return {'ok': False, 'error': str(e)}

    # ── environment report ────────────────────────────────────────────────────

    def environment(self):
        """What the module can and cannot do on this machine right now."""
        keys = [{'name': n, 'path': str(p), 'present': p.exists(), 'what': what}
                for n, (p, what) in KEY_FILES.items()]
        tools = [{'name': n, 'path': str(p), 'present': p.exists(), 'what': what}
                 for n, (p, what) in EXTERNAL_TOOLS.items()]
        by_engine = {}
        for cid, spec in CONVERSIONS.items():
            st = conversion_status(cid)
            by_engine.setdefault(spec['engine'], {'total': 0, 'available': 0})
            by_engine[spec['engine']]['total'] += 1
            by_engine[spec['engine']]['available'] += bool(st['available'])
        return {'keys': keys, 'tools': tools, 'engines': by_engine,
                'romtools_dir': str(ROMTOOLS_DIR)}

    def start_learn_headers(self, folder):
        """Teach the header library from a folder of headered dumps (zips too)."""
        if self._busy:
            return {'ok': False, 'error': 'already running'}
        if not folder or not Path(folder).is_dir():
            return {'ok': False, 'error': 'pick a folder of headered dumps first'}
        self._stop.clear()
        self._busy = True

        def work():
            try:
                self._log(f'Learning headers from {folder} ...', 'info')

                def progress(done, total, current):
                    self._emit('scan_progress', {'done': done, 'total': total, 'current': current})

                counts = learn_headers_from_folder(folder, progress, self._stop.is_set)
                for kind, c in counts.items():
                    if c['seen']:
                        self._log(f'{HEADER_KINDS[kind][4]}: {c["seen"]} headered file(s), '
                                  f'{c["new"]} new header(s) learned '
                                  f'({len(header_library(kind))} known)', 'ok')
                if not any(c['seen'] for c in counts.values()):
                    self._log('No A78, LNX or 2IMG headers found in that folder.', 'warn')
                self._emit('catalog_changed', {})
            except Exception as e:
                self._log(f'Learning headers failed: {type(e).__name__}: {e}', 'err')
            finally:
                self._busy = False
                self._emit('idle', {})

        threading.Thread(target=work, daemon=True).start()
        return {'ok': True}

    def system_catalog(self):
        """The information view: every system, what it converts, what it needs."""
        try:
            return {'ok': True, 'systems': system_catalog(),
                    'apps_dir': str(HERE / 'apps'), 'keys_dir': str(KEYS_DIR)}
        except Exception as e:
            return {'ok': False, 'error': f'{type(e).__name__}: {e}'}

    # ── scan ──────────────────────────────────────────────────────────────────

    def start_scan(self, cfg):
        if self._busy:
            return {'ok': False, 'error': 'already running'}
        self.save_config(cfg)
        self._stop.clear()
        self._busy = True
        threading.Thread(target=self._scan_thread, args=(cfg,), daemon=True).start()
        return {'ok': True}

    def _scan_thread(self, cfg):
        try:
            src = (cfg.get('source') or '').strip()
            if not src or not Path(src).is_dir():
                self._log('Pick a source folder first.', 'err')
                return
            self._log(f'Scanning {src} ...', 'info')

            def progress(done, total, current):
                if done % 25 == 0 or done == total:
                    self._emit('scan_progress',
                               {'done': done, 'total': total, 'current': current})

            items = scan_folder(src, recursive=cfg.get('recursive', True),
                                progress=progress,
                                should_stop=self._stop.is_set,
                                skip_exts=cfg.get('skip_exts'))
            # Decorate with availability so the table can grey out what cannot run.
            for it in items:
                it['options'] = describe_conversions(it['conversions'])
            self._items = items

            shown = [i for i in items if i['system'] not in ('UNKNOWN', 'ERROR')]
            self._emit('scan_done', {
                'items': items,
                'summary': self._summarise(items),
            })
            self._log(f'Scan complete — {len(items)} files, '
                      f'{len(shown)} recognised.', 'ok')
            for f in detector_failures():
                self._log(f'detector error: {f["detector"]} on {f["path"]}: '
                          f'{f["error"]}', 'warn')
        except Exception as e:
            self._log(f'Scan failed: {type(e).__name__}: {e}', 'err')
        finally:
            self._busy = False
            self._emit('idle', {})

    @staticmethod
    def _summarise(items):
        counts = {}
        for it in items:
            key = f'{it["system"]} / {it["format"]}'
            c = counts.setdefault(key, {'count': 0, 'bytes': 0, 'convertible': 0})
            c['count'] += 1
            c['bytes'] += it.get('size', 0)
            c['convertible'] += bool([o for o in it.get('options', [])
                                      if o['available']])
        return counts

    # ── convert ───────────────────────────────────────────────────────────────

    def start_convert(self, jobs, cfg):
        """jobs: [{'path': ..., 'conversion': ...}, ...]"""
        if self._busy:
            return {'ok': False, 'error': 'already running'}
        if not jobs:
            return {'ok': False, 'error': 'nothing selected'}
        out = (cfg or {}).get('output', '').strip()
        if not out:
            return {'ok': False, 'error': 'pick an output folder first'}
        self.save_config(cfg)
        self._stop.clear()
        self._busy = True
        threading.Thread(target=self._convert_thread, args=(jobs, cfg),
                         daemon=True).start()
        return {'ok': True}

    def _convert_thread(self, jobs, cfg):
        by_path = {i['path']: i for i in self._items}
        src_root = cfg.get('source', '')
        out_root = cfg.get('output', '')
        verify = bool(cfg.get('verify', True))
        overwrite = bool(cfg.get('overwrite', False))
        done = ok = failed = skipped = 0
        try:
            self._log(f'Converting {len(jobs)} job(s) into {out_root}'
                      + (' with round-trip verification' if verify else ''), 'info')
            for job in jobs:
                if self._stop.is_set():
                    self._log('Stopped by user.', 'warn')
                    break
                item = by_path.get(job['path']) or identify(job['path'])

                def progress(d, t, label, _n=Path(job['path']).name):
                    self._emit('job_progress',
                               {'name': _n, 'done': d, 'total': t, 'label': label})

                res = run_conversion(item, job['conversion'], src_root, out_root,
                                     verify=verify, progress=progress,
                                     overwrite=overwrite)
                done += 1
                if res.get('ok'):
                    ok += 1
                    v = ' [verified byte-exact]' if res.get('verified') else ''
                    self._log(f'OK   {Path(job["path"]).name} -> '
                              f'{Path(res["output"]).name}{v}', 'ok')
                elif res.get('skipped'):
                    skipped += 1
                    self._log(f'SKIP {Path(job["path"]).name}: {res.get("error")}',
                              'dim')
                else:
                    failed += 1
                    self._log(f'FAIL {Path(job["path"]).name}: {res.get("error")}',
                              'err')
                self._emit('job_done', dict(res, index=done, total=len(jobs)))
            self._log(f'Finished — {ok} converted, {failed} failed, '
                      f'{skipped} skipped.', 'ok' if not failed else 'warn')
        except Exception as e:
            self._log(f'Convert run failed: {type(e).__name__}: {e}', 'err')
        finally:
            self._busy = False
            self._emit('idle', {})

    def stop(self):
        self._stop.set()
        return {'ok': True}


# ══════════════════════════════════════════════════════════════════════════════
#  GAMECUBE CISO  (native - no external tool supports it any more)
#
#  Layout: 'CISO', u32 block size, then 0x7FF8 presence flags (1 byte each),
#  making a 0x8000 header. Present blocks follow consecutively; absent blocks
#  are all-zero and are simply not stored.
#
#  The format carries no total-length field, so a naive writer loses the exact
#  size whenever the image does not end on a block boundary. This implementation
#  always marks the FINAL block present and stores it unpadded, which makes the
#  original length recoverable from the file size - and keeps the result a
#  perfectly ordinary CISO that other readers still accept.
# ══════════════════════════════════════════════════════════════════════════════

CISO_HEADER = 0x8000
CISO_MAX_BLOCKS = 0x7FF8
CISO_DEFAULT_BLOCK = 2 * 1024 * 1024


def iso_to_ciso(src, dst, block_size=CISO_DEFAULT_BLOCK, progress=None):
    total = os.path.getsize(src)
    blocks = (total + block_size - 1) // block_size
    if blocks > CISO_MAX_BLOCKS:
        raise ConversionError(
            f'image needs {blocks:,} blocks at {block_size:,} B but CISO allows '
            f'{CISO_MAX_BLOCKS:,}; use a larger block size')
    flags = bytearray(CISO_MAX_BLOCKS)
    zero = b'\x00' * block_size

    with open(src, 'rb') as fi, open(dst, 'wb') as fo:
        fo.seek(CISO_HEADER)
        for i in range(blocks):
            chunk = fi.read(block_size)
            last = (i == blocks - 1)
            # An all-zero block is dropped - unless it is the last one, which is
            # kept so the exact image length survives the round trip.
            if chunk == zero and not last:
                continue
            flags[i] = 1
            fo.write(chunk)
            if progress and (i % 32 == 0):
                progress(i * block_size, total, 'scrubbing')
        fo.seek(0)
        fo.write(CSO_MAGIC)
        fo.write(struct.pack('<I', block_size))
        fo.write(bytes(flags))
    return dst


def ciso_to_iso(src, dst, progress=None):
    size = os.path.getsize(src)
    with open(src, 'rb') as fi:
        head = fi.read(CISO_HEADER)
        if head[:4] != CSO_MAGIC:
            raise ConversionError('not a CISO file')
        block_size = struct.unpack_from('<I', head, 4)[0]
        if block_size == 0 or size < CISO_HEADER:
            raise ConversionError('CISO header declares a zero block size')
        flags = head[8:8 + CISO_MAX_BLOCKS]
        present = [i for i, f in enumerate(flags) if f]
        if not present:
            raise ConversionError('CISO contains no data blocks')
        last = present[-1]
        stored = len(present)
        # Every stored block is full except possibly the final one, whose length
        # is whatever is left in the file.
        tail = size - CISO_HEADER - (stored - 1) * block_size
        if tail <= 0 or tail > block_size:
            raise ConversionError(
                f'CISO data length {size - CISO_HEADER:,} does not match '
                f'{stored:,} blocks of {block_size:,} B')
        zero = b'\x00' * block_size
        with open(dst, 'wb') as fo:
            for i in range(last + 1):
                if flags[i]:
                    want = tail if i == last else block_size
                    fo.write(fi.read(want))
                else:
                    fo.write(zero)
                if progress and (i % 32 == 0):
                    progress(i * block_size, (last + 1) * block_size, 'expanding')
    return dst


# ══════════════════════════════════════════════════════════════════════════════
#  EXTERNAL ENGINE  (GameCube / Wii via DolphinTool and NKit)
# ══════════════════════════════════════════════════════════════════════════════

import shutil
import subprocess

# Target RVZ configuration, matching the detector's TARGET_RVZ.
RVZ_CODEC, RVZ_LEVEL, RVZ_BLOCK = TARGET_RVZ


def _run_tool(argv, progress=None, label='converting'):
    """Run an external converter, streaming its output into the log.

    Uses CREATE_NO_WINDOW so a batch run does not flash a console per file."""
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True,
                            encoding='utf-8', errors='replace',
                            creationflags=flags)
    tail = []
    for line in proc.stdout:
        line = line.rstrip()
        if line:
            tail.append(line)
            del tail[:-40]
            if progress:
                progress(0, 0, f'{label}: {line[:70]}')
    code = proc.wait()
    if code != 0:
        raise ConversionError(
            f'{Path(argv[0]).name} exited {code}: ' + ' | '.join(tail[-4:]))
    return tail


def dolphin_convert(src, dst, fmt, progress=None):
    """DolphinTool handles iso/gcz/wia/rvz, file in -> file out."""
    exe = _tool_path('dolphintool')
    if not exe:
        raise ConversionError('DolphinTool.exe not found in apps/romtools')
    argv = [str(exe), 'convert', '-i', str(src), '-o', str(dst), '-f', fmt]
    if fmt in ('rvz', 'wia'):
        argv += ['-c', RVZ_CODEC, '-l', str(RVZ_LEVEL), '-b', str(RVZ_BLOCK)]
    _run_tool(argv, progress, f'-> {fmt}')
    return dst


def nkit_convert(src, dst, target, progress=None):
    """NKit writes into an output DIRECTORY and names the file itself, so the
    conversion runs in a temp dir and the single produced file is moved to dst.

    deleteProcessed is pinned to 'n': this module never removes a source."""
    exe = _tool_path('nkit')
    if not exe:
        raise ConversionError('nkit.exe not found in apps/romtools')
    spec = {'rvz': f'rvz:{RVZ_CODEC}:{RVZ_LEVEL}:{RVZ_BLOCK // 1024}k:4',
            'wbfs': 'wbfs:y'}.get(target)
    task = 'expand' if target == 'iso' else 'convert'
    work = Path(tempfile.mkdtemp(prefix='nkit_'))
    try:
        # NKit 2.x takes "-name value" pairs. The "name=value" form this used
        # to pass is silently ignored ("Task [NotSet]"), so every NKit
        # conversion - WBFS, WUX, PS3 deciso - failed on real files.
        opts = {'task': task, 'in': str(src), 'out': str(work), 'tmp': str(work),
                'cfg': 'n', 'v': 'n', 'deleteProcessed': 'n', 'r': 'n',
                'consoleLevel': 'info', 'results': 'n'}
        if spec:
            opts['convert'] = spec
        argv = [str(exe)]
        for k, v in opts.items():
            argv += [f'-{k}', v]
        _run_tool(argv, progress, f'-> {target}')
        made = [p for p in work.iterdir() if p.is_file()
                and p.suffix.lower() not in ('.log', '.txt', '.yaml')]
        if not made:
            raise ConversionError('NKit produced no output file')
        if len(made) > 1:
            made.sort(key=lambda p: p.stat().st_size, reverse=True)
        shutil.move(str(made[0]), str(dst))
        return dst
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _direct_fn(a, b):
    """Engine for a conversion neither end of which is CISO.

    Anything touching WBFS goes to NKit (DolphinTool cannot write it).
    Everything else prefers DolphinTool, whose file-in/file-out CLI has far
    fewer moving parts than driving NKit's directory-based workflow."""
    if 'wbfs' in (a, b):
        return (lambda s, d, p=None, _t=b: nkit_convert(s, d, _t, p)), 'nkit'
    return (lambda s, d, p=None, _f=b: dolphin_convert(s, d, _f, p)), 'dolphintool'


def _chain_via_iso(first, second, progress_label):
    """Run two conversions through a temporary ISO.

    CISO is a plain block container that no external tool reads or writes any
    more, so ciso<->rvz and ciso<->wbfs are genuinely two hops. Doing it in one
    callable keeps that invisible to the runner - and the temp ISO is always
    removed, including on failure."""
    def run(src, dst, progress=None):
        work = Path(tempfile.mkdtemp(prefix='discchain_'))
        mid = work / 'middle.iso'
        try:
            first(src, mid, progress)
            second(mid, dst, progress)
            return dst
        finally:
            shutil.rmtree(work, ignore_errors=True)
    run.__doc__ = progress_label
    return run


def _disc_fn(system, a, b):
    """Pick the engine for one disc conversion.

    CISO is native at both ends; a CISO paired with anything other than ISO is
    chained through a temporary ISO, since no current external tool speaks it."""
    if a == 'ciso' and b == 'iso':
        return (lambda s, d, p=None: ciso_to_iso(s, d, p)), None
    if b == 'ciso' and a == 'iso':
        return (lambda s, d, p=None: iso_to_ciso(s, d, progress=p)), None
    if a == 'ciso':
        onward, tool = _direct_fn('iso', b)
        return _chain_via_iso(lambda s, d, p=None: ciso_to_iso(s, d, p),
                              onward, f'ciso -> iso -> {b}'), tool
    if b == 'ciso':
        inward, tool = _direct_fn(a, 'iso')
        return _chain_via_iso(inward,
                              lambda s, d, p=None: iso_to_ciso(s, d, progress=p),
                              f'{a} -> iso -> ciso'), tool
    return _direct_fn(a, b)


def _wire_disc_engines():
    """Attach the real engines to the disc entries.

    The registry is built before these functions exist (the N64/disc families
    are generated up front), so the disc specs are finished here: each gets its
    callable, and its engine is corrected to 'native' for the CISO paths, which
    need no external tool."""
    for cid, spec in CONVERSIONS.items():
        if not cid.startswith('disc:'):
            continue
        _, system, pair = cid.split(':', 2)
        a, b = pair.split('->')
        if a == b:                      # rvz -> rvz recompression
            fn, tool = (lambda s, d, p=None: dolphin_convert(s, d, 'rvz', p)), 'dolphintool'
        else:
            fn, tool = _disc_fn(system, a, b)
        spec['fn'] = fn
        # A compressed source (RVZ, WBFS, CISO) can be decoded exactly but not
        # re-encoded byte-for-byte - encoder versions and settings differ - so
        # the round-trip check failed on correct output and the runner then
        # DELETED it (an RVZ -> ISO that matched Redump exactly, in testing).
        # Compare the decoded disc data instead; for "-> iso" there is nothing
        # independent to compare, so the DAT is the oracle.
        if a != 'iso':
            if b == 'iso':
                spec['verify_mode'] = 'none'
                spec['note'] = (f'Decoded from {a.upper()}, which cannot be '
                                'rebuilt byte-for-byte; check the ISO against a DAT.')
            else:
                spec['verify_mode'] = 'payload'
                spec['payload_src'] = f'disc:{system}:{a}->iso'
                spec['payload_out'] = f'disc:{system}:{b}->iso'
        if tool is None:
            spec['engine'] = ENGINE_NATIVE
            spec.pop('requires', None)
        else:
            spec['engine'] = ENGINE_EXTERNAL
            spec['requires'] = tool
            spec['why'] = EXTERNAL_TOOLS[tool][1]


_wire_disc_engines()


# ══════════════════════════════════════════════════════════════════════════════
#  CHD  (MAME Compressed Hunks of Data) via chdman
#
#  CHD is lossless for the DATA, but a CHD rebuilt from extracted files is not
#  byte-identical to the original container - hunk size, codec set and chdman
#  version all change the bytes. So verification direction matters:
#
#    something -> chd   round trip gives back the original file, byte-exact.
#                       Verified normally.
#    chd -> something   the inverse would produce a DIFFERENT-but-equivalent
#                       CHD, so a byte comparison of containers is meaningless.
#                       Verified with chdman's own integrity check instead.
# ══════════════════════════════════════════════════════════════════════════════

CHD_MAGIC = b'MComprHD'
CHD_TAG = 'chdman'

EXTERNAL_TOOLS['chdman'] = (HERE / 'apps' / 'chdman.exe',
                            'chdman.exe (ships with MAME) - CHD create/extract')

CHD_CODECS = {
    b'zlib': 'zlib', b'zstd': 'zstd', b'lzma': 'lzma', b'huff': 'huffman',
    b'flac': 'flac', b'cdzl': 'cd/zlib', b'cdzs': 'cd/zstd', b'cdlz': 'cd/lzma',
    b'cdfl': 'cd/flac', b'avhu': 'av/huffman', b'none': 'none',
}


def _detect_chd(buf, path, fh):
    if _at(buf, 0, 8) != CHD_MAGIC:
        return None
    version = _u32be(buf, 0x0C) if _have(buf, 0x10, 0) else 0
    codecs, logical = [], 0
    if version >= 5 and _have(buf, 0x40, 0):
        for i in range(4):
            tag = _at(buf, 0x10 + i * 4, 4)
            if tag and tag != b'\x00\x00\x00\x00':
                codecs.append(CHD_CODECS.get(tag, _clean(tag, 4) or 'unknown'))
        logical = struct.unpack_from('>Q', buf, 0x20)[0]
    detail = f'CHD v{version}'
    if logical:
        detail += f', {logical / (1024**3):.2f} GiB logical'
    if codecs:
        detail += f', {"+".join(codecs)}'
    return _result('CHD', 'CHD', 'compressed', confidence='high', detail=detail,
                   conversions=['chd:chd->cd', 'chd:chd->dvd', 'chd:chd->raw'],
                   meta={'version': version, 'codecs': codecs,
                         'logical_bytes': logical})


# A cue/gdi/toc sheet is plain text, so it is matched on content rather than on
# a magic number - and only when it actually parses as a track listing.
CUE_HINT = re.compile(rb'^\s*(REM\b|FILE\s+"|TRACK\s+\d+)', re.M | re.I)
GDI_HINT = re.compile(rb'^\s*\d+\s*\r?\n\s*1\s+\d+\s+[04]\s+\d+\s+', re.M)


def _detect_cd_sheet(buf, path, fh):
    ext = path.suffix.lower()
    if ext not in ('.cue', '.gdi', '.toc'):
        return None
    head = buf[:8192]
    if ext == '.cue' and not CUE_HINT.search(head):
        return None
    if ext == '.gdi' and not GDI_HINT.search(head):
        return None
    kind = {'cue': 'CUE sheet', 'gdi': 'GDI (Dreamcast)', 'toc': 'TOC sheet'}[ext[1:]]
    return _result('CD', kind, 'uncompressed', confidence='high',
                   detail=f'{kind} - track listing for a disc image',
                   conversions=['chd:cd->chd'],
                   notes=['The referenced track files are read alongside it.'])


def chdman_create(src, dst, mode='cd', progress=None):
    """createcd/createraw. chdman refuses to overwrite, so dst must not exist."""
    exe = _tool_path('chdman')
    if not exe:
        raise ConversionError('chdman.exe not found in apps/')
    cmd = {'cd': 'createcd', 'dvd': 'createdvd', 'raw': 'createraw',
           'hd': 'createhd'}[mode]
    argv = [str(exe), cmd, '-i', str(src), '-o', str(dst)]
    if mode == 'raw':
        argv += ['-hs', '2048', '-us', '2048']
    _run_tool(argv, progress, f'-> chd ({mode})')
    return dst


def chdman_extract(src, dst, mode='cd', progress=None):
    """extractcd writes a sheet AND its track binary, so the run happens in a
    temp dir and every produced file is handed back; the runner puts the
    sidecars next to the primary output."""
    exe = _tool_path('chdman')
    if not exe:
        raise ConversionError('chdman.exe not found in apps/')
    # The runner hands us a '.part' scratch path; the track names written INTO
    # the cue sheet must come from the real destination name, or the sheet ends
    # up pointing at 'disc.cue.bin'.
    final = Path(dst)
    if final.suffix == '.part':
        final = final.with_suffix('')
    stem = final.stem
    work = Path(tempfile.mkdtemp(prefix='chd_'))
    try:
        if mode == 'cd':
            out = work / (stem + '.cue')
            # --splitbin writes "<stem> (Track NN).bin" plus a matching sheet:
            # verified byte-identical to a Redump PS1 set, cue included. A
            # single merged .bin holds the same data but matches no DAT.
            argv = [str(exe), 'extractcd', '-i', str(src), '-o', str(out), '-sb']
        else:
            out = work / (stem + '.iso')
            cmd = 'extractdvd' if mode == 'dvd' else 'extractraw'
            argv = [str(exe), cmd, '-i', str(src), '-o', str(out)]
        _run_tool(argv, progress, f'chd -> {mode}')
        made = sorted(p for p in work.iterdir() if p.is_file())
        if not made:
            raise ConversionError('chdman produced no output')
        bins = [p for p in made if p.suffix.lower() == '.bin']
        if mode == 'cd' and len(bins) == 1 and out.exists():
            # Redump names a single-track disc "<name>.bin", not "(Track 1)".
            # Verified on 3DO: the data is identical either way, only the name
            # decides whether the set matches. (A CATALOG line in the original
            # cue is not stored in a CHD, so such a cue cannot be recreated.)
            single = work / (stem + '.bin')
            bins[0].rename(single)
            out.write_text(out.read_text().replace(bins[0].name, single.name))
        if mode == 'cd' and out.exists() and REFERENCE_DATS is not None:
            # Redump writes "CATALOG 0000000000000" (an empty media catalogue
            # number) on some discs; CHD does not store it. Measured on Saturn,
            # Sega CD, Neo Geo CD, PC Engine CD and 3DO: every track was exact
            # and that one line was the only difference. Let the DAT decide.
            # "FLAGS DCP" (digital copy permitted) on every track is the other
            # line CHD drops (Neo Geo CD). INDEX 02 markers are also dropped
            # and cannot be recreated - their position is not stored anywhere.
            sheet = out.read_bytes()
            if not REFERENCE_DATS.lookup(out)[0]:
                nl = b'\r\n' if b'\r\n' in sheet else b'\n'
                dcp = re.sub(rb'(TRACK \d+ [^\r\n]+' + re.escape(nl) + rb')',
                             rb'\1    FLAGS DCP' + nl, sheet)
                catalog = b'CATALOG 0000000000000' + nl
                for variant in (catalog + sheet, dcp, catalog + dcp):
                    out.write_bytes(variant)
                    if REFERENCE_DATS.lookup(out)[0]:
                        break
                else:
                    out.write_bytes(sheet)
        primary = out if out.exists() else made[0]
        shutil.move(str(primary), str(dst))
        sidecars = []
        for p in work.iterdir():
            if p.is_file():
                target = Path(dst).parent / p.name
                shutil.move(str(p), str(target))
                sidecars.append(str(target))
        return {'sidecars': sidecars}
    finally:
        shutil.rmtree(work, ignore_errors=True)


def chdman_verify(path, progress=None):
    exe = _tool_path('chdman')
    if not exe:
        return False, 'chdman.exe not found'
    try:
        _run_tool([str(exe), 'verify', '-i', str(path)], progress, 'verify')
        return True, 'chdman verify passed'
    except ConversionError as e:
        return False, str(e)


CONVERSIONS.update({
    'chd:cd->chd': {
        'label': 'CHD: CD image -> CHD (compress)', 'engine': ENGINE_EXTERNAL,
        'system': 'CD', 'ext': '.chd', 'requires': 'chdman',
        'why': EXTERNAL_TOOLS['chdman'][1],
        'fn': lambda s, d, p=None: chdman_create(s, d, 'cd', p),
        'inverse': 'chd:chd->cd', 'verify_mode': 'tool',
    },
    'chd:iso->chd': {
        'label': 'CHD: ISO -> CHD (compress)', 'engine': ENGINE_EXTERNAL,
        'system': 'CD', 'ext': '.chd', 'requires': 'chdman',
        'why': EXTERNAL_TOOLS['chdman'][1],
        'fn': lambda s, d, p=None: chdman_create(s, d, 'dvd', p),
        'inverse': 'chd:chd->dvd', 'verify_mode': 'roundtrip',
    },
    'chd:chd->cd': {
        'label': 'CHD: CHD -> CUE/BIN (extract)', 'engine': ENGINE_EXTERNAL,
        'system': 'CHD', 'ext': '.cue', 'requires': 'chdman',
        'why': EXTERNAL_TOOLS['chdman'][1],
        'fn': lambda s, d, p=None: chdman_extract(s, d, 'cd', p),
        'inverse': None, 'verify_mode': 'tool_source',
    },
    'chd:chd->dvd': {
        'label': 'CHD: CHD -> ISO (DVD)', 'engine': ENGINE_EXTERNAL,
        'system': 'CHD', 'ext': '.iso', 'requires': 'chdman',
        'why': EXTERNAL_TOOLS['chdman'][1],
        'fn': lambda s, d, p=None: chdman_extract(s, d, 'dvd', p),
        'inverse': 'chd:iso->chd', 'verify_mode': 'tool_source',
    },
    'chd:chd->raw': {
        'label': 'CHD: CHD -> ISO (extract raw)', 'engine': ENGINE_EXTERNAL,
        'system': 'CHD', 'ext': '.iso', 'requires': 'chdman',
        'why': EXTERNAL_TOOLS['chdman'][1],
        'fn': lambda s, d, p=None: chdman_extract(s, d, 'raw', p),
        'inverse': None, 'verify_mode': 'tool_source',
    },
})

# CHD and cue/gdi detection run ahead of the generic ISO9660 probe.
DETECTORS.insert(1, _detect_chd)
DETECTORS.insert(2, _detect_cd_sheet)


# ══════════════════════════════════════════════════════════════════════════════
#  SNES  -  512-byte copier (SMC) header
#
#  There is no magic to look for: a copier header is 512 arbitrary bytes glued
#  to the front. What IS checkable is the cartridge's own internal header, whose
#  checksum and complement must XOR to 0xFFFF. Testing that at both the headered
#  and headerless offsets identifies the file AND tells us which it is, without
#  relying on the file size alone (which misfires on overdumps).
# ══════════════════════════════════════════════════════════════════════════════

SNES_BASES = (0x7FC0, 0xFFC0, 0x40FFC0)      # LoROM, HiROM, ExHiROM
SMC_HEADER = 512


def _snes_header_ok(buf, fh, base):
    """True when the internal header at base looks like a real SNES one."""
    h = _read_at(buf, fh, base, 32)
    if len(h) < 32:
        return False
    checksum = struct.unpack_from('<H', h, 30)[0]
    complement = struct.unpack_from('<H', h, 28)[0]
    if checksum == 0 and complement == 0:
        return False
    if (checksum ^ complement) != 0xFFFF:
        return False
    title = h[0:21]
    printable = sum(1 for c in title if 0x20 <= c <= 0x7E)
    return printable >= 18


def _detect_snes(buf, path, fh):
    for offset, headered in ((SMC_HEADER, True), (0, False)):
        for base in SNES_BASES:
            if _snes_header_ok(buf, fh, base + offset):
                h = _read_at(buf, fh, base + offset, 32)
                title = _clean(h[0:21], 21)
                mapmode = {0x20: 'LoROM', 0x21: 'HiROM', 0x30: 'LoROM/FastROM',
                           0x31: 'HiROM/FastROM', 0x35: 'ExHiROM'}.get(
                               h[21], f'map 0x{h[21]:02X}')
                return _result(
                    'SNES', 'SFC', 'headered' if headered else 'headerless',
                    detail=f'{title} - {mapmode}'
                           + (', 512-byte copier header' if headered else ''),
                    conversions=['snes:headered->headerless'] if headered
                                else ['snes:headerless->headered'],
                    meta={'base': base, 'headered': headered,
                          'header': bytes(_at(buf, 0, SMC_HEADER)).hex()
                                    if headered else ''},
                )
    return None


def _detect_snes_loose(buf, path, fh, folder_hint=None):
    """SNES by extension or folder plus a cartridge-shaped size, for prototypes
    whose internal checksum was never filled in. Low confidence by design."""
    ext = path.suffix.lower()
    by_ext = ext in ('.sfc', '.smc', '.swc', '.fig')
    if not by_ext and folder_hint != 'SNES':
        return None
    size = fh['size']
    if size < 0x40000 or size % 0x8000 not in (0, SMC_HEADER):
        return None
    headered = size % 0x8000 == SMC_HEADER
    return _result(
        'SNES', 'SFC', 'headered' if headered else 'headerless', confidence='low',
        detail=f'{size // 1024} KiB, no valid internal header - identified by '
               + ('extension' if by_ext else 'folder'),
        conversions=['snes:headered->headerless'] if headered
                    else ['snes:headerless->headered'],
        notes=['The internal checksum is blank or wrong, as on many prototypes.'],
        meta={'headered': headered,
              'header': bytes(_at(buf, 0, SMC_HEADER)).hex() if headered else ''},
    )


def strip_smc_header(src, dst, progress=None):
    """Remove the 512-byte copier header, returning it for verification."""
    with open(src, 'rb') as fi, open(dst, 'wb') as fo:
        header = fi.read(SMC_HEADER)
        if len(header) < SMC_HEADER:
            raise ConversionError('file is shorter than a copier header')
        _copy_rest(fi, fo, os.path.getsize(src), progress, 'stripping')
    return header


def add_smc_header(src, dst, header=None, progress=None):
    """Prepend a copier header. Unlike iNES, the 512 bytes carry nothing an
    emulator needs, so a zero-filled header is correct when none was kept."""
    header = bytes(header) if header else b'\x00' * SMC_HEADER
    if len(header) != SMC_HEADER:
        raise ConversionError('copier header must be exactly 512 bytes')
    with open(src, 'rb') as fi, open(dst, 'wb') as fo:
        fo.write(header)
        _copy_rest(fi, fo, os.path.getsize(src), progress, 'adding header')
    return dst


def _copy_rest(fi, fo, total, progress, label):
    done = 0
    while True:
        b = fi.read(CHUNK)
        if not b:
            break
        fo.write(b)
        done += len(b)
        if progress:
            progress(done, total, label)


# ══════════════════════════════════════════════════════════════════════════════
#  Mega Drive / Genesis  -  SMD interleave
#
#  A .smd holds a 512-byte header then 16 KiB blocks, each stored as 8 KiB of
#  the ODD bytes followed by 8 KiB of the EVEN bytes. Deinterleaving is exactly
#  reversible, so both directions are byte-exact.
# ══════════════════════════════════════════════════════════════════════════════

SMD_BLOCK = 16384
SMD_HEADER = 512


def _detect_megadrive(buf, path, fh):
    # Raw ROM: the console name sits at 0x100 of the cartridge image.
    tag = _at(buf, 0x100, 16)
    if tag[:4] in (b'SEGA', b'\x20SEG'):
        name = _clean(_at(buf, 0x120, 48), 48)
        return _result('MD', 'BIN', 'plain',
                       detail=f'raw Mega Drive image - {name[:40]}',
                       conversions=['md:bin->smd'])
    # SMD: bytes 8 and 9 of the copier header are the format signature.
    if _have(buf, 10, 0) and buf[8] == 0xAA and buf[9] == 0xBB:
        blocks = buf[0] if _have(buf, 1, 0) else 0
        return _result('MD', 'SMD', 'interleaved',
                       detail=f'Super Magic Drive interleaved, {blocks} x 16 KiB',
                       conversions=['md:smd->bin'])
    return None


def _deinterleave_smd(block):
    """8 KiB of odd bytes + 8 KiB of even bytes -> normal byte order."""
    half = len(block) // 2
    odd, even = block[:half], block[half:]
    out = bytearray(len(block))
    out[0::2] = even
    out[1::2] = odd
    return bytes(out)


def _interleave_smd(block):
    out = bytearray(len(block))
    half = len(block) // 2
    out[:half] = block[1::2]
    out[half:] = block[0::2]
    return bytes(out)


def smd_to_bin(src, dst, progress=None):
    total = os.path.getsize(src)
    with open(src, 'rb') as fi, open(dst, 'wb') as fo:
        fi.read(SMD_HEADER)
        done = 0
        while True:
            block = fi.read(SMD_BLOCK)
            if not block:
                break
            if len(block) % 2:
                raise ConversionError('SMD block is not an even length')
            fo.write(_deinterleave_smd(block))
            done += len(block)
            if progress:
                progress(done, total, 'deinterleaving')
    return dst


def bin_to_smd(src, dst, progress=None):
    total = os.path.getsize(src)
    blocks = (total + SMD_BLOCK - 1) // SMD_BLOCK
    header = bytearray(SMD_HEADER)
    header[0] = blocks & 0xFF
    header[1] = 0x03
    header[8], header[9] = 0xAA, 0xBB
    with open(src, 'rb') as fi, open(dst, 'wb') as fo:
        fo.write(header)
        done = 0
        while True:
            block = fi.read(SMD_BLOCK)
            if not block:
                break
            if len(block) % 2:
                block += b'\x00'
            fo.write(_interleave_smd(block))
            done += len(block)
            if progress:
                progress(done, total, 'interleaving')
    return dst


# ══════════════════════════════════════════════════════════════════════════════
#  Atari 7800 / Lynx  -  fixed-size headers
# ══════════════════════════════════════════════════════════════════════════════

A78_HEADER = 128
LNX_HEADER = 64


def _detect_atari(buf, path, fh):
    if _at(buf, 1, 9) == b'ATARI7800':
        return _result('A7800', 'A78', 'headered',
                       detail='Atari 7800, 128-byte .a78 header',
                       conversions=['a78:headered->headerless'],
                       meta={'header': bytes(_at(buf, 0, A78_HEADER)).hex()})
    if _at(buf, 0, 4) == b'LYNX':
        ver = _u16le(buf, 4) if _have(buf, 6, 2) else 0
        name = _at(buf, 10, 32).split(b'\x00')[0].decode('ascii', 'replace')
        return _result('LYNX', 'LNX', 'headered',
                       detail=f'Atari Lynx v{ver} header - {name}',
                       conversions=['lnx:headered->headerless'],
                       meta={'header': bytes(_at(buf, 0, LNX_HEADER)).hex()})
    return None


def _strip_fixed(size):
    def run(src, dst, progress=None):
        with open(src, 'rb') as fi, open(dst, 'wb') as fo:
            header = fi.read(size)
            if len(header) < size:
                raise ConversionError('file is shorter than its header')
            _copy_rest(fi, fo, os.path.getsize(src), progress, 'stripping')
        return header
    return run


def _add_fixed(size, magic_check):
    def run(src, dst, header=None, progress=None):
        if not header or len(header) != size or not magic_check(bytes(header)):
            raise ConversionError(
                f'refusing to write a {size}-byte header that is missing or '
                'does not carry the right signature')
        with open(src, 'rb') as fi, open(dst, 'wb') as fo:
            fo.write(bytes(header))
            _copy_rest(fi, fo, os.path.getsize(src), progress, 'adding header')
        return dst
    return run


strip_a78_header = _strip_fixed(A78_HEADER)
strip_lnx_header = _strip_fixed(LNX_HEADER)
add_a78_header = _add_fixed(A78_HEADER, lambda h: h[1:10] == b'ATARI7800')
add_lnx_header = _add_fixed(LNX_HEADER, lambda h: h[0:4] == b'LYNX')


# ══════════════════════════════════════════════════════════════════════════════
#  Famicom Disk System  -  16-byte header
#
#  Unlike iNES, this header IS computable: it is the magic plus a side count,
#  and a raw FDS image is an exact multiple of 65500 bytes per side.
# ══════════════════════════════════════════════════════════════════════════════

FDS_SIDE = 65500
FDS_HEADER = 16


def strip_fds_header(src, dst, progress=None):
    with open(src, 'rb') as fi, open(dst, 'wb') as fo:
        header = fi.read(FDS_HEADER)
        if header[:4] != b'FDS\x1a':
            raise ConversionError('not a headered FDS image')
        _copy_rest(fi, fo, os.path.getsize(src), progress, 'stripping')
    return header


def add_fds_header(src, dst, progress=None):
    size = os.path.getsize(src)
    if size % FDS_SIDE:
        raise ConversionError(
            f'{size:,} bytes is not a whole number of {FDS_SIDE:,}-byte disk '
            'sides, so the side count cannot be trusted')
    sides = size // FDS_SIDE
    header = b'FDS\x1a' + bytes([sides]) + b'\x00' * 11
    with open(src, 'rb') as fi, open(dst, 'wb') as fo:
        fo.write(header)
        _copy_rest(fi, fo, size, progress, 'adding header')
    return dst


# ══════════════════════════════════════════════════════════════════════════════
#  NDS  -  trimmed vs untrimmed
#
#  A cart dump is padded out to the physical chip size; the header records how
#  much of it is real at 0x80, and the chip capacity at 0x14. Trimming drops the
#  padding.
#
#  Untrimming cannot be perfect on its own - the pad byte is a property of the
#  original dump, not of the data - so trimming first CHECKS that the padding is
#  a single repeated byte and refuses if it is not. That both guarantees the
#  round trip and stops the tool from silently discarding real data.
# ══════════════════════════════════════════════════════════════════════════════

def _nds_used_size(head):
    """Bytes of real ROM data. 0x80 is the NTR (DS) ROM end; DSi-enhanced and
    DSi-only carts (unitcode bit 1) keep a DSi area past it, whose total size
    is at 0x210. Using 0x80 for those made trimming either refuse or, worse,
    cut the DSi binaries off."""
    used = struct.unpack_from('<I', head, 0x80)[0] if len(head) >= 0x84 else 0
    if len(head) >= 0x214 and head[0x12] in (0x02, 0x03):
        twl = struct.unpack_from('<I', head, 0x210)[0]
        capacity = 128 * 1024 << head[0x14] if head[0x14] < 16 else 0
        # a garbage 0x210 must not be believed: it has to fit the cartridge
        if used < twl <= capacity:
            used = twl
    return used


def _nds_sizes(path):
    with open(path, 'rb') as f:
        head = f.read(0x1000)
    if len(head) < 0x90:
        raise ConversionError('file is too small to hold an NDS header')
    used = _nds_used_size(head)
    capacity = 128 * 1024 << head[0x14] if head[0x14] < 16 else 0
    return used, capacity


def nds_trim(src, dst, progress=None):
    size = os.path.getsize(src)
    used, _cap = _nds_sizes(src)
    if used == 0 or used > size:
        raise ConversionError(
            f'header claims {used:,} bytes used but the file is {size:,}')
    if used == size:
        raise ConversionError('already trimmed - nothing to remove')

    pad_byte, uniform = _scan_padding(src, used, size, progress)
    if not uniform:
        raise ConversionError(
            'the area past the end of the ROM is not uniform padding, so '
            'trimming it would destroy real data')

    with open(src, 'rb') as fi, open(dst, 'wb') as fo:
        left = used
        while left:
            b = fi.read(min(CHUNK, left))
            if not b:
                break
            fo.write(b)
            left -= len(b)
            if progress:
                progress(used - left, used, 'trimming')
    return {'pad': pad_byte, 'orig_size': size, 'used': used}


def _scan_padding(src, used, size, progress=None):
    """Is everything past `used` one repeated byte? Returns (byte, uniform)."""
    with open(src, 'rb') as f:
        f.seek(used)
        first = f.read(1)
        if not first:
            return 0xFF, True
        pad = first[0]
        expect_chunk = bytes([pad]) * CHUNK
        done = 0
        while True:
            b = f.read(CHUNK)
            if not b:
                break
            if b != expect_chunk[:len(b)]:
                return pad, False
            done += len(b)
            if progress:
                progress(done, size - used, 'checking padding')
    return pad, True


def nds_untrim(src, dst, pad=0xFF, target=None, progress=None):
    size = os.path.getsize(src)
    used, capacity = _nds_sizes(src)
    target = target or capacity
    if not target or target < size:
        raise ConversionError(
            f'cannot work out a sensible untrimmed size (header capacity '
            f'{capacity:,}, file {size:,})')
    with open(src, 'rb') as fi, open(dst, 'wb') as fo:
        _copy_rest(fi, fo, size, progress, 'copying')
        left = target - size
        block = bytes([pad]) * min(CHUNK, left) if left else b''
        while left > 0:
            n = min(len(block), left)
            fo.write(block[:n])
            left -= n
            if progress:
                progress(target - left, target, 'padding')
    return dst


def _nds_rebuild(out, tmp, extra, progress=None):
    """Put the padding back exactly as it was, for round-trip verification."""
    return nds_untrim(out, tmp, pad=extra['pad'], target=extra['orig_size'],
                      progress=progress)


# ══════════════════════════════════════════════════════════════════════════════
#  ZSO  -  the zstd-compressed sibling of CSO (PSP and PS2)
# ══════════════════════════════════════════════════════════════════════════════

ZSO_MAGIC = b'ZISO'


def _zstd_codec():
    try:
        import zstandard
    except ImportError:
        raise ConversionError('the zstandard package is required for ZSO')
    return zstandard


def iso_to_zso(src, dst, block_size=2048, level=17, progress=None):
    z = _zstd_codec()
    c = z.ZstdCompressor(level=level)
    return _pack_blocks(src, dst, ZSO_MAGIC, block_size,
                        lambda raw: c.compress(raw), progress)


def zso_to_iso(src, dst, progress=None):
    z = _zstd_codec()
    d = z.ZstdDecompressor()
    return _unpack_blocks(src, dst, ZSO_MAGIC,
                          lambda blob, n: d.decompress(blob, max_output_size=n),
                          progress)


def _detect_zso(buf, path, fh):
    if _at(buf, 0, 4) != ZSO_MAGIC:
        return None
    total = _u64le(buf, 8) if _have(buf, 16, 0) else 0
    block = _u32le(buf, 16) if _have(buf, 20, 0) else 0
    return _result('PSP/PS2', 'ZSO', 'compressed',
                   detail=f'zstd-compressed ISO - {total // (1024*1024)} MiB '
                          f'uncompressed, {block} B blocks',
                   conversions=['iso:zso->iso'])


# ══════════════════════════════════════════════════════════════════════════════
#  Wii U and PS3  -  both already covered by the NKit binary we installed
# ══════════════════════════════════════════════════════════════════════════════

def _detect_wiiu(buf, path, fh):
    if _at(buf, 0, 4) == b'WUX0':
        block = _u32le(buf, 8) if _have(buf, 12, 0) else 0
        return _result('WIIU', 'WUX', 'compressed',
                       detail=f'Wii U compressed disc image, {block} B blocks',
                       conversions=['wiiu:wux->wud'])
    # A raw WUD carries the Wii U disc magic at the start of its header block.
    if _have(buf, 4, 0) and _u32be(buf, 0) == 0xCC549EB9:
        return _result('WIIU', 'WUD', 'plain', detail='raw Wii U disc image',
                       conversions=['wiiu:wud->wux'])
    return None


def _ps3_iso(buf, fh):
    """PS3 game ISOs name their volume PS3VOLUME in the ISO9660 descriptor."""
    vol = _read_at(buf, fh, 0x8028, 32)
    return b'PS3VOLUME' in vol.upper()


# ══════════════════════════════════════════════════════════════════════════════
#  REGISTRY ADDITIONS
# ══════════════════════════════════════════════════════════════════════════════

def _hdr_pair(cid_strip, cid_add, label, strip_fn, add_fn, ext, system,
              add_needs_header=True):
    """Register a strip/add header pair.

    The strip side always verifies, because it hands back the exact bytes it
    removed and the rebuild puts them straight back."""
    CONVERSIONS[cid_strip] = {
        'label': f'{label}: strip header', 'engine': ENGINE_NATIVE,
        'system': system, 'ext': ext, 'fn': strip_fn, 'inverse': None,
        'rebuild': lambda out, tmp, extra, p=None: add_fn(out, tmp, extra, p),
    }
    CONVERSIONS[cid_add] = {
        'label': f'{label}: add header', 'engine': ENGINE_NATIVE,
        'system': system, 'ext': ext,
        'fn': (None if add_needs_header
               else lambda s, d, p=None: add_fn(s, d, p)),
        'inverse': cid_strip,
    }
    if add_needs_header:
        CONVERSIONS[cid_add].update(
            engine=ENGINE_KEYED, requires='known-header',
            why='The original header bytes are needed; they cannot be derived '
                'from the ROM body. Strip and re-add in one run, or supply a '
                'DAT match.')


_hdr_pair('snes:headered->headerless', 'snes:headerless->headered',
          'SNES', lambda s, d, p=None: strip_smc_header(s, d, p),
          lambda s, d, h=None, p=None: add_smc_header(s, d, h, p),
          '.sfc', 'SNES', add_needs_header=False)

_hdr_pair('a78:headered->headerless', 'a78:headerless->headered',
          'Atari 7800', lambda s, d, p=None: strip_a78_header(s, d, p),
          lambda s, d, h=None, p=None: add_a78_header(s, d, h, p),
          '.a78', 'A7800')

_hdr_pair('lnx:headered->headerless', 'lnx:headerless->headered',
          'Atari Lynx', lambda s, d, p=None: strip_lnx_header(s, d, p),
          lambda s, d, h=None, p=None: add_lnx_header(s, d, h, p),
          '.lnx', 'LYNX')

CONVERSIONS['nes:fds-headered->headerless'] = {
    'label': 'FDS: strip header', 'engine': ENGINE_NATIVE, 'system': 'NES',
    'ext': '.fds', 'fn': lambda s, d, p=None: strip_fds_header(s, d, p),
    'inverse': 'nes:fds-headerless->headered',
}
CONVERSIONS['nes:fds-headerless->headered'] = {
    'label': 'FDS: add header (side count is computed)',
    'engine': ENGINE_NATIVE, 'system': 'NES', 'ext': '.fds',
    'fn': lambda s, d, p=None: add_fds_header(s, d, p),
    'inverse': 'nes:fds-headered->headerless',
}

CONVERSIONS['md:smd->bin'] = {
    'label': 'Mega Drive: SMD -> BIN (deinterleave)', 'engine': ENGINE_NATIVE,
    'system': 'MD', 'ext': '.bin', 'fn': lambda s, d, p=None: smd_to_bin(s, d, p),
    'inverse': 'md:bin->smd',
}
CONVERSIONS['md:bin->smd'] = {
    'label': 'Mega Drive: BIN -> SMD (interleave)', 'engine': ENGINE_NATIVE,
    'system': 'MD', 'ext': '.smd', 'fn': lambda s, d, p=None: bin_to_smd(s, d, p),
    'inverse': 'md:smd->bin',
}

CONVERSIONS['nds:untrimmed->trimmed'] = {
    'label': 'NDS: trim padding', 'engine': ENGINE_NATIVE, 'system': 'NDS',
    'ext': '.nds', 'fn': lambda s, d, p=None: nds_trim(s, d, p),
    'inverse': None, 'rebuild': _nds_rebuild,
}
CONVERSIONS['nds:trimmed->untrimmed'] = {
    'label': 'NDS: restore padding to cart size', 'engine': ENGINE_NATIVE,
    'system': 'NDS', 'ext': '.nds',
    'fn': lambda s, d, p=None: nds_untrim(s, d, progress=p),
    'inverse': None, 'verify_mode': 'none',
    'note': 'Pads with 0xFF; the original dump pad byte cannot be known.',
}

CONVERSIONS['iso:iso->zso'] = {
    'label': 'PSP/PS2: ISO -> ZSO (zstd)', 'engine': ENGINE_NATIVE,
    'system': 'PSP/PS2', 'ext': '.zso',
    'fn': lambda s, d, p=None: iso_to_zso(s, d, progress=p),
    'inverse': 'iso:zso->iso',
}
CONVERSIONS['iso:zso->iso'] = {
    'label': 'PSP/PS2: ZSO -> ISO', 'engine': ENGINE_NATIVE,
    'system': 'PSP/PS2', 'ext': '.iso',
    'fn': lambda s, d, p=None: zso_to_iso(s, d, p),
    'inverse': 'iso:iso->zso',
}

for _cid, _label, _target in (
    ('wiiu:wud->wux', 'Wii U: WUD -> WUX (compress)', 'wux'),
    ('wiiu:wux->wud', 'Wii U: WUX -> WUD (expand)', 'iso'),
):
    CONVERSIONS[_cid] = {
        'label': _label, 'engine': ENGINE_EXTERNAL,
        'system': _cid.split(':')[0].upper(),
        'ext': '.wux' if _target == 'wux' else '.iso',
        'requires': 'nkit', 'why': EXTERNAL_TOOLS['nkit'][1],
        'fn': (lambda s, d, p=None, _t=_target: nkit_convert(s, d, _t, p)),
        'inverse': None, 'verify_mode': 'none',
    }

# Detector order: strong magics first, then the structural SNES probe, and the
# folder-hinted headerless NES fallback stays last of all.
DETECTORS.extend([_detect_zso, _detect_wiiu, _detect_atari,
                  _detect_megadrive, _detect_snes])


def _iso_conversions(is_psp, is_ps3):
    """Conversions a plain ISO9660 image can feed.

    Every ISO can be compressed (CHD, ZSO); the platform-specific entries are
    added only when the volume actually identifies as that platform."""
    convs = ['chd:iso->chd', 'iso:iso->zso']
    if is_psp:
        convs.insert(0, 'psp:iso->cso')
    if is_ps3:
        convs.insert(0, 'ps3:iso->deciso')
    return convs


# ══════════════════════════════════════════════════════════════════════════════
#  NDS KEY1  -  Secure Area encryption
#
#  KEY1 is Blowfish keyed from a 0x1048-byte table that lives in the DS ARM7
#  BIOS at 0x30..0x1077. Algorithm follows GBATEK exactly; the indices below are
#  its byte offsets divided by four:
#
#      keybuf[0x00..0x11]   P-array (18 words)
#      keybuf[0x12 + n]     S-box 0   (BIOS byte offset 0x048)
#      keybuf[0x112 + n]    S-box 1   (0x448)
#      keybuf[0x212 + n]    S-box 2   (0x848)
#      keybuf[0x312 + n]    S-box 3   (0xC48)
#
#  The Secure Area is encrypted with level 3 over its first 2 KiB, and then the
#  first 8 bytes are encrypted AGAIN at level 2 on top. Decryption therefore
#  undoes the level-2 pass first.
# ══════════════════════════════════════════════════════════════════════════════

NDS_KEYTABLE_OFFSET = 0x30
NDS_KEYTABLE_SIZE = 0x1048
NDS_KEYTABLE_SIG = bytes([0x99, 0xD5, 0x20, 0x5F])   # first word, per GBATEK
NDS_SECURE_LEN = 0x800
NDS_CART_MODULO = 8

M32 = 0xFFFFFFFF


def _bswap32(v):
    return ((v & 0xFF) << 24) | ((v & 0xFF00) << 8) | \
           ((v >> 8) & 0xFF00) | ((v >> 24) & 0xFF)


class NdsKey1:
    """KEY1 Blowfish engine, keyed from the DS ARM7 BIOS table."""

    def __init__(self, keytable):
        if len(keytable) != NDS_KEYTABLE_SIZE:
            raise ConversionError(
                f'KEY1 table must be {NDS_KEYTABLE_SIZE} bytes, got {len(keytable)}')
        self._base = list(struct.unpack(f'<{NDS_KEYTABLE_SIZE // 4}I', keytable))
        self.keybuf = list(self._base)

    # ── the 64-bit block function ────────────────────────────────────────────

    def _round(self, z):
        k = self.keybuf
        x = k[0x012 + ((z >> 24) & 0xFF)]
        x = (k[0x112 + ((z >> 16) & 0xFF)] + x) & M32
        x = k[0x212 + ((z >> 8) & 0xFF)] ^ x
        x = (k[0x312 + (z & 0xFF)] + x) & M32
        return x

    def encrypt64(self, y, x):
        k = self.keybuf
        for i in range(0x00, 0x10):
            z = k[i] ^ x
            x = self._round(z) ^ y
            y = z
        return (x ^ k[0x10]) & M32, (y ^ k[0x11]) & M32

    def decrypt64(self, y, x):
        k = self.keybuf
        for i in range(0x11, 0x01, -1):
            z = k[i] ^ x
            x = self._round(z) ^ y
            y = z
        return (x ^ k[0x01]) & M32, (y ^ k[0x00]) & M32

    # ── key schedule ─────────────────────────────────────────────────────────

    def _apply_keycode(self, keycode, modulo):
        keycode[1], keycode[2] = self.encrypt64(keycode[1], keycode[2])
        keycode[0], keycode[1] = self.encrypt64(keycode[0], keycode[1])
        for i in range(0, 0x48, 4):
            self.keybuf[i >> 2] ^= _bswap32(keycode[(i % modulo) >> 2])
            self.keybuf[i >> 2] &= M32
        s0 = s1 = 0
        for i in range(0, 0x1048, 8):
            s0, s1 = self.encrypt64(s0, s1)
            self.keybuf[i >> 2] = s1
            self.keybuf[(i >> 2) + 1] = s0

    def init_keycode(self, idcode, level, modulo=NDS_CART_MODULO):
        self.keybuf = list(self._base)
        keycode = [idcode & M32, (idcode // 2) & M32, (idcode * 2) & M32]
        if level >= 1:
            self._apply_keycode(keycode, modulo)
        if level >= 2:
            self._apply_keycode(keycode, modulo)
        keycode[1] = (keycode[1] * 2) & M32
        keycode[2] = (keycode[2] // 2) & M32
        if level >= 3:
            self._apply_keycode(keycode, modulo)

    # ── block helpers over a bytearray ───────────────────────────────────────

    def _apply_block(self, buf, offset, fn):
        y, x = struct.unpack_from('<II', buf, offset)
        y, x = fn(y, x)
        struct.pack_into('<II', buf, offset, y, x)


def load_nds_keytable(path=None):
    """Pull the KEY1 table out of a DS ARM7 BIOS (or accept the bare table).

    Accepts bios7.bin directly so the user drops in the BIOS rather than having
    to pre-extract anything; the table's first word is checked against the value
    GBATEK documents, which catches a wrong or truncated file immediately."""
    path = Path(path) if path else _key_path('nds_blow')
    if not path or not Path(path).exists():
        raise ConversionError('no DS ARM7 BIOS / KEY1 table available')
    data = Path(path).read_bytes()
    if len(data) == NDS_KEYTABLE_SIZE:
        table = data
    elif len(data) >= NDS_KEYTABLE_OFFSET + NDS_KEYTABLE_SIZE:
        table = data[NDS_KEYTABLE_OFFSET:
                     NDS_KEYTABLE_OFFSET + NDS_KEYTABLE_SIZE]
    else:
        raise ConversionError(
            f'{Path(path).name} is {len(data)} bytes - too small to hold the '
            'KEY1 table')
    if table[:4] != NDS_KEYTABLE_SIG:
        raise ConversionError(
            f'{Path(path).name} does not contain the KEY1 table at 0x30 '
            f'(expected {NDS_KEYTABLE_SIG.hex()}, found {table[:4].hex()})')
    return table


def _nds_idcode(header):
    """The gamecode at 0x0C, read as a little-endian word - that is the KEY1 id."""
    return struct.unpack_from('<I', header, 0x0C)[0]


def _nds_secure_offset(header):
    off = struct.unpack_from('<I', header, 0x20)[0]
    if not (0x4000 <= off < 0x8000):
        raise ConversionError(
            f'ARM9 binary starts at 0x{off:X}, so there is no Secure Area')
    return off


def nds_crypt_secure_area(src, dst, encrypt, keytable=None, progress=None):
    """Encrypt or decrypt the Secure Area, copying the rest of the ROM verbatim.

    Only the 2 KiB Secure Area is touched; everything else is a byte copy, so a
    conversion can never disturb the parts of the ROM it is not responsible for.
    """
    table = keytable if keytable is not None else load_nds_keytable()
    engine = NdsKey1(table)

    total = os.path.getsize(src)
    with open(src, 'rb') as fi:
        header = fi.read(0x200)
        if len(header) < 0x200:
            raise ConversionError('file is too small to hold an NDS header')
        secure_off = _nds_secure_offset(header)
        idcode = _nds_idcode(header)
        if total < secure_off + NDS_SECURE_LEN:
            raise ConversionError('file ends before the Secure Area does')
        fi.seek(secure_off)
        secure = bytearray(fi.read(NDS_SECURE_LEN))

    was = bytes(secure[:8])
    if not any(secure):
        raise ConversionError(
            'Secure Area is zeroed (a prototype or homebrew dump) - there is '
            'nothing to encrypt or decrypt')
    if encrypt:
        if was not in (NDS_SECURE_MAGIC, NDS_DECRYPTED_MARKER):
            raise ConversionError(
                'Secure Area starts with neither the decrypted marker nor '
                f'{NDS_SECURE_MAGIC!r} - it is not decrypted, so encrypting it '
                'would produce nonsense')
        secure[:8] = NDS_SECURE_MAGIC        # the plaintext the cart really holds
        engine.init_keycode(idcode, 3)
        for i in range(0, NDS_SECURE_LEN, 8):
            engine._apply_block(secure, i, engine.encrypt64)
        engine.init_keycode(idcode, 2)
        engine._apply_block(secure, 0, engine.encrypt64)
    else:
        if was in (NDS_SECURE_MAGIC, NDS_DECRYPTED_MARKER):
            raise ConversionError('Secure Area is already decrypted')
        engine.init_keycode(idcode, 2)
        engine._apply_block(secure, 0, engine.decrypt64)
        engine.init_keycode(idcode, 3)
        for i in range(0, NDS_SECURE_LEN, 8):
            engine._apply_block(secure, i, engine.decrypt64)
        if bytes(secure[:8]) != NDS_SECURE_MAGIC:
            raise ConversionError(
                'decryption did not yield the expected Secure Area ID - wrong '
                'key table, or this ROM is not KEY1 encrypted')
        secure[:8] = NDS_DECRYPTED_MARKER

    with open(src, 'rb') as fi, open(dst, 'wb') as fo:
        done = 0
        while done < secure_off:
            b = fi.read(min(CHUNK, secure_off - done))
            if not b:
                break
            fo.write(b)
            done += len(b)
        fo.write(secure)
        fi.seek(secure_off + NDS_SECURE_LEN)
        done = secure_off + NDS_SECURE_LEN
        while True:
            b = fi.read(CHUNK)
            if not b:
                break
            fo.write(b)
            done += len(b)
            if progress:
                progress(done, total, 'copying')
    return dst


def _wire_nds_crypto():
    """Attach the KEY1 engine now that it exists.

    The registry is built before the crypto is defined, so the two NDS entries
    are finished here. They stay ENGINE_KEYED, so conversion_status still greys
    them out until bios7.bin is actually present."""
    CONVERSIONS['nds:encrypted->decrypted'].update(
        fn=lambda s, d, p=None: nds_crypt_secure_area(s, d, False, progress=p),
        inverse='nds:decrypted->encrypted', ext='.nds')
    CONVERSIONS['nds:decrypted->encrypted'].update(
        fn=lambda s, d, p=None: nds_crypt_secure_area(s, d, True, progress=p),
        inverse='nds:encrypted->decrypted', ext='.nds')


_wire_nds_crypto()


# ══════════════════════════════════════════════════════════════════════════════
#  3DS NCCH  -  AES-CTR content encryption
#
#  KeyX comes from boot9; KeyY is the first 16 bytes of the NCCH signature. The
#  normal key is the 3DS scrambler:
#
#      normal = rol( (rol(keyX, 2) XOR keyY) + C , 87 )   over 128 bits
#      C      = 0x1FF9E9AAC5FE0408024591DC5D52768A
#
#  Three regions are encrypted, each with its own counter: ExHeader (section 1),
#  ExeFS (2) and RomFS (3). ExHeader and the ExeFS *header* always use the
#  primary keyslot 0x2C; RomFS and most ExeFS files use the secondary slot named
#  by the crypto method. For method 0x00 the two are the same slot, which is why
#  the common case is simple.
#
#  AES-CTR is symmetric, so encrypting is the same pass as decrypting.
#
#  Seed crypto (flag 0x20) swaps the SECONDARY KeyY for
#  SHA-256(KeyY || seed)[:16], with the 16-byte seed looked up by program ID in
#  seeddb.bin. The primary key is untouched, so a seeded title always has
#  primary != secondary, even with method 0x00.
#
#  Whenever primary != secondary the ExeFS is split per file: the header,
#  "icon", "banner" and the padding between files use the primary key; every
#  other file (".code" above all) uses the secondary.
# ══════════════════════════════════════════════════════════════════════════════

B9_KEYBLOB_OFFSET = 0x5860
B9_NCCH_KEYX_OFFSET = 0x170          # relative to the keyblob
B9_FULL_SIZE = 0x10000
SCRAMBLE_C = 0x1FF9E9AAC5FE0408024591DC5D52768A
M128 = (1 << 128) - 1

NCCH_KEYSLOTS = (0x2C, 0x25, 0x18, 0x1B)
CRYPTO_METHOD_SLOT = {0x00: 0x2C, 0x01: 0x25, 0x0A: 0x18, 0x0B: 0x1B}

SECTION_EXHEADER, SECTION_EXEFS, SECTION_ROMFS = 1, 2, 3
MEDIA_UNIT_SIZE = 0x200
EXHEADER_LEN = 0x800

NCCH_FLAG_SEED = 0x20

SEEDDB_HEADER = 0x10
SEEDDB_ENTRY = 0x20                  # title ID u64le, 16-byte seed, 8 padding
EXEFS_HEADER_LEN = 0x200
EXEFS_PRIMARY_FILES = (b'icon', b'banner')


def _rol128(value, bits):
    bits %= 128
    return ((value << bits) | (value >> (128 - bits))) & M128


def _scramble_key(key_x, key_y):
    return _rol128((_rol128(key_x, 2) ^ key_y) + SCRAMBLE_C & M128, 87)


def load_boot9_keyx(path=None, aes_keys_path=None):
    """The NCCH KeyX values: {slot: int}.

    Only slot 0x2C comes from boot9. The keyblob there holds ONE KeyX per group
    of four slots (0x2C, 0x30, 0x34, 0x38) - the 7.x key (0x25) and the New 3DS
    keys (0x18, 0x1B) were introduced by later firmware and are not in boot9 at
    all. Reading the next three blob entries as those slots (as this once did)
    yields 0x30/0x34/0x38 and silently mis-keys every method 0x01/0x0A/0x0B
    title. Those three come from aes_keys.txt; a slot that is missing there is
    simply absent from the result, and _ncch_regions names it when needed.

    Accepts either the full 64 KiB boot9.bin or the 32 KiB protected half; the
    keyblob simply sits 0x8000 further into the full dump."""
    path = Path(path) if path else _key_path('boot9')
    if not path or not Path(path).exists():
        raise ConversionError('boot9.bin not available')
    data = Path(path).read_bytes()
    offset = B9_KEYBLOB_OFFSET + (0x8000 if len(data) >= B9_FULL_SIZE else 0)
    base = offset + B9_NCCH_KEYX_OFFSET
    if len(data) < base + 0x10:
        raise ConversionError(
            f'{Path(path).name} is {len(data)} bytes - too small to hold the '
            'boot9 keyblob')
    keys = {0x2C: int.from_bytes(data[base:base + 0x10], 'big')}
    if not keys[0x2C]:
        raise ConversionError('boot9 slot 0x2C KeyX reads back as zero - this '
                              'is not a valid boot9 dump')

    aes = Path(aes_keys_path) if aes_keys_path else _key_path('aes_keys')
    if aes and Path(aes).exists():
        text = Path(aes).read_text(errors='replace')
        for slot in NCCH_KEYSLOTS[1:]:
            m = re.search(rf'slot0x{slot:02X}KeyX\s*=\s*([0-9A-Fa-f]{{32}})',
                          text, re.IGNORECASE)
            if m and int(m.group(1), 16):
                keys[slot] = int(m.group(1), 16)
        m = re.search(r'slot0x2CKeyX\s*=\s*([0-9A-Fa-f]{32})', text, re.IGNORECASE)
        if m and int(m.group(1), 16) != keys[0x2C]:
            raise ConversionError(
                'aes_keys.txt and boot9.bin disagree on slot 0x2C - one of them '
                'is wrong or from a different console dump')
    return keys


def load_seeddb(path=None):
    """Read seeddb.bin into {program_id: 16-byte seed}."""
    path = Path(path) if path else _key_path('seeddb')
    if not path or not Path(path).exists():
        raise ConversionError(
            'this title uses seed crypto, which needs seeddb.bin in '
            f'{KEYS_DIR}')
    data = Path(path).read_bytes()
    if len(data) < SEEDDB_HEADER:
        raise ConversionError(f'{Path(path).name} is too small to be a seeddb')
    count = _u32le(data, 0)
    if len(data) < SEEDDB_HEADER + count * SEEDDB_ENTRY:
        raise ConversionError(
            f'{Path(path).name} claims {count} seeds but is only '
            f'{len(data)} bytes - truncated or not a seeddb')
    seeds = {}
    for i in range(count):
        off = SEEDDB_HEADER + i * SEEDDB_ENTRY
        seeds[_u64le(data, off)] = data[off + 8:off + 0x18]
    return seeds


def _seeded_key_y(header, seeds):
    """The secondary KeyY of a seed-crypto NCCH.

    The header carries the first four bytes of SHA-256(seed || program ID) at
    0x114, so a wrong or stale seed is caught here instead of producing noise."""
    import hashlib
    program_id = _u64le(header, 0x118)
    seed = seeds.get(program_id)
    if seed is None:
        raise ConversionError(
            f'title {program_id:016X} uses seed crypto but is not in '
            'seeddb.bin - a newer seeddb is needed')
    check = hashlib.sha256(seed + program_id.to_bytes(8, 'little')).digest()
    if check[:4] != header[0x114:0x118]:
        raise ConversionError(
            f'the seeddb.bin seed for {program_id:016X} does not match the '
            "title's seed checksum - wrong or corrupt seed")
    return int.from_bytes(
        hashlib.sha256(header[0x00:0x10] + seed).digest()[:16], 'big')


def _ncch_counter(partition_id, version, section):
    """The AES-CTR counter for one NCCH section."""
    if version in (0, 2):
        return partition_id.to_bytes(8, 'big') + bytes([section]) + bytes(7)
    if version == 1:
        return partition_id.to_bytes(8, 'little') + bytes(8)
    raise ConversionError(f'unsupported NCCH version {version}')


def _aes_ctr(key_int, counter):
    from Crypto.Cipher import AES
    from Crypto.Util import Counter
    ctr = Counter.new(128, initial_value=int.from_bytes(counter, 'big'))
    return AES.new(key_int.to_bytes(16, 'big'), AES.MODE_CTR, counter=ctr)


class NcchRegion:
    """One encrypted span of an NCCH, with the key and counter it needs."""

    def __init__(self, name, offset, size, key, counter, skip=0):
        self.name, self.offset, self.size = name, offset, size
        self.key, self.counter = key, counter
        self.skip = skip                   # keystream bytes to burn: mid-block start

    def __repr__(self):
        return f'<{self.name} @0x{self.offset:X} +0x{self.size:X}>'


def _split_exefs(region, table, primary, secondary):
    """Break the ExeFS region into same-key spans, driven by its plaintext
    header. Files are not always block-aligned at their END, so a span can
    start mid AES block; `skip` carries the keystream position across."""
    ctr = int.from_bytes(region.counter, 'big')

    def span(name, start, end, key):
        return NcchRegion(name, region.offset + start, end - start, key,
                          ((ctr + start // 0x10) & M128).to_bytes(16, 'big'),
                          start % 0x10)

    files = []
    for i in range(10):
        name = table[i * 16:i * 16 + 8].rstrip(b'\x00')
        off, size = struct.unpack_from('<II', table, i * 16 + 8)
        if not name or not size or name in EXEFS_PRIMARY_FILES:
            continue
        start = EXEFS_HEADER_LEN + off
        if start + size > region.size:
            raise ConversionError(
                f'ExeFS entry {name!r} lies outside the ExeFS - the header did '
                'not decrypt to a valid file table')
        files.append((start, start + size, name))

    out, pos = [], 0
    for start, end, name in sorted(files):
        if start < pos:
            raise ConversionError('ExeFS entries overlap')
        if start > pos:
            out.append(span('exefs', pos, start, primary))
        out.append(span('exefs:' + name.decode('ascii', 'replace'),
                        start, end, secondary))
        pos = end
    if pos < region.size:
        out.append(span('exefs', pos, region.size, primary))
    return out


def _ncch_regions(header, base, keys, encrypt=False, seeds=None,
                  enc_method=0x00):
    """Work out which spans of one NCCH need transforming, and with what.

    `seeds` is only consulted for seed-crypto titles; pass None to have them
    refused. The ExeFS region's key is None when primary != secondary - the
    caller must split it with _split_exefs, which needs the file table."""
    if header[0x100:0x104] != b'NCCH':
        raise ConversionError('not an NCCH partition')
    key_y = int.from_bytes(header[0x00:0x10], 'big')
    partition_id = struct.unpack_from('<Q', header, 0x108)[0]
    version = struct.unpack_from('<H', header, 0x112)[0]
    flags = header[0x188:0x190]
    method, f7 = flags[3], flags[7]

    if bool(f7 & NCCH_NOCRYPTO) != bool(encrypt):
        raise ConversionError('partition is already '
                              + ('encrypted' if encrypt else 'decrypted'))
    if method not in CRYPTO_METHOD_SLOT:
        raise ConversionError(f'unknown NCCH crypto method 0x{method:02X}')

    if encrypt:
        method = enc_method                # see ncch_crypt for why
    slot = CRYPTO_METHOD_SLOT[method]
    if not f7 & NCCH_FIXEDKEY and slot not in keys:
        raise ConversionError(
            f'this title uses crypto method 0x{method:02X}, which needs '
            f'slot0x{slot:02X}KeyX in aes_keys.txt')
    if f7 & NCCH_FIXEDKEY:
        primary = secondary = 0            # zero key, used by some system titles
    else:
        secondary_y = key_y
        if f7 & NCCH_FLAG_SEED:
            if seeds is None:
                raise ConversionError(
                    'this title uses seed crypto, which needs seeddb.bin')
            secondary_y = _seeded_key_y(header, seeds)
        primary = _scramble_key(keys[0x2C], key_y)
        secondary = _scramble_key(keys[CRYPTO_METHOD_SLOT[method]], secondary_y)

    exh_size = struct.unpack_from('<I', header, 0x180)[0]
    exefs_off = struct.unpack_from('<I', header, 0x1A0)[0] * MEDIA_UNIT_SIZE
    exefs_size = struct.unpack_from('<I', header, 0x1A4)[0] * MEDIA_UNIT_SIZE
    romfs_off = struct.unpack_from('<I', header, 0x1B0)[0] * MEDIA_UNIT_SIZE
    romfs_size = struct.unpack_from('<I', header, 0x1B4)[0] * MEDIA_UNIT_SIZE

    regions = []
    if exh_size:
        regions.append(NcchRegion(
            'exheader', base + MEDIA_UNIT_SIZE, EXHEADER_LEN, primary,
            _ncch_counter(partition_id, version, SECTION_EXHEADER)))
    if exefs_size:
        regions.append(NcchRegion(
            'exefs', base + exefs_off, exefs_size,
            primary if primary == secondary else None,
            _ncch_counter(partition_id, version, SECTION_EXEFS)))
    if romfs_size:
        regions.append(NcchRegion(
            'romfs', base + romfs_off, romfs_size, secondary,
            _ncch_counter(partition_id, version, SECTION_ROMFS)))
    return regions, primary, secondary, method


def _transform_region(fh_in, fh_out, region, progress=None, label=''):
    """AES-CTR a span in place, streaming, preserving cipher state."""
    cipher = _aes_ctr(region.key, region.counter)
    if region.skip:
        cipher.decrypt(bytes(region.skip))
    pos, left = region.offset, region.size
    while left > 0:
        # re-seek both every chunk: ncch_crypt passes ONE handle as fh_in and
        # fh_out, and the read has already moved it past where the write goes
        fh_in.seek(pos)
        chunk = fh_in.read(min(CHUNK, left))
        if not chunk:
            break
        fh_out.seek(pos)
        fh_out.write(cipher.decrypt(chunk))
        pos += len(chunk)
        left -= len(chunk)
        if progress:
            progress(region.size - left, region.size,
                     f'{label}{region.name}')


def _check_decrypted(path, regions):
    """Fail loudly if the result does not look like plaintext.

    Without this a wrong key would write a file full of noise that still passes
    every structural check; the ExeFS header is the cheapest honest oracle,
    since its first entry name is always printable ASCII."""
    exefs = next((r for r in regions if r.name == 'exefs'), None)
    romfs = next((r for r in regions if r.name == 'romfs'), None)
    notes = []
    with open(path, 'rb') as f:
        if exefs:
            f.seek(exefs.offset)
            head = f.read(0x40)
            names = [head[i * 16:i * 16 + 8].rstrip(b'\x00') for i in range(4)]
            first = names[0]
            ok = bool(first) and all(0x20 <= c <= 0x7E for c in first)
            known = {b'.code', b'icon', b'banner', b'logo'}
            if ok and not (set(n for n in names if n) & known):
                ok = False
            if not ok:
                return False, 'ExeFS header is not plaintext after the pass'
            notes.append('ExeFS names look sane: ' + ', '.join(
                n.decode('ascii', 'replace') for n in names if n))
        # The ExeFS header only ever proves the PRIMARY key. RomFS always uses
        # the secondary one, so its IVFC magic is what catches a bad seed or a
        # wrong 7.x/9.x keyslot.
        if romfs:
            f.seek(romfs.offset)
            if f.read(4) != b'IVFC':
                return False, 'RomFS is not plaintext after the pass (no IVFC)'
            notes.append('RomFS IVFC present')
    return True, '; '.join(notes) or 'no ExeFS or RomFS to check'


def ncch_crypt(src, dst, encrypt, progress=None, method=0x00, seed=False):
    """Decrypt, or re-encrypt, every NCCH in a CCI/CXI.

    AES-CTR is its own inverse, so one pass serves both directions. The catch is
    metadata, not cipher: decryption zeroes the crypto-method byte and clears
    the seed flag, so nothing records how a title was originally keyed. When
    re-encrypting, `method` and `seed` say how to key the CONTENT partition
    (the first NCCH); every other partition - manual, Download Play child,
    update data - always uses the standard key, as retail carts do. The right
    choice cannot be read from a decrypted file; ncch_encrypt_matching finds it
    against a DAT."""
    keys = load_boot9_keyx()
    if encrypt and method not in CRYPTO_METHOD_SLOT:
        raise ConversionError(f'unknown NCCH crypto method 0x{method:02X}')
    seeds = None                           # loaded on the first seeded partition
    shutil.copyfile(src, dst)
    first_regions = None
    seeded = 0

    with open(dst, 'r+b') as f:
        head = f.read(0x200)
        partitions = []
        if head[0x100:0x104] == b'NCSD':
            for i in range(8):
                off, size = struct.unpack_from('<II', head, 0x120 + i * 8)
                if size:
                    partitions.append(off * MEDIA_UNIT_SIZE)
        elif head[0x100:0x104] == b'NCCH':
            partitions.append(0)
        else:
            raise ConversionError('not an NCSD or NCCH image')

        touched = 0
        for base in partitions:
            f.seek(base)
            ncch_head = f.read(0x200)
            if ncch_head[0x100:0x104] != b'NCCH':
                continue
            content = touched == 0
            part_method = method if (encrypt and content) else 0x00
            if encrypt and content and seed and \
                    ncch_head[0x18F] & NCCH_NOCRYPTO:
                ncch_head = bytearray(ncch_head)
                ncch_head[0x18F] |= NCCH_FLAG_SEED
                ncch_head = bytes(ncch_head)
            f7 = ncch_head[0x18F]
            # only a partition we will actually transform may demand seeddb,
            # or "already decrypted" would surface as "seeddb missing"
            is_seeded = bool(f7 & NCCH_FLAG_SEED) and not f7 & NCCH_FIXEDKEY \
                and bool(f7 & NCCH_NOCRYPTO) == bool(encrypt)
            if is_seeded and seeds is None:
                seeds = load_seeddb()
            seeded += is_seeded
            regions, primary, secondary, used = _ncch_regions(
                ncch_head, base, keys, encrypt, seeds, part_method)
            if primary != secondary:
                exefs = next((r for r in regions if r.name == 'exefs'), None)
                if exefs:
                    f.seek(exefs.offset)
                    table = f.read(EXEFS_HEADER_LEN)
                    if not encrypt:
                        table = _aes_ctr(primary, exefs.counter).decrypt(table)
                    i = regions.index(exefs)
                    regions[i:i + 1] = _split_exefs(exefs, table, primary,
                                                    secondary)
            for region in regions:
                _transform_region(f, f, region, progress,
                                  f'partition {touched}: ')
            flags = bytearray(ncch_head[0x188:0x190])
            if encrypt:
                flags[3] = part_method
                flags[7] &= ~NCCH_NOCRYPTO
            else:
                flags[3] = 0x00
                flags[7] = (flags[7] & ~NCCH_FLAG_SEED) | NCCH_NOCRYPTO
            f.seek(base + 0x188)
            f.write(bytes(flags))
            if first_regions is None:
                first_regions = regions
            touched += 1

        if not touched:
            raise ConversionError('no NCCH partitions found')

    if not encrypt:
        ok, why = _check_decrypted(dst, first_regions)
        if not ok:
            Path(dst).unlink(missing_ok=True)
            raise ConversionError(
                f'decryption produced non-plaintext output: {why}')
    else:
        why = (f'content keyed with method 0x{method:02X}'
               + (' + seed' if seed else '')
               if (method or seed) else 're-encrypted with the standard key (0x2C)')
    if seeded:
        why += f'; {seeded} seed-crypto partition(s) keyed from seeddb.bin'
    return {'partitions': touched, 'oracle': why}


# Optional DatIndex. When set, keyed engines that lose information on the way
# in (3DS re-encryption) use it to find the one output that is the real dump.
REFERENCE_DATS = None


def _ncch_key_candidates(src, keys):
    """(method, seed) combinations worth trying for a decrypted title, most
    common first. Seeded variants are offered only when the title carries a
    seed checksum and seeddb.bin knows its program ID."""
    out = [(m, False) for m in (0x00, 0x01, 0x0A, 0x0B)
           if CRYPTO_METHOD_SLOT[m] in keys]
    try:
        with open(src, 'rb') as f:
            head = f.read(0x200)
            base = 0
            if head[0x100:0x104] == b'NCSD':
                base = struct.unpack_from('<I', head, 0x120)[0] * MEDIA_UNIT_SIZE
            f.seek(base)
            ncch = f.read(0x200)
        if ncch[0x114:0x118] != bytes(4) and _key_path('seeddb'):
            if _u64le(ncch, 0x118) in load_seeddb():
                out += [(m, True) for m, _ in list(out)]
    except (OSError, ConversionError):
        pass
    return out


def ncch_encrypt_matching(src, dst, progress=None, dats=None):
    """Re-encrypt, choosing the content key that reproduces a DAT entry.

    Without a DAT this is plain standard-key re-encryption. With one, each
    candidate key is tried until the output's hash is in the DAT - one full
    pass per candidate, so a 4 GB title can take several minutes."""
    dats = dats if dats is not None else REFERENCE_DATS
    info = ncch_crypt(src, dst, True, progress)
    if dats is None:
        return info
    rec, _ = dats.lookup(dst)
    if rec:
        return dict(info, oracle=info['oracle'] + f'; matches DAT "{rec["game"]}"')
    tried = ['0x00']
    for method, seed in _ncch_key_candidates(src, load_boot9_keyx())[1:]:
        info = ncch_crypt(src, dst, True, progress, method=method, seed=seed)
        tried.append(f'0x{method:02X}' + ('+seed' if seed else ''))
        rec, _ = dats.lookup(dst)
        if rec:
            return dict(info, oracle=info['oracle']
                        + f'; matches DAT "{rec["game"]}"')
    info = ncch_crypt(src, dst, True, progress)
    return dict(info, oracle=info['oracle'] + '; no DAT match with any key ('
                + ', '.join(tried) + ') - standard key used')


def _wire_3ds_crypto():
    CONVERSIONS['3ds:encrypted->decrypted'].update(
        fn=lambda s, d, p=None: ncch_crypt(s, d, False, p),
        ext='.3ds', inverse=None, verify_mode='none',
        note='AES-CTR decryption; verified by the ExeFS and RomFS plaintext '
             'checks inside the engine.')
    CONVERSIONS['3ds:decrypted->encrypted'].update(
        fn=lambda s, d, p=None: ncch_encrypt_matching(s, d, p),
        ext='.3ds', inverse='3ds:encrypted->decrypted', verify_mode='roundtrip',
        note='Re-encrypts the content partition with whichever key reproduces a '
             'DAT entry (7.x, New 3DS and seeded keys are tried) when reference '
             'DATs are loaded; otherwise with the standard keyslot 0x2C.')


_wire_3ds_crypto()


# ══════════════════════════════════════════════════════════════════════════════
#  DAT VERIFICATION
#
#  The strongest oracle available: hash a converted file and look it up in a
#  known-good DAT. Unlike the built-in plaintext checks, this proves the output
#  is byte-identical to the canonical dump rather than merely well-formed.
#
#  Parsing is reused from dat_merger rather than reimplemented, so both tools
#  agree on what a DAT says.
# ══════════════════════════════════════════════════════════════════════════════

class DatIndex:
    """Hash -> DAT entry lookup, built from one or more DAT files."""

    def __init__(self):
        self.by_sha1 = {}
        self.by_md5 = {}
        self.by_crc_size = {}
        self.dats = []
        self.rom_count = 0

    def add_dat(self, path):
        from dat_merger import parse_dat_file
        header, entries = parse_dat_file(Path(path))
        name = getattr(header, 'name', None) or Path(path).stem
        added = 0
        for entry in entries:
            for rom in entry.roms:
                rec = {'dat': name, 'game': entry.name,
                       'rom': rom.get('name', ''), 'size': rom.get('size')}
                if rom.get('sha1'):
                    self.by_sha1[rom['sha1'].lower().strip()] = rec
                if rom.get('md5'):
                    self.by_md5[rom['md5'].lower().strip()] = rec
                crc, size = rom.get('crc'), rom.get('size')
                if crc and size:
                    key = f"{str(crc).lower().strip().zfill(8)}_{size}"
                    self.by_crc_size[key] = rec
                added += 1
        self.dats.append({'path': str(path), 'name': name, 'roms': added})
        self.rom_count += added
        return added

    def __len__(self):
        return self.rom_count

    def lookup(self, path, progress=None):
        """Hash a file once and try every index. Returns (rec, how) or (None, '')."""
        crc, md5, sha1, size = hash_file(path, progress)
        rec = self.by_sha1.get(sha1)
        if rec:
            return rec, 'sha1'
        rec = self.by_md5.get(md5)
        if rec:
            return rec, 'md5'
        rec = self.by_crc_size.get(f'{crc}_{size}')
        if rec:
            return rec, 'crc+size'
        return None, ''


def hash_file(path, progress=None):
    """CRC32, MD5 and SHA1 in a single pass. Returns (crc, md5, sha1, size)."""
    crc = 0
    md5 = hashlib.md5()
    sha1 = hashlib.sha1()
    total = os.path.getsize(path)
    done = 0
    with open(path, 'rb') as f:
        while True:
            b = f.read(CHUNK)
            if not b:
                break
            crc = binascii.crc32(b, crc)
            md5.update(b)
            sha1.update(b)
            done += len(b)
            if progress:
                progress(done, total, 'hashing')
    return (f'{crc & 0xFFFFFFFF:08x}', md5.hexdigest(), sha1.hexdigest(), total)


def build_dat_index(paths, progress=None):
    """Index every DAT under the given files or folders."""
    index = DatIndex()
    files = []
    for p in paths:
        p = Path(p)
        if p.is_dir():
            files += sorted(list(p.rglob('*.dat')) + list(p.rglob('*.xml')))
        elif p.is_file():
            files.append(p)
    for i, f in enumerate(files, 1):
        try:
            index.add_dat(f)
        except Exception as e:
            if progress:
                progress(i, len(files), f'skipped {f.name}: {e}')
            continue
        if progress:
            progress(i, len(files), f'indexed {f.name}')
    return index


def _api_verify_dats(self, files, dat_paths):
    """Check converted files against known-good DATs.

    Bound onto RomToolsAPI below so the GUI can run the same check the user
    would otherwise do by hand in RomVault."""
    try:
        index = build_dat_index(dat_paths)
    except Exception as e:
        return {'ok': False, 'error': f'{type(e).__name__}: {e}'}
    if not len(index):
        return {'ok': False, 'error': 'no ROM entries found in those DATs'}
    results = []
    for f in files:
        try:
            rec, how = index.lookup(f)
        except OSError as e:
            results.append({'path': f, 'match': False, 'error': str(e)})
            continue
        results.append({'path': f, 'match': bool(rec), 'how': how,
                        'game': rec['game'] if rec else '',
                        'dat': rec['dat'] if rec else ''})
    matched = sum(1 for r in results if r.get('match'))
    return {'ok': True, 'results': results, 'matched': matched,
            'total': len(results), 'indexed': len(index),
            'dats': index.dats}


RomToolsAPI.verify_against_dats = _api_verify_dats


# ══════════════════════════════════════════════════════════════════════════════
#  TIER 3  -  PC Engine, Apple II, Amiga, Xbox
# ══════════════════════════════════════════════════════════════════════════════

# ── PC Engine / TurboGrafx-16 ─────────────────────────────────────────────────
# Same idea as the SNES copier header: 512 arbitrary bytes on the front. A HuCard
# dump is a whole number of 8 KiB banks, so a 512-byte remainder is the header.

PCE_HEADER = 512
PCE_BANK = 8192


def _detect_pcengine(buf, path, fh):
    ext = path.suffix.lower()
    if ext not in ('.pce', '.sgx'):
        return None
    size = fh['size']
    if size % PCE_BANK == PCE_HEADER:
        return _result('PCE', 'PCE', 'headered', confidence='medium',
                       detail=f'{(size - PCE_HEADER) // 1024} KiB HuCard plus a '
                              '512-byte copier header',
                       conversions=['pce:headered->headerless'],
                       meta={'header': bytes(_at(buf, 0, PCE_HEADER)).hex()})
    if size % PCE_BANK == 0 and size:
        return _result('PCE', 'PCE', 'headerless', confidence='medium',
                       detail=f'{size // 1024} KiB HuCard, no copier header',
                       conversions=['pce:headerless->headered'])
    return None


strip_pce_header = _strip_fixed(PCE_HEADER)


def add_pce_header(src, dst, header=None, progress=None):
    """A PC Engine copier header carries nothing an emulator reads, so a
    zero-filled one is correct when the original was not kept."""
    header = bytes(header) if header else bytes(PCE_HEADER)
    if len(header) != PCE_HEADER:
        raise ConversionError('PC Engine header must be exactly 512 bytes')
    with open(src, 'rb') as fi, open(dst, 'wb') as fo:
        fo.write(header)
        _copy_rest(fi, fo, os.path.getsize(src), progress, 'adding header')
    return dst


# ── Apple II ──────────────────────────────────────────────────────────────────
# A 5.25" image is 35 tracks x 16 sectors x 256 bytes. DOS order (.do/.dsk) and
# ProDOS order (.po) differ only in how the 16 sectors of each track are laid
# out. The mapping is an involution - applying it twice is the identity - so one
# table serves both directions and a round trip is exact by construction.

APPLE_TRACKS = 35
APPLE_SECTORS = 16
APPLE_SECTOR = 256
APPLE_TRACK = APPLE_SECTORS * APPLE_SECTOR          # 4096
APPLE_DISK = APPLE_TRACKS * APPLE_TRACK             # 143360

DO_PO_MAP = (0x0, 0xE, 0xD, 0xC, 0xB, 0xA, 0x9, 0x8,
             0x7, 0x6, 0x5, 0x4, 0x3, 0x2, 0x1, 0xF)

TWOMG_MAGIC = b'2IMG'
TWOMG_HEADER = 64


def _detect_apple(buf, path, fh):
    if _at(buf, 0, 4) == TWOMG_MAGIC:
        creator = _clean(_at(buf, 4, 4), 4)
        fmt = _u32le(buf, 0x0C) if _have(buf, 0x10, 4) else 0
        order = {0: 'DOS order', 1: 'ProDOS order', 2: 'NIB'}.get(fmt, f'{fmt}')
        return _result('APPLE2', '2MG', 'headered',
                       detail=f'2IMG by "{creator}", {order}',
                       conversions=['apple:2mg->raw'],
                       meta={'header': bytes(_at(buf, 0, TWOMG_HEADER)).hex(),
                             'format': fmt})
    ext = path.suffix.lower()
    if ext in ('.do', '.dsk', '.po') and fh['size'] == APPLE_DISK:
        po = ext == '.po'
        return _result('APPLE2', 'PO' if po else 'DO',
                       'prodos-order' if po else 'dos-order',
                       confidence='low',
                       detail=f'140 KiB 5.25" image, '
                              f'{"ProDOS" if po else "DOS 3.3"} sector order '
                              '(inferred from the extension)',
                       conversions=['apple:po->do', 'apple:dsk->nib'] if po
                       else ['apple:do->po', 'apple:dsk->nib'])
    return None


def _reorder_apple(src, dst, progress=None):
    """Swap between DOS and ProDOS sector order. Self-inverse."""
    size = os.path.getsize(src)
    if size % APPLE_TRACK:
        raise ConversionError(
            f'{size:,} bytes is not a whole number of {APPLE_TRACK}-byte '
            'tracks, so the sector order cannot be remapped safely')
    tracks = size // APPLE_TRACK
    with open(src, 'rb') as fi, open(dst, 'wb') as fo:
        for t in range(tracks):
            track = fi.read(APPLE_TRACK)
            out = bytearray(APPLE_TRACK)
            for s in range(APPLE_SECTORS):
                src_s = DO_PO_MAP[s]
                out[s * APPLE_SECTOR:(s + 1) * APPLE_SECTOR] = \
                    track[src_s * APPLE_SECTOR:(src_s + 1) * APPLE_SECTOR]
            fo.write(out)
            if progress:
                progress(t + 1, tracks, 'reordering sectors')
    return dst


strip_2mg_header = _strip_fixed(TWOMG_HEADER)
add_2mg_header = _add_fixed(TWOMG_HEADER, lambda h: h[0:4] == TWOMG_MAGIC)


# ── Amiga DMS ─────────────────────────────────────────────────────────────────

def _detect_dms(buf, path, fh):
    if _at(buf, 0, 4) != b'DMS!':
        return None
    return _result('AMIGA', 'DMS', 'compressed',
                   detail='Amiga DMS compressed disk image',
                   conversions=['amiga:dms->adf'],
                   notes=['DMS is decompress-only; there is no re-compressor.'])


def _detect_adf(buf, path, fh):
    if path.suffix.lower() != '.adf':
        return None
    # A standard Amiga floppy is 880 KiB; the bootblock names the filesystem.
    if fh['size'] not in (901120, 1802240):
        return None
    fs = _at(buf, 0, 3)
    kind = {b'DOS': 'AmigaDOS'}.get(fs, 'unknown/bootable')
    return _result('AMIGA', 'ADF', 'plain', confidence='medium',
                   detail=f'{fh["size"] // 1024} KiB Amiga disk image ({kind})',
                   conversions=[])


def dms_to_adf(src, dst, progress=None):
    """xdms-rs takes an explicit output path: `xdms-rs u <in.dms> <out.adf>`."""
    exe = _tool_path('xdms')
    if not exe:
        raise ConversionError('xdms-rs.exe not available')
    _run_tool([str(exe), 'u', str(src), str(dst)], progress, 'dms -> adf')
    if not Path(dst).exists() or os.path.getsize(dst) == 0:
        raise ConversionError('xdms-rs produced no output')
    return dst


def dms_verify(src, out=None, progress=None):
    """Test the DMS archive's own CRCs. This checks the SOURCE, which is the
    only integrity claim available: DMS has no re-compressor, so the ADF cannot
    be turned back into a DMS for comparison."""
    exe = _tool_path('xdms')
    if not exe:
        return False, 'xdms-rs.exe not available'
    try:
        _run_tool([str(exe), 't', str(src)], progress, 'verify')
        return True, 'source DMS passed its own CRC check'
    except ConversionError as e:
        return False, str(e)


# ── Xbox ──────────────────────────────────────────────────────────────────────
# The XDVDFS volume descriptor is a fixed string at sector 32 of the game
# partition. Where that partition starts depends on how the disc was imaged.

XBOX_MAGIC = b'MICROSOFT*XBOX*MEDIA'
XBOX_BASES = {
    0x00000000: 'XISO (trimmed)',
    0x18300000: 'XGD1 (redump)',
    0x1FB20000: 'XGD2 (redump)',
    0x02080000: 'XGD3 (redump)',
}


def _detect_xbox(buf, path, fh):
    for base, label in XBOX_BASES.items():
        probe = base + 0x10000
        if probe + 0x14 > fh['size']:
            continue
        if _read_at(buf, fh, probe, 0x14) == XBOX_MAGIC:
            return _result('XBOX', 'XISO', label.split()[0].lower(),
                           detail=f'Xbox disc image - {label}',
                           conversions=[],
                           notes=['Rebuilding/trimming an XISO needs '
                                  'extract-xiso; detection only for now.'])
    return None


# ── registry ──────────────────────────────────────────────────────────────────

_hdr_pair('pce:headered->headerless', 'pce:headerless->headered',
          'PC Engine', lambda s, d, p=None: strip_pce_header(s, d, p),
          lambda s, d, h=None, p=None: add_pce_header(s, d, h, p),
          '.pce', 'PCE', add_needs_header=False)

_hdr_pair('apple:2mg->raw', 'apple:raw->2mg',
          'Apple II 2IMG', lambda s, d, p=None: strip_2mg_header(s, d, p),
          lambda s, d, h=None, p=None: add_2mg_header(s, d, h, p),
          '.po', 'APPLE2')

CONVERSIONS['apple:do->po'] = {
    'label': 'Apple II: DOS order -> ProDOS order', 'engine': ENGINE_NATIVE,
    'system': 'APPLE2', 'ext': '.po',
    'fn': lambda s, d, p=None: _reorder_apple(s, d, p),
    'inverse': 'apple:po->do',
}
CONVERSIONS['apple:po->do'] = {
    'label': 'Apple II: ProDOS order -> DOS order', 'engine': ENGINE_NATIVE,
    'system': 'APPLE2', 'ext': '.do',
    'fn': lambda s, d, p=None: _reorder_apple(s, d, p),
    'inverse': 'apple:do->po',
}

# The original xdms.exe shipped here was built against an old MSVC runtime and
# fails to start ("side-by-side configuration is incorrect"). xdms-rs is a
# statically-linked pure-Rust reimplementation, so it has no runtime to miss.
EXTERNAL_TOOLS['xdms'] = (HERE / 'apps' / 'xdms-rs.exe',
                          'xdms-rs.exe - Amiga DMS decompressor (static build)')
CONVERSIONS['amiga:dms->adf'] = {
    'label': 'Amiga: DMS -> ADF (decompress)', 'engine': ENGINE_EXTERNAL,
    'system': 'AMIGA', 'ext': '.adf', 'requires': 'xdms',
    'why': EXTERNAL_TOOLS['xdms'][1],
    'fn': lambda s, d, p=None: dms_to_adf(s, d, p),
    'inverse': None, 'verify_mode': 'verifier',
    'verifier': dms_verify,
    'note': 'DMS has no re-compressor, so the ADF cannot be round-tripped; the '
            'source archive CRCs are checked instead.',
}

# Xbox goes ahead of the ISO9660 probe: a Redump Xbox image opens with a small
# ISO9660 video partition, so the generic probe would claim it first.
DETECTORS.insert(DETECTORS.index(_detect_psp), _detect_xbox)
DETECTORS.extend([_detect_dms, _detect_apple, _detect_adf, _detect_pcengine])


# ── DAT verification, exposed to the GUI ──────────────────────────────────────

def _api_browse_dats(self):
    """Multi-select file picker for DAT files."""
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk(); root.withdraw()
        root.attributes('-topmost', True)
        paths = filedialog.askopenfilenames(
            title='Select DAT files',
            filetypes=[('DAT files', '*.dat *.xml'), ('All files', '*.*')])
        root.destroy()
        return list(paths)
    except Exception:
        return []


def _api_start_dat_verify(self, folder, dat_paths):
    """Hash every file under folder and look it up in the given DATs.

    Runs on a worker thread: indexing a full No-Intro set and hashing a folder
    of disc images both take real time."""
    if self._busy:
        return {'ok': False, 'error': 'already running'}
    folder = (folder or '').strip()
    if not folder or not Path(folder).is_dir():
        return {'ok': False, 'error': 'pick a folder to verify'}
    if not dat_paths:
        return {'ok': False, 'error': 'pick at least one DAT'}
    self._stop.clear()
    self._busy = True
    threading.Thread(target=self._dat_verify_thread,
                     args=(folder, list(dat_paths)), daemon=True).start()
    return {'ok': True}


def _dat_verify_thread(self, folder, dat_paths):
    try:
        self._log(f'Indexing {len(dat_paths)} DAT file(s)...', 'info')
        index = build_dat_index(
            dat_paths,
            progress=lambda i, n, msg: self._emit('scan_progress',
                                                  {'done': i, 'total': n,
                                                   'current': msg}))
        if not len(index):
            self._log('No ROM entries found in those DATs.', 'err')
            return
        for d in index.dats:
            self._log(f'  {d["name"]}: {d["roms"]:,} roms', 'dim')

        files = [p for p in Path(folder).rglob('*') if p.is_file()]
        self._log(f'Hashing {len(files)} file(s) in {folder} ...', 'info')
        matched = missed = 0
        for i, f in enumerate(files, 1):
            if self._stop.is_set():
                self._log('Stopped by user.', 'warn')
                break
            try:
                rec, how = index.lookup(f)
            except OSError as e:
                self._log(f'  ERROR {f.name}: {e}', 'err')
                continue
            if rec:
                matched += 1
                self._log(f'  MATCH {f.name}  ->  {rec["game"]}  '
                          f'[{rec["dat"]}, by {how}]', 'ok')
            else:
                missed += 1
                self._log(f'  MISS  {f.name}  (not in any supplied DAT)', 'warn')
            self._emit('scan_progress',
                       {'done': i, 'total': len(files), 'current': f.name})
        self._log(f'DAT check complete - {matched} matched, {missed} not found.',
                  'ok' if matched and not missed else 'warn')
    except Exception as e:
        self._log(f'DAT verify failed: {type(e).__name__}: {e}', 'err')
    finally:
        self._busy = False
        self._emit('idle', {})


RomToolsAPI.browse_dats = _api_browse_dats
RomToolsAPI.start_dat_verify = _api_start_dat_verify
RomToolsAPI._dat_verify_thread = _dat_verify_thread



# ══════════════════════════════════════════════════════════════════════════════
#  RETRO FORMATS  -  Famicom Disk System QD, Casio Loopy, C64, ZX Spectrum,
#  Apple II disk containers, headerless Atari carts
#
#  Added against real No-Intro/TOSEC samples (2026-09-13). Most computer
#  formats are detection-only for now: knowing what a file IS comes first, and
#  a wrong conversion there is worse than none.
# ══════════════════════════════════════════════════════════════════════════════

# ── Famicom Disk System: raw FDS <-> QD ───────────────────────────────────────
# Both hold the same blocks: disk info (0x38), file count (2), then a 16-byte
# header and data block per file. QD is the raw Quick Disk layout: every block
# is followed by its CRC-16 and a side is 0x10000 bytes. Raw FDS drops the CRCs
# and a side is 65500 bytes. The CRC is fully determined by the block, so the
# conversion is exact in both directions - PROVIDED nothing follows the last
# block, which is checked rather than assumed.

FDS_QD_SIDE = 0x10000
FDS_MAGIC = b'*NINTENDO-HVC*'


def _fds_crc(block):
    """The disk drive's CRC-16 (poly 0x8408, init 0x8000, 16 zero bits
    flushed). Verified against every block of a real QD dump."""
    crc = 0x8000
    for b in bytes(block) + b'\x00\x00':
        for bit in range(8):
            carry = crc & 1
            crc = (crc >> 1) | (((b >> bit) & 1) << 15)
            if carry:
                crc ^= 0x8408
    return crc


def _fds_side_blocks(side, with_crc):
    """(blocks, end offset). Walks every file block present, including hidden
    files past the file count, which some disks carry."""
    blocks, pos, header = [], 0, None
    step = 2 if with_crc else 0
    while pos < len(side):
        code = side[pos]
        if code == 1 and not blocks:
            n = 0x38
        elif code == 2 and len(blocks) == 1:
            n = 2
        elif code == 3 and len(blocks) >= 2 and header is None:
            n = 16
        elif code == 4 and header is not None:
            n = struct.unpack_from('<H', header, 0x0D)[0] + 1
        else:
            break
        if pos + n + step > len(side):
            raise ConversionError(f'disk block at 0x{pos:X} runs past the side')
        block = side[pos:pos + n]
        if with_crc:
            stored = struct.unpack_from('<H', side, pos + n)[0]
            if stored != _fds_crc(block):
                raise ConversionError(f'QD block CRC mismatch at 0x{pos:X} - '
                                      'damaged or not a QD image')
        blocks.append(block)
        header = block if code == 3 else None
        pos += n + step
    if not blocks or blocks[0][1:15] != FDS_MAGIC:
        raise ConversionError('disk side does not start with a *NINTENDO-HVC* '
                              'info block')
    if any(side[pos:]):
        raise ConversionError(
            f'non-zero data after the last block (0x{pos:X}) - converting would '
            'silently drop it')
    return blocks


def _fds_convert(src, dst, to_qd, progress=None):
    data = Path(src).read_bytes()
    in_side, out_side = (FDS_SIDE, FDS_QD_SIDE) if to_qd else (FDS_QD_SIDE, FDS_SIDE)
    if not data or len(data) % in_side:
        raise ConversionError(f'{len(data):,} bytes is not a whole number of '
                              f'{in_side:,}-byte sides')
    out = bytearray()
    for i in range(len(data) // in_side):
        blocks = _fds_side_blocks(data[i * in_side:(i + 1) * in_side], not to_qd)
        body = b''.join(b + (_fds_crc(b).to_bytes(2, 'little') if to_qd else b'')
                        for b in blocks)
        if len(body) > out_side:
            raise ConversionError(f'side {i + 1} does not fit a {out_side:,}-byte side')
        out += body + bytes(out_side - len(body))
        if progress:
            progress(i + 1, len(data) // in_side, 'converting sides')
    Path(dst).write_bytes(bytes(out))
    return dst


def _detect_fds_raw(buf, path, fh):
    if not (_have(buf, 0x38, 0) and buf[0] == 1 and _at(buf, 1, 14) == FDS_MAGIC):
        return None
    size = fh['size']
    title = _clean(_at(buf, 0x10, 3), 3)
    if size % FDS_QD_SIDE == 0 and \
            _u16le(buf, 0x38) == _fds_crc(_at(buf, 0, 0x38)):
        return _result('FDS', 'QD', 'qd', detail=f'Quick Disk image [{title}], '
                       f'{size // FDS_QD_SIDE} side(s), block CRCs present',
                       conversions=['fds:qd->fds'])
    if size % FDS_SIDE == 0:
        return _result('FDS', 'FDS', 'headerless',
                       detail=f'raw FDS image [{title}], {size // FDS_SIDE} side(s)',
                       conversions=['fds:fds->qd', 'nes:fds-headerless->headered'])
    return None


CONVERSIONS['fds:qd->fds'] = {
    'label': 'FDS: QD -> raw FDS (drop block CRCs)', 'engine': ENGINE_NATIVE,
    'system': 'FDS', 'ext': '.fds',
    'fn': lambda s, d, p=None: _fds_convert(s, d, False, p),
    'inverse': 'fds:fds->qd',
}
CONVERSIONS['fds:fds->qd'] = {
    'label': 'FDS: raw FDS -> QD (recompute block CRCs)', 'engine': ENGINE_NATIVE,
    'system': 'FDS', 'ext': '.qd',
    'fn': lambda s, d, p=None: _fds_convert(s, d, True, p),
    'inverse': 'fds:qd->fds',
}


# ── Casio Loopy: big-endian <-> little-endian ─────────────────────────────────
# The SH-1 is big-endian; a little-endian dump is the same ROM 16-bit swapped.
# A cart begins with two pointers into the 0x0E000000 cartridge space.

def _loopy_ptrs_ok(head):
    return (len(head) >= 8 and struct.unpack_from('>I', head, 0)[0] >> 22 == 0x038
            and struct.unpack_from('>I', head, 4)[0] >> 22 == 0x038)


def _detect_loopy(buf, path, fh):
    if fh['size'] < 0x80000 or fh['size'] % 2:
        return None
    head = bytes(_at(buf, 0, 8))
    for order, data in (('big-endian', head), ('little-endian', _swap16(head))):
        if _loopy_ptrs_ok(data):
            other = 'little-endian' if order == 'big-endian' else 'big-endian'
            return _result('LOOPY', 'BIN', order, confidence='medium',
                           detail=f'Casio Loopy cartridge, {order}',
                           conversions=[f'loopy:{order}->{other}'])
    return None


def _swap16_file(src, dst, progress=None):
    total = os.path.getsize(src)
    done = 0
    with open(src, 'rb') as fi, open(dst, 'wb') as fo:
        while True:
            b = fi.read(CHUNK)             # CHUNK is even, so pairs never split
            if not b:
                break
            fo.write(_swap16(b))
            done += len(b)
            if progress:
                progress(done, total, 'swapping')
    return dst


for _a, _b in (('big-endian', 'little-endian'), ('little-endian', 'big-endian')):
    CONVERSIONS[f'loopy:{_a}->{_b}'] = {
        'label': f'Loopy: {_a} -> {_b}', 'engine': ENGINE_NATIVE,
        'system': 'LOOPY', 'ext': '.bin',
        'fn': lambda s, d, p=None: _swap16_file(s, d, p),
        'inverse': f'loopy:{_b}->{_a}',
    }


# ── Commodore 64 ──────────────────────────────────────────────────────────────
C64_P00_MAGIC = b'C64File\x00'
C64_P00_HEADER = 26
C64_D64_SIZES = {174848: '35 tracks', 175531: '35 tracks + error bytes',
                 196608: '40 tracks', 197376: '40 tracks + error bytes',
                 205312: '42 tracks', 206114: '42 tracks + error bytes'}


def _detect_c64(buf, path, fh):
    size, ext = fh['size'], path.suffix.lower()
    if _at(buf, 0, 16) == b'C64 CARTRIDGE   ':
        name = _clean(_at(buf, 0x20, 32), 32)
        return _result('C64', 'CRT', 'headered',
                       detail=f'C64 cartridge "{name}", hardware type '
                              f'{struct.unpack_from(">H", buf, 0x16)[0]}')
    if _at(buf, 0, 8) == C64_P00_MAGIC:
        name = _clean(_at(buf, 8, 16), 16)
        return _result('C64', 'P00', 'headered',
                       detail=f'PC64 container for "{name}"',
                       conversions=['c64:p00->prg'],
                       meta={'header': bytes(_at(buf, 0, C64_P00_HEADER)).hex()})
    if _at(buf, 0, 19) in (b'C64 tape image file', b'C64S tape image fil') or \
            _at(buf, 0, 14) == b'C64S tape file':
        return _result('C64', 'T64', None, detail='T64 tape container',
                       conversions=['c64:t64->prg'])
    if size in C64_D64_SIZES:
        bam = _read_at(buf, fh, 0x16500, 3)
        if len(bam) == 3 and bam[0] == 18 and bam[2] in (0x41, 0x00):
            return _result('C64', 'D64', None,
                           detail=f'1541 disk image, {C64_D64_SIZES[size]}',
                           conversions=['c64:d64->files'])
    if ext == '.prg' and 2 < size <= 0x10002:
        load = _u16le(buf, 0)
        return _result('C64', 'PRG', None, confidence='low',
                       detail=f'program file, load address ${load:04X}')
    return None


CONVERSIONS['c64:p00->prg'] = {
    'label': 'C64: P00 -> PRG (strip PC64 header)', 'engine': ENGINE_NATIVE,
    'system': 'C64', 'ext': '.prg',
    'fn': _strip_fixed(C64_P00_HEADER), 'inverse': None,
    'rebuild': lambda out, tmp, extra, p=None: _add_fixed(
        C64_P00_HEADER, lambda h: h[:8] == C64_P00_MAGIC)(out, tmp, extra, p),
}


# ── ZX Spectrum ───────────────────────────────────────────────────────────────
def _zx_tap_ok(buf, fh):
    """A TAP is nothing but <u16 length><block> repeated to the exact end."""
    size, pos, blocks = fh['size'], 0, 0
    while pos + 2 <= size and blocks < 4096:
        head = _read_at(buf, fh, pos, 2)
        if len(head) < 2:
            return False
        n = struct.unpack('<H', head)[0]
        if n < 2:
            return False
        pos += 2 + n
        blocks += 1
    return pos == size and blocks > 0


def _detect_zx(buf, path, fh):
    size, ext = fh['size'], path.suffix.lower()
    if _at(buf, 0, 8) == b'ZXTape!\x1a':
        return _result('ZX', 'TZX', None,
                       detail=f'TZX tape v{buf[8]}.{buf[9]:02d}',
                       conversions=['zx:tzx->tap'])
    if _at(buf, 0, 8) == b'SINCLAIR':
        return _result('ZX', 'SCL', None, detail=f'SCL archive, {buf[8]} file(s)',
                       conversions=['zx:scl->trd'])
    if size <= 0x100000 and size % 256 == 0 and size >= 0x900 and \
            _have(buf, 0x8E8, 0) and buf[0x8E7] == 0x10:
        label = _clean(_at(buf, 0x8F5, 8), 8)
        return _result('ZX', 'TRD', None, detail=f'TR-DOS disk "{label}"',
                       conversions=['zx:trd->scl'])
    if ext == '.tap' and _zx_tap_ok(buf, fh):
        return _result('ZX', 'TAP', None, detail='TAP tape (block chain verified)',
                       conversions=['zx:tap->tzx'])
    if ext == '.z80' and size >= 30:
        pc = _u16le(buf, 6)
        extra = _u16le(buf, 30) if _have(buf, 32, 0) else 0
        version = 1 if pc else {23: 2, 54: 3, 55: 3}.get(extra)
        if version:
            return _result('ZX', 'Z80', None, confidence='medium',
                           detail=f'Z80 snapshot v{version}')
    return None


# ── Apple II containers ───────────────────────────────────────────────────────
def _detect_apple_containers(buf, path, fh):
    size, ext = fh['size'], path.suffix.lower()
    sig = bytes(_at(buf, 0, 8))
    if sig[:4] in (b'WOZ1', b'WOZ2') and sig[4:] == b'\xff\n\r\n':
        return _result('APPLE2', 'WOZ', None,
                       detail=f'{sig[:4].decode()} flux-level disk image',
                       conversions=['apple:woz->dsk'])
    if sig[:4] in (b'A2R2', b'A2R3') and sig[4:] == b'\xff\n\r\n':
        return _result('APPLE2', 'A2R', None,
                       detail=f'{sig[:4].decode()} Applesauce raw capture')
    if ext == '.nib' and size in (232960, 223440):
        return _result('APPLE2', 'NIB', None, confidence='low',
                       detail=f'nibble image, {size // 6656} tracks',
                       conversions=['apple:nib->dsk'])
    if ext in ('.po', '.hdv') and size % 512 == 0 and size >= 0x600:
        vol = _read_at(buf, fh, 0x400, 5)
        if len(vol) == 5 and vol[4] >> 4 == 0x0F:
            name = _clean(_read_at(buf, fh, 0x405, vol[4] & 0x0F), 15)
            convs = ['apple:po->do'] if size == APPLE_DISK else []
            return _result('APPLE2', ext[1:].upper(), 'prodos-order',
                           detail=f'ProDOS volume "/{name}", {size // 1024} KiB',
                           conversions=convs)
    if ext == '.edd':
        return _result('APPLE2', 'EDD', None, confidence='low',
                       detail='EDD bit-stream disk image')
    return None


# ── headerless Atari carts (no signature: extension only, low confidence) ────
def _detect_adf_loose(buf, path, fh):
    """ADFs the AmigaDOS probe misses: UAE extended images, and disks with a
    custom (non-DOS) bootblock, which games often use."""
    magic = bytes(_at(buf, 0, 8))
    if magic in (b'UAE-1ADF', b'UAE--ADF'):
        return _result('AMIGA', 'ADF', 'extended',
                       detail=f'UAE extended ADF ({magic.decode()}), track-level')
    if path.suffix.lower() == '.adf' and fh['size'] in (901120, 1802240):
        return _result('AMIGA', 'ADF', 'plain', confidence='low',
                       detail=f'{fh["size"] // 1024} KiB Amiga disk image, '
                              'non-DOS bootblock')
    return None


_ATARI_EXT = {'.a26': ('A2600', 'Atari 2600'), '.a52': ('A5200', 'Atari 5200'),
              '.lyx': ('LYNX', 'Atari Lynx, headerless'), '.bll': ('LYNX', 'Lynx BLL')}


def _detect_atari_raw(buf, path, fh):
    hit = _ATARI_EXT.get(path.suffix.lower())
    if not hit:
        return None
    system, what = hit
    return _result(system, path.suffix[1:].upper(), 'headerless', confidence='low',
                   detail=f'{what} ({fh["size"] // 1024} KiB) - identified by extension')


# Raw FDS/QD must win before the generic detectors; the rest are weak or
# extension-gated and go last.
DETECTORS.insert(DETECTORS.index(_detect_nes) + 1, _detect_fds_raw)
DETECTORS.extend([_detect_loopy, _detect_c64, _detect_zx,
                  _detect_apple_containers, _detect_atari_raw,
                  _detect_adf_loose])



# ══════════════════════════════════════════════════════════════════════════════
#  ZX SPECTRUM TAPES  -  TAP <-> TZX
#
#  TAP is a bare chain of <u16 length><data> blocks. TZX wraps the same data in
#  typed blocks that also carry timing. A TAP therefore maps losslessly onto
#  standard-speed (0x10) TZX blocks. The reverse keeps the DATA of standard,
#  turbo (0x11) and pure-data (0x14) blocks and drops everything else - so it is
#  exact only for a TZX made of standard blocks, and is reported as lossy
#  otherwise rather than passed off as a clean conversion.
# ══════════════════════════════════════════════════════════════════════════════

TZX_MAGIC = b'ZXTape!\x1a'
TZX_VERSION = (1, 20)
TZX_STD_PAUSE = 1000


def _tzx_block_length(b, pos):
    """Length of the block BODY at pos (just past the id byte), per the TZX
    1.20 spec. None for an id this does not know."""
    u8 = lambda o: b[pos + o]
    u16 = lambda o: struct.unpack_from('<H', b, pos + o)[0]
    u24 = lambda o: int.from_bytes(b[pos + o:pos + o + 3], 'little')
    u32 = lambda o: struct.unpack_from('<I', b, pos + o)[0]
    t = b[pos - 1]
    return {
        0x10: lambda: 4 + u16(2), 0x11: lambda: 18 + u24(15), 0x12: lambda: 4,
        0x13: lambda: 1 + u8(0) * 2, 0x14: lambda: 10 + u24(7),
        0x15: lambda: 8 + u24(5), 0x18: lambda: 4 + u32(0), 0x19: lambda: 4 + u32(0),
        0x20: lambda: 2, 0x21: lambda: 1 + u8(0), 0x22: lambda: 0, 0x23: lambda: 2,
        0x24: lambda: 2, 0x25: lambda: 0, 0x26: lambda: 2 + u16(0) * 2, 0x27: lambda: 0,
        0x28: lambda: 2 + u16(0), 0x2A: lambda: 4, 0x2B: lambda: 5,
        0x30: lambda: 1 + u8(0), 0x31: lambda: 2 + u8(1), 0x32: lambda: 2 + u16(0),
        0x33: lambda: 1 + u8(0) * 3, 0x35: lambda: 20 + u32(16), 0x5A: lambda: 9,
    }.get(t, lambda: None)()


def _tzx_blocks(data):
    if data[:8] != TZX_MAGIC or len(data) < 10:
        raise ConversionError('not a TZX file')
    pos, blocks = 10, []
    while pos < len(data):
        t = data[pos]
        n = _tzx_block_length(data, pos + 1)
        if n is None:
            raise ConversionError(f'unknown TZX block 0x{t:02X} at 0x{pos:X}')
        if pos + 1 + n > len(data):
            raise ConversionError(f'TZX block 0x{t:02X} at 0x{pos:X} is truncated')
        blocks.append((t, data[pos + 1:pos + 1 + n]))
        pos += 1 + n
    return (data[8], data[9]), blocks


def _tap_blocks(data):
    pos, out = 0, []
    while pos < len(data):
        if pos + 2 > len(data):
            raise ConversionError('TAP ends inside a block length')
        n = struct.unpack_from('<H', data, pos)[0]
        if pos + 2 + n > len(data):
            raise ConversionError(f'TAP block at 0x{pos:X} runs past the end')
        out.append(data[pos + 2:pos + 2 + n])
        pos += 2 + n
    return out


def tap_to_tzx(src, dst, progress=None):
    blocks = _tap_blocks(Path(src).read_bytes())
    if not blocks:
        raise ConversionError('TAP holds no blocks')
    out = bytearray(TZX_MAGIC + bytes(TZX_VERSION))
    for blk in blocks:
        out += b'\x10' + struct.pack('<HH', TZX_STD_PAUSE, len(blk)) + blk
    Path(dst).write_bytes(bytes(out))
    return dst


def _tzx_payload(blocks):
    """(tap bytes, dropped block ids, standard-only?)"""
    tap, dropped = bytearray(), []
    for t, body in blocks:
        if t == 0x10:
            data = body[4:]
        elif t == 0x11:
            data = body[18:]
        elif t == 0x14:
            data = body[10:]
        else:
            dropped.append(t)
            continue
        if len(data) > 0xFFFF:
            raise ConversionError('a TZX data block is too large for TAP')
        tap += struct.pack('<H', len(data)) + data
    return bytes(tap), dropped


def tzx_to_tap(src, dst, progress=None):
    _, blocks = _tzx_blocks(Path(src).read_bytes())
    tap, dropped = _tzx_payload(blocks)
    if not tap:
        raise ConversionError('TZX holds no data blocks a TAP can carry')
    Path(dst).write_bytes(tap)
    return {'dropped': sorted(set(dropped))}


def _tzx_tap_verifier(src, out, progress=None):
    """Exact when the TZX is standard blocks with the default pause and header
    version: then TAP -> TZX must rebuild the source byte-for-byte. Otherwise
    the data is still re-derived and compared, but timing was dropped - that is
    reported as unprovable rather than as a pass."""
    data = Path(src).read_bytes()
    version, blocks = _tzx_blocks(data)
    tap, dropped = _tzx_payload(blocks)
    if tap != Path(out).read_bytes():
        return False, 'TAP data does not match the TZX data blocks'
    simple = all(t == 0x10 and struct.unpack_from('<H', b, 0)[0] == TZX_STD_PAUSE
                 for t, b in blocks)
    if simple and version == TZX_VERSION:
        work = Path(tempfile.mkdtemp(prefix='tzxverify_'))
        try:
            tap_to_tzx(out, work / 'back.tzx')
            ok = (work / 'back.tzx').read_bytes() == data
        finally:
            shutil.rmtree(work, ignore_errors=True)
        return ok, 'rebuilt TZX is byte-exact' if ok else 'rebuilt TZX differs'
    lost = [', '.join(f'0x{k:02X}' for k in sorted({t for t, _ in blocks if t != 0x10}))
            + ' blocks'] if any(t != 0x10 for t, _ in blocks) else []
    if any(t == 0x10 and struct.unpack_from('<H', b, 0)[0] != TZX_STD_PAUSE
           for t, b in blocks):
        lost.append('non-standard pauses')
    if version != TZX_VERSION:
        lost.append(f'the v{version[0]}.{version[1]:02d} header')
    return None, ('data blocks carried over exactly; lossy - TAP cannot keep '
                  + ' or '.join(lost))


CONVERSIONS['zx:tap->tzx'] = {
    'label': 'ZX Spectrum: TAP -> TZX (standard-speed blocks)', 'engine': ENGINE_NATIVE,
    'system': 'ZX', 'ext': '.tzx', 'fn': lambda s, d, p=None: tap_to_tzx(s, d, p),
    'inverse': 'zx:tzx->tap',
}
CONVERSIONS['zx:tzx->tap'] = {
    'label': 'ZX Spectrum: TZX -> TAP (data blocks; lossy for turbo/timing)',
    'engine': ENGINE_NATIVE, 'system': 'ZX', 'ext': '.tap',
    'fn': lambda s, d, p=None: tzx_to_tap(s, d, p),
    'inverse': 'zx:tap->tzx', 'verify_mode': 'verifier', 'verifier': _tzx_tap_verifier,
}


# ══════════════════════════════════════════════════════════════════════════════
#  COMMODORE 64  -  file extraction from T64 tapes and D64 disks
#
#  Both are containers, so extraction is one-way: the container metadata (tape
#  name, disk ID, BAM, interleave) is not in the files. The first program goes
#  to the requested output and every other file is written beside it.
# ══════════════════════════════════════════════════════════════════════════════

def _petscii_name(raw):
    name = bytes(raw).rstrip(b'\xa0').rstrip(b' ')
    out = ''.join(chr(c) if 0x20 <= c < 0x7F and c not in b'\\/:*?"<>|' else '_'
                  for c in name)
    return out.strip() or 'unnamed'


def _write_extracted(dst, files):
    """files: [(name, ext, bytes)]. First -> dst, the rest beside it."""
    if not files:
        raise ConversionError('no files could be extracted')
    final = Path(dst)
    base = final.with_suffix('') if final.suffix == '.part' else final
    Path(dst).write_bytes(files[0][2])
    sidecars, used = [], {base.name.lower()}
    for i, (name, ext, data) in enumerate(files[1:], 2):
        target = base.with_name(f'{base.stem} - {i:02d} {name}.{ext}')
        if target.name.lower() in used:
            target = target.with_name(f'{target.stem} ({i}){target.suffix}')
        used.add(target.name.lower())
        target.write_bytes(data)
        sidecars.append(str(target))
    return {'files': [f'{n}.{e}' for n, e, _ in files], 'sidecars': sidecars}


def t64_to_prg(src, dst, progress=None):
    data = Path(src).read_bytes()
    if not (data[:19] in (b'C64 tape image file', b'C64S tape image fil')
            or data[:14] == b'C64S tape file'):
        raise ConversionError('not a T64 tape image')
    max_entries = struct.unpack_from('<H', data, 34)[0] or 1
    entries = []
    for i in range(max_entries):
        e = data[64 + i * 32:96 + i * 32]
        if len(e) < 32 or e[0] == 0:
            continue
        start, end = struct.unpack_from('<HH', e, 2)
        offset = struct.unpack_from('<I', e, 8)[0]
        entries.append((offset, start, end, _petscii_name(e[16:32])))
    entries.sort()
    files = []
    for n, (offset, start, end, name) in enumerate(entries):
        limit = entries[n + 1][0] if n + 1 < len(entries) else len(data)
        size = end - start
        # A long-standing T64 writer bug records end = $C3C6 whatever the file
        # really is; the space up to the next entry is the only honest bound.
        if size <= 0 or offset + size > limit:
            size = limit - offset
        if offset >= len(data) or size <= 0:
            continue
        files.append((name, 'prg', struct.pack('<H', start) + data[offset:offset + size]))
    return _write_extracted(dst, files)


_D64_SECTORS = [21] * 17 + [19] * 7 + [18] * 6 + [17] * 10 + [17] * 2


def _d64_offset(track, sector, tracks):
    if not 1 <= track <= tracks or sector >= _D64_SECTORS[track - 1]:
        raise ConversionError(f'track {track} sector {sector} is outside the disk')
    return (sum(_D64_SECTORS[:track - 1]) + sector) * 256


def d64_to_files(src, dst, progress=None):
    data = Path(src).read_bytes()
    tracks = {174848: 35, 175531: 35, 196608: 40, 197376: 40,
              205312: 42, 206114: 42}.get(len(data))
    if not tracks:
        raise ConversionError(f'{len(data):,} bytes is not a D64 size')

    def chain(t, s):
        out, seen = bytearray(), set()
        while True:
            if (t, s) in seen:
                raise ConversionError('sector chain loops')
            seen.add((t, s))
            blk = data[_d64_offset(t, s, tracks):_d64_offset(t, s, tracks) + 256]
            if blk[0] == 0:
                out += blk[2:max(2, blk[1] + 1)]
                return bytes(out)
            out += blk[2:]
            t, s = blk[0], blk[1]

    files, dt, ds, seen = [], 18, 1, set()
    types = {1: 'seq', 2: 'prg', 3: 'usr', 4: 'rel'}
    while dt and (dt, ds) not in seen:
        seen.add((dt, ds))
        blk = data[_d64_offset(dt, ds, tracks):_d64_offset(dt, ds, tracks) + 256]
        for i in range(8):
            e = blk[2 + i * 32:2 + i * 32 + 30]
            kind = e[0]
            if not kind & 0x80 or (kind & 0x07) not in types:
                continue                    # scratched, DEL or unclosed
            try:
                body = chain(e[1], e[2])
            except ConversionError:
                continue                    # a broken chain loses one file, not the disk
            files.append((_petscii_name(e[3:19]), types[kind & 0x07], body))
        dt, ds = blk[0], blk[1]
    files.sort(key=lambda f: f[1] != 'prg')     # a program first, if there is one
    return _write_extracted(dst, files)


for _cid, _label, _fn in (
    ('c64:t64->prg', 'C64: T64 tape -> PRG files', t64_to_prg),
    ('c64:d64->files', 'C64: D64 disk -> PRG/SEQ files', d64_to_files),
):
    CONVERSIONS[_cid] = {
        'label': _label, 'engine': ENGINE_NATIVE, 'system': 'C64', 'ext': '.prg',
        'fn': (lambda s, d, p=None, _f=_fn: _f(s, d, p)), 'inverse': None,
        'verify_mode': 'none',
        'note': 'Extraction from a container - the tape or disk metadata is not '
                'in the files, so it cannot be rebuilt. Check the files against a DAT.',
    }



# ══════════════════════════════════════════════════════════════════════════════
#  3DS CIA  -  decrypt / encrypt, and CIA <-> CDN files
#
#  A CIA is: 0x2020 header | certificate chain (CA, XS, CP) | ticket | TMD |
#  contents | optional meta, every section aligned to 64 bytes. Each content is
#  an NCCH, AES-128-CBC encrypted with the title key (IV = content index as a
#  big-endian u16, zero padded). The title key sits in the ticket, itself
#  encrypted with keyslot 0x3D (KeyX from aes_keys.txt, KeyY = commonN).
#
#  Verified against real eShop and Virtual Console CIAs (2026-09-13):
#  * the encrypted contents are byte-identical to the No-Intro CDN files;
#  * a CDN tmd is the CIA's TMD followed by the CP and CA certificates;
#  * the TMD content hash is SHA-256 of the title-key-DECRYPTED content.
#
#  Decryption strips both layers and re-hashes the TMD so the result is still a
#  valid CIA. That throws away how each NCCH was keyed, exactly as for .3ds, so
#  re-encryption tries the candidate keys against a reference (the source CIA
#  when verifying, a CDN DAT otherwise).
# ══════════════════════════════════════════════════════════════════════════════

CIA_HEADER_SIZE = 0x2020
CIA_CERT_NAMES = ('CA00000003', 'XS0000000c', 'CP0000000b')


def _al64(n):
    return (n + 63) & ~63


def _sig_block_len(sig_type):
    """Signature type -> bytes before the signed body (type + sig + padding)."""
    return {0x10000: 0x240, 0x10001: 0x140, 0x10002: 0x80,
            0x10003: 0x240, 0x10004: 0x140, 0x10005: 0x80}.get(sig_type)


def _aes_keys_value(name):
    aes = _key_path('aes_keys')
    if not aes:
        raise ConversionError('CIA crypto needs aes_keys.txt in ' + str(KEYS_DIR))
    m = re.search(rf'^\s*{re.escape(name)}\s*=\s*([0-9A-Fa-f]{{32}})',
                  Path(aes).read_text(errors='replace'), re.MULTILINE | re.IGNORECASE)
    if not m:
        raise ConversionError(f'aes_keys.txt has no {name}')
    return int(m.group(1), 16)


def _split_certs(blob):
    """[(name, bytes)] for a run of certificates."""
    out, pos = [], 0
    while pos + 4 <= len(blob):
        sig = struct.unpack_from('>I', blob, pos)[0]
        body = pos + (_sig_block_len(sig) or 0)
        if body == pos or body + 0x88 > len(blob):
            break
        key_type = struct.unpack_from('>I', blob, body + 0x40)[0]
        key_len = {0: 0x238, 1: 0x138, 2: 0x78}.get(key_type)
        if key_len is None:
            break
        end = body + 0x88 + key_len
        name = blob[body + 0x44:body + 0x84].split(b'\x00')[0].decode('ascii', 'replace')
        out.append((name, bytes(blob[pos:end])))
        pos = end
    return out


def _tmd_layout(tmd):
    h = _sig_block_len(struct.unpack_from('>I', tmd, 0)[0])
    if not h or len(tmd) < h + 0xC4 + 0x900:
        raise ConversionError('TMD is truncated or has an unknown signature type')
    count = struct.unpack_from('>H', tmd, h + 0x9E)[0]
    return h, count, h + 0xC4 + 0x900


def _tmd_records(tmd):
    h, count, base = _tmd_layout(tmd)
    recs = []
    for i in range(count):
        off = base + i * 0x30
        cid, idx, typ, size = struct.unpack_from('>IHHQ', tmd, off)
        recs.append({'off': off, 'id': cid, 'index': idx, 'type': typ,
                     'size': size, 'hash': bytes(tmd[off + 16:off + 48])})
    return recs


def _tmd_rehash(tmd):
    """Recompute the content-info hashes and the header hash after the content
    records changed, so the TMD stays internally consistent."""
    h, count, base = _tmd_layout(tmd)
    info = h + 0xC4
    for i in range(64):
        at = info + i * 0x24
        start, cmds = struct.unpack_from('>HH', tmd, at)
        if cmds:
            tmd[at + 4:at + 36] = hashlib.sha256(
                bytes(tmd[base + start * 0x30:base + (start + cmds) * 0x30])).digest()
    tmd[h + 0xA4:h + 0xC4] = hashlib.sha256(bytes(tmd[info:info + 0x900])).digest()


def _ticket_len(ticket):
    d = _sig_block_len(struct.unpack_from('>I', ticket, 0)[0])
    if not d:
        raise ConversionError('ticket has an unknown signature type')
    return d + 0x164 + struct.unpack_from('>I', ticket, d + 0x168)[0]


class _Cia:
    def __init__(self, path):
        self.path = Path(path)
        with open(path, 'rb') as f:
            head = f.read(CIA_HEADER_SIZE)
            if len(head) < CIA_HEADER_SIZE or struct.unpack_from('<I', head, 0)[0] != CIA_HEADER_SIZE:
                raise ConversionError('not a CIA (header size is not 0x2020)')
            (_, self.cia_type, self.cia_version, cert, tik, tmd,
             meta) = struct.unpack_from('<IHHIIII', head, 0)
            self.content_size = struct.unpack_from('<Q', head, 0x18)[0]
            self.o_cert = _al64(CIA_HEADER_SIZE)
            self.o_tik = self.o_cert + _al64(cert)
            self.o_tmd = self.o_tik + _al64(tik)
            self.o_content = self.o_tmd + _al64(tmd)
            self.o_meta = self.o_content + _al64(self.content_size)
            f.seek(self.o_cert)
            self.certs = f.read(cert)
            f.seek(self.o_tik)
            self.ticket = f.read(tik)
            f.seek(self.o_tmd)
            self.tmd = bytearray(f.read(tmd))
            self.meta_size = meta
        self.records = _tmd_records(self.tmd)
        pos = self.o_content
        for r in self.records:
            r['offset'] = pos
            pos = _al64(pos + r['size'])
        d = _sig_block_len(struct.unpack_from('>I', self.ticket, 0)[0])
        self.title_id = self.ticket[d + 0x9C:d + 0xA4]

    def title_key(self):
        d = _sig_block_len(struct.unpack_from('>I', self.ticket, 0)[0])
        enc = self.ticket[d + 0x7F:d + 0x8F]
        index = self.ticket[d + 0xB1]
        normal = _scramble_key(_aes_keys_value('slot0x3DKeyX'),
                               _aes_keys_value(f'common{index}'))
        from Crypto.Cipher import AES
        return AES.new(normal.to_bytes(16, 'big'), AES.MODE_CBC,
                       iv=bytes(self.title_id) + bytes(8)).decrypt(enc)


def _cbc_file(fin, offset, size, key, index, decrypt, out_path, progress=None, label=''):
    from Crypto.Cipher import AES
    cipher = AES.new(key, AES.MODE_CBC, iv=struct.pack('>H', index) + bytes(14))
    op = cipher.decrypt if decrypt else cipher.encrypt
    fin.seek(offset)
    left = size
    with open(out_path, 'wb') as fo:
        while left:
            b = fin.read(min(CHUNK, left))
            if not b:
                raise ConversionError('CIA content is truncated')
            fo.write(op(b))
            left -= len(b)
            if progress:
                progress(size - left, size, label)


def _file_digest(path, algo):
    h = hashlib.new(algo)
    with open(path, 'rb') as f:
        for b in iter(lambda: f.read(CHUNK), b''):
            h.update(b)
    return h.digest()


def _copy_into(fo, path, pad_to=None):
    with open(path, 'rb') as fi:
        shutil.copyfileobj(fi, fo, CHUNK)
    if pad_to:
        size = os.path.getsize(path)
        fo.write(bytes(_al64(size) - size))


def _ncch_state(path):
    with open(path, 'rb') as f:
        head = f.read(0x200)
    if head[0x100:0x104] != b'NCCH':
        return None
    return {'encrypted': not head[0x18F] & NCCH_NOCRYPTO,
            'method': head[0x18B], 'seed': bool(head[0x18F] & NCCH_FLAG_SEED)}


def cia_decrypt(src, dst, progress=None):
    cia = _Cia(src)
    key = cia.title_key()
    tmd = bytearray(cia.tmd)
    work = Path(tempfile.mkdtemp(prefix='ciadec_'))
    try:
        with open(src, 'rb') as fi, open(dst, 'wb') as fo:
            shutil.copyfileobj(_LimitedReader(fi, cia.o_content), fo, CHUNK)
            for n, r in enumerate(cia.records):
                plain = work / 'plain.bin'
                if r['type'] & 1:
                    _cbc_file(fi, r['offset'], r['size'], key, r['index'], True,
                              plain, progress, f'content {n}: title key')
                else:
                    fi.seek(r['offset'])
                    with open(plain, 'wb') as t:
                        shutil.copyfileobj(_LimitedReader(fi, r['size']), t, CHUNK)
                state = _ncch_state(plain)
                if n == 0 and state is None:
                    raise ConversionError('content 0 is not an NCCH after title-key '
                                          'decryption - wrong title key or common key')
                final = plain
                if state and state['encrypted']:
                    final = work / 'ncch.bin'
                    ncch_crypt(plain, final, False, progress)
                digest = _file_digest(final, 'sha256')
                tmd[r['off'] + 16:r['off'] + 48] = digest
                struct.pack_into('>H', tmd, r['off'] + 6, r['type'] & ~1)
                fo.seek(r['offset'])
                _copy_into(fo, final, pad_to=True)
            fi.seek(cia.o_meta)
            fo.seek(cia.o_meta)
            shutil.copyfileobj(fi, fo, CHUNK)
            _tmd_rehash(tmd)
            fo.seek(cia.o_tmd)
            fo.write(tmd)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return {'contents': len(cia.records)}


class _LimitedReader:
    def __init__(self, f, n):
        self.f, self.left = f, n

    def read(self, size=-1):
        if self.left <= 0:
            return b''
        b = self.f.read(self.left if size < 0 else min(size, self.left))
        self.left -= len(b)
        return b


def cia_encrypt(src, dst, progress=None, match=None, hints=None):
    """Re-encrypt a decrypted CIA.

    match(n, sha1_of_encrypted_content) says whether a candidate reproduces the
    real content; hints[n] = (method, seed) skips the search when the original
    keying is known. Without either, the standard key is used."""
    cia = _Cia(src)
    key = cia.title_key()
    tmd = bytearray(cia.tmd)
    keys = load_boot9_keyx()
    work = Path(tempfile.mkdtemp(prefix='ciaenc_'))
    report = []
    try:
        with open(src, 'rb') as fi, open(dst, 'wb') as fo:
            shutil.copyfileobj(_LimitedReader(fi, cia.o_content), fo, CHUNK)
            for n, r in enumerate(cia.records):
                plain = work / 'plain.bin'
                fi.seek(r['offset'])
                with open(plain, 'wb') as t:
                    shutil.copyfileobj(_LimitedReader(fi, r['size']), t, CHUNK)
                state = _ncch_state(plain)
                if state and not state['encrypted']:
                    cands = [hints[n]] if hints and n in hints else \
                        _ncch_key_candidates(plain, keys)
                else:
                    cands = [None]
                chosen = None
                for cand in cands:
                    body = plain
                    if cand is not None:
                        body = work / 'ncch.bin'
                        ncch_crypt(plain, body, True, progress, method=cand[0], seed=cand[1])
                    enc = work / 'enc.bin'
                    with open(body, 'rb') as b:
                        _cbc_file(b, 0, os.path.getsize(body), key, r['index'], False,
                                  enc, progress, f'content {n}: title key')
                    if match is None or match(n, _file_digest(enc, 'sha1').hex()):
                        chosen = (cand, body, enc)
                        break
                if chosen is None:           # nothing matched: fall back to standard
                    cand = cands[0]
                    body = plain
                    if cand is not None:
                        body = work / 'ncch.bin'
                        ncch_crypt(plain, body, True, progress, method=cand[0], seed=cand[1])
                    enc = work / 'enc.bin'
                    with open(body, 'rb') as b:
                        _cbc_file(b, 0, os.path.getsize(body), key, r['index'], False,
                                  enc, progress, f'content {n}: title key')
                    chosen = (cand, body, enc)
                    report.append(f'content {n}: no match, standard key')
                else:
                    report.append(f'content {n}: ' + ('not an NCCH' if chosen[0] is None else
                                  f'method 0x{chosen[0][0]:02X}' + (' + seed' if chosen[0][1] else '')))
                cand, body, enc = chosen
                tmd[r['off'] + 16:r['off'] + 48] = _file_digest(body, 'sha256')
                struct.pack_into('>H', tmd, r['off'] + 6, r['type'] | 1)
                fo.seek(r['offset'])
                _copy_into(fo, enc, pad_to=True)
            fi.seek(cia.o_meta)
            fo.seek(cia.o_meta)
            shutil.copyfileobj(fi, fo, CHUNK)
            _tmd_rehash(tmd)
            fo.seek(cia.o_tmd)
            fo.write(tmd)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return {'keys': report}


def _cia_original_keying(src_encrypted):
    """(method, seed) per content, read from the encrypted CIA's own NCCH
    headers - one CBC block each, no full pass needed."""
    cia = _Cia(src_encrypted)
    key = cia.title_key()
    from Crypto.Cipher import AES
    hints, digests = {}, {}
    with open(src_encrypted, 'rb') as f:
        for n, r in enumerate(cia.records):
            f.seek(r['offset'])
            head = f.read(0x200)
            if r['type'] & 1:
                head = AES.new(key, AES.MODE_CBC,
                               iv=struct.pack('>H', r['index']) + bytes(14)).decrypt(head)
            if head[0x100:0x104] == b'NCCH' and not head[0x18F] & NCCH_NOCRYPTO:
                hints[n] = (head[0x18B], bool(head[0x18F] & NCCH_FLAG_SEED))
            f.seek(r['offset'])
            h = hashlib.sha1()
            left = r['size']
            while left:
                b = f.read(min(CHUNK, left))
                h.update(b)
                left -= len(b)
            digests[n] = h.hexdigest()
    return hints, digests


def _cia_decrypt_verifier(src, out, progress=None):
    """Re-encrypt the output with the source's own keying and demand the source
    back byte-for-byte. This proves both crypto layers AND the TMD re-hash."""
    hints, digests = _cia_original_keying(src)
    work = Path(tempfile.mkdtemp(prefix='ciaver_'))
    try:
        back = work / 'back.cia'
        cia_encrypt(out, back, progress, match=lambda n, d: d == digests[n], hints=hints)
        ok = _file_digest(back, 'sha1') == _file_digest(src, 'sha1')
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return ok, ('re-encrypted CIA is byte-identical to the source' if ok
                else 're-encrypted CIA differs from the source')


def _cia_encrypt_verifier(src, out, progress=None):
    work = Path(tempfile.mkdtemp(prefix='ciaver_'))
    try:
        back = work / 'back.cia'
        cia_decrypt(out, back, progress)
        ok = _file_digest(back, 'sha1') == _file_digest(src, 'sha1')
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return ok, 'decrypts back to the source' if ok else 'does not decrypt back to the source'


def cia_encrypt_matching(src, dst, progress=None):
    dats = REFERENCE_DATS
    match = (lambda n, d: d in dats.by_sha1) if dats is not None else None
    return cia_encrypt(src, dst, progress, match=match)


# ── CIA <-> CDN ───────────────────────────────────────────────────────────────

def _cdn_folder(dst):
    final = Path(dst)
    base = final.with_suffix('') if final.suffix == '.part' else final
    return base.with_name(base.stem + ' [CDN]')


def cia_to_cdn(src, dst, progress=None):
    """Write the files a CDN download consists of: one file per content (named
    by content ID), tmd.<version> (TMD + CP + CA certs) and cetk (ticket + XS +
    CA certs). The TMD also goes to dst; everything lands in '<name> [CDN]'."""
    cia = _Cia(src)
    certs = dict(_split_certs(cia.certs))
    missing = [c for c in CIA_CERT_NAMES if c not in certs]
    if missing:
        raise ConversionError(f'CIA certificate chain lacks {", ".join(missing)}')
    h, _, _ = _tmd_layout(cia.tmd)
    version = struct.unpack_from('>H', cia.tmd, h + 0x9C)[0]
    tmd_cdn = bytes(cia.tmd) + certs['CP0000000b'] + certs['CA00000003']
    cetk = cia.ticket[:_ticket_len(cia.ticket)] + certs['XS0000000c'] + certs['CA00000003']
    folder = _cdn_folder(dst)
    folder.mkdir(parents=True, exist_ok=True)
    files = {f'tmd.{version}': tmd_cdn, 'cetk': cetk}
    for name, blob in files.items():
        (folder / name).write_bytes(blob)
    with open(src, 'rb') as fi:
        for n, r in enumerate(cia.records):
            fi.seek(r['offset'])
            with open(folder / f'{r["id"]:08x}', 'wb') as fo:
                shutil.copyfileobj(_LimitedReader(fi, r['size']), fo, CHUNK)
            if progress:
                progress(n + 1, len(cia.records), 'writing contents')
    Path(dst).write_bytes(tmd_cdn)
    return {'sidecars': [str(p) for p in sorted(folder.iterdir())]}


def cdn_to_cia(src, dst, progress=None, folder=None):
    """Build a CIA from a CDN tmd plus the cetk and content files beside it."""
    blob = Path(src).read_bytes()
    h, count, base = _tmd_layout(blob)
    tmd = blob[:base + count * 0x30]
    certs = dict(_split_certs(blob[len(tmd):]))
    folder = Path(folder) if folder else Path(src).parent
    cetk_path = next((p for p in (folder / 'cetk', folder / 'CETK') if p.exists()), None)
    if cetk_path is None:
        raise ConversionError('building a CIA needs the title\'s cetk (ticket) beside the tmd')
    cetk = cetk_path.read_bytes()
    ticket = cetk[:_ticket_len(cetk)]
    certs.update(dict(_split_certs(cetk[len(ticket):])))
    missing = [c for c in CIA_CERT_NAMES if c not in certs]
    if missing:
        raise ConversionError(f'tmd/cetk certificates lack {", ".join(missing)}')
    chain = b''.join(certs[c] for c in CIA_CERT_NAMES)
    records = _tmd_records(tmd)
    paths = []
    for r in records:
        cand = [folder / f'{r["id"]:08x}', folder / f'{r["id"]:08X}',
                folder / f'{r["id"]:08x}.app']
        path = next((c for c in cand if c.exists()), None)
        if path is None:
            raise ConversionError(f'content {r["id"]:08x} is missing from {folder}')
        if path.stat().st_size != r['size']:
            raise ConversionError(f'content {r["id"]:08x} is {path.stat().st_size:,} bytes, '
                                  f'TMD says {r["size"]:,}')
        paths.append(path)
    bitmap = bytearray(0x2000)
    for r in records:
        bitmap[r['index'] >> 3] |= 0x80 >> (r['index'] & 7)
    content_size = sum(_al64(r['size']) for r in records)
    header = struct.pack('<IHHIIIIQ', CIA_HEADER_SIZE, 0, 0, len(chain), len(ticket),
                         len(tmd), 0, content_size) + bytes(bitmap)
    with open(dst, 'wb') as fo:
        for part in (header, chain, ticket, tmd):
            fo.write(part + bytes(_al64(len(part)) - len(part)))
        for n, path in enumerate(paths):
            _copy_into(fo, path, pad_to=True)
            if progress:
                progress(n + 1, len(paths), 'writing contents')
    return dst


def _cia_cdn_verifier(src, out, progress=None):
    work = Path(tempfile.mkdtemp(prefix='ciacdn_'))
    try:
        back = work / 'back.cia'
        cdn_to_cia(out, back, progress, folder=_cdn_folder(out))
        ok = _file_digest(back, 'sha1') == _file_digest(src, 'sha1')
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return ok, ('CDN files rebuild the CIA byte-for-byte' if ok
                else 'CIA rebuilt from the CDN files differs')


def _detect_cia(buf, path, fh):
    try:
        cia = _Cia(path)
    except (ConversionError, struct.error, OSError):
        return _result('3DS', 'CIA', None, confidence='medium',
                       detail='CIA header, but the sections do not parse')
    encrypted = any(r['type'] & 1 for r in cia.records)
    return _result(
        '3DS', 'CIA', 'encrypted' if encrypted else 'decrypted',
        detail=f'CIA [{bytes(cia.title_id).hex().upper()}], {len(cia.records)} '
               f'content(s), {"title-key encrypted" if encrypted else "decrypted"}',
        conversions=(['cia:encrypted->decrypted', 'cia:cia->cdn'] if encrypted
                     else ['cia:decrypted->encrypted', 'cia:cia->cdn']))


def _detect_cdn_tmd(buf, path, fh):
    if not _have(buf, 0x180, 0):
        return None
    h = _sig_block_len(_u32be(buf, 0))
    if h != 0x140 or not bytes(_at(buf, h, 26)).startswith(b'Root-CA00000003-CP0000000b'):
        return None
    return _result('3DS', 'TMD', 'cdn', detail='CDN title metadata (tmd)',
                   conversions=['cia:cdn->cia'])


for _cid, _label, _fn, _ver, _inv in (
    ('cia:encrypted->decrypted', '3DS CIA: decrypt (title key + NCCH)', cia_decrypt,
     _cia_decrypt_verifier, 'cia:decrypted->encrypted'),
    ('cia:decrypted->encrypted', '3DS CIA: encrypt (keys matched to a DAT when loaded)',
     cia_encrypt_matching, _cia_encrypt_verifier, 'cia:encrypted->decrypted'),
    ('cia:cia->cdn', '3DS CIA: -> CDN files (contents, tmd, cetk)', cia_to_cdn,
     _cia_cdn_verifier, 'cia:cdn->cia'),
):
    CONVERSIONS[_cid] = {
        'label': _label, 'engine': ENGINE_KEYED, 'system': '3DS',
        'ext': '.tmd' if _cid == 'cia:cia->cdn' else '.cia',
        'fn': (lambda s, d, p=None, _f=_fn: _f(s, d, p)), 'inverse': _inv,
        'requires': 'aes_keys' if _cid != 'cia:cia->cdn' else None,
        'why': KEY_FILES['aes_keys'][1],
        'verify_mode': 'verifier', 'verifier': _ver,
    }
CONVERSIONS['cia:cia->cdn']['engine'] = ENGINE_NATIVE
CONVERSIONS['cia:cdn->cia'] = {
    'label': '3DS CDN files (tmd + cetk + contents) -> CIA', 'engine': ENGINE_NATIVE,
    'system': '3DS', 'ext': '.cia', 'fn': lambda s, d, p=None: cdn_to_cia(s, d, p),
    'inverse': 'cia:cia->cdn', 'verify_mode': 'none',
    'note': 'Checked by splitting the CIA back into CDN files and comparing.',
}
DETECTORS.append(_detect_cdn_tmd)



# ══════════════════════════════════════════════════════════════════════════════
#  PSP NPDRM  -  PSN PKG extraction and EDAT/PGD decryption
#
#  A PSN PKG is AES-128-CTR over its data area (IV at 0x70). The item table and
#  items flagged 0x90 use the PSP key; the rest use the PS3 key (pkg2zip).
#
#  EDAT files wrap a PGD. For DNAS PGDs (drm type 1) the version key can be
#  recovered from the header's own MAC, so no licence is needed. The algorithm
#  is tpu's amctrl.prx reversal as carried in PPSSPP's libkirk, using only the
#  public KIRK keys 0x38/0x39/0x63 - the fuse-keyed variants (drm type 2) are
#  console specific and refused. Output is checked against No-Intro's PSN
#  (Decrypted) DAT, which lists the decrypted EDAT payloads.
# ══════════════════════════════════════════════════════════════════════════════

PKG_KEYS = {'psp': bytes.fromhex('07F2C68290B50D2C33818D709B60E62B'),
            'ps3': bytes.fromhex('2E7B71D7C9C9A14EA3221F188828B8F8')}
PKG_VITA_KEYS = {2: bytes.fromhex('E31A70C9CE1DD72BF3C0622963F2ECCB'),
                 3: bytes.fromhex('423ACA3A2BD5649F9686ABAD6FD8801F'),
                 4: bytes.fromhex('AF07FD59652527BAF13389668B17D9EA')}
_KIRK = {0x38: bytes.fromhex('12468d7e1c42209bba5426835eb03303'),
         0x39: bytes.fromhex('c43bb6d653ee67493ea95fbc0ced6f8a'),
         0x3A: bytes.fromhex('2cc3cf8c2878a5a663e2af2d715e86ba'),
         0x63: bytes.fromhex('9c9b1372f8c640cf1c62f5d592ddb582')}
_LOC_1CD4 = bytes.fromhex('E350ED1D910A1FD029BB1C3EF34077FB')
_LOC_1CE4 = bytes.fromhex('135FA47CAB395BA476B8CCA98F3A0445')
_LOC_1CF4 = bytes.fromhex('678D7FA32A9CA0D1508AD8385E4B017E')
_DNAS_KEYS = {2: bytes.fromhex('EDE25D2DBBF812E53C5C5932FAE3E243'),
              1: bytes.fromhex('2774FBEBA4A001D702569E338C195783')}


def _xor(a, b):
    return bytes(x ^ y for x, y in zip(a, b))


def _kirk4(data, key_id, iv=bytes(16)):
    from Crypto.Cipher import AES
    return AES.new(_KIRK[key_id], AES.MODE_CBC, iv=iv).encrypt(data)


def _kirk7(data, key_id, iv=bytes(16)):
    from Crypto.Cipher import AES
    return AES.new(_KIRK[key_id], AES.MODE_CBC, iv=iv).decrypt(data)


def _gf_double(block):
    v = (int.from_bytes(block, 'big') << 1)
    if block[0] & 0x80:
        v ^= 0x87
    return (v & ((1 << 128) - 1)).to_bytes(16, 'big')


class _BBMac:
    """sceDrmBBMac*: a CMAC-like chain over KIRK key 0x38 (0x3A for fuse
    type 2, which is refused)."""

    def __init__(self, mac_type):
        if mac_type == 2:
            raise ConversionError('this PGD uses the console fuse key - it can only '
                                  'be decrypted on the PSP it was bought on')
        self.type, self.key, self.pad = mac_type, bytes(16), b''
        self.code = 0x38

    def update(self, data):
        data = self.pad + bytes(data)
        keep = len(data) & 0x0F or 16
        body, self.pad = data[:-keep], data[-keep:]
        if body:
            enc = _kirk4(body, self.code, iv=self.key)
            self.key = enc[-16:]

    def final(self, vkey=None):
        tmp = _gf_double(_kirk4(bytes(16), self.code))
        pad = self.pad
        if len(pad) < 16:
            tmp = _gf_double(tmp)
            pad = pad + b'\x80' + bytes(15 - len(pad))
        out = _kirk4(_xor(pad, tmp), self.code, iv=self.key)
        out = _xor(out, _LOC_1CD4)
        if vkey is not None:
            out = _kirk4(_xor(out, vkey), self.code)
        return out

    def check(self, expected, vkey):
        want = _kirk7(bytes(expected), 0x63) if self.type == 3 else bytes(expected)
        return self.final(vkey) == want

    def recover_vkey(self, mac):
        tmp = self.final(None)
        t = _kirk7(bytes(mac), 0x63) if self.type == 3 else bytes(mac)
        return _xor(tmp, _kirk7(t, self.code))


def _bb_cipher(data, header_key, vkey, seed, cipher_type):
    """sceDrmBBCipher decrypt (mode 2): a counter keystream built from KIRK
    0x39/0x63. Symmetric, so it also re-encrypts."""
    if cipher_type == 2:
        raise ConversionError('this PGD uses the console fuse key')
    key = _xor(header_key, vkey) if vkey else bytes(header_key)
    base = _xor(_kirk7(_xor(key, _LOC_1CF4), 0x39), _LOC_1CE4)
    counter = seed + 1
    blocks = (len(data) + 15) // 16
    ctr = b''.join(base[:12] + struct.pack('<I', counter + i) for i in range(blocks))
    prev = bytes(16) if counter == 1 else base[:12] + struct.pack('<I', counter - 1)
    stream = _kirk7(ctr, 0x63, iv=prev)
    return _xor(data, stream[:len(data)])


def pgd_decrypt(pgd):
    """Decrypt one PGD blob. Returns the payload bytes."""
    if pgd[:4] != b'\x00PGD':
        raise ConversionError('not a PGD block')
    key_index, drm_type = struct.unpack_from('<II', pgd, 4)
    if drm_type == 1:
        mac_type, cipher_type = (3 if key_index > 1 else 1), 1
    else:
        mac_type, cipher_type = 2, 2
    fkey = None
    for flag in (2, 1):
        m = _BBMac(mac_type)
        m.update(pgd[:0x80])
        if m.check(pgd[0x80:0x90], _DNAS_KEYS[flag]):
            fkey = _DNAS_KEYS[flag]
            break
    if fkey is None:
        raise ConversionError('PGD header MAC does not verify against either DNAS key '
                              '- damaged, or not a DNAS PGD')
    m = _BBMac(mac_type)
    m.update(pgd[:0x70])
    vkey = m.recover_vkey(pgd[0x70:0x80])
    desc = _bb_cipher(pgd[0x30:0x60], pgd[0x10:0x20], vkey, 0, cipher_type)
    dkey = desc[0:16]
    data_size, block_size, data_offset = struct.unpack_from('<III', desc, 0x14)
    if not block_size or data_offset + data_size > len(pgd) + 16:
        raise ConversionError('PGD descriptor decrypted to nonsense - wrong version key')
    align = (data_size + 15) & ~15
    out = bytearray()
    for off in range(0, align, block_size):
        chunk = pgd[data_offset + off:data_offset + min(off + block_size, align)]
        out += _bb_cipher(chunk, dkey, vkey, off >> 4, cipher_type)
    return bytes(out[:data_size])


def edat_decrypt_bytes(edat):
    if edat[:8] != b'\x00PSPEDAT':
        raise ConversionError('not a PSP EDAT')
    return pgd_decrypt(edat[edat[0x0C]:])


class _Pkg:
    """Streaming PSN PKG reader: nothing is loaded whole, so 2 GB games are fine."""

    def __init__(self, path):
        from Crypto.Cipher import AES
        self._AES = AES
        self.f = open(path, 'rb')
        head = self.f.read(0x100)
        if head[:4] != b'\x7fPKG':
            raise ConversionError('not a PKG')
        self.pkg_type = struct.unpack_from('>H', head, 6)[0]
        count = struct.unpack_from('>I', head, 0x14)[0]
        self.enc_off, self.enc_size = struct.unpack_from('>QQ', head, 0x20)
        self.iv = int.from_bytes(head[0x70:0x80], 'big')
        key_type = head[0xE7] & 7
        self.vita = key_type in (2, 3) or (key_type == 4 and self.pkg_type != 1)
        if key_type == 1:
            self.main = PKG_KEYS['psp']
        elif self.vita:
            # PS Vita / PSM: the item key is the header IV encrypted under a
            # fixed per-key-type key (pkg2zip)
            self.main = AES.new(PKG_VITA_KEYS[key_type], AES.MODE_ECB).encrypt(head[0x70:0x80])
        elif key_type == 4:
            self.main = PKG_KEYS['ps3']
        else:
            raise ConversionError(f'PKG key type {key_type} is not a known package key')
        self.content_id = head[0x30:0x54].decode('ascii', 'replace')
        self.content_type, self.items_size = 0, count * 32
        meta_off, meta_count = struct.unpack_from('>II', head, 8)
        for _ in range(meta_count):
            self.f.seek(meta_off)
            block = self.f.read(16)
            mtype, msize = struct.unpack_from('>II', block, 0)
            if mtype == 2:
                self.content_type = struct.unpack_from('>I', block, 8)[0]
            elif mtype == 13:
                self.items_size = struct.unpack_from('>I', block, 12)[0]
            meta_off += 8 + msize
        table = self.read(self.main, 0, count * 32)
        self.items = []
        for i in range(count):
            no, ns, do, ds = struct.unpack_from('>IIQQ', table, i * 32)
            flags = table[i * 32 + 24:i * 32 + 32]
            key = self.main if self.vita or flags[0] == 0x90 else PKG_KEYS['ps3']
            name = self.read(key, no, ns).decode('utf-8', 'replace')
            self.items.append({'name': name, 'kind': flags[3], 'key': key,
                               'offset': do, 'size': ds})

    def read(self, key, offset, size):
        """AES-CTR decrypt [offset, offset+size) of the data area."""
        if size <= 0:
            return b''
        skip = offset % 16
        self.f.seek(self.enc_off + offset - skip)
        raw = self.f.read(size + skip)
        from Crypto.Util import Counter
        ctr = Counter.new(128, initial_value=(self.iv + (offset - skip) // 16) & ((1 << 128) - 1))
        return self._AES.new(key, self._AES.MODE_CTR, counter=ctr).decrypt(raw)[skip:skip + size]

    def item_bytes(self, it, offset=0, size=None):
        size = it['size'] - offset if size is None else size
        return self.read(it['key'], it['offset'] + offset, size)

    def close(self):
        self.f.close()


# ── LZRC (tpu's range coder, as used by PSP NPUMDIMG) ─────────────────────────
# The C original indexes its probability tables through raw pointers and runs
# past the end of one table into the next, so they live in ONE bytearray laid
# out exactly like the C struct.
_LZ_LIT, _LZ_DBITS, _LZ_DIST, _LZ_MATCH, _LZ_LEN = 0, 2048, 2360, 2504, 2568
_LZ_TOTAL = 2816


def lzrc_decompress(src, out_len):
    if len(src) < 5:
        raise ConversionError('LZRC block underflow')
    lc = src[0]
    code = int.from_bytes(src[1:5], 'big')
    if lc & 0x80:
        return bytes(src[5:5 + code])
    probs = bytearray(b'\x80' * _LZ_TOTAL)
    out = bytearray()
    rng, pos, n_in = 0xFFFFFFFF, 5, len(src)
    state, last = 0, 0

    def bit(i):
        nonlocal rng, code, pos
        if rng < 0x01000000:
            rng = (rng << 8) & 0xFFFFFFFF
            code = ((code << 8) | (src[pos] if pos < n_in else 0)) & 0xFFFFFFFF
            pos += 1
        p = probs[i]
        bound = (rng >> 8) * p
        p -= p >> 3
        if code < bound:
            rng = bound
            probs[i] = p + 31
            return 1
        code -= bound
        rng -= bound
        probs[i] = p
        return 0

    def bittree(base, limit):
        number = 1
        while number < limit:
            number = (number << 1) + bit(base + number)
        return number

    def number(base, n):
        nonlocal rng, code, pos
        num = 1
        if n > 3:
            num = (num << 1) + bit(base + 3)
            if n > 4:
                num = (num << 1) + bit(base + 3)
                if n > 5:
                    if rng < 0x01000000:
                        rng = (rng << 8) & 0xFFFFFFFF
                        code = ((code << 8) | (src[pos] if pos < n_in else 0)) & 0xFFFFFFFF
                        pos += 1
                    for _ in range(n - 5):
                        rng >>= 1
                        num <<= 1
                        if code < rng:
                            num += 1
                        else:
                            code -= rng
        if n > 0:
            num = (num << 1) + bit(base)
            if n > 1:
                num = (num << 1) + bit(base + 1)
                if n > 2:
                    num = (num << 1) + bit(base + 2)
        return num

    while True:
        step = 0
        if not bit(_LZ_MATCH + state * 8 + step):          # literal
            if state:
                state -= 1
            byte = bittree(_LZ_LIT + ((last >> lc) & 7) * 256, 0x100) - 0x100
            if len(out) >= out_len:
                raise ConversionError('LZRC output overflow')
            out.append(byte)
            last = byte
            continue
        len_bits = 0
        for _ in range(7):
            step += 1
            if not bit(_LZ_MATCH + state * 8 + step):
                break
            len_bits += 1
        if len_bits == 0:
            match_len = 1
        else:
            len_state = ((len_bits - 1) << 2) + ((len(out) << (len_bits - 1)) & 3)
            match_len = number(_LZ_LEN + state * 31 + len_state, len_bits)
            if match_len == 0xFF:
                return bytes(out)
        dist_state, limit = (7, 44) if match_len > 2 else (0, 8)
        dist_bits = bittree(_LZ_DBITS + len_bits * 39 + dist_state, limit) - limit
        dist = number(_LZ_DIST + dist_bits * 8, dist_bits) if dist_bits > 0 else 1
        if dist > len(out) or len(out) + match_len + 1 > out_len:
            raise ConversionError('LZRC match out of range - corrupt block')
        start = len(out) - dist
        for i in range(match_len + 1):
            out.append(out[start + i])
        last = out[-1]
        state = 6 + ((len(out) + 1) & 1)


def _npumdimg_to_iso(pkg, item, dst, progress=None):
    """EBOOT.PBP (NPUMDIMG) inside a PKG -> the plain UMD ISO."""
    head = pkg.item_bytes(item, 0, 0x28)
    if head[:4] != b'\x00PBP':
        raise ConversionError('EBOOT.PBP has no PBP signature')
    psar = struct.unpack_from('<I', head, 0x24)[0]
    hdr = bytearray(pkg.item_bytes(item, psar, 0x100))
    if hdr[:8] != b'NPUMDIMG':
        raise ConversionError('DATA.PSAR is not an NPUMDIMG (PS1 classics are not handled)')
    iso_block = struct.unpack_from('<I', hdr, 0x0C)[0]
    if not 1 <= iso_block <= 16:
        raise ConversionError(f'unsupported NPUMDIMG block size {iso_block}')
    mac = _BBMac(3)
    mac.update(bytes(hdr[:0xC0]))
    vkey = mac.recover_vkey(bytes(hdr[0xC0:0xD0]))
    hkey = bytes(hdr[0xA0:0xB0])
    hdr[0x40:0xA0] = _bb_cipher(bytes(hdr[0x40:0xA0]), hkey, vkey, 0, 1)
    iso_start, iso_end = struct.unpack_from('<I', hdr, 0x54)[0], struct.unpack_from('<I', hdr, 0x64)[0]
    table = struct.unpack_from('<I', hdr, 0x6C)[0]
    total = iso_end - iso_start - 1
    # a compressed image can be far larger than its EBOOT (111 MB from 1.8 MB
    # for a Mini), so only structure that must hold is checked
    if total <= 0 or total > 0x800000 or psar + table >= item['size']:
        raise ConversionError('NPUMDIMG header decrypted to nonsense - bad version key')
    blocks = (total + iso_block - 1) // iso_block
    full = iso_block * 2048
    with open(dst, 'wb') as fo:
        for i in range(blocks):
            t = struct.unpack('<8I', pkg.item_bytes(item, psar + table + 32 * i, 32))
            b_off = t[4] ^ t[2] ^ t[3]
            b_size = t[5] ^ t[1] ^ t[2]
            b_flags = t[6] ^ t[0] ^ t[3]
            if b_size % 16 or psar + b_off + b_size > item['size']:
                raise ConversionError(f'block {i} lies outside the EBOOT')
            data = pkg.item_bytes(item, psar + b_off, b_size)
            if not b_flags & 4:
                data = _bb_cipher(data, hkey, vkey, b_off // 16, 1)
            if b_size != full:
                data = lzrc_decompress(data, full)
                if len(data) != full:
                    raise ConversionError(f'block {i} decompressed to {len(data)} bytes, '
                                          f'expected {full}')
            fo.write(data)
            if progress:
                progress(i + 1, blocks, 'rebuilding ISO')
    return blocks * full


def pkg_extract(src, dst, progress=None):
    """PSN PKG -> what No-Intro keeps as the decrypted set:

    * a game or mini: the UMD ISO rebuilt from EBOOT.PBP (written to dst);
    * DLC: USRDIR/CONTENT under <TITLE ID>, EDATs with their PGD removed;
    * a theme: the .PTF.
    Every other file in the package is kept under _pkg, so nothing is lost."""
    pkg = _Pkg(src)
    try:
        title = pkg.content_id[7:16]
        final = Path(dst)
        base = final.with_suffix('') if final.suffix == '.part' else final
        root = base.with_name(base.stem + ' [PKG]')
        written, primary, kept_encrypted, iso = [], None, [], None
        for n, it in enumerate(pkg.items):
            if it['kind'] == 4:
                continue
            name = it['name'].replace('\\', '/')
            upper = name.upper()
            if upper == 'USRDIR/CONTENT/EBOOT.PBP' and iso is None:
                head = pkg.item_bytes(it, 0, 0x28)
                psar = struct.unpack_from('<I', head, 0x24)[0] if head[:4] == b'\x00PBP' else 0
                if psar and pkg.item_bytes(it, psar, 8) == b'NPUMDIMG':
                    iso = _npumdimg_to_iso(pkg, it, dst, progress)
                    continue
                if psar and pkg.item_bytes(it, psar, 8) in (b'PSISOIMG', b'PSTITLEI'):
                    psx = psx_classic_to_bin(pkg, it, dst, progress)
                    iso = psx
                    written += psx['sidecars']
                    continue
            if pkg.pkg_type == 1:
                # PS3: the whole package tree under the title ID, the layout the
                # (Unofficial) PS3 PSN Decrypted DAT uses (NPEB00287\ICON0.PNG).
                # SELF/EDAT/SDAT contents stay encrypted - that is a further layer.
                rel = Path(title) / name
            elif upper.startswith('USRDIR/CONTENT/'):
                rel = Path(title) / name[len('USRDIR/CONTENT/'):]
            elif upper.endswith('.PTF') and '/' not in name:
                rel = Path(title) / name                 # a PSP theme
            else:
                safe = ''.join(c if c.isprintable() and c not in '<>:"|?*' else '_' for c in name)
                rel = Path('_pkg') / (safe or f'item{n}')
            body = pkg.item_bytes(it)
            if body[:8] == b'\x00PSPEDAT':              # EDATs, and themes (.PTF)
                try:
                    body = edat_decrypt_bytes(body)
                except ConversionError as e:
                    kept_encrypted.append(f'{rel.name}: {e}')
            target = root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(body)
            written.append(str(target))
            if primary is None and (rel.suffix.upper() == '.PTF' or rel.name.upper() == 'PARAM.PBP'):
                primary = body
            if progress and iso is None:
                progress(n + 1, len(pkg.items), 'extracting')
        if iso is None:
            if not written:
                raise ConversionError('the PKG contains no files')
            Path(dst).write_bytes(primary if primary is not None
                                  else Path(written[0]).read_bytes())
        return {'sidecars': written, 'content_id': pkg.content_id,
                'kept_encrypted': kept_encrypted,
                'iso_bytes': iso}
    finally:
        pkg.close()


def edat_decrypt_file(src, dst, progress=None):
    Path(dst).write_bytes(edat_decrypt_bytes(Path(src).read_bytes()))
    return dst


def _detect_edat(buf, path, fh):
    if _at(buf, 0, 8) != b'\x00PSPEDAT':
        return None
    return _result('PSP', 'EDAT', 'encrypted', detail='PSP NPDRM EDAT',
                   conversions=['psp:edat->decrypted'])


CONVERSIONS['psp:pkg->decrypted'] = {
    'label': 'PSP: PSN PKG -> ISO (games/minis) or decrypted files (DLC/themes)',
    'engine': ENGINE_NATIVE, 'system': 'PSP', 'ext': '.iso',
    'fn': lambda s, d, p=None: pkg_extract(s, d, p), 'inverse': None,
    'verify_mode': 'none',
    'note': 'Extraction; the package signature cannot be rebuilt. Check the files '
            'against the PSN (Decrypted) DAT.',
}
CONVERSIONS['psp:edat->decrypted'] = {
    'label': 'PSP: EDAT -> decrypted payload', 'engine': ENGINE_NATIVE,
    'system': 'PSP', 'ext': '.bin', 'fn': lambda s, d, p=None: edat_decrypt_file(s, d, p),
    'inverse': None, 'verify_mode': 'none',
    'note': 'The PGD MAC is verified before decrypting; check the payload against a DAT.',
}
DETECTORS.append(_detect_edat)



# ══════════════════════════════════════════════════════════════════════════════
#  BATCH 4  -  UNIF, Atari Jaguar, Atari 8-bit ATR/XFD, Atari ST MSA,
#              ZX Spectrum SCL/TRD
# ══════════════════════════════════════════════════════════════════════════════

# ── NES UNIF -> iNES / NES 2.0 ────────────────────────────────────────────────
# UNIF is a chunked container: PRG0..PRGF and CHR0..CHRF hold the ROM data.
# Concatenated in that order they ARE the headerless ROM, so the header comes
# from the same hash-verified No-Intro source as headerless->headered - no
# board-name-to-mapper table (and none of its guesswork) is needed.

def _unif_chunks(data):
    if data[:4] != b'UNIF':
        raise ConversionError('not a UNIF file')
    pos, chunks = 32, {}
    while pos + 8 <= len(data):
        cid = data[pos:pos + 4].decode('latin1')
        n = struct.unpack_from('<I', data, pos + 4)[0]
        chunks[cid] = data[pos + 8:pos + 8 + n]
        pos += 8 + n
    return chunks


def unif_to_nes(src, dst, progress=None):
    chunks = _unif_chunks(Path(src).read_bytes())
    body = b''.join(chunks.get(f'PRG{i:X}', b'') for i in range(16)) + \
        b''.join(chunks.get(f'CHR{i:X}', b'') for i in range(16))
    if not body:
        raise ConversionError('UNIF has no PRG/CHR chunks')
    board = chunks.get('MAPR', b'').split(b'\x00')[0].decode('ascii', 'replace')
    work = Path(tempfile.mkdtemp(prefix='unif_'))
    try:
        raw = work / 'body.nes'
        raw.write_bytes(body)
        try:
            info = nes_header_from_db(raw, dst, progress)
        except ConversionError:
            raise ConversionError(f'board "{board}": the ROM data is in neither the '
                                  'No-Intro headered DATs nor nes20db, so no header '
                                  'can be proven for it')
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return dict(info, board=board)


CONVERSIONS['nes:unif->nes'] = {
    'label': 'NES: UNIF -> headered NES (header from No-Intro DAT / nes20db)',
    'engine': ENGINE_NATIVE, 'system': 'NES', 'ext': '.nes',
    'fn': lambda s, d, p=None: unif_to_nes(s, d, p), 'inverse': None,
    'verify_mode': 'none',
    'note': 'The header is accepted only when the headered file hash is in a DAT '
            '(or comes from nes20db); UNIF metadata chunks are not kept.',
}


# ── Atari Jaguar: J64 <-> ROM ─────────────────────────────────────────────────
# Verified on 91 No-Intro pairs: ROM = J64 minus its first 0x2000 bytes. That
# block is the cartridge's boot header; most carts share one, but not all, so
# adding it back uses the common header and is checked against a DAT.

JAG_HEADER = 0x2000
JAG_HEADER_FILE = 'jaguar_header.bin'


def _detect_jaguar(buf, path, fh):
    ext = path.suffix.lower()
    if ext == '.j64' and fh['size'] > JAG_HEADER:
        return _result('JAGUAR', 'J64', 'headered', confidence='low',
                       detail=f'Jaguar cartridge with 8 KiB boot header',
                       conversions=['jag:j64->rom'])
    if ext in ('.rom', '.jag') and fh['size'] % 0x1000 == 0 and \
            folder_hint(path) in (None, 'JAGUAR') and 'JAGUAR' in str(path).upper():
        return _result('JAGUAR', 'ROM', 'headerless', confidence='low',
                       detail='Jaguar cartridge ROM (no boot header)',
                       conversions=['jag:rom->j64'])
    return None


def jag_rom_to_j64(src, dst, progress=None):
    head = KEYS_DIR / JAG_HEADER_FILE
    if not head.exists():
        raise ConversionError(f'adding a J64 header needs {JAG_HEADER_FILE} in {KEYS_DIR}')
    header = head.read_bytes()
    if len(header) != JAG_HEADER:
        raise ConversionError(f'{JAG_HEADER_FILE} must be exactly 8 KiB')
    with open(src, 'rb') as fi, open(dst, 'wb') as fo:
        fo.write(header)
        _copy_rest(fi, fo, os.path.getsize(src), progress, 'adding header')
    if REFERENCE_DATS is not None:
        rec, _ = REFERENCE_DATS.lookup(dst)
        if not rec:
            Path(dst).unlink(missing_ok=True)
            raise ConversionError('this cart does not use the common Jaguar boot header '
                                  '- the J64 would match no DAT, so none was written')
    return dst


CONVERSIONS['jag:j64->rom'] = {
    'label': 'Jaguar: J64 -> ROM (strip 8 KiB boot header)', 'engine': ENGINE_NATIVE,
    'system': 'JAGUAR', 'ext': '.rom', 'fn': _strip_fixed(JAG_HEADER), 'inverse': None,
    'rebuild': lambda out, tmp, extra, p=None: _add_fixed(JAG_HEADER, lambda h: True)(
        out, tmp, extra, p),
}
CONVERSIONS['jag:rom->j64'] = {
    'label': 'Jaguar: ROM -> J64 (common boot header, DAT-checked)', 'engine': ENGINE_NATIVE,
    'system': 'JAGUAR', 'ext': '.j64', 'fn': lambda s, d, p=None: jag_rom_to_j64(s, d, p),
    'inverse': 'jag:j64->rom',
}


# ── Atari 8-bit: ATR <-> XFD ──────────────────────────────────────────────────
ATR_MAGIC = 0x0296


def _atr_header(size, sector_size):
    paragraphs = size // 16
    return struct.pack('<HHHB', ATR_MAGIC, paragraphs & 0xFFFF, sector_size,
                       (paragraphs >> 16) & 0xFF) + bytes(9)


def _detect_atr(buf, path, fh):
    if _have(buf, 16, 0) and _u16le(buf, 0) == ATR_MAGIC:
        sector = _u16le(buf, 4)
        size = (_u16le(buf, 2) | buf[6] << 16) * 16
        return _result('A8BIT', 'ATR', 'headered',
                       detail=f'Atari 8-bit disk, {size // 1024} KiB, {sector}-byte sectors',
                       conversions=['a8:atr->xfd'],
                       meta={'header': bytes(_at(buf, 0, 16)).hex()})
    if path.suffix.lower() == '.xfd' and fh['size'] % 128 == 0:
        return _result('A8BIT', 'XFD', 'headerless', confidence='low',
                       detail=f'Atari 8-bit raw disk, {fh["size"] // 1024} KiB',
                       conversions=['a8:xfd->atr'])
    return None


def atr_to_xfd(src, dst, progress=None):
    with open(src, 'rb') as fi:
        header = fi.read(16)
        if struct.unpack_from('<H', header, 0)[0] != ATR_MAGIC:
            raise ConversionError('not an ATR image')
        with open(dst, 'wb') as fo:
            _copy_rest(fi, fo, os.path.getsize(src), progress, 'stripping')
    return header


def xfd_to_atr(src, dst, progress=None):
    size = os.path.getsize(src)
    if size % 256 == 0 and size > 133120:
        raise ConversionError('double-density XFD stores full 256-byte boot sectors, '
                              'which ATR stores as 128 - not a lossless header add')
    if size % 128:
        raise ConversionError('an XFD must be a whole number of 128-byte sectors')
    with open(src, 'rb') as fi, open(dst, 'wb') as fo:
        fo.write(_atr_header(size, 128))
        _copy_rest(fi, fo, size, progress, 'adding header')
    return dst


CONVERSIONS['a8:atr->xfd'] = {
    'label': 'Atari 8-bit: ATR -> XFD (strip header)', 'engine': ENGINE_NATIVE,
    'system': 'A8BIT', 'ext': '.xfd', 'fn': lambda s, d, p=None: atr_to_xfd(s, d, p),
    'inverse': None,
    'rebuild': lambda out, tmp, extra, p=None: _add_fixed(
        16, lambda h: struct.unpack_from('<H', h, 0)[0] == ATR_MAGIC)(out, tmp, extra, p),
}
CONVERSIONS['a8:xfd->atr'] = {
    'label': 'Atari 8-bit: XFD -> ATR (computed header)', 'engine': ENGINE_NATIVE,
    'system': 'A8BIT', 'ext': '.atr', 'fn': lambda s, d, p=None: xfd_to_atr(s, d, p),
    'inverse': 'a8:atr->xfd',
}


# ── Atari ST: ST <-> MSA ──────────────────────────────────────────────────────
# MSA = 10-byte header (0x0E0F, sectors/track, sides-1, first and last track),
# then per track a u16 length and either raw data or RLE (0xE5, byte, u16 run).

def _st_geometry(data):
    spt = struct.unpack_from('<H', data, 0x18)[0]
    sides = struct.unpack_from('<H', data, 0x1A)[0]
    if not 8 <= spt <= 40 or sides not in (1, 2) or len(data) % (spt * 512 * sides):
        # copy-protected disks often carry a junk boot sector; the image size
        # then has to decide. MSA records the geometry, so the round trip holds.
        for spt, sides in ((9, 2), (10, 2), (11, 2), (9, 1), (10, 1), (11, 1), (18, 2), (36, 2)):
            tracks, rem = divmod(len(data), spt * 512 * sides)
            if not rem and 76 <= tracks <= 86:
                break
        else:
            raise ConversionError('neither the boot sector nor the image size gives '
                                  'a disk geometry')
    track = spt * 512 * sides
    if len(data) % track:
        # images are sometimes trimmed; only whole tracks are convertible
        raise ConversionError(f'{len(data):,} bytes is not a whole number of tracks')
    return spt, sides, len(data) // track


def _msa_rle(track):
    out, i, n = bytearray(), 0, len(track)
    while i < n:
        b = track[i]
        j = i
        while j < n and track[j] == b and j - i < 0xFFFF:
            j += 1
        run = j - i
        if run >= 4 or b == 0xE5:
            out += bytes([0xE5, b]) + struct.pack('>H', run)
        else:
            out += track[i:j]
        i = j
    return bytes(out)


def st_to_msa(src, dst, progress=None):
    data = Path(src).read_bytes()
    spt, sides, tracks = _st_geometry(data)
    out = bytearray(struct.pack('>HHHHH', 0x0E0F, spt, sides - 1, 0, tracks - 1))
    tlen = spt * 512
    for t in range(tracks * sides):
        track = data[t * tlen:(t + 1) * tlen]
        packed = _msa_rle(track)
        body = packed if len(packed) < tlen else track
        out += struct.pack('>H', len(body)) + body
    Path(dst).write_bytes(bytes(out))
    return dst


def msa_to_st(src, dst, progress=None):
    data = Path(src).read_bytes()
    magic, spt, sides1, first, last = struct.unpack_from('>HHHHH', data, 0)
    if magic != 0x0E0F:
        raise ConversionError('not an MSA image')
    tlen, pos, out = spt * 512, 10, bytearray()
    for _ in range((last - first + 1) * (sides1 + 1)):
        n = struct.unpack_from('>H', data, pos)[0]
        pos += 2
        chunk = data[pos:pos + n]
        pos += n
        if n == tlen:
            out += chunk
            continue
        track, i = bytearray(), 0
        while i < len(chunk):
            if chunk[i] == 0xE5:
                track += bytes([chunk[i + 1]]) * struct.unpack_from('>H', chunk, i + 2)[0]
                i += 4
            else:
                track.append(chunk[i])
                i += 1
        if len(track) != tlen:
            raise ConversionError('MSA track does not decode to a full track')
        out += track
    Path(dst).write_bytes(bytes(out))
    return dst


def _detect_st(buf, path, fh):
    if _at(buf, 0, 2) == b'\x0e\x0f' and path.suffix.lower() == '.msa':
        return _result('ATARIST', 'MSA', 'compressed', detail='Atari ST MSA disk image',
                       conversions=['st:msa->st'])
    if path.suffix.lower() == '.st' and fh['size'] % 512 == 0:
        return _result('ATARIST', 'ST', 'raw', confidence='low',
                       detail=f'Atari ST raw disk, {fh["size"] // 1024} KiB',
                       conversions=['st:st->msa'])
    return None


CONVERSIONS['st:st->msa'] = {
    'label': 'Atari ST: ST -> MSA', 'engine': ENGINE_NATIVE, 'system': 'ATARIST',
    'ext': '.msa', 'fn': lambda s, d, p=None: st_to_msa(s, d, p), 'inverse': 'st:msa->st',
}
CONVERSIONS['st:msa->st'] = {
    'label': 'Atari ST: MSA -> ST', 'engine': ENGINE_NATIVE, 'system': 'ATARIST',
    'ext': '.st', 'fn': lambda s, d, p=None: msa_to_st(s, d, p), 'inverse': 'st:st->msa',
}


# ── ZX Spectrum TR-DOS: SCL <-> TRD ───────────────────────────────────────────
# SCL = "SINCLAIR", file count, 14-byte catalogue entries, the files' sectors in
# order, and a u32 sum of every preceding byte. TRD is the whole 640 KiB disk:
# catalogue in track 0 sectors 1-8, disk info in sector 9, data from track 1.

TRD_SIZE = 640000
TRD_SPT = 16


def scl_to_trd(src, dst, progress=None):
    data = Path(src).read_bytes()
    if data[:8] != b'SINCLAIR':
        raise ConversionError('not an SCL file')
    count = data[8]
    if struct.unpack_from('<I', data, len(data) - 4)[0] != sum(data[:-4]) & 0xFFFFFFFF:
        raise ConversionError('SCL checksum does not match - damaged file')
    disk = bytearray(TRD_SIZE)
    pos = 9 + count * 14
    track, sector = 1, 0
    for i in range(count):
        entry = data[9 + i * 14:23 + i * 14]
        nsec = entry[13]
        at = (track * TRD_SPT + sector) * 256
        disk[at:at + nsec * 256] = data[pos:pos + nsec * 256]
        disk[i * 16:i * 16 + 14] = entry
        disk[i * 16 + 14] = sector
        disk[i * 16 + 15] = track
        pos += nsec * 256
        sector += nsec
        track += sector // TRD_SPT
        sector %= TRD_SPT
    used = (track * TRD_SPT + sector) - TRD_SPT
    info = 8 * 256
    disk[info + 0xE1] = sector
    disk[info + 0xE2] = track
    disk[info + 0xE3] = 0x16                     # 80 tracks, double sided
    disk[info + 0xE4] = count
    struct.pack_into('<H', disk, info + 0xE5, 2544 - used)
    disk[info + 0xE7] = 0x10                     # TR-DOS signature
    disk[info + 0xEA:info + 0xF3] = b' ' * 9
    disk[info + 0xF5:info + 0xFD] = b'        '
    Path(dst).write_bytes(bytes(disk))
    return dst


def trd_to_scl(src, dst, progress=None):
    disk = Path(src).read_bytes()
    if len(disk) < 9 * 256 or disk[8 * 256 + 0xE7] != 0x10:
        raise ConversionError('not a TR-DOS disk')
    entries, blobs = [], []
    for i in range(128):
        e = disk[i * 16:i * 16 + 16]
        if e[0] == 0:
            break
        if e[0] == 1:
            continue                              # deleted file
        start = (e[15] * TRD_SPT + e[14]) * 256
        blobs.append(disk[start:start + e[13] * 256])
        entries.append(e[:14])
    body = b'SINCLAIR' + bytes([len(entries)]) + b''.join(entries) + b''.join(blobs)
    Path(dst).write_bytes(body + struct.pack('<I', sum(body) & 0xFFFFFFFF))
    return dst


CONVERSIONS['zx:scl->trd'] = {
    'label': 'ZX Spectrum: SCL -> TRD (TR-DOS disk)', 'engine': ENGINE_NATIVE,
    'system': 'ZX', 'ext': '.trd', 'fn': lambda s, d, p=None: scl_to_trd(s, d, p),
    'inverse': 'zx:trd->scl',
}
CONVERSIONS['zx:trd->scl'] = {
    'label': 'ZX Spectrum: TRD -> SCL (files only; lossy for unused sectors)',
    'engine': ENGINE_NATIVE, 'system': 'ZX', 'ext': '.scl',
    'fn': lambda s, d, p=None: trd_to_scl(s, d, p), 'inverse': 'zx:scl->trd',
}

DETECTORS.extend([_detect_atr, _detect_st, _detect_jaguar])



# ══════════════════════════════════════════════════════════════════════════════
#  BATCH 5  -  C64 D81, and PC floppy images: IMD / TD0 -> raw IMG
# ══════════════════════════════════════════════════════════════════════════════

# ── Commodore 1581: D81 file extraction ───────────────────────────────────────
D81_SIZES = {819200: False, 822400: True}           # True = error bytes appended


def _d81_offset(track, sector):
    if not 1 <= track <= 80 or not 0 <= sector < 40:
        raise ConversionError(f'track {track} sector {sector} is outside a 1581 disk')
    return ((track - 1) * 40 + sector) * 256


def d81_to_files(src, dst, progress=None):
    data = Path(src).read_bytes()
    if len(data) not in D81_SIZES:
        raise ConversionError(f'{len(data):,} bytes is not a D81 size')

    def block(t, s):
        o = _d81_offset(t, s)
        return data[o:o + 256]

    def chain(t, s):
        out, seen = bytearray(), set()
        while True:
            if (t, s) in seen:
                raise ConversionError('sector chain loops')
            seen.add((t, s))
            b = block(t, s)
            if b[0] == 0:
                out += b[2:max(2, b[1] + 1)]
                return bytes(out)
            out += b[2:]
            t, s = b[0], b[1]

    types = {1: 'seq', 2: 'prg', 3: 'usr', 4: 'rel'}
    files, t, s, seen = [], 40, 3, set()
    while t and (t, s) not in seen:
        seen.add((t, s))
        b = block(t, s)
        for i in range(8):
            e = b[2 + i * 32:2 + i * 32 + 30]
            kind = e[0]
            if not kind & 0x80 or (kind & 0x07) not in types:
                continue
            try:
                body = chain(e[1], e[2])
            except ConversionError:
                continue
            files.append((_petscii_name(e[3:19]), types[kind & 0x07], body))
        t, s = b[0], b[1]
    files.sort(key=lambda f: f[1] != 'prg')
    return _write_extracted(dst, files)


# ── PC floppies: sector map -> raw image ──────────────────────────────────────
def _raw_from_sectors(tracks):
    """tracks: {(cyl, head): {sector_no: bytes}} -> raw image bytes, laid out
    cylinder by cylinder, head by head, sectors in ascending number. Sectors a
    track does not have are filled with zeros so every track has the same
    length, which is what a raw .IMG needs."""
    if not tracks:
        raise ConversionError('the image holds no sectors')
    sizes = {len(b) for secs in tracks.values() for b in secs.values()}
    size = max(sizes, key=lambda z: sum(1 for secs in tracks.values()
                                        for b in secs.values() if len(b) == z))
    first = min(min(secs) for secs in tracks.values() if secs)
    spt = max(max(secs) for secs in tracks.values() if secs) - first + 1
    cyls = max(c for c, _ in tracks) + 1
    heads = max(h for _, h in tracks) + 1
    out = bytearray()
    irregular = 0
    for c in range(cyls):
        for h in range(heads):
            secs = tracks.get((c, h), {})
            for n in range(first, first + spt):
                b = secs.get(n)
                if b is None or len(b) != size:
                    irregular += 1
                    b = (b or b'')[:size].ljust(size, b'\x00')
                out += b
    return bytes(out), {'geometry': f'{cyls}x{heads}x{spt}x{size}',
                        'irregular_sectors': irregular}


def imd_to_img(src, dst, progress=None):
    data = Path(src).read_bytes()
    if not data.startswith(b'IMD '):
        raise ConversionError('not an ImageDisk file')
    pos = data.index(b'\x1a') + 1
    tracks = {}
    while pos < len(data):
        mode, cyl, head, nsec, code = data[pos:pos + 5]
        pos += 5
        numbers = data[pos:pos + nsec]
        pos += nsec
        if head & 0x80:
            pos += nsec                          # cylinder map
        if head & 0x40:
            pos += nsec                          # head map
        if code == 0xFF:
            sizes = struct.unpack_from(f'<{nsec}H', data, pos)
            pos += 2 * nsec
        else:
            if code > 6:
                raise ConversionError(f'bad IMD sector size code {code}')
            sizes = (128 << code,) * nsec
        secs = tracks.setdefault((cyl, head & 0x3F), {})
        for n, size in zip(numbers, sizes):
            kind = data[pos]
            pos += 1
            if kind == 0:
                continue                          # sector not readable
            if kind in (2, 4, 6, 8):
                secs[n] = bytes([data[pos]]) * size
                pos += 1
            elif kind in (1, 3, 5, 7):
                secs[n] = data[pos:pos + size]
                pos += size
            else:
                raise ConversionError(f'bad IMD sector record type {kind}')
    raw, info = _pc_raw_or_refuse(tracks)
    Path(dst).write_bytes(raw)
    return info


def _pc_raw_or_refuse(tracks):
    """A raw IMG is only honest for a regular disk. Copy-protected layouts
    (duplicate or out-of-range sector numbers, odd sizes, missing sectors)
    would come out as a padded, mis-shaped image that no emulator reads
    correctly - refuse those instead of writing one."""
    raw, info = _raw_from_sectors(tracks)
    spt = int(info['geometry'].split('x')[2])
    if info['irregular_sectors'] or spt > 36:
        raise ConversionError(
            f"irregular (probably copy-protected) layout - {info['irregular_sectors']} "
            f"sectors do not fit a {info['geometry']} disk, so a raw IMG would lose data")
    return raw, info


# Teledisk "advanced compression": LZSS + adaptive Huffman (Yoshizaki's LZHUF),
# as ported in MAME's td0_dsk.cpp.
_TD_N, _TD_F, _TD_THRESHOLD = 4096, 60, 2
_TD_NCHAR = 256 - _TD_THRESHOLD + _TD_F
_TD_T = _TD_NCHAR * 2 - 1
_TD_R = _TD_T - 1
_TD_MAXFREQ = 0x8000
_TD_DCODE = bytes([0] * 32 + [1] * 16 + [2] * 16 + [3] * 16 + [4] * 8 + [5] * 8 + [6] * 8 +
                  [7] * 8 + [8] * 8 + [9] * 8 + [10] * 8 + [11] * 8 +
                  [12] * 4 + [13] * 4 + [14] * 4 + [15] * 4 + [16] * 4 + [17] * 4 +
                  [18] * 4 + [19] * 4 + [20] * 4 + [21] * 4 + [22] * 4 + [23] * 4 +
                  [24] * 2 + [25] * 2 + [26] * 2 + [27] * 2 + [28] * 2 + [29] * 2 +
                  [30] * 2 + [31] * 2 + [32] * 2 + [33] * 2 + [34] * 2 + [35] * 2 +
                  [36] * 2 + [37] * 2 + [38] * 2 + [39] * 2 + [40] * 2 + [41] * 2 +
                  [42] * 2 + [43] * 2 + [44] * 2 + [45] * 2 + [46] * 2 + [47] * 2 +
                  list(range(48, 64)))
_TD_DLEN = bytes([3] * 32 + [4] * 48 + [5] * 64 + [6] * 48 + [7] * 48 + [8] * 16)


def _td0_lzhuf(src):
    freq = [0] * (_TD_T + 1)
    prnt = [0] * (_TD_T + _TD_NCHAR)
    son = [0] * _TD_T
    for i in range(_TD_NCHAR):
        freq[i] = 1
        son[i] = i + _TD_T
        prnt[i + _TD_T] = i
    i, j = 0, _TD_NCHAR
    while j <= _TD_R:
        freq[j] = freq[i] + freq[i + 1]
        son[j] = i
        prnt[i] = prnt[i + 1] = j
        i += 2
        j += 1
    freq[_TD_T] = 0xFFFF
    prnt[_TD_R] = 0
    state = {'buf': 0, 'len': 0, 'pos': 0}

    def need(n):
        while state['len'] <= 8:
            if state['pos'] >= len(src):
                return state['len'] >= n
            state['buf'] |= src[state['pos']] << (8 - state['len'])
            state['pos'] += 1
            state['len'] += 8
        return True

    def getbit():
        if not need(1):
            return -1
        bit = (state['buf'] >> 15) & 1
        state['buf'] = (state['buf'] << 1) & 0xFFFF
        state['len'] -= 1
        return bit

    def getbyte():
        if not need(8):
            return -1
        v = state['buf'] >> 8
        state['buf'] = (state['buf'] << 8) & 0xFFFF
        state['len'] -= 8
        return v

    def reconst():
        j = 0
        for i in range(_TD_T):
            if son[i] >= _TD_T:
                freq[j] = (freq[i] + 1) // 2
                son[j] = son[i]
                j += 1
        i, j = 0, _TD_NCHAR
        while j < _TD_T:
            f = freq[i] + freq[i + 1]
            freq[j] = f
            k = j - 1
            while f < freq[k]:
                k -= 1
            k += 1
            freq[k + 1:j + 1] = freq[k:j]
            freq[k] = f
            son[k + 1:j + 1] = son[k:j]
            son[k] = i
            i += 2
            j += 1
        for i in range(_TD_T):
            k = son[i]
            if k >= _TD_T:
                prnt[k] = i
            else:
                prnt[k] = prnt[k + 1] = i

    def update(c):
        if freq[_TD_R] == _TD_MAXFREQ:
            reconst()
        c = prnt[c + _TD_T]
        while True:
            freq[c] += 1
            k = freq[c]
            l = c + 1
            if k > freq[l]:
                l += 1
                while k > freq[l]:
                    l += 1
                l -= 1
                freq[c] = freq[l]
                freq[l] = k
                i = son[c]
                prnt[i] = l
                if i < _TD_T:
                    prnt[i + 1] = l
                j = son[l]
                son[l] = i
                prnt[j] = c
                if j < _TD_T:
                    prnt[j + 1] = c
                son[c] = j
                c = l
            c = prnt[c]
            if c == 0:
                break

    text = bytearray(b' ' * (_TD_N + _TD_F - 1))
    r = _TD_N - _TD_F
    out = bytearray()
    while True:
        c = son[_TD_R]
        while c < _TD_T:
            bit = getbit()
            if bit < 0:
                return bytes(out)
            c = son[c + bit]
        c -= _TD_T
        update(c)
        if c < 256:
            out.append(c)
            text[r] = c
            r = (r + 1) & (_TD_N - 1)
            continue
        b = getbyte()
        if b < 0:
            return bytes(out)
        hi = _TD_DCODE[b] << 6
        i = b
        for _ in range(_TD_DLEN[b] - 2):
            bit = getbit()
            if bit < 0:
                return bytes(out)
            i = (i << 1) + bit
        pos = hi | (i & 0x3F)
        start = (r - pos - 1) & (_TD_N - 1)
        for k in range(c - 255 + _TD_THRESHOLD):
            ch = text[(start + k) & (_TD_N - 1)]
            out.append(ch)
            text[r] = ch
            r = (r + 1) & (_TD_N - 1)


def td0_to_img(src, dst, progress=None):
    raw = Path(src).read_bytes()
    if raw[:2] not in (b'TD', b'td'):
        raise ConversionError('not a Teledisk image')
    header = raw[:12]
    body = _td0_lzhuf(raw[12:]) if raw[:2] == b'td' else raw[12:]
    pos = 10 + struct.unpack_from('<H', body, 2)[0] if header[7] & 0x80 else 0
    tracks = {}
    while pos < len(body):
        nsec = body[pos]
        if nsec == 0xFF:
            break
        cyl, head = body[pos + 1], body[pos + 2] & 0x7F
        pos += 4
        secs = tracks.setdefault((cyl, head), {})
        for _ in range(nsec):
            _c, _h, num, code, flags = body[pos:pos + 5]
            pos += 6
            if flags & 0x30:
                continue                          # no data recorded for this sector
            size = 128 << code
            enc = body[pos + 2]
            pos += 3
            if enc == 0:
                data = body[pos:pos + size]
                pos += size
            elif enc == 1:
                count, a, b = struct.unpack_from('<HBB', body, pos)
                pos += 4
                data = (bytes([a, b]) * count)[:size].ljust(size, b'\x00')
            elif enc == 2:
                buf = bytearray()
                while len(buf) < size:
                    kind, rep = body[pos], body[pos + 1]
                    pos += 2
                    if kind == 0:
                        buf += body[pos:pos + rep]
                        pos += rep
                    else:
                        n = 1 << kind
                        buf += body[pos:pos + n] * rep
                        pos += n
                data = bytes(buf[:size])
            else:
                raise ConversionError(f'unknown Teledisk sector encoding {enc}')
            secs[num] = data
    img, info = _pc_raw_or_refuse(tracks)
    Path(dst).write_bytes(img)
    return dict(info, compressed=raw[:2] == b'td')


def _detect_pc_floppy(buf, path, fh):
    if _at(buf, 0, 4) == b'IMD ':
        return _result('PCFLOPPY', 'IMD', None, detail='ImageDisk floppy image',
                       conversions=['pc:imd->img'])
    if _at(buf, 0, 2) in (b'TD', b'td') and path.suffix.lower() == '.td0':
        return _result('PCFLOPPY', 'TD0', 'compressed' if buf[0] == 0x74 else None,
                       detail='Teledisk floppy image'
                              + (' (advanced compression)' if buf[0] == 0x74 else ''),
                       conversions=['pc:td0->img'])
    return None


def _detect_d81(buf, path, fh):
    if fh['size'] in D81_SIZES:
        hdr = _read_at(buf, fh, _d81_offset(40, 0), 3)
        if len(hdr) == 3 and hdr[0] == 40 and hdr[1] == 3 and hdr[2] == 0x44:
            return _result('C64', 'D81', None, detail='1581 3.5" disk image',
                           conversions=['c64:d81->files'])
    return None


CONVERSIONS['c64:d81->files'] = {
    'label': 'C64: D81 disk -> PRG/SEQ files', 'engine': ENGINE_NATIVE, 'system': 'C64',
    'ext': '.prg', 'fn': lambda s, d, p=None: d81_to_files(s, d, p), 'inverse': None,
    'verify_mode': 'none',
    'note': 'Extraction from a container; check the files against a DAT.',
}
for _cid, _label, _fn in (('pc:imd->img', 'PC floppy: IMD -> raw IMG', imd_to_img),
                          ('pc:td0->img', 'PC floppy: TD0 (Teledisk) -> raw IMG', td0_to_img)):
    CONVERSIONS[_cid] = {
        'label': _label, 'engine': ENGINE_NATIVE, 'system': 'PCFLOPPY', 'ext': '.img',
        'fn': (lambda s, d, p=None, _f=_fn: _f(s, d, p)), 'inverse': None,
        'verify_mode': 'none',
        'note': 'Sector-level images keep timing, CRC-error and deleted-data flags a raw '
                'image cannot hold, so this is one-way; check the IMG against a DAT.',
    }
DETECTORS.extend([_detect_pc_floppy, _detect_d81])



# ══════════════════════════════════════════════════════════════════════════════
#  PS3 DISC  -  Redump ISO <-> decrypted ISO
#
#  Sector 0 lists the PLAIN sector ranges; everything between them is
#  AES-128-CBC encrypted per 2048-byte sector with the disc key, IV = the
#  sector number big-endian. Redump's per-disc ".key" is that disc key (verified
#  against an IRD, whose data1 encrypts to it under a fixed secret).
#
#  Proof: an IRD lists the MD5 of every file on the decrypted disc, so a
#  decrypted ISO is checked file by file - not just "it decrypted".
# ══════════════════════════════════════════════════════════════════════════════

PS3_SECTOR = 2048
PS3_KEY_SECRET = bytes.fromhex('380bcf0b53455b3c7817ab4fa3ba90ed')
PS3_KEY_IV = bytes.fromhex('69474772af6fdab342743aefaa186287')
KEY_FILES['ps3_keys'] = (KEYS_DIR / 'ps3_keys', 'PS3 - folder of Redump disc keys (<game>.key)')
KEY_FILES['ps3_irds'] = (KEYS_DIR / 'ps3_irds', 'PS3 - folder of IRD files (<game>.ird), for verification')


def _ps3_plain_ranges(sector0):
    count = struct.unpack_from('>I', sector0, 0)[0]
    if not 1 <= count <= 64:
        raise ConversionError('sector 0 is not a PS3 region table')
    return [struct.unpack_from('>II', sector0, 8 + i * 8) for i in range(count)]


def _ps3_find(stem, folder_key, exts):
    for folder in (KEYS_DIR / folder_key.split('_')[1] if False else KEY_FILES[folder_key][0],):
        for ext in exts:
            cand = Path(folder) / (stem + ext)
            if cand.exists():
                return cand
    return None


def ps3_disc_key(iso):
    iso = Path(iso)
    for path in (iso.with_suffix('.key'), iso.with_suffix('.dkey'),
                 _ps3_find(iso.stem, 'ps3_keys', ('.key', '.dkey'))):
        if path and Path(path).exists():
            raw = Path(path).read_bytes()
            if len(raw) == 16:
                return raw
            text = raw.decode('ascii', 'ignore').strip()
            if len(text) >= 32:
                return bytes.fromhex(text[:32])
    ird = ps3_read_ird(iso)
    if ird:
        from Crypto.Cipher import AES
        return AES.new(PS3_KEY_SECRET, AES.MODE_CBC, iv=PS3_KEY_IV).encrypt(ird['data1'])
    raise ConversionError(f'no disc key for "{iso.stem}" - put its Redump .key (or .dkey/.ird) '
                          f'in {KEY_FILES["ps3_keys"][0]}')


def ps3_read_ird(iso):
    """{'files': [(sector, md5)], 'regions': [md5], 'data1': bytes} or None."""
    import gzip
    iso = Path(iso)
    path = next((p for p in (iso.with_suffix('.ird'), _ps3_find(iso.stem, 'ps3_irds', ('.ird',)))
                 if p and Path(p).exists()), None)
    if not path:
        return None
    d = Path(path).read_bytes()
    if d[:2] == b'\x1f\x8b':
        d = gzip.decompress(d)
    if d[:4] != b'3IRD':
        raise ConversionError(f'{Path(path).name} is not an IRD')
    ver = d[4]
    pos = 5 + 9
    pos += 1 + d[pos] + 4 + 5 + 5
    if ver == 7:
        pos += 4
    for _ in range(2):                               # header, footer
        pos += 4 + struct.unpack_from('<i', d, pos)[0]
    rc = d[pos]
    pos += 1
    regions = [d[pos + i * 16:pos + i * 16 + 16] for i in range(rc)]
    pos += rc * 16
    fc = struct.unpack_from('<i', d, pos)[0]
    pos += 4
    files = [(struct.unpack_from('<q', d, pos + i * 24)[0], d[pos + i * 24 + 8:pos + i * 24 + 24])
             for i in range(fc)]
    pos += fc * 24 + 4
    if ver == 9:
        pos += 115
    return {'files': files, 'regions': regions, 'data1': d[pos:pos + 16]}


def _ps3_crypt(src, dst, decrypt, progress=None):
    from Crypto.Cipher import AES
    key = ps3_disc_key(src)
    total = os.path.getsize(src) // PS3_SECTOR
    with open(src, 'rb') as fi:
        plain = _ps3_plain_ranges(fi.read(PS3_SECTOR))
    is_plain = lambda n: any(a <= n <= b for a, b in plain)
    ecb = AES.new(key, AES.MODE_ECB)
    batch = 512
    with open(src, 'rb') as fi, open(dst, 'wb') as fo:
        n = 0
        while n < total:
            count = min(batch, total - n)
            chunk = fi.read(count * PS3_SECTOR)
            if all(is_plain(n + i) for i in (0, count - 1)) and \
                    not any(n < b + 1 and a <= n + count - 1 and not (a <= n and n + count - 1 <= b)
                            for a, b in plain):
                fo.write(chunk)                       # wholly plain batch
            elif decrypt:
                # CBC decrypt of many sectors at once: ECB-decrypt everything,
                # then XOR each block with the previous ciphertext block (the
                # per-sector IV for a sector's first block).
                out = bytearray(ecb.decrypt(chunk))
                prev = bytearray(len(chunk))
                for i in range(count):
                    o = i * PS3_SECTOR
                    prev[o:o + 16] = (n + i).to_bytes(16, 'big')
                    prev[o + 16:o + PS3_SECTOR] = chunk[o:o + PS3_SECTOR - 16]
                mixed = (int.from_bytes(out, 'big') ^ int.from_bytes(prev, 'big')).to_bytes(len(chunk), 'big')
                for i in range(count):
                    if is_plain(n + i):
                        o = i * PS3_SECTOR
                        mixed = mixed[:o] + chunk[o:o + PS3_SECTOR] + mixed[o + PS3_SECTOR:]
                fo.write(mixed)
            else:
                for i in range(count):
                    o = i * PS3_SECTOR
                    sec = chunk[o:o + PS3_SECTOR]
                    if not is_plain(n + i):
                        sec = AES.new(key, AES.MODE_CBC, iv=(n + i).to_bytes(16, 'big')).encrypt(sec)
                    fo.write(sec)
            n += count
            if progress:
                progress(n, total, 'decrypting' if decrypt else 'encrypting')
    return {'plain_ranges': plain}


def _iso9660_files(path):
    """{first sector: size} for every file on an ISO9660 volume (multi-extent
    files are summed onto their first extent)."""
    out = {}
    with open(path, 'rb') as f:
        f.seek(16 * PS3_SECTOR)
        pvd = f.read(PS3_SECTOR)
        if pvd[1:6] != b'CD001':
            raise ConversionError('no ISO9660 volume descriptor')
        root = pvd[156:190]
        todo, seen = [(struct.unpack_from('<I', root, 2)[0], struct.unpack_from('<I', root, 10)[0])], set()
        while todo:
            ext, size = todo.pop()
            if ext in seen:
                continue
            seen.add(ext)
            f.seek(ext * PS3_SECTOR)
            data = f.read(size)
            pos, pending = 0, None
            while pos < len(data):
                ln = data[pos]
                if ln == 0:
                    pos = (pos // PS3_SECTOR + 1) * PS3_SECTOR
                    continue
                rec = data[pos:pos + ln]
                e, sz, flags = struct.unpack_from('<I', rec, 2)[0], struct.unpack_from('<I', rec, 10)[0], rec[25]
                name = rec[33:33 + rec[32]]
                if name not in (b'\x00', b'\x01'):
                    if flags & 0x02:
                        todo.append((e, sz))
                    elif pending is not None:
                        out[pending] += sz
                    else:
                        out[e] = sz
                    pending = (pending if pending is not None else e) if flags & 0x80 else None
                pos += ln
    return out


def _ps3_ird_verifier(src, out, progress=None):
    ird = ps3_read_ird(src)
    if not ird:
        return None, 'no IRD for this disc - decrypted, but not verified file by file'
    sizes = _iso9660_files(out)
    bad = missing = 0
    with open(out, 'rb') as f:
        for sector, md5 in ird['files']:
            size = sizes.get(sector)
            if size is None:
                missing += 1
                continue
            h = hashlib.md5()
            f.seek(sector * PS3_SECTOR)
            left = size
            while left:
                b = f.read(min(CHUNK, left))
                if not b:
                    break
                h.update(b)
                left -= len(b)
            bad += h.digest() != md5
    n = len(ird['files'])
    if bad or missing:
        return False, f'IRD check: {bad} of {n} files differ, {missing} not found'
    return True, f'all {n} files match the IRD MD5s'


CONVERSIONS['ps3:iso->deciso'] = {
    'label': 'PS3: Redump ISO -> decrypted ISO (disc key)', 'engine': ENGINE_NATIVE,
    'system': 'PS3', 'ext': '.iso', 'fn': lambda s, d, p=None: _ps3_crypt(s, d, True, p),
    'inverse': 'ps3:deciso->iso', 'verify_mode': 'verifier', 'verifier': _ps3_ird_verifier,
}
CONVERSIONS['ps3:deciso->iso'] = {
    'label': 'PS3: decrypted ISO -> Redump ISO (disc key)', 'engine': ENGINE_NATIVE,
    'system': 'PS3', 'ext': '.iso', 'fn': lambda s, d, p=None: _ps3_crypt(s, d, False, p),
    'inverse': 'ps3:iso->deciso',
}


# ══════════════════════════════════════════════════════════════════════════════
#  PS ONE CLASSICS  -  PSN PKG -> Redump-style BIN/CUE
#
#  EBOOT.PBP DATA.PSAR "PSISOIMG0000" (one disc) or "PSTITLEIMG0000" (up to 5).
#  Per disc: a PGD at +0x400 holds the track table and the block table (0x3C00);
#  the image lives from +0x100000 in 16-sector (0x9300) blocks, compressed with
#  the POPS range coder and stored with sync, MSF, EDC and ECC blanked. Rebuilding
#  those fields is what makes the result a real disc image - verified byte-exact
#  against Redump (Dosukoi Densetsu, 75,698 sectors). Ported from psxtract
#  (Hykem's lz.c and Daniel Huguenin's cdrom.c).
#
#  CD audio tracks are stored as ATRAC3, a lossy codec: they cannot become the
#  original PCM, so multi-track games get their data track only, flagged.
# ══════════════════════════════════════════════════════════════════════════════


def psx_lz_decompress(src, size):
    head = src[0]
    code = (src[1] << 24) | (src[2] << 16) | (src[3] << 8) | src[4]
    if head & 0x80:
        return bytes(src[5:5 + code])
    tmp = bytearray(b'\x80' * 0xA60 + bytes(0x10))
    rng = 0xFFFFFFFF
    ip = 5
    n_src = len(src)
    out = bytearray()
    offset = 0
    prev = 0
    M = 0xFFFFFFFF

    while True:
        # decode_bit(tmp + offset + 0x920), inlined
        c = offset + 0x920
        p = tmp[c]
        if not rng >> 24:
            val = rng * p
            rng = (rng << 8) & M
            code = ((code << 8) | (src[ip] if ip < n_src else 0)) & M
            ip += 1
        else:
            val = (rng >> 8) * p
        p -= p >> 3
        if code < val:
            rng = val
            tmp[c] = p + 31
            bit = 1
        else:
            code -= val
            rng -= val
            tmp[c] = p
            bit = 0

        if not bit:                                   # raw byte
            if offset > 0:
                offset -= 1
            if len(out) == size:
                return bytes(out)
            base = (((((len(out) & 7) << 8) + prev) >> head) & 7) * 0xFF - 1
            index = 1
            while True:
                c = base + index
                p = tmp[c]
                if not rng >> 24:
                    val = rng * p
                    rng = (rng << 8) & M
                    code = ((code << 8) | (src[ip] if ip < n_src else 0)) & M
                    ip += 1
                else:
                    val = (rng >> 8) * p
                p -= p >> 3
                if code < val:
                    rng = val
                    tmp[c] = p + 31
                    index = (index << 1) | 1
                else:
                    code -= val
                    rng -= val
                    tmp[c] = p
                    index <<= 1
                if index >> 8:
                    break
            out.append(index & 0xFF)
            prev = index & 0xFF
            continue

        # compressed run -------------------------------------------------------
        def dbit(c):
            nonlocal rng, code, ip
            p = tmp[c]
            if not rng >> 24:
                val = rng * p
                rng = (rng << 8) & M
                code = ((code << 8) | (src[ip] if ip < n_src else 0)) & M
                ip += 1
            else:
                val = (rng >> 8) * p
            p -= p >> 3
            if code < val:
                rng = val
                tmp[c] = p + 31
                return 1
            code -= val
            rng -= val
            tmp[c] = p
            return 0

        def drange():
            nonlocal rng, code, ip
            if not rng >> 24:
                rng = (rng << 8) & M
                code = ((code << 8) | (src[ip] if ip < n_src else 0)) & M
                ip += 1

        s1 = offset + 0x920
        index = -1
        while True:
            s1 += 8
            bf = dbit(s1)
            index += bf
            if not (bf and index < 6):
                break
        b_size = 0x40
        s2 = index + 0x7F1
        if index >= 0 or bf:
            sect = (index << 5) | (((len(out) << index) & 3) << 3) | (offset & 7)
            ptr = 0x960 + sect
            # decode_number(ptr, index)
            i, idx = 1, index
            if idx >= 3:
                i = (i << 1) | dbit(ptr + 0x18)
                if idx >= 4:
                    i = (i << 1) | dbit(ptr + 0x18)
                    if idx >= 5:
                        drange()
                        while idx >= 5:
                            i <<= 1
                            rng >>= 1
                            if code < rng:
                                i += 1
                            else:
                                code -= rng
                            idx -= 1
            bf = dbit(ptr)
            i = (i << 1) | bf
            if idx >= 1:
                i = (i << 1) | dbit(ptr + 0x8)
                if idx >= 2:
                    i = (i << 1) | dbit(ptr + 0x10)
            data_length = i
            if data_length != 3 and (index > 0 or bf):
                s2 += 0x38
                b_size = 0x80
        else:
            data_length = 1
        shift = 1
        while True:
            diff = (shift << 4) - b_size
            bf = dbit(s2 + (shift << 3))
            shift = (shift << 1) | bf
            if not diff < 0:
                break
        if diff > 0 or bf:
            if not bf:
                diff -= 8
            ptr = 0x8A8 + diff
            idx = diff // 8
            i = 1
            if idx >= 3:
                i = (i << 1) | dbit(ptr)
                if idx >= 4:
                    i = (i << 1) | dbit(ptr)
                    if idx >= 5:
                        drange()
                        while idx >= 5:
                            i <<= 1
                            rng >>= 1
                            if code < rng:
                                i += 1
                            else:
                                code -= rng
                            idx -= 1
            bf = dbit(ptr + 3)
            i = (i << 1) | bf
            if idx >= 1:
                i = (i << 1) | dbit(ptr + 2)
                if idx >= 2:
                    i = (i << 1) | dbit(ptr + 1)
            data_offset = i
        else:
            data_offset = 1
        start = len(out) - data_offset
        end = len(out) + data_length + 1
        if start < 0 or end > size:
            # the stream ends on an out-of-range "match"; psxtract's C returns
            # -1 there but has already filled the buffer, and uses it
            if len(out) == size:
                return bytes(out)
            raise ConversionError('PS1 LZ block is corrupt (match out of range)')
        offset = ((end + 1) & 1) + 6
        if data_offset >= end - len(out):
            out += out[start:start + (end - len(out))]
        else:
            for k in range(end - len(out)):
                out.append(out[start + k])
        prev = out[-1]


CD_SYNC = bytes([0x00] + [0xFF] * 10 + [0x00])

def _cd_tables():
    edc = []
    for i in range(256):
        e = i
        for _ in range(8):
            e = (e >> 1) ^ (0xD8018001 if e & 1 else 0)
        edc.append(e)
    f, b = [0] * 256, [0] * 256
    for i in range(256):
        f[i] = ((i << 1) ^ (0x11D if i & 0x80 else 0)) & 0xFF
        b[i ^ f[i]] = i
    return edc, f, b


_CD_EDC, _CD_F, _CD_B = _cd_tables()


def cd_edc(data):
    e = 0
    t = _CD_EDC
    for b in data:
        e = (e >> 8) ^ t[(e ^ b) & 0xFF]
    return e


def _cd_ecc(sector, major_count, minor_count, major_mult, minor_inc, dest):
    size = major_count * minor_count
    src = sector[0x0C:0x0C + size]
    F, B = _CD_F, _CD_B
    out = bytearray(major_count * 2)
    for major in range(major_count):
        index = (major >> 1) * major_mult + (major & 1)
        a = b = 0
        for _ in range(minor_count):
            t = src[index]
            index += minor_inc
            if index >= size:
                index -= size
            a ^= t
            b ^= t
            a = F[a]
        a = B[F[a] ^ b]
        out[major] = a
        out[major + major_count] = a ^ b
    sector[dest:dest + major_count * 2] = out


def cd_msf_next(minutes, seconds, frames):
    frames += 1
    if frames & 0x0F == 0x0A:
        frames += 6
    if frames == 0x75:
        frames = 0
        seconds += 1
        if seconds & 0x0F == 0x0A:
            seconds += 6
        if seconds == 0x60:
            seconds = 0
            minutes += 1
            if minutes & 0x0F == 0x0A:
                minutes += 6
    return minutes, seconds, frames


def cd_fix_sector(sector, msf, form2_edc):
    """sector: bytearray(2352), fixed in place. Returns 'mode0'/'form1'/'form2'."""
    mode = sector[15]
    if mode == 0:
        sector[0:12] = CD_SYNC
        sector[12:15] = bytes(msf)
        return 'mode0'
    if mode != 2:
        raise ConversionError(f'unsupported sector mode {mode}')
    sector[0:12] = CD_SYNC
    sub = sector[16:24]
    # the two subheader copies can legitimately differ on a real disc; the
    # first copy decides the form, as a drive reading the disc would
    if sub[2] & 0x20:                                   # form 2
        sector[12:15] = bytes(msf)
        if form2_edc:
            e = cd_edc(sector[16:0x92C])
            sector[0x92C:0x930] = e.to_bytes(4, 'little')
        else:
            sector[0x92C:0x930] = bytes(4)
        return 'form2'
    e = cd_edc(sector[16:0x818])
    sector[0x818:0x81C] = e.to_bytes(4, 'little')
    sector[12:16] = bytes(4)                           # ECC is taken with a zero header
    _cd_ecc(sector, 86, 24, 2, 86, 0x81C)
    _cd_ecc(sector, 52, 43, 86, 88, 0x8C8)
    sector[12:15] = bytes(msf)
    sector[15] = 2
    return 'form1'



PSX_BLOCK = 0x9300


def _psx_disc_offsets(pkg, it, psar):
    magic = pkg.item_bytes(it, psar, 16)
    if magic[:12] == b'PSISOIMG0000':
        return [0]
    if magic[:12] == b'PSTITLEIMG00':
        table = pgd_decrypt(pkg.item_bytes(it, psar + 0x200, 0x2A0))
        return [o for o in struct.unpack_from('<5I', table, 0) if o]
    return None


def _psx_build_disc(pkg, it, psar, base, dst, progress=None, label=''):
    table = pgd_decrypt(pkg.item_bytes(it, psar + base + 0x400, 0xB6600))
    disc_id = table[:16].split(b'\x00')[0].decode('ascii', 'replace')
    entries, off = [], 0x3C00
    while True:
        o, size, marker = struct.unpack_from('<IHH', table, off)
        if size == 0:
            break
        entries.append((o, size, marker))
        off += 32
    audio = 0
    for k in range(0x800, 0xE20, 16):
        if not struct.unpack_from('<I', table, k)[0]:
            break
        audio += 1
    data_base = psar + base + 0x100000
    form2_edc, msf, held, written = None, (0, 2, 0), [], 0
    with open(dst, 'wb') as fo:
        for n, (o, size, marker) in enumerate(entries):
            if not marker:
                continue                        # psxtract's "junk" blocks: not disc data
            blob = pkg.item_bytes(it, data_base + o, size)
            block = psx_lz_decompress(blob, PSX_BLOCK) if size < PSX_BLOCK else blob[:PSX_BLOCK]
            if len(block) != PSX_BLOCK:
                raise ConversionError(f'block {n} decompressed to {len(block)} bytes')
            if form2_edc is None:
                # INFER, as psxtract does: the boot area's form-2 sectors say
                # whether this disc carries form-2 EDC at all
                edcs = [block[k * 2352 + 0x92C:k * 2352 + 0x930] for k in (12, 13, 14, 15)]
                form2_edc = sum(e != bytes(4) for e in edcs) > sum(e == bytes(4) for e in edcs)
            for k in range(16):
                sec = bytearray(block[k * 2352:(k + 1) * 2352])
                if sec[15] == 0 and not any(sec[12:]):
                    held.append(msf)            # maybe the trailing zero padding
                else:
                    for hmsf in held:           # it was not: real mode-0 sectors
                        z = bytearray(2352)
                        cd_fix_sector(z, hmsf, form2_edc)
                        fo.write(z)
                        written += 1
                    held = []
                    cd_fix_sector(sec, msf, form2_edc)
                    fo.write(sec)
                    written += 1
                msf = cd_msf_next(*msf)
            if progress:
                progress(n + 1, len(entries), f'{label}rebuilding disc')
    return {'disc_id': disc_id, 'sectors': written, 'audio_tracks': audio}


def psx_classic_to_bin(pkg, it, dst, progress=None):
    head = pkg.item_bytes(it, 0, 0x28)
    psar = struct.unpack_from('<I', head, 0x24)[0]
    discs = _psx_disc_offsets(pkg, it, psar)
    if not discs:
        return None
    final = Path(dst)
    base = final.with_suffix('') if final.suffix == '.part' else final
    results, sidecars = [], []
    for i, off in enumerate(discs):
        stem = base.stem if i == 0 else f'{base.stem} (Disc {i + 1})'
        target = Path(dst) if i == 0 else base.with_name(stem + '.bin')
        info = _psx_build_disc(pkg, it, psar, off, target, progress,
                               f'disc {i + 1}/{len(discs)}: ' if len(discs) > 1 else '')
        cue = base.with_name(stem + '.cue')
        cue.write_bytes(f'FILE "{stem}.bin" BINARY\r\n  TRACK 01 MODE2/2352\r\n'
                        '    INDEX 01 00:00:00\r\n'.encode())
        sidecars.append(str(cue))
        if i:
            sidecars.append(str(target))
        results.append(info)
    note = ''
    if any(r['audio_tracks'] for r in results):
        note = ('CD audio tracks are stored as lossy ATRAC3 and were not rebuilt - the '
                'data track alone cannot match a multi-track Redump set')
    return {'discs': results, 'sidecars': sidecars, 'note': note}



# ══════════════════════════════════════════════════════════════════════════════
#  PS VITA  -  PSN PKG -> NoNpDrm folder
#
#  A Vita package decrypts (outer layer only) to the installed folder tree. The
#  game files inside - eboot.bin, SELFs, PFS-protected data - stay exactly as
#  shipped, which is what the Unofficial NoNpDrm DAT lists (PCSB00395\eboot.bin).
#  The licence goes to sce_sys/package/work.bin: taken from a work.bin beside the
#  PKG (No-Intro's Content set ships one) or decoded from the zRIF in the
#  NoPayStation TSVs. head.bin/tail.bin/stat.bin/body.bin follow pkg2zip, which
#  is what NoNpDrm needs to install the folder.
# ══════════════════════════════════════════════════════════════════════════════

_ZRIF_DICT = None


def zrif_decode(zrif):
    """NoPayStation zRIF string -> the licence (512 bytes; 1024 for PSM)."""
    import base64
    import zlib
    global _ZRIF_DICT
    if _ZRIF_DICT is None:
        _ZRIF_DICT = zlib.decompress(base64.b64decode(
            'eNpjYBgFo2AU0AsYAIElGt8MRJiDCAsw3xhEmIAIU4N4AwNdRxcXZ3+/EJCAkW6Ac7C7ARwYgviuQAaIdoPSzlDa'
            'BUo7QmknIM3ACIZM78+u7kx3VWYEAGJ9HV0='))
    try:
        return zlib.decompressobj(zdict=_ZRIF_DICT).decompress(base64.b64decode(zrif.strip()))
    except Exception as e:
        raise ConversionError(f'zRIF does not decode: {e}')


def vita_licence(src, content_id):
    """(bytes, where it came from) or (None, reason)."""
    src = Path(src)
    for cand in (src.with_name('work.bin'), src.with_suffix('.rif')):
        if cand.exists() and cand.stat().st_size in (512, 1024):
            return cand.read_bytes(), cand.name
    folder = KEYS_DIR / 'nps'
    for tsv in sorted(folder.glob('PSV_*.tsv')) + sorted(folder.glob('PSM_*.tsv')):
        with open(tsv, encoding='utf-8', errors='replace') as f:
            header = f.readline().rstrip('\n').split('\t')
            try:
                ci, zi = header.index('Content ID'), header.index('zRIF')
            except ValueError:
                continue
            for line in f:
                row = line.rstrip('\n').split('\t')
                if len(row) > max(ci, zi) and row[ci] == content_id:
                    z = row[zi].strip()
                    if z and z.upper() not in ('MISSING', 'NOT REQUIRED'):
                        return zrif_decode(z), f'zRIF from {tsv.name}'
    return None, f'no work.bin beside the PKG and no zRIF for {content_id} in {folder}'


VITA_APP, VITA_DLC, VITA_PATCH, VITA_THEME = 0x15, 0x16, 0x17, 0x1F
VITA_PSM = (0x18, 0x1D)


def vita_pkg_extract(src, dst, progress=None, decrypt=False):
    import shutil
    pkg = _Pkg(src)
    try:
        if not pkg.vita:
            raise ConversionError('not a PS Vita / PSM package')
        ct = pkg.content_type
        psm = ct in VITA_PSM
        if not psm and ct not in (VITA_APP, VITA_DLC, VITA_PATCH, VITA_THEME):
            raise ConversionError(f'unsupported Vita content type 0x{ct:x}')
        title = pkg.content_id[7:16]
        final = Path(dst)
        base = final.with_suffix('') if final.suffix == '.part' else final
        root = base.with_name(base.stem + ' [NoNpDrm]') / title
        written, primary, sfo = [], None, None
        total = len(pkg.items)
        for n, it in enumerate(pkg.items):
            if it['kind'] in (4, 18):
                continue
            name = it['name'].replace('\\', '/')
            raw = False
            if psm:
                if not name.startswith('content/'):
                    continue
                rel = name[8:] if 'runtime' in name else 'RO/' + name[8:]
            elif name in ('sce_sys/package/digs.bin', 'sce_sys/package/cert.bin'):
                rel, raw = 'sce_sys/package/body.bin', True        # kept as stored
            else:
                rel = name
            if '..' in Path(rel).parts or Path(rel).is_absolute():
                raise ConversionError(f'unsafe path in package: {name}')
            target = root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(target, 'wb') as fo:
                done = 0
                while done < it['size']:
                    step = min(CHUNK, it["size"] - done)
                    if raw:
                        pkg.f.seek(pkg.enc_off + it['offset'] + done)
                        fo.write(pkg.f.read(step))
                    else:
                        fo.write(pkg.item_bytes(it, done, step))
                    done += step
            written.append(str(target))
            if rel.lower() == 'eboot.bin':
                primary = target
            elif rel == 'sce_sys/param.sfo':
                sfo = target
            if progress:
                progress(n + 1, total, 'extracting')
        pkgdir = root / ('RO/License' if psm else 'sce_sys/package')
        pkgdir.mkdir(parents=True, exist_ok=True)
        extra = []
        if not psm:
            with open(src, 'rb') as f:
                (pkgdir / 'head.bin').write_bytes(f.read(pkg.enc_off + pkg.items_size))
                f.seek(pkg.enc_off + pkg.enc_size)
                (pkgdir / 'tail.bin').write_bytes(f.read())
            (pkgdir / 'stat.bin').write_bytes(bytes(768))
            extra += [pkgdir / 'head.bin', pkgdir / 'tail.bin', pkgdir / 'stat.bin']
        rif, where = vita_licence(src, pkg.content_id)
        if rif is not None:
            lic = pkgdir / ('FAKE.rif' if psm else 'work.bin')
            lic.write_bytes(rif)
            extra.append(lic)
            note = f'licence: {where}'
        else:
            note = f'no licence written - {where}'
        written += [str(x) for x in extra]
        primary = primary or sfo
        if primary is None:
            raise ConversionError('the package has no eboot.bin or param.sfo')
        if decrypt:
            if psm:
                raise ConversionError('PSM packages have no PFS layer to remove')
            if rif is None:
                raise ConversionError(f'cannot remove the PFS layer: {note}')
            pkg.close()
            out_root = base.with_name(base.stem + ' [Decrypted]') / title
            if out_root.exists():
                shutil.rmtree(out_root)
            written = VitaPfs(root, rif[0x50:0x60]).decrypt_to(out_root, progress)
            shutil.rmtree(root.parent)                   # the still-encrypted install tree
            primary = out_root / primary.relative_to(root)
        shutil.copyfile(primary, dst)
        return {'sidecars': written, 'content_id': pkg.content_id, 'note': note}
    finally:
        pkg.close()


# ── PS Vita PFS (the inner layer) ─────────────────────────────────────────────
# Port of Vita3K's psvpfsparser for gamedata and DLC (unicv.db). Each file of an
# installed title is encrypted per 0x8000 sector with the licence key (klicensee,
# work.bin + 0x50) run through a fixed F00D key; the tweak mask comes from the
# table's dbseed (or the salts on old images). unicv.db does not say which file a
# table belongs to, so tables are matched to files by the HMAC of the first
# sector, as psvpfsparser does. Verified 1,262 of 1,262 files against the
# Unofficial NoNpDrm DAT (Final Fantasy X HD Remaster (Europe)).

PFS_HMAC_KEY0 = bytes.fromhex('E462258B1F3121560745DB62B1436723D2BF80FE')
PFS_HMAC_KEY1 = bytes.fromhex('AFE656BB3C17256A3C809F6E9BF19FDD5A388543')
PFS_IV0 = bytes.fromhex('74D20CC39881C213EE770B1010E4BEA7')
PFS_CONTRACT_KEY0 = bytes.fromhex('E12213B48016B0E99AB81F8EC02AD4A2')
PFS_SECTOR = 0x8000
PFS_PAGE = 0x400


def _pfs_hmac(key, data):
    import hmac
    return hmac.new(key, data, hashlib.sha1).digest()


def _pfs_cbc(key, iv, data, decrypt):
    """The Vita's CBC with a short tail: whole blocks CBC, the tail XORed with
    the encryption of the last ciphertext block (or of the IV)."""
    from Crypto.Cipher import AES
    n = len(data) & ~0xF
    out = bytearray()
    if n:
        c = AES.new(key, AES.MODE_CBC, iv=iv)
        out += c.decrypt(data[:n]) if decrypt else c.encrypt(data[:n])
        iv = data[n - 16:n] if decrypt else bytes(out[n - 16:n])
    if len(data) > n:
        ks = AES.new(key, AES.MODE_ECB).encrypt(iv)
        out += bytes(a ^ b for a, b in zip(data[n:], ks))
    return bytes(out)


def _pfs_salts(files_salt, icv_salt):
    return struct.pack('<I', icv_salt) if files_salt == 0 else struct.pack('<II', files_salt, icv_salt)


class VitaPfs:
    def __init__(self, root, klicensee):
        from Crypto.Cipher import AES
        self.root = Path(root)
        self.drv = AES.new(PFS_CONTRACT_KEY0, AES.MODE_ECB).decrypt(klicensee)
        if not (self.root / 'sce_pfs' / 'files.db').exists():
            raise ConversionError('no sce_pfs/files.db - not an installed Vita title')
        self._read_files_db()
        self._read_unicv()

    def _read_files_db(self):
        d = (self.root / 'sce_pfs' / 'files.db').read_bytes()
        if d[:8] != b'SCENGPFS':
            raise ConversionError('files.db: bad magic')
        _ver, self.image_spec, _key_id, page_size, _order, _root, self.files_salt = \
            struct.unpack_from('<IHHIIII', d, 8)
        if self.image_spec not in (1, 4):
            raise ConversionError(f'PFS image spec {self.image_spec} is savedata/ADDCONT (icv.db) - '
                                  'only gamedata and DLC are supported')
        if page_size != PFS_PAGE:
            raise ConversionError('files.db: unexpected page size')
        entries = []
        for off in range(PFS_PAGE, len(d), PFS_PAGE):
            nfiles = struct.unpack_from('<I', d, off + 8)[0]
            if nfiles > 9:
                continue                                   # psvpfsparser's "bad block"
            for i in range(nfiles):
                parent = struct.unpack_from('<I', d, off + 16 + i * 72)[0]
                name = d[off + 20 + i * 72:off + 88 + i * 72].split(b'\x00')[0].decode('utf-8', 'replace')
                idx, ftype, _p0, size = struct.unpack_from('<IHHI', d, off + 16 + 9 * 72 + i * 16)
                entries.append((idx, parent, name, ftype, size))
        dirs = {idx: (parent, name) for idx, parent, name, ftype, size in entries if ftype & 0x8000}

        def path_of(parent, name):
            parts = [name]
            while parent != 0:
                if parent not in dirs or len(parts) > 64:
                    raise ConversionError(f'files.db: broken directory chain for {name}')
                parent, pname = dirs[parent]
                parts.append(pname)
            rel = '/'.join(reversed(parts))
            if '..' in rel.split('/'):
                raise ConversionError(f'files.db: unsafe path {rel}')
            return rel

        self.files = {}
        for idx, parent, name, ftype, size in entries:
            if ftype & 0x8000 or (ftype == 0 and size == 0):
                continue
            rel = path_of(parent, name)
            self.files[rel.upper()] = (rel, ftype or 1, size)   # untyped files are encrypted

    def _read_unicv(self):
        d = (self.root / 'sce_pfs' / 'unicv.db').read_bytes()
        if d[:8] != b'SCEIRODB':
            raise ConversionError('unicv.db: bad magic')
        block_size = struct.unpack_from('<I', d, 12)[0]
        data_size = struct.unpack_from('<Q', d, 24)[0]
        if block_size != PFS_PAGE or len(d) != data_size + block_size:
            raise ConversionError('unicv.db: bad header')
        self.tables = []
        off = PFS_PAGE
        while off < len(d):
            page = d[off:off + PFS_PAGE]
            if not any(page):
                off += PFS_PAGE
                continue
            if page[:8] != b'SCEIFTBL':
                raise ConversionError(f'unicv.db: expected SCEIFTBL at 0x{off:x}')
            version, _ps, _per_page, nsec, sector_size = struct.unpack_from('<IIIII', page, 8)
            off += PFS_PAGE
            table = {'version': version, 'nsec': nsec, 'sector': sector_size,
                     'salt': off // PFS_PAGE - 1, 'dbseed': page[52:72], 'sigs': []}
            left = nsec
            while left > 0:
                sp = d[off:off + PFS_PAGE]
                n = struct.unpack_from('<I', sp, 8)[0]
                if not 0 < n <= left:
                    raise ConversionError('unicv.db: bad signature page')
                table['sigs'] += [sp[16 + k * 20:36 + k * 20] for k in range(n)]
                left -= n
                off += PFS_PAGE
            self.tables.append(table)

    def secret(self, icv_salt):
        combo = _pfs_hmac(PFS_HMAC_KEY1, _pfs_salts(self.files_salt, icv_salt))
        return _pfs_cbc(self.drv, PFS_IV0, combo, decrypt=False)

    def mask(self, table):
        if table['version'] > 1:
            return _pfs_hmac(PFS_HMAC_KEY0, table['dbseed'])[:16]
        return _pfs_hmac(PFS_HMAC_KEY0, _pfs_salts(self.files_salt, table['salt']))[:16]

    def map_tables(self, progress=None):
        """{upper-case path: table}, matched by the HMAC of each file's first sector."""
        real = {}
        for f in self.root.rglob('*'):
            rel = f.relative_to(self.root).as_posix()
            if f.is_file() and not rel.startswith(('sce_pfs/', 'sce_sys/package/')):
                real[rel.upper()] = f
        by_nsec, heads, mapping = {}, {}, {}
        for key, f in real.items():
            size = f.stat().st_size
            if size:
                by_nsec.setdefault(-(-size // PFS_SECTOR), []).append(key)
        todo = [t for t in self.tables if t['nsec']]
        for i, t in enumerate(todo):
            sig_key = _pfs_hmac(self.secret(t['salt']), struct.pack('<I', 0))
            found = None
            for key in by_nsec.get(t['nsec'], []):
                if key not in heads:
                    with open(real[key], 'rb') as fh:
                        heads[key] = fh.read(PFS_SECTOR)
                if _pfs_hmac(sig_key, heads[key]) == t['sigs'][0]:
                    found = key
                    break
            if found is None:
                raise ConversionError(f'no file matches the PFS table at page {t["salt"]} - '
                                      'wrong licence (work.bin) for this package?')
            by_nsec[t['nsec']].remove(found)
            heads.pop(found, None)
            mapping[found] = t
            if progress:
                progress(i + 1, len(todo), 'matching PFS tables')
        return mapping, real

    def decrypt_to(self, dest, progress=None):
        dest = Path(dest)
        mapping, real = self.map_tables(progress)
        written = []
        for n, (key, (rel, ftype, size)) in enumerate(sorted(self.files.items())):
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            src = real.get(key)
            if src is None:
                if size:
                    raise ConversionError(f'{rel} is listed in files.db but is not in the package')
                target.write_bytes(b'')
                written.append(str(target))
                continue
            t = mapping.get(key)
            with open(src, 'rb') as fi, open(target, 'wb') as fo:
                if ftype not in (1, 6, 7) or t is None:        # stored unencrypted
                    shutil.copyfileobj(fi, fo, CHUNK)
                else:
                    mask, k = self.mask(t), 0
                    while True:
                        sec = fi.read(t['sector'])
                        if not sec:
                            break
                        tweak = bytes(a ^ b for a, b in
                                      zip(struct.pack('<Q', k * t['sector']) + bytes(8), mask))
                        fo.write(_pfs_cbc(self.drv, tweak, sec, decrypt=True))
                        k += 1
            written.append(str(target))
            if progress:
                progress(n + 1, len(self.files), 'decrypting PFS')
        return written


CONVERSIONS['vita:pkg->nonpdrm'] = {
    'label': 'PS Vita: PSN PKG -> NoNpDrm folder (+ work.bin)', 'engine': ENGINE_NATIVE,
    'system': 'VITA', 'ext': '.bin', 'fn': lambda s, d, p=None: vita_pkg_extract(s, d, p),
    'inverse': None, 'verify_mode': 'none',
    'note': 'Outer decryption only, as pkg2zip does: the installable folder with its PFS '
            'layer intact.',
}
CONVERSIONS['vita:pkg->decrypted'] = {
    'label': 'PS Vita: PSN PKG -> decrypted files (PFS removed)', 'engine': ENGINE_NATIVE,
    'system': 'VITA', 'ext': '.bin', 'fn': lambda s, d, p=None: vita_pkg_extract(s, d, p, decrypt=True),
    'inverse': None, 'verify_mode': 'none',
    'note': 'The layout of the Unofficial NoNpDrm DAT. SELF files stay signed/encrypted '
            'as on the console; needs work.bin beside the PKG or a zRIF in keys/nps.',
}



# ══════════════════════════════════════════════════════════════════════════════
#  APPLE II  -  NIB / WOZ (nibble and bit level) <-> DSK (sectors)
#
#  A 5.25" DOS 3.3 / ProDOS disk stores each 256-byte sector as 6-and-2 GCR
#  nibbles behind an address field (D5 AA 96, volume/track/sector in 4-and-4)
#  and a data field (D5 AA AD, 342 nibbles + checksum). Decoding keeps only the
#  sector contents, so it is LOSSY: sync gaps, volume numbers, timing and any
#  copy protection are gone, and a protected disk (missing or non-standard
#  sectors) is refused rather than half-converted. Tables follow Tom Harte's
#  dsk2woz; the layout of a WOZ follows the Applesauce WOZ 1/2 reference.
# ══════════════════════════════════════════════════════════════════════════════

A2_GCR62 = bytes.fromhex('96979a9b9d9e9fa6a7abacadaeafb2b3b4b5b6b7b9babbbcbdbebfcbcdcecfd3'
                         'd6d7d9dadbdcdddedfe5e6e7e9eaebecedeeeff2f3f4f5f6f7f9fafbfcfdfeff')
_A2_UNGCR = {v: i for i, v in enumerate(A2_GCR62)}
A2_DOS_ORDER = (0, 7, 14, 6, 13, 5, 12, 4, 11, 3, 10, 2, 9, 1, 8, 15)     # physical -> DOS sector
A2_PRODOS_ORDER = (0, 8, 1, 9, 2, 10, 3, 11, 4, 12, 5, 13, 6, 14, 7, 15)  # physical -> ProDOS
A2_TRACKS, A2_NIB_TRACK = 35, 6656
_A2_BITREV = (0, 2, 1, 3)


def _a2_encode_sector(data):
    enc = [0] * 342
    for c in range(84):
        enc[c] = _A2_BITREV[data[c] & 3] | (_A2_BITREV[data[c + 86] & 3] << 2) | \
                 (_A2_BITREV[data[c + 172] & 3] << 4)
    enc[84] = _A2_BITREV[data[84] & 3] | (_A2_BITREV[data[170] & 3] << 2)
    enc[85] = _A2_BITREV[data[85] & 3] | (_A2_BITREV[data[171] & 3] << 2)
    for c in range(256):
        enc[86 + c] = data[c] >> 2
    out, last = bytearray(), 0
    for v in enc:
        out.append(A2_GCR62[v ^ last])
        last = v
    out.append(A2_GCR62[last])
    return bytes(out)


def _a2_decode_sector(nibs):
    last, vals = 0, []
    for n in nibs[:342]:
        v = _A2_UNGCR.get(n)
        if v is None:
            return None
        last ^= v
        vals.append(last)
    if _A2_UNGCR.get(nibs[342]) != last:
        return None                                  # checksum
    out = bytearray(256)
    for c in range(256):
        aux = (vals[c % 86] >> (2 * (c // 86))) & 3
        out[c] = ((vals[86 + c] << 2) & 0xFC) | _A2_BITREV[aux]
    return bytes(out)


def _a2_44(v):
    return bytes(((v >> 1) | 0xAA, v | 0xAA))


def _a2_track_sectors(nibs, track):
    """{physical sector: 256 bytes} from one track's nibbles (read twice round,
    so a sector that straddles the index is found)."""
    n = len(nibs)
    ring = nibs + nibs[:512]
    found, i = {}, 0
    while True:
        j = ring.find(b'\xd5\xaa\x96', i)
        if j < 0 or j >= n:
            break
        i = j + 3
        h = ring[j + 3:j + 11]
        if len(h) < 8:
            break
        vol, trk, sec, chk = (((h[k] << 1) | 1) & h[k + 1] for k in (0, 2, 4, 6))
        if vol ^ trk ^ sec ^ chk or sec > 15 or trk != track or sec in found:
            continue
        k = ring.find(b'\xd5\xaa\xad', j + 11, j + 11 + 48)
        if k < 0:
            continue
        data = _a2_decode_sector(ring[k + 3:k + 346])
        if data is not None:
            found[sec] = data
    return found


def _a2_tracks_to_dsk(tracks, dst, prodos=False, progress=None):
    order = A2_PRODOS_ORDER if prodos else A2_DOS_ORDER
    image = bytearray(A2_TRACKS * 16 * 256)
    bad = []
    for t in range(A2_TRACKS):
        nibs = tracks[t] if t < len(tracks) else b''
        secs = _a2_track_sectors(nibs, t) if nibs else {}
        if len(secs) != 16:
            bad.append(f'{t}' + (f' ({len(secs)}/16 sectors)' if secs else ' (unreadable)'))
            continue
        for phys, data in secs.items():
            off = (t * 16 + order[phys]) * 256
            image[off:off + 256] = data
        if progress:
            progress(t + 1, A2_TRACKS, 'decoding tracks')
    if bad:
        # track 0 alone unreadable is the classic 13-sector (DOS 3.2) or
        # protected disk; either way the sectors do not exist to extract
        raise ConversionError(f'not a standard 16-sector disk - track(s) {", ".join(bad[:6])}'
                              f'{" ..." if len(bad) > 6 else ""}: probably copy-protected')
    Path(dst).write_bytes(bytes(image))
    return {'tracks': A2_TRACKS}


def apple_nib_to_dsk(src, dst, progress=None):
    data = Path(src).read_bytes()
    if len(data) % A2_NIB_TRACK:
        raise ConversionError(f'{len(data):,} bytes is not a whole number of 6,656-byte NIB tracks')
    tracks = [data[i:i + A2_NIB_TRACK] for i in range(0, len(data), A2_NIB_TRACK)]
    return _a2_tracks_to_dsk(tracks, dst, str(dst).lower().endswith('.po'), progress)


def _a2_bits_to_nibbles(bitstream, bit_count):
    """Read a WOZ track the way the disk controller does: shift bits in until
    the top bit is set. Twice round, then keep one revolution's worth."""
    bits = int.from_bytes(bitstream, 'big')
    total = len(bitstream) * 8
    out, reg = bytearray(), 0
    for rev in range(2):
        for i in range(bit_count):
            reg = ((reg << 1) | ((bits >> (total - 1 - i)) & 1)) & 0xFF
            if reg & 0x80:
                out.append(reg)
                reg = 0
    return bytes(out)


def apple_woz_to_dsk(src, dst, progress=None):
    d = Path(src).read_bytes()
    if d[:4] not in (b'WOZ1', b'WOZ2') or d[4:8] != b'\xff\n\r\n':
        raise ConversionError('not a WOZ file')
    chunks, pos = {}, 12
    while pos + 8 <= len(d):
        cid, size = d[pos:pos + 4], struct.unpack_from('<I', d, pos + 4)[0]
        chunks[cid] = (pos + 8, size)
        pos += 8 + size
    if b'INFO' not in chunks or b'TMAP' not in chunks or b'TRKS' not in chunks:
        raise ConversionError('WOZ is missing INFO/TMAP/TRKS')
    info = d[chunks[b'INFO'][0]:]
    if info[1] != 1:
        raise ConversionError('a 3.5" WOZ is not a 140K 5.25" disk - no DSK form exists')
    tmap = d[chunks[b'TMAP'][0]:chunks[b'TMAP'][0] + 160]
    trks = chunks[b'TRKS'][0]
    tracks = []
    for t in range(A2_TRACKS):
        idx = tmap[t * 4]
        if idx == 0xFF:
            tracks.append(b'')
            continue
        if d[:4] == b'WOZ1':
            # WOZ1 track record: 6646-byte bitstream FIRST, then bytes used
            # (u16) and bit count (u16) at +6646 / +6648
            e = trks + idx * 6656
            stream = d[e:e + 6646]
            bit_count = min(struct.unpack_from('<H', d, e + 6648)[0], 6646 * 8)
        else:
            start, blocks, bit_count = struct.unpack_from('<HHI', d, trks + idx * 8)
            stream = d[start * 512:(start + blocks) * 512]
            bit_count = min(bit_count, len(stream) * 8)
        tracks.append(_a2_bits_to_nibbles(stream, bit_count))
        if progress:
            progress(t + 1, A2_TRACKS, 'reading bit streams')
    return _a2_tracks_to_dsk(tracks, dst, str(dst).lower().endswith('.po'), None)


def apple_dsk_to_nib(src, dst, progress=None):
    """Sectors -> a standard DOS 3.3 formatted nibble image (volume 254). Only the
    sector contents are original; gaps and volume number are the usual defaults."""
    data = Path(src).read_bytes()
    if len(data) != APPLE_DISK:
        raise ConversionError(f'{len(data):,} bytes is not a 140K 5.25" disk image')
    order = A2_PRODOS_ORDER if str(src).lower().endswith('.po') else A2_DOS_ORDER
    out = bytearray()
    for t in range(A2_TRACKS):
        trk = bytearray(b'\xff' * 48)
        for phys in range(16):
            off = (t * 16 + order[phys]) * 256
            trk += b'\xd5\xaa\x96' + _a2_44(254) + _a2_44(t) + _a2_44(phys) + \
                _a2_44(254 ^ t ^ phys) + b'\xde\xaa\xeb' + b'\xff' * 6
            trk += b'\xd5\xaa\xad' + _a2_encode_sector(data[off:off + 256]) + b'\xde\xaa\xeb'
            trk += b'\xff' * 27
        out += trk + b'\xff' * (A2_NIB_TRACK - len(trk))
        if progress:
            progress(t + 1, A2_TRACKS, 'encoding tracks')
    Path(dst).write_bytes(bytes(out))
    return {'tracks': A2_TRACKS}


def _a2_nib_roundtrip_verifier(src, out, progress=None):
    """A decoded DSK re-encoded and decoded again must give the same sectors -
    proves the decode is self-consistent; the DAT decides if it is the release."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        nib, again = Path(tmp) / 'x.nib', Path(tmp) / 'x.dsk'
        apple_dsk_to_nib(out, nib)
        apple_nib_to_dsk(nib, again)
        same = again.read_bytes() == Path(out).read_bytes()
    return (None if same else False), ('sectors decoded; lossy (gaps, volume and timing '
                                       'are not kept)' if same else 'sector re-encode mismatch')


CONVERSIONS['apple:nib->dsk'] = {
    'label': 'Apple II: NIB nibble image -> DSK sectors (lossy)', 'engine': ENGINE_NATIVE,
    'system': 'APPLE2', 'ext': '.dsk', 'fn': lambda s, d, p=None: apple_nib_to_dsk(s, d, p),
    'inverse': None, 'verify_mode': 'verifier', 'verifier': _a2_nib_roundtrip_verifier,
    'note': 'Lossy: only sector contents survive. Copy-protected disks are refused.',
}
CONVERSIONS['apple:woz->dsk'] = {
    'label': 'Apple II: WOZ bit-stream image -> DSK sectors (lossy)', 'engine': ENGINE_NATIVE,
    'system': 'APPLE2', 'ext': '.dsk', 'fn': lambda s, d, p=None: apple_woz_to_dsk(s, d, p),
    'inverse': None, 'verify_mode': 'verifier', 'verifier': _a2_nib_roundtrip_verifier,
    'note': 'Lossy: only sector contents survive. Copy-protected disks are refused.',
}
CONVERSIONS['apple:dsk->nib'] = {
    'label': 'Apple II: DSK sectors -> NIB nibble image (standard format)', 'engine': ENGINE_NATIVE,
    'system': 'APPLE2', 'ext': '.nib', 'fn': lambda s, d, p=None: apple_dsk_to_nib(s, d, p),
    'inverse': 'apple:nib->dsk',
    'note': 'Writes a freshly formatted-style nibble image; it will not be byte-identical '
            'to a NIB captured from the original disk.',
}


# ══════════════════════════════════════════════════════════════════════════════
#  PSP / PS one Classics EBOOT.PBP, and the DAX and JSO compressed ISOs
#
#  A standalone EBOOT.PBP is the same file a PSN package carries, so it goes
#  through the engines already proven on PKGs: NPUMDIMG -> UMD ISO, PSISOIMG /
#  PSTITLEIMG -> Redump-style bin/cue. A small reader serves the file where the
#  engines expect a package item.
#
#  DAX (Dark_AleX) and JSO (Uncle Jam) are old PSP compressed-ISO containers.
#  Layouts follow maxcso's DAX reader/writer and ARK-4's Inferno reader for
#  both. JSO's LZO1X blocks use a port of minilzo, proven against real lzop
#  output. JSO keeps an MD5 of the original image in its header, which the
#  verifier checks.
# ══════════════════════════════════════════════════════════════════════════════

class _PbpFile:
    """A file on disk that answers like a PKG item (item_bytes / size)."""

    def __init__(self, path):
        self.f = open(path, 'rb')
        self.size = os.path.getsize(path)
        self.item = {'size': self.size, 'name': Path(path).name}

    def item_bytes(self, it, offset=0, size=None):
        size = self.size - offset if size is None else size
        self.f.seek(offset)
        return self.f.read(size)

    def close(self):
        self.f.close()


def _pbp_psar_magic(pbp):
    head = pbp.item_bytes(pbp.item, 0, 0x28)
    if head[:4] != b'\x00PBP':
        raise ConversionError('not a PBP file')
    psar = struct.unpack_from('<I', head, 0x24)[0]
    return pbp.item_bytes(pbp.item, psar, 12)


def pbp_to_iso(src, dst, progress=None):
    pbp = _PbpFile(src)
    try:
        if _pbp_psar_magic(pbp)[:8] != b'NPUMDIMG':
            raise ConversionError('this EBOOT.PBP is not a PSP game (no NPUMDIMG) - '
                                  'PS one Classics use the PBP -> bin/cue conversion')
        return _npumdimg_to_iso(pbp, pbp.item, dst, progress)
    finally:
        pbp.close()


def pbp_to_psx_bin(src, dst, progress=None):
    pbp = _PbpFile(src)
    try:
        if _pbp_psar_magic(pbp)[:8] not in (b'PSISOIMG', b'PSTITLEI'):
            raise ConversionError('this EBOOT.PBP is not a PS one Classic (no PSISOIMG)')
        res = psx_classic_to_bin(pbp, pbp.item, dst, progress)
        if res is None:
            raise ConversionError('no disc image found in the PBP')
        return res
    finally:
        pbp.close()


def lzo1x_decompress(src, out_len=None):
    src = bytes(src)
    n = len(src)
    out = bytearray()
    ip = 0

    def need(k):
        if ip + k > n:
            raise ConversionError('LZO block is truncated')

    def ext(t, base):
        nonlocal ip
        while True:
            need(1)
            if src[ip] != 0:
                break
            t += 255
            ip += 1
        t += base + src[ip]
        ip += 1
        return t

    def copy_match(dist, length):
        start = len(out) - dist
        if start < 0:
            raise ConversionError('LZO block refers back past its start')
        if dist >= length:
            out.extend(out[start:start + length])
        else:
            for k in range(length):
                out.append(out[start + k])

    def literals(t):
        nonlocal ip
        need(t)
        out.extend(src[ip:ip + t])
        ip += t

    state = 'start'
    t = 0
    need(1)
    if src[0] > 17:
        t = src[0] - 17
        ip = 1
        literals(t)
        state = 'first_literal_run' if t >= 4 else 'match_next_token'
    while True:
        if state == 'start':
            need(1)
            t = src[ip]
            ip += 1
            if t >= 16:
                state = 'match'
            else:
                if t == 0:
                    t = ext(t, 15)
                literals(t + 3)
                state = 'first_literal_run'
            continue
        if state == 'first_literal_run':
            need(1)
            t = src[ip]
            ip += 1
            if t >= 16:
                state = 'match'
                continue
            need(1)
            dist = 1 + 0x0800 + (t >> 2) + (src[ip] << 2)
            ip += 1
            copy_match(dist, 3)
            state = 'match_done'
            continue
        if state == 'match_next_token':
            need(1)
            t = src[ip]
            ip += 1
            state = 'match'
            continue
        if state == 'match':
            if t >= 64:                                   # M2
                need(1)
                dist = 1 + ((t >> 2) & 7) + (src[ip] << 3)
                ip += 1
                copy_match(dist, (t >> 5) - 1 + 2)
            elif t >= 32:                                 # M3
                t &= 31
                if t == 0:
                    t = ext(t, 31)
                need(2)
                dist = 1 + ((src[ip] | (src[ip + 1] << 8)) >> 2)
                ip += 2
                copy_match(dist, t + 2)
            elif t >= 16:                                 # M4 / end of stream
                dist = (t & 8) << 11
                t &= 7
                if t == 0:
                    t = ext(t, 7)
                need(2)
                dist += (src[ip] | (src[ip + 1] << 8)) >> 2
                ip += 2
                if dist == 0:
                    break                                 # end-of-stream marker
                copy_match(dist + 0x4000, t + 2)
            else:                                         # M1 (after a match)
                need(1)
                dist = 1 + (t >> 2) + (src[ip] << 2)
                ip += 1
                copy_match(dist, 2)
            state = 'match_done'
            continue
        if state == 'match_done':
            t = src[ip - 2] & 3
            if t == 0:
                state = 'start'
            else:
                literals(t)
                state = 'match_next_token'
            continue
    if ip != n:
        raise ConversionError(f'{n - ip} bytes left after end marker' if ip < n else 'input overrun')
    if out_len is not None and len(out) != out_len:
        raise ConversionError(f'decompressed to {len(out)} bytes, expected {out_len}')
    return bytes(out)


DAX_FRAME = 0x2000


def dax_to_iso(src, dst, progress=None):
    with open(src, 'rb') as f:
        head = f.read(32)
        if head[:4] != b'DAX\x00':
            raise ConversionError('not a DAX file')
        total, version, nc_count = struct.unpack_from('<III', head, 4)
        if version > 1:
            raise ConversionError(f'DAX version {version} is not supported')
        frames = (total + DAX_FRAME - 1) // DAX_FRAME
        index = struct.unpack(f'<{frames}I', f.read(4 * frames))
        sizes = struct.unpack(f'<{frames}H', f.read(2 * frames))
        stored = set()
        if version >= 1:
            for _ in range(nc_count):
                start, count = struct.unpack('<II', f.read(8))
                stored.update(range(start, start + count))
        with open(dst, 'wb') as fo:
            for n in range(frames):
                want = min(DAX_FRAME, total - n * DAX_FRAME)
                f.seek(index[n])
                blob = f.read(sizes[n])
                if n in stored:
                    data = blob[:want]
                else:
                    try:
                        data = zlib.decompress(blob)
                    except zlib.error as e:
                        raise ConversionError(f'DAX frame {n} does not inflate: {e}')
                if len(data) < want:
                    raise ConversionError(f'DAX frame {n} is {len(data)} bytes, expected {want}')
                fo.write(data[:want])
                if progress and n % 256 == 0:
                    progress(n, frames, 'inflating DAX')
    return {'frames': frames}


JSO_HEADER = 0x30


def _jso_header(head):
    if head[:4] != b'JISO':
        raise ConversionError('not a JSO file')
    block_size = struct.unpack_from('<H', head, 6)[0]
    block_headers, method = head[8], head[10]
    total = struct.unpack_from('<I', head, 12)[0]
    if not block_size or block_size & (block_size - 1):
        raise ConversionError(f'JSO block size {block_size} is not a power of two')
    if method not in (0, 1):
        raise ConversionError(f'unknown JSO compression method {method}')
    return block_size, block_headers, method, total, bytes(head[16:32])


def jso_to_iso(src, dst, progress=None):
    with open(src, 'rb') as f:
        block_size, block_headers, method, total, md5 = _jso_header(f.read(JSO_HEADER))
        blocks = (total + block_size - 1) // block_size
        index = struct.unpack(f'<{blocks + 1}I', f.read(4 * (blocks + 1)))
        skip = 4 if block_headers else 0
        with open(dst, 'wb') as fo:
            for n in range(blocks):
                want = min(block_size, total - n * block_size)
                start, end = index[n] & 0x7FFFFFFF, index[n + 1] & 0x7FFFFFFF
                f.seek(start + skip)
                blob = f.read(end - start - skip)
                if len(blob) == block_size:
                    data = blob                                  # stored block
                elif method == 0:
                    data = lzo1x_decompress(blob)
                else:
                    try:
                        data = zlib.decompress(blob, -15)
                    except zlib.error:
                        try:
                            data = zlib.decompress(blob)         # zlib-wrapped variant
                        except zlib.error as e:
                            raise ConversionError(f'JSO block {n} does not inflate: {e}')
                if len(data) < want:
                    raise ConversionError(f'JSO block {n} is {len(data)} bytes, expected {want}')
                fo.write(data[:want])
                if progress and n % 512 == 0:
                    progress(n, blocks, 'decompressing JSO')
    return {'blocks': blocks, 'method': 'LZO' if method == 0 else 'deflate'}


def _jso_md5_verifier(src, out, progress=None):
    with open(src, 'rb') as f:
        md5 = _jso_header(f.read(JSO_HEADER))[4]
    if not any(md5):
        return None, 'decompressed; this JSO carries no MD5 to check against'
    got = hashlib.md5()
    with open(out, 'rb') as f:
        for chunk in iter(lambda: f.read(CHUNK), b''):
            got.update(chunk)
    if got.digest() == md5:
        return True, 'matches the MD5 of the original image stored in the JSO header'
    return None, ('decompressed, but the header MD5 differs - that field is not '
                  'documented, so this is not treated as a failure')


CONVERSIONS['psp:pbp->iso'] = {
    'label': 'PSP: EBOOT.PBP (PSN game) -> ISO', 'engine': ENGINE_NATIVE,
    'system': 'PSP', 'ext': '.iso', 'fn': lambda s, d, p=None: pbp_to_iso(s, d, p),
    'inverse': None, 'verify_mode': 'none',
    'note': 'The same engine as PSN PKG -> ISO; check the ISO against the PSN (Decrypted) DAT.',
}
CONVERSIONS['psx:pbp->bin'] = {
    'label': 'PS one Classics: EBOOT.PBP -> Redump bin/cue', 'engine': ENGINE_NATIVE,
    'system': 'PSX', 'ext': '.bin', 'fn': lambda s, d, p=None: pbp_to_psx_bin(s, d, p),
    'inverse': None, 'verify_mode': 'none',
    'note': 'Data-only games can match Redump exactly. CD audio is stored as lossy ATRAC3 '
            'and is not rebuilt, so games with audio tracks will not match.',
}
CONVERSIONS['psp:dax->iso'] = {
    'label': 'PSP: DAX -> ISO (decompress)', 'engine': ENGINE_NATIVE,
    'system': 'PSP', 'ext': '.iso', 'fn': lambda s, d, p=None: dax_to_iso(s, d, p),
    'inverse': None, 'verify_mode': 'none',
    'note': 'Every frame is checked by zlib as it inflates; DAX stores no checksum of the '
            'whole image, so compare the ISO against a DAT.',
}
CONVERSIONS['psp:jso->iso'] = {
    'label': 'PSP: JSO -> ISO (decompress)', 'engine': ENGINE_NATIVE,
    'system': 'PSP', 'ext': '.iso', 'fn': lambda s, d, p=None: jso_to_iso(s, d, p),
    'inverse': None, 'verify_mode': 'verifier', 'verifier': _jso_md5_verifier,
    'note': 'LZO or deflate blocks. Not yet tested on a real JSO file.',
}


# ══════════════════════════════════════════════════════════════════════════════
#  HEADER LIBRARY  -  Atari 7800 (.a78), Atari Lynx (.lnx), Apple II 2IMG
#
#  These headers hold facts the ROM body does not: the title, cart mapper and
#  controllers of an A78, the name and bank layout of an LNX, the creator and
#  flags of a 2IMG. None can be computed, and the DATs list only whole-file
#  hashes. So headers are LEARNED from headered dumps the user already owns and
#  kept per system, keyed by the SHA-1 of the headerless body: adding a header
#  back writes the original bytes exactly, and an unknown ROM is refused rather
#  than given a guessed header. Stripping a header also teaches the library.
# ══════════════════════════════════════════════════════════════════════════════

HEADER_LIBRARY_DIR = KEYS_DIR / 'header_library'
HEADER_KINDS = {
    # kind: (library file, header size, signature check, system, label)
    'a78': ('a78.tsv', A78_HEADER, lambda h: h[1:10] == b'ATARI7800', 'A7800', 'Atari 7800'),
    'lnx': ('lnx.tsv', LNX_HEADER, lambda h: h[0:4] == b'LYNX', 'LYNX', 'Atari Lynx'),
    '2mg': ('2mg.tsv', TWOMG_HEADER, lambda h: h[0:4] == TWOMG_MAGIC, 'APPLE2', 'Apple II 2IMG'),
}
for _kind, (_file, _size, _chk, _sys, _label) in HEADER_KINDS.items():
    KEY_FILES[f'{_kind}_headers'] = (HEADER_LIBRARY_DIR / _file,
                                     f'{_label} - header library ({_file}); learned from a headered '
                                     'set with "Learn headers from a folder", or by stripping headers')
_HEADER_CACHE = {}


def header_library(kind):
    """{body sha1: header bytes} for one kind, cached until the file changes."""
    path = HEADER_LIBRARY_DIR / HEADER_KINDS[kind][0]
    stamp = path.stat().st_mtime if path.exists() else None
    cached = _HEADER_CACHE.get(kind)
    if cached and cached[0] == stamp:
        return cached[1]
    table = {}
    if path.exists():
        for line in path.read_text(encoding='utf-8').splitlines():
            parts = line.split('\t')
            if len(parts) >= 2 and len(parts[0]) == 40:
                try:
                    table[parts[0]] = bytes.fromhex(parts[1])
                except ValueError:
                    continue
    _HEADER_CACHE[kind] = (stamp, table)
    return table


def _header_kind_of(header):
    for kind, (_f, size, check, _s, _l) in HEADER_KINDS.items():
        if len(header) >= size and check(bytes(header[:size])):
            return kind
    return None


def learn_header(kind, header, body_sha1, name=''):
    """Record one header; returns True when it was new."""
    _f, size, check, _s, _l = HEADER_KINDS[kind]
    header = bytes(header[:size])
    if len(header) != size or not check(header):
        return False
    table = header_library(kind)
    if table.get(body_sha1) == header:
        return False
    HEADER_LIBRARY_DIR.mkdir(parents=True, exist_ok=True)
    with open(HEADER_LIBRARY_DIR / HEADER_KINDS[kind][0], 'a', encoding='utf-8') as f:
        f.write(f'{body_sha1}\t{header.hex()}\t{name.replace(chr(9), " ")}\n')
    _HEADER_CACHE.pop(kind, None)
    return True


def learn_headers_from_folder(folder, progress=None, should_stop=None):
    """Walk a folder (loose files and zips, including RomVault zstd zips) and
    learn every A78 / LNX / 2IMG header found. Returns counts per kind."""
    import zipfile
    try:
        import zipfile_zstd  # noqa: F401 - adds zstd (method 93) to zipfile
    except ImportError:
        pass
    counts = {k: {'seen': 0, 'new': 0} for k in HEADER_KINDS}
    files = [q for q in Path(folder).rglob('*') if q.is_file()]

    def consider(data, name):
        kind = _header_kind_of(data[:128])
        if not kind:
            return
        size = HEADER_KINDS[kind][1]
        counts[kind]['seen'] += 1
        counts[kind]['new'] += learn_header(kind, data[:size],
                                           hashlib.sha1(data[size:]).hexdigest(), name)

    for n, q in enumerate(files):
        if should_stop and should_stop():
            break
        try:
            if q.suffix.lower() == '.zip':
                with zipfile.ZipFile(q) as z:
                    for info in z.infolist():
                        if not info.is_dir() and info.file_size <= 64 * 1024 * 1024:
                            with z.open(info) as member:
                                if _header_kind_of(member.read(128)):
                                    consider(z.read(info), info.filename)
            elif q.stat().st_size <= 64 * 1024 * 1024:
                with open(q, 'rb') as f:
                    if _header_kind_of(f.read(128)):
                        consider(q.read_bytes(), q.name)
        except (OSError, zipfile.BadZipFile, NotImplementedError, RuntimeError):
            continue
        if progress and n % 50 == 0:
            progress(n + 1, len(files), q.name)
    return counts


def _strip_and_learn(kind, strip_fn):
    def run(src, dst, progress=None):
        header = strip_fn(src, dst, progress)
        try:
            learn_header(kind, header, _sha1_file(dst), Path(src).name)
        except OSError:
            pass                            # a read-only keys folder must not fail a strip
        return header
    return run


def _add_from_library(kind, add_fn):
    def run(src, dst, progress=None):
        body = _sha1_file(src)
        header = header_library(kind).get(body)
        if header is None:
            label = HEADER_KINDS[kind][4]
            raise ConversionError(
                f'no known {label} header for this ROM (body SHA-1 {body[:12]}...) - '
                'teach the header library from a headered set first')
        return add_fn(src, dst, header, progress)
    return run


for _kind, (_strip_id, _add_id, _strip_fn, _add_fn) in {
        'a78': ('a78:headered->headerless', 'a78:headerless->headered', strip_a78_header, add_a78_header),
        'lnx': ('lnx:headered->headerless', 'lnx:headerless->headered', strip_lnx_header, add_lnx_header),
        '2mg': ('apple:2mg->raw', 'apple:raw->2mg', strip_2mg_header, add_2mg_header)}.items():
    _learning_strip = _strip_and_learn(_kind, _strip_fn)
    CONVERSIONS[_strip_id]['fn'] = lambda s, d, p=None, _f=_learning_strip: _f(s, d, p)
    CONVERSIONS[_add_id].update({
        'engine': ENGINE_NATIVE, 'requires': f'{_kind}_headers',
        'fn': (lambda s, d, p=None, _f=_add_from_library(_kind, _add_fn): _f(s, d, p)),
        'note': 'Writes the original header, looked up by the ROM\'s SHA-1 in the header '
                'library; ROMs the library has not seen are refused, never guessed.',
    })
    CONVERSIONS[_add_id].pop('why', None)


# ══════════════════════════════════════════════════════════════════════════════
#  CATALOGUE  -  what each system can do, and what it needs (for the GUI)
#
#  The conversion registry says HOW to convert; this says what a person needs to
#  know before trying: the file types a conversion starts from, what has to be
#  dropped into apps/ first, and how the result is checked. tests keep it in step
#  with CONVERSIONS (tests/test_rom_catalog.py), so a new conversion without an
#  entry here fails a test rather than silently vanishing from the info view.
# ══════════════════════════════════════════════════════════════════════════════

# data files some native conversions read, registered like the other key files
# so the environment report and the catalogue can both see them
KEY_FILES['nes_header_dats'] = (KEYS_DIR / 'nes_header_dats',
                                'NES - No-Intro "(Headered)" NES DATs (their header= attributes '
                                'prove each header); preferred over nes20db.xml')
KEY_FILES['jaguar_header'] = (KEYS_DIR / JAG_HEADER_FILE,
                              'Atari Jaguar - the common 8 KiB J64 boot header (jaguar_header.bin)')
KEY_FILES['nps'] = (KEYS_DIR / 'nps',
                    'PS Vita - NoPayStation TSV files (PSV_GAMES.tsv etc.) holding zRIF licences; '
                    'not needed when a work.bin sits beside the PKG')

SYSTEM_NAMES = {
    'N64': ('Nintendo 64 / Aleck64', 'Cartridge byte orders (.z64 / .v64 / .n64).'),
    'NES': ('NES / Famicom', 'iNES headers, headerless No-Intro sets and UNIF.'),
    'FDS': ('Famicom Disk System', 'FDS headers and the QD (Quick Disk) block format.'),
    'SNES': ('Super Nintendo / Super Famicom', 'Copier headers.'),
    'NDS': ('Nintendo DS / DSi', 'Secure Area encryption and trimming.'),
    '3DS': ('Nintendo 3DS', 'NCCH encryption for .3ds carts and CIA packages.'),
    'GC': ('Nintendo GameCube', 'ISO, CISO and RVZ disc images.'),
    'WII': ('Nintendo Wii', 'ISO, WBFS and RVZ disc images.'),
    'WIIU': ('Nintendo Wii U', 'WUD and compressed WUX images.'),
    'PSP': ('PlayStation Portable', 'CSO, DAX and JSO compression, EBOOT.PBP and PSN packages - '
            'including PS one Classics PKGs, which become PlayStation bin/cue.'),
    'PSP/PS2': ('PSP / PS2 (ZSO)', 'zstd-compressed ISO containers.'),
    'PS3': ('PlayStation 3', 'Redump disc encryption.'),
    'PSX': ('PlayStation (PS one Classics)', 'PSN EBOOT.PBP files to Redump-style bin/cue.'),
    'VITA': ('PlayStation Vita', 'PSN packages to NoNpDrm / decrypted folders.'),
    'CD': ('CD images (CHD)', 'Any cue/bin CD image: PlayStation, Saturn, Sega CD, '
           'Dreamcast, PC Engine CD, Neo Geo CD, 3DO ...'),
    'CHD': ('DVD / ISO images (CHD)', 'Plain ISO images in and out of CHD.'),
    'MD': ('Sega Mega Drive / Genesis', 'SMD interleaved copier dumps.'),
    'PCE': ('PC Engine / TurboGrafx-16', 'Copier headers.'),
    'A7800': ('Atari 7800', 'A78 headers.'),
    'LYNX': ('Atari Lynx', 'LNX headers.'),
    'JAGUAR': ('Atari Jaguar', 'J64 and ROM layouts.'),
    'A8BIT': ('Atari 8-bit', 'ATR and XFD disk images.'),
    'ATARIST': ('Atari ST', 'ST and MSA disk images.'),
    'AMIGA': ('Commodore Amiga', 'DMS compressed disks.'),
    'C64': ('Commodore 64', 'PC64, tape and disk images to files.'),
    'ZX': ('ZX Spectrum', 'Tape (TAP/TZX) and TR-DOS disk (TRD/SCL) images.'),
    'APPLE2': ('Apple II', 'Sector order, 2IMG headers, nibble (NIB) and flux-level (WOZ) images.'),
    'LOOPY': ('Casio Loopy', 'Cartridge byte order.'),
    'PCFLOPPY': ('IBM PC floppy', 'ImageDisk (IMD) and Teledisk (TD0) to raw images.'),
}

# typical file types each conversion starts from. Detection is by content, so
# these are what people will recognise, not a filter.
_N64_EXT = {'big-endian': '.z64', 'byteswapped': '.v64', 'little-endian': '.n64'}
CONVERSION_INPUTS = {
    'nes:headered->headerless': ('.nes',), 'nes:headerless->headered': ('.nes',),
    'nes:unif->nes': ('.unf', '.unif'),
    'psp:iso->cso': ('.iso',), 'psp:cso->iso': ('.cso',),
    'psp:pbp->iso': ('.pbp',), 'psx:pbp->bin': ('.pbp',),
    'psp:dax->iso': ('.dax',), 'psp:jso->iso': ('.jso',),
    'psp:pkg->decrypted': ('.pkg',), 'psp:edat->decrypted': ('.edat',),
    'nds:encrypted->decrypted': ('.nds', '.dsi', '.srl'),
    'nds:decrypted->encrypted': ('.nds', '.dsi', '.srl'),
    'nds:untrimmed->trimmed': ('.nds',), 'nds:trimmed->untrimmed': ('.nds',),
    '3ds:encrypted->decrypted': ('.3ds', '.cci', '.cxi'),
    '3ds:decrypted->encrypted': ('.3ds', '.cci', '.cxi'),
    'cia:encrypted->decrypted': ('.cia',), 'cia:decrypted->encrypted': ('.cia',),
    'cia:cia->cdn': ('.cia',), 'cia:cdn->cia': ('tmd', 'cetk', 'content files'),
    'disc:gc:iso->ciso': ('.iso', '.gcm'), 'disc:gc:iso->rvz': ('.iso', '.gcm'),
    'disc:gc:ciso->iso': ('.ciso',), 'disc:gc:ciso->rvz': ('.ciso',),
    'disc:gc:rvz->iso': ('.rvz',), 'disc:gc:rvz->ciso': ('.rvz',), 'disc:gc:rvz->rvz': ('.rvz',),
    'disc:wii:iso->wbfs': ('.iso',), 'disc:wii:iso->rvz': ('.iso',),
    'disc:wii:wbfs->iso': ('.wbfs',), 'disc:wii:wbfs->rvz': ('.wbfs',),
    'disc:wii:rvz->iso': ('.rvz',), 'disc:wii:rvz->wbfs': ('.rvz',), 'disc:wii:rvz->rvz': ('.rvz',),
    'wiiu:wud->wux': ('.wud',), 'wiiu:wux->wud': ('.wux',),
    'chd:cd->chd': ('.cue', '.gdi', '.toc'), 'chd:iso->chd': ('.iso',),
    'chd:chd->cd': ('.chd',), 'chd:chd->dvd': ('.chd',), 'chd:chd->raw': ('.chd',),
    'iso:iso->zso': ('.iso',), 'iso:zso->iso': ('.zso',),
    'ps3:iso->deciso': ('.iso',), 'ps3:deciso->iso': ('.iso',),
    'vita:pkg->nonpdrm': ('.pkg',), 'vita:pkg->decrypted': ('.pkg',),
    'snes:headered->headerless': ('.smc', '.sfc', '.swc', '.fig'),
    'snes:headerless->headered': ('.sfc',),
    'md:smd->bin': ('.smd',), 'md:bin->smd': ('.bin', '.md', '.gen'),
    'pce:headered->headerless': ('.pce',), 'pce:headerless->headered': ('.pce',),
    'a78:headered->headerless': ('.a78',), 'a78:headerless->headered': ('.bin',),
    'lnx:headered->headerless': ('.lnx',), 'lnx:headerless->headered': ('.lyx', '.bin'),
    'jag:j64->rom': ('.j64',), 'jag:rom->j64': ('.rom', '.jag'),
    'a8:atr->xfd': ('.atr',), 'a8:xfd->atr': ('.xfd',),
    'st:st->msa': ('.st',), 'st:msa->st': ('.msa',),
    'amiga:dms->adf': ('.dms',),
    'c64:p00->prg': ('.p00',), 'c64:t64->prg': ('.t64',),
    'c64:d64->files': ('.d64',), 'c64:d81->files': ('.d81',),
    'zx:tap->tzx': ('.tap',), 'zx:tzx->tap': ('.tzx',),
    'zx:scl->trd': ('.scl',), 'zx:trd->scl': ('.trd',),
    'apple:2mg->raw': ('.2mg', '.2img'), 'apple:raw->2mg': ('.po', '.dsk'),
    'apple:do->po': ('.do', '.dsk'), 'apple:po->do': ('.po',),
    'apple:nib->dsk': ('.nib',), 'apple:woz->dsk': ('.woz',),
    'apple:dsk->nib': ('.dsk', '.do', '.po'),
    'loopy:big-endian->little-endian': ('.bin',), 'loopy:little-endian->big-endian': ('.bin',),
    'nes:fds-headered->headerless': ('.fds',), 'nes:fds-headerless->headered': ('.fds',),
    'fds:qd->fds': ('.qd',), 'fds:fds->qd': ('.fds',),
    'pc:imd->img': ('.imd',), 'pc:td0->img': ('.td0',),
}
for _cid in CONVERSIONS:
    if _cid.startswith('n64:'):
        CONVERSION_INPUTS[_cid] = (_N64_EXT[_cid[4:].split('->')[0]],)

# what beyond the script itself a conversion reads: (requirement, required?, why)
# requirement names are KEY_FILES / EXTERNAL_TOOLS entries
_3DS_NEEDS = [('boot9', True, 'the 3DS key scrambler inputs'),
              ('aes_keys', True, 'KeyX for 7.x and New 3DS titles'),
              ('seeddb', False, 'only for eShop titles that use seed crypto')]
_NES_HEADER_NEEDS = [('nes_header_dats', False, 'preferred source of proven headers'),
                     ('nes20db', False, 'fallback header database - at least one of the two is needed')]
CONVERSION_NEEDS = {
    'nds:encrypted->decrypted': [('nds_blow', True, 'the KEY1 Blowfish table')],
    'nds:decrypted->encrypted': [('nds_blow', True, 'the KEY1 Blowfish table')],
    '3ds:encrypted->decrypted': _3DS_NEEDS, '3ds:decrypted->encrypted': _3DS_NEEDS,
    'cia:encrypted->decrypted': _3DS_NEEDS, 'cia:decrypted->encrypted': _3DS_NEEDS,
    'nes:headerless->headered': _NES_HEADER_NEEDS, 'nes:unif->nes': _NES_HEADER_NEEDS,
    'jag:rom->j64': [('jaguar_header', True, 'the boot header J64 files share')],
    'a78:headerless->headered': [('a78_headers', True, 'the original header for this ROM')],
    'lnx:headerless->headered': [('lnx_headers', True, 'the original header for this ROM')],
    'apple:raw->2mg': [('2mg_headers', True, 'the original header for this disk')],
    'ps3:iso->deciso': [('ps3_keys', True, 'the disc key (or an IRD, which also holds it)'),
                        ('ps3_irds', False, 'checks every file of the decrypted disc')],
    'ps3:deciso->iso': [('ps3_keys', True, 'the disc key (or an IRD, which also holds it)')],
    'vita:pkg->nonpdrm': [('nps', False, 'licence (zRIF) when no work.bin sits beside the PKG')],
    'vita:pkg->decrypted': [('nps', False, 'licence (zRIF) when no work.bin sits beside the PKG')],
}
# Python packages some native engines import (requirements.txt installs them)
PYTHON_PACKAGES = {
    'pycryptodome': ('Crypto', 'pycryptodome - AES for the 3DS, CIA, PSN and PS3/Vita engines'),
    'zstandard': ('zstandard', 'zstandard - zstd compression for ZSO'),
}
_AES = ('pycryptodome', True, 'AES decryption')
for _cid in CONVERSIONS:
    if _cid.split(':')[0] in ('3ds', 'cia', 'ps3', 'vita') or _cid in (
            'psp:pkg->decrypted', 'psp:edat->decrypted', 'psp:pbp->iso', 'psx:pbp->bin'):
        CONVERSION_NEEDS[_cid] = CONVERSION_NEEDS.get(_cid, []) + [_AES]
    elif _cid in ('iso:iso->zso', 'iso:zso->iso'):
        CONVERSION_NEEDS[_cid] = [('zstandard', True, 'zstd compression')]
for _cid, _spec in CONVERSIONS.items():
    _tool = _spec.get('requires')
    if _spec['engine'] == ENGINE_EXTERNAL and _tool in EXTERNAL_TOOLS:
        CONVERSION_NEEDS.setdefault(_cid, [(_tool, True, 'does the conversion')])

# conversions whose optional needs are alternatives - one of them is enough
NEEDS_ONE_OF = {'nes:headerless->headered', 'nes:unif->nes'}

_VERIFY_TEXT = {
    'none': 'Not checked automatically - compare the result against a DAT.',
    'payload': 'Both images are decoded and the disc data compared.',
    'tool': 'chdman verifies the CHD it wrote.',
    'tool_source': 'chdman verifies the source CHD before extracting.',
    'verifier': 'A format-specific check (see the note).',
    'roundtrip': 'Converted back and compared byte for byte.',
}


def _requirement(name):
    if name in PYTHON_PACKAGES:
        import importlib.util
        module, what = PYTHON_PACKAGES[name]
        return {'name': name, 'kind': 'python package', 'what': what,
                'path': f'pip install {name}',
                'present': importlib.util.find_spec(module) is not None}
    if name in KEY_FILES:
        path, what = KEY_FILES[name]
        kind = 'folder' if not path.suffix else 'key / data file'
    else:
        path, what = EXTERNAL_TOOLS[name]
        kind = 'program'
    present = path.exists() and (not path.is_dir() or any(path.iterdir()))
    return {'name': name, 'kind': kind, 'what': what, 'path': str(path), 'present': present}


def system_catalog():
    """Every system the script knows, its conversions and their requirements."""
    systems = {}
    for cid, spec in CONVERSIONS.items():
        code = spec['system']
        name, blurb = SYSTEM_NAMES.get(code, (code, ''))
        sysd = systems.setdefault(code, {'code': code, 'name': name, 'about': blurb,
                                         'conversions': [], 'needs': {}})
        st = conversion_status(cid)
        needs = []
        for req, required, why in CONVERSION_NEEDS.get(cid, []):
            r = dict(_requirement(req), required=required, why=why)
            needs.append(r)
            known = sysd['needs'].setdefault(req, dict(r))
            known['required'] = known['required'] or required
        # optional needs that come as alternatives: at least one must be present
        either_missing = bool(needs) and all(not n['required'] for n in needs) and             cid in NEEDS_ONE_OF and not any(n['present'] for n in needs)
        if spec.get('fn') is None:
            status = 'planned'
        elif not st['available'] or either_missing or                 any(n['required'] and not n['present'] for n in needs):
            status = 'missing'
        else:
            status = 'ready'
        inverse_built = bool(CONVERSIONS.get(spec.get('inverse') or '', {}).get('fn'))
        mode = spec.get('verify_mode')
        if mode is None:
            verify = (_VERIFY_TEXT['roundtrip'] if inverse_built or spec.get('rebuild')
                      else _VERIFY_TEXT['none'])
        else:
            verify = _VERIFY_TEXT.get(mode, mode)
        reason = ''
        if status == 'planned':
            reason = 'not implemented yet'
        elif status == 'missing':
            reason = st['reason'] or 'a required file is missing'
        sysd['conversions'].append({
            'id': cid, 'label': spec['label'].split(': ', 1)[-1],
            'from': list(CONVERSION_INPUTS.get(cid, ())), 'to': spec['ext'],
            'engine': spec['engine'], 'status': status, 'reason': reason,
            'lossy': 'lossy' in (spec['label'] + spec.get('note', '')).lower(),
            'reversible': inverse_built,
            'verify': verify, 'note': spec.get('note', ''), 'needs': needs,
        })
    out = []
    for sysd in systems.values():
        sysd['needs'] = sorted(sysd['needs'].values(),
                               key=lambda r: (not r['required'], r['name']))
        convs = sysd['conversions']
        sysd['ready'] = sum(c['status'] == 'ready' for c in convs)
        sysd['planned'] = sum(c['status'] == 'planned' for c in convs)
        sysd['total'] = len(convs)
        out.append(sysd)
    return sorted(out, key=lambda s: s['name'].lower())

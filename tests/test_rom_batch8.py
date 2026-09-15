"""Batch 8: ECM <-> BIN, and ROM patches (IPS / UPS / BPS / xdelta).

    python tests/test_rom_batch8.py

Patches here are built by small independent encoders written from the format
specs, so the decoders are checked against something other than themselves.
Real-world proof comes from known-answer patches (RomPatcher.js's list: base
ROM CRC32, patch, expected output CRC32) run in the real-file matrix.
"""
import os
import struct
import sys
import tempfile
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import rom_tools as rt

results = []


def check(name, ok, detail=''):
    results.append(ok)
    print(f'{"PASS" if ok else "FAIL"}  {name}  {detail}')


crc = lambda b: zlib.crc32(b) & 0xFFFFFFFF


# ── encoders (independent of rom_tools) ──────────────────────────────────────
def beat(n):
    out = bytearray()
    while True:
        x = n & 0x7F
        n >>= 7
        if n == 0:
            out.append(0x80 | x)
            return bytes(out)
        out.append(x)
        n -= 1


def make_ips(records, truncate=None):
    out = bytearray(b'PATCH')
    for off, data in records:
        out += off.to_bytes(3, 'big')
        if isinstance(data, tuple):                       # RLE: (count, byte)
            out += b'\x00\x00' + data[0].to_bytes(2, 'big') + bytes([data[1]])
        else:
            out += len(data).to_bytes(2, 'big') + data
    out += b'EOF'
    if truncate is not None:
        out += truncate.to_bytes(3, 'big')
    return bytes(out)


def make_ups(src, dst):
    out = bytearray(b'UPS1') + beat(len(src)) + beat(len(dst))
    last, i, n = 0, 0, max(len(src), len(dst))
    s = src + bytes(n - len(src))
    d = dst + bytes(n - len(dst))
    while i < n:
        if s[i] == d[i]:
            i += 1
            continue
        out += beat(i - last)
        while i < n and s[i] != d[i]:
            out.append(s[i] ^ d[i])
            i += 1
        out.append(0)
        i += 1
        last = i
    out += struct.pack('<II', crc(src), crc(dst))
    out += struct.pack('<I', crc(bytes(out)))
    return bytes(out)


def make_bps(src, dst, actions):
    """actions: ('sr', n) | ('tr', n) | ('sc', n, source_offset) | ('tc', n, target_offset)"""
    out = bytearray(b'BPS1') + beat(len(src)) + beat(len(dst)) + beat(3) + b'x=1'
    src_rel = dst_rel = op = 0
    for a in actions:
        kind, n = a[0], a[1]
        cmd = {'sr': 0, 'tr': 1, 'sc': 2, 'tc': 3}[kind]
        out += beat(((n - 1) << 2) | cmd)
        if kind == 'tr':
            out += dst[op:op + n]
        elif kind in ('sc', 'tc'):
            rel = src_rel if kind == 'sc' else dst_rel
            delta = a[2] - rel
            out += beat((abs(delta) << 1) | (delta < 0))
            if kind == 'sc':
                src_rel = a[2] + n
            else:
                dst_rel = a[2] + n
        op += n
    out += struct.pack('<II', crc(src), crc(dst))
    out += struct.pack('<I', crc(bytes(out)))
    return bytes(out)


def vint(n):
    parts = [n & 0x7F]
    n >>= 7
    while n:
        parts.append(0x80 | (n & 0x7F))
        n >>= 7
    return bytes(reversed(parts))


class VcdWindow:
    """Emits instructions with explicit default-code-table indices and keeps the
    RFC 3284 address caches in step, as an encoder must."""

    def __init__(self, seg_len):
        self.seg_len, self.data, self.inst, self.addr = seg_len, bytearray(), bytearray(), bytearray()
        self.near, self.slot, self.same, self.tlen = [0] * 4, 0, [0] * 768, 0

    def add(self, b):
        self.inst += bytes([1]) + vint(len(b))                 # ADD, size in instructions
        self.data += b
        self.tlen += len(b)

    def run(self, byte, n):
        self.inst += bytes([0]) + vint(n)
        self.data.append(byte)
        self.tlen += n

    def copy(self, a, n, mode):
        here = self.seg_len + self.tlen
        if mode == 0:
            enc = vint(a)
        elif mode == 1:
            enc = vint(here - a)
        elif mode < 6:
            enc = vint(a - self.near[mode - 2])
        else:
            enc = bytes([self.same.index(a) - (mode - 6) * 256])
        self.inst += bytes([19 + 16 * mode]) + vint(n)          # COPY size-0 entry for the mode
        self.addr += enc
        self.near[self.slot] = a
        self.slot = (self.slot + 1) % 4
        self.same[a % 768] = a
        self.tlen += n

    def encode(self, win_ind, seg_pos, target):
        body = bytearray()
        if win_ind & 3:
            body += vint(self.seg_len) + vint(seg_pos)
        rest = vint(self.tlen) + b'\x00' + vint(len(self.data)) + vint(len(self.inst)) + vint(len(self.addr))
        if win_ind & 4:
            rest += zlib.adler32(target).to_bytes(4, 'big')
        rest += self.data + self.inst + self.addr
        return bytes([win_ind]) + bytes(body) + vint(len(rest)) + rest


with tempfile.TemporaryDirectory() as tmp:
    tmp = Path(tmp)
    src = bytes(os.urandom(3000)) + bytes(1000) + b'NINTENDO' * 50

    # ── IPS ──────────────────────────────────────────────────────────────────
    dst = bytearray(src)
    dst[10:14] = b'HACK'
    dst[500:700] = b'\xAA' * 200
    dst += b'EXTENDED'
    ips = make_ips([(10, b'HACK'), (500, (200, 0xAA)), (len(src), b'EXTENDED')])
    check('IPS records, RLE and growth', rt.apply_ips(ips, src) == bytes(dst))
    check('IPS truncation marker', rt.apply_ips(make_ips([(0, b'Z')], truncate=100), src) == b'Z' + src[1:100])

    # ── UPS ──────────────────────────────────────────────────────────────────
    dst = bytearray(src)
    dst[5:9] = b'abcd'
    dst[4000:4003] = b'\x00\x01\x02'
    dst += os.urandom(300)
    ups = make_ups(src, bytes(dst))
    check('UPS applies and passes its target CRC', rt.apply_ups(ups, src) == bytes(dst))
    check('UPS given the patched ROM gives back the original', rt.apply_ups(ups, bytes(dst)) == src)
    try:
        rt.apply_ups(ups, src[:-1] + b'?')
        check('UPS refuses the wrong base ROM', False)
    except rt.ConversionError:
        check('UPS refuses the wrong base ROM', True)

    # ── BPS ──────────────────────────────────────────────────────────────────
    dst = bytearray(src[:1000]) + b'NEW DATA HERE!!!' + src[2000:2500] + bytearray(b'NEW DATA HERE!!!') + src[1000:1200]
    rle_part = b'\x55' * 40
    dst = bytes(dst) + rle_part
    L = len(src[:1000]) + 16
    actions = [('sr', 1000), ('tr', 16), ('sc', 500, 2000), ('tc', 16, 1000), ('sc', 200, 1000),
               ('tr', 1), ('tc', 39, len(dst) - 40)]
    bps = make_bps(src, dst, actions)
    check('BPS source/target read and copy (with backward deltas)', rt.apply_bps(bps, src) == dst)
    damaged = bytearray(bps)
    damaged[20] ^= 1
    try:
        rt.apply_bps(bytes(damaged), src)
        check('damaged BPS refused by its own CRC', False)
    except rt.ConversionError as e:
        check('damaged BPS refused by its own CRC', 'damaged' in str(e))

    # ── xdelta / VCDIFF ──────────────────────────────────────────────────────
    w1 = VcdWindow(len(src))
    w1.add(b'HELLO')
    w1.copy(100, 300, 0)                                # from the base ROM, absolute
    w1.run(0x77, 64)
    w1.copy(len(src) + 5, 20, 1)                        # from the target already written (overlap)
    w1.copy(120, 50, 2)                                 # near cache
    w1.copy(120, 10, 6)                                 # same cache
    t1 = bytearray(b'HELLO') + src[100:400] + b'\x77' * 64
    for k in range(20):
        t1.append(t1[5 + k])
    t1 += src[120:170] + src[120:130]
    w2 = VcdWindow(200)                                 # a window copying from the output so far
    w2.copy(0, 200, 0)
    w2.add(b'!')
    t2 = bytes(t1[50:250]) + b'!'
    patch = (rt.VCDIFF_MAGIC + bytes([0x04]) + vint(len(b'out.bin//base.bin/')) + b'out.bin//base.bin/'
             + w1.encode(0x01 | 0x04, 0, bytes(t1)) + w2.encode(0x02 | 0x04, 50, t2))
    out, checked, windows = rt.apply_xdelta(patch, src)
    check('xdelta ADD / RUN / COPY (self, here, near, same) and a target window',
          out == bytes(t1) + t2 and checked == windows == 2, f'{checked}/{windows} Adler-32 checked')
    try:
        rt.apply_xdelta(patch, bytes(len(src)))
        check('xdelta refuses the wrong base by its Adler-32', False)
    except rt.ConversionError as e:
        check('xdelta refuses the wrong base by its Adler-32', 'Adler-32' in str(e))

    # ── finding the base ROM next to the patch ────────────────────────────────
    folder = tmp / 'hacks'
    folder.mkdir()
    headered = bytes(512) + src
    (folder / 'Some Game (USA).sfc').write_bytes(headered)
    (folder / 'Other Game (USA).sfc').write_bytes(os.urandom(4096))
    (folder / 'readme.txt').write_text('hi')
    (folder / 'Cool Hack.bps').write_bytes(bps)
    base, strip, how = rt.find_patch_base(folder / 'Cool Hack.bps')
    check('BPS base found by CRC32 behind a copier header', base.name == 'Some Game (USA).sfc' and strip, how)
    det = rt.identify(str(folder / 'Cool Hack.bps'))
    out_name = rt.output_path(folder / 'Cool Hack.bps', 'patch:apply', folder, tmp / 'out')
    res = rt.run_conversion({'path': str(folder / 'Cool Hack.bps')}, 'patch:apply', folder, tmp / 'out')
    check('patch applied through the runner, named after the patch with the base extension',
          det['conversions'] == ['patch:apply'] and out_name.name == 'Cool Hack.sfc'
          and res.get('ok') and res.get('verified') is True
          and (tmp / 'out' / 'Cool Hack.sfc').read_bytes() == dst, str(res.get('error') or res.get('detail')))
    (folder / 'Mystery.ips').write_bytes(ips)
    try:
        rt.find_patch_base(folder / 'Mystery.ips')
        check('IPS with several ROMs and no name match is refused', False)
    except rt.ConversionError:
        check('IPS with several ROMs and no name match is refused', True)

    # ── ECM ──────────────────────────────────────────────────────────────────
    def mode1():
        s = bytearray(2352)
        s[0:12] = rt.CD_SYNC
        s[12:16] = b'\x00\x02\x00\x01'
        s[0x10:0x810] = os.urandom(0x800)
        s[0x810:0x814] = rt.cd_edc(s[:0x810]).to_bytes(4, 'little')
        rt._cd_ecc(s, 86, 24, 2, 86, 0x81C)             # the original byte-wise coder
        rt._cd_ecc(s, 52, 43, 86, 88, 0x8C8)
        return bytes(s)

    def mode2(form2):
        s = bytearray(2352)
        s[0:12] = rt.CD_SYNC
        s[12:16] = b'\x00\x02\x10\x02'
        sub = bytes([1, 0, 0x20 if form2 else 0x08, 0])
        s[0x10:0x18] = sub + sub
        if form2:
            s[0x18:0x92C] = os.urandom(0x914)
            s[0x92C:0x930] = rt.cd_edc(s[0x10:0x92C]).to_bytes(4, 'little')
        else:
            s[0x18:0x818] = os.urandom(0x800)
            rt.cd_fix_sector(s, (0, 2, 0x10), True)      # EDC + ECC the slow, proven way
        return bytes(s)

    disc = (b''.join(mode1() for _ in range(40)) + bytes(2352 * 8) + os.urandom(3000)
            + b''.join(mode2(i % 3 == 0) for _ in range(1) for i in range(60)) + os.urandom(2352 * 5))
    (tmp / 'disc.bin').write_bytes(disc)
    rt.bin_to_ecm(tmp / 'disc.bin', tmp / 'disc.bin.ecm')
    ecm = (tmp / 'disc.bin.ecm').read_bytes()
    rt.ecm_to_bin(tmp / 'disc.bin.ecm', tmp / 'back.bin')
    check('ECM encode/decode byte-exact (mode 1, mode 2 form 1/2, zeros, audio)',
          (tmp / 'back.bin').read_bytes() == disc, f'{len(disc):,} -> {len(ecm):,} bytes')
    check('ECM ends with the reference end marker and whole-image EDC',
          ecm[-9:-4] == b'\xfc\xff\xff\xff\x3f' and ecm[-4:] == rt.cd_edc(disc).to_bytes(4, 'little'))
    bad = bytearray(ecm)
    bad[200] ^= 0x10                                    # inside the first mode 1 sector's data
    (tmp / 'bad.ecm').write_bytes(bad)
    try:
        rt.ecm_to_bin(tmp / 'bad.ecm', tmp / 'bad.bin')
        check('damaged ECM refused by its checksum', False)
    except rt.ConversionError as e:
        check('damaged ECM refused by its checksum', True, str(e)[:50])
    s = bytearray(os.urandom(2352))
    ref = bytearray(s)
    rt._cd_ecc(ref, 86, 24, 2, 86, 0x81C)
    rt._cd_ecc(ref, 52, 43, 86, 88, 0x8C8)
    fast = bytearray(s)
    rt.cd_ecc_fast(fast, bytes(s[12:16]))
    check('fast ECC equals the byte-wise ECC', fast[0x81C:0x930] == ref[0x81C:0x930])
    check('fast EDC equals the byte-wise EDC', rt.cd_edc_fast(s[:2047]) == rt.cd_edc(s[:2047]))

    # ── CUE/BIN merge and split ──────────────────────────────────────────────
    CRLF = chr(13) + chr(10)
    cd = tmp / 'cd'
    cd.mkdir()
    tracks = [os.urandom(2352 * n) for n in (30, 170, 90)]
    for i, blob in enumerate(tracks, 1):
        (cd / f'Game (Track {i}).bin').write_bytes(blob)
    sheet = CRLF.join(['CATALOG 0000000000000', 'FILE "Game (Track 1).bin" BINARY', '  TRACK 01 MODE2/2352',
                          '    INDEX 01 00:00:00', 'FILE "Game (Track 2).bin" BINARY', '  TRACK 02 AUDIO',
                          '    FLAGS DCP', '    INDEX 00 00:00:00', '    INDEX 01 00:02:00',
                          'FILE "Game (Track 3).bin" BINARY', '  TRACK 03 AUDIO', '    INDEX 00 00:00:00',
                          '    INDEX 01 00:01:15']) + CRLF
    (cd / 'Game.cue').write_bytes(sheet.encode())
    r1 = rt.run_conversion({'path': str(cd / 'Game.cue')}, 'cue:split->merged', cd, tmp / 'merged')
    merged = (tmp / 'merged' / 'Game.cue').read_text()
    check('merge: one BIN, absolute INDEX times, verified by splitting again',
          r1.get('ok') and r1.get('verified') is True and '00:02:30' in merged and '00:03:65' in merged
          and (tmp / 'merged' / 'Game.bin').read_bytes() == b''.join(tracks), str(r1.get('detail') or r1.get('error')))
    r2 = rt.run_conversion({'path': str(tmp / 'merged' / 'Game.cue')}, 'cue:merged->split', tmp / 'merged', tmp / 'split')
    check('split: Redump-style tracks and the original sheet back byte for byte',
          r2.get('ok') and r2.get('verified') is True and (tmp / 'split' / 'Game.cue').read_bytes() == sheet.encode()
          and all((tmp / 'split' / f'Game (Track {i}).bin').read_bytes() == b for i, b in enumerate(tracks, 1)),
          str(r2.get('detail') or r2.get('error')))
    try:
        rt.cue_merge(cd / 'Game.cue', cd / 'Game.bin.part')
        check('merge refuses to overwrite its own source cue', False)
    except rt.ConversionError:
        check('merge refuses to overwrite its own source cue', True)

    # -- Dreamcast Redump CUE <-> TOSEC GDI ------------------------------------
    dc = tmp / 'dc'
    dc.mkdir()
    F = rt.CD_FRAME

    def data_track(first_lba, count):
        return b''.join(rt._empty_mode1(first_lba + k)[:16] + os.urandom(F - 16) for k in range(count))

    def audio_file(sectors):
        body = bytearray(os.urandom(sectors * F))
        body[:150 * F] = bytes(150 * F)                   # pregap silence
        body[-2 * F:] = bytes(2 * F)                      # run-out silence (longer than the offset)
        return bytes(body)

    red = {
        1: data_track(0, 300),
        2: audio_file(550),                               # SD audio: 150 pregap + 400
        3: data_track(45000, 300),
        4: audio_file(650),                               # HD audio: 150 pregap + 500
        5: bytes(75 * F) + b''.join(rt._empty_mode1(45950 + 75 + k) for k in range(150)) + data_track(46175, 200),
    }
    lines = ['REM SINGLE-DENSITY AREA']
    for n in range(1, 6):
        if n == 3:
            lines.append('REM HIGH-DENSITY AREA')
        (dc / f'Toy (Track {n}).bin').write_bytes(red[n])
        lines += [f'FILE "Toy (Track {n}).bin" BINARY', f'  TRACK {n:02d} {"AUDIO" if n in (2, 4) else "MODE1/2352"}']
        pre = {2: 150, 4: 150, 5: 225}.get(n, 0)
        lines += (['    INDEX 00 00:00:00', f'    INDEX 01 00:{pre // 75:02d}:{pre % 75:02d}'] if pre else ['    INDEX 01 00:00:00'])
    CRLF = chr(13) + chr(10)
    (dc / 'Toy.cue').write_bytes((CRLF.join(lines) + CRLF).encode())
    check('Dreamcast cue offered the GDI conversion', 'dc:cue->gdi' in rt.identify(str(dc / 'Toy.cue'))['conversions'])
    r1 = rt.run_conversion({'path': str(dc / 'Toy.cue')}, 'dc:cue->gdi', dc, tmp / 'gdi')
    gdi_text = (tmp / 'gdi' / 'Toy.gdi').read_text()
    check('Redump cue -> TOSEC GDI: LBAs and track names, verified by converting back',
          r1.get('ok') and r1.get('verified') is True and '4 45450 0 2352 track04.raw 0' in gdi_text
          and '5 46175 4 2352 track05.bin 0' in gdi_text, str(r1.get('detail') or r1.get('error')))
    r2 = rt.run_conversion({'path': str(tmp / 'gdi' / 'Toy.gdi')}, 'dc:gdi->cue', tmp / 'gdi', tmp / 'cue')
    back_ok = all((tmp / 'cue' / f'Toy (Track {n}).bin').read_bytes() == red[n] for n in range(1, 6))
    check('TOSEC GDI -> Redump cue rebuilds every track (audio offset, empty pregap sectors) and the sheet',
          r2.get('ok') and back_ok and (tmp / 'cue' / 'Toy.cue').read_bytes() == (dc / 'Toy.cue').read_bytes(),
          str(r2.get('detail') or r2.get('error')))

print('=' * 70)
print('ALL PASS' if all(results) else f'{results.count(False)} FAILED')
sys.exit(0 if all(results) else 1)

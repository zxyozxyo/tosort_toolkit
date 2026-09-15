"""Batch 7: standalone EBOOT.PBP, DAX and JSO compressed ISOs, LZO1X.

    python tests/test_rom_batch7.py

The LZO1X decoder was proven separately against real lzop output
(cyberdelia/lzo testdata/pg135.txt.lzo: 102 blocks, 3.3 MB, identical); the
tests here build DAX/JSO containers the way maxcso and ARK-4 describe them.
"""
import hashlib
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


def lzo_literals(data):
    """A valid LZO1X stream made only of literal runs, then the end marker."""
    out = bytearray()
    if len(data) <= 238:
        out.append(17 + len(data))
    else:
        t = len(data) - 3 - 15
        out.append(0)
        while t > 255:
            out.append(0)
            t -= 255
        out.append(t)
    return bytes(out + data + b'\x11\x00\x00')


def image(size):
    # compressible and incompressible stretches, so both stored and packed blocks occur
    blob = bytearray()
    while len(blob) < size:
        blob += os.urandom(3000) + bytes(9000) + b'PSP GAME ' * 500
    return bytes(blob[:size])


def make_dax(iso, nc_frames=()):
    total, frames = len(iso), (len(iso) + 0x1FFF) // 0x2000
    body, index, sizes = bytearray(), [], []
    base = 32 + frames * 6 + (8 * len(nc_frames))
    for n in range(frames):
        raw = iso[n * 0x2000:(n + 1) * 0x2000]
        blob = raw if n in nc_frames else zlib.compress(raw, 9)
        index.append(base + len(body))
        sizes.append(len(blob))
        body += blob
    version = 1 if nc_frames else 0
    head = b'DAX\x00' + struct.pack('<III', total, version, len(nc_frames)) + bytes(16)
    nc = b''.join(struct.pack('<II', f, 1) for f in nc_frames)
    return head + struct.pack(f'<{frames}I', *index) + struct.pack(f'<{frames}H', *sizes) + nc + body


def make_jso(iso, block_size=2048, method=1, block_headers=0, wrap=False, md5=True):
    blocks = (len(iso) + block_size - 1) // block_size
    body, index = bytearray(), []
    base = rt.JSO_HEADER + 4 * (blocks + 1)
    for n in range(blocks):
        raw = iso[n * block_size:(n + 1) * block_size].ljust(block_size, b'\0')
        if method == 0:
            blob = lzo_literals(raw) if any(raw) else lzo_literals(raw)
        else:
            c = zlib.compressobj(9, zlib.DEFLATED, 15 if wrap else -15)
            blob = c.compress(raw) + c.flush()
        if len(blob) >= block_size:
            blob = raw
        index.append(base + len(body))
        body += (bytes(4) if block_headers else b'') + blob
    index.append(base + len(body))
    head = bytearray(rt.JSO_HEADER)
    head[:4] = b'JISO'
    head[4], head[5] = 3, 1
    struct.pack_into('<H', head, 6, block_size)
    head[8], head[10] = block_headers, method
    struct.pack_into('<I', head, 12, len(iso))
    if md5:
        head[16:32] = hashlib.md5(iso).digest()
    struct.pack_into('<I', head, 32, rt.JSO_HEADER)
    return bytes(head) + struct.pack(f'<{blocks + 1}I', *index) + body


with tempfile.TemporaryDirectory() as tmp:
    tmp = Path(tmp)
    iso = image(2048 * 300)

    # ── LZO1X token decoding ─────────────────────────────────────────────────
    for n in (5, 238, 239, 700):
        data = os.urandom(n)
        check(f'LZO1X literal run of {n}', rt.lzo1x_decompress(lzo_literals(data)) == data)
    # a match token: 4 literals "abcd" then M2-ish copy via M3 (dist 4, len 8)
    stream = bytes([17 + 4]) + b'abcd' + bytes([32 | 6, (3 << 2) & 0xFF, 0]) + b'\x11\x00\x00'
    check('LZO1X overlapping back-reference', rt.lzo1x_decompress(stream) == b'abcd' + b'abcdabcd')
    try:
        rt.lzo1x_decompress(bytes([17 + 4]) + b'ab')
        check('truncated LZO block refused', False)
    except rt.ConversionError:
        check('truncated LZO block refused', True)

    # ── DAX ──────────────────────────────────────────────────────────────────
    for label, nc in (('DAX v0 (all frames deflated)', ()), ('DAX v1 with stored NC frames', (0, 5, 74))):
        (tmp / 'g.dax').write_bytes(make_dax(iso, nc))
        det = rt.identify(str(tmp / 'g.dax'))
        rt.CONVERSIONS['psp:dax->iso']['fn'](tmp / 'g.dax', tmp / 'g.iso')
        check(label, (tmp / 'g.iso').read_bytes() == iso and 'psp:dax->iso' in det['conversions'])
    bad = bytearray(make_dax(iso))
    bad[-50:] = bytes(50)
    (tmp / 'bad.dax').write_bytes(bad)
    try:
        rt.dax_to_iso(tmp / 'bad.dax', tmp / 'bad.iso')
        check('damaged DAX frame refused', False)
    except rt.ConversionError as e:
        check('damaged DAX frame refused', True, str(e)[:60])

    # ── JSO ──────────────────────────────────────────────────────────────────
    cases = [('JSO deflate, 2K blocks', dict()),
             ('JSO deflate with 4-byte block headers', dict(block_headers=1)),
             ('JSO zlib-wrapped blocks', dict(wrap=True)),
             ('JSO LZO, 8K blocks', dict(method=0, block_size=8192))]
    for label, kw in cases:
        (tmp / 'g.jso').write_bytes(make_jso(iso, **kw))
        det = rt.identify(str(tmp / 'g.jso'))
        rt.jso_to_iso(tmp / 'g.jso', tmp / 'g.iso')
        ok, why = rt._jso_md5_verifier(tmp / 'g.jso', tmp / 'g.iso')
        check(label, (tmp / 'g.iso').read_bytes() == iso and ok is True
              and 'psp:jso->iso' in det['conversions'], why)
    (tmp / 'n.jso').write_bytes(make_jso(iso, md5=False))
    rt.jso_to_iso(tmp / 'n.jso', tmp / 'n.iso')
    ok, why = rt._jso_md5_verifier(tmp / 'n.jso', tmp / 'n.iso')
    check('JSO without an MD5 is unverified, not failed', ok is None, why)

    # ── EBOOT.PBP routing ─────────────────────────────────────────────────────
    for magic, system, conv in ((b'NPUMDIMG', 'PSP', 'psp:pbp->iso'),
                                (b'PSISOIMG', 'PSX', 'psx:pbp->bin'),
                                (b'PSTITLEIMG00', 'PSX', 'psx:pbp->bin')):
        pbp = bytearray(0x1000)
        pbp[:4] = b'\x00PBP'
        struct.pack_into('<I', pbp, 0x24, 0x800)
        pbp[0x800:0x800 + len(magic)] = magic
        (tmp / 'EBOOT.PBP').write_bytes(pbp)
        det = rt.identify(str(tmp / 'EBOOT.PBP'))
        check(f'EBOOT.PBP with {magic[:8].decode()} -> {system} {conv}',
              det['system'] == system and det['conversions'] == [conv], det['detail'])
    try:
        rt.pbp_to_iso(tmp / 'EBOOT.PBP', tmp / 'x.iso')          # a PS one Classic
        check('PS one Classic PBP refused by the PSP ISO conversion', False)
    except rt.ConversionError as e:
        check('PS one Classic PBP refused by the PSP ISO conversion', 'PS one Classics' in str(e))

    # ── header library (A78 / LNX / 2IMG) ─────────────────────────────────────
    import zipfile
    lib = tmp / 'header_library'
    rt.HEADER_LIBRARY_DIR = lib                  # never touch the real keys folder
    for kind, (fname, *_rest) in rt.HEADER_KINDS.items():
        rt.KEY_FILES[f'{kind}_headers'] = (lib / fname, rt.KEY_FILES[f'{kind}_headers'][1])
    rt._HEADER_CACHE.clear()
    head = bytearray(128)
    head[0] = 3
    head[1:10] = b'ATARI7800'
    head[17:17 + 12] = b'Test Cart 78'
    body = os.urandom(48 * 1024)
    (tmp / 'game.a78').write_bytes(bytes(head) + body)
    try:
        rt.CONVERSIONS['a78:headerless->headered']['fn'](tmp / 'game.a78', tmp / 'x.a78')
        check('unknown ROM refused, not given a guessed header', False)
    except rt.ConversionError as e:
        check('unknown ROM refused, not given a guessed header', 'no known' in str(e), str(e)[:70])
    rt.CONVERSIONS['a78:headered->headerless']['fn'](tmp / 'game.a78', tmp / 'game.bin')
    check('stripping an A78 header teaches the library', len(rt.header_library('a78')) == 1)
    rt.CONVERSIONS['a78:headerless->headered']['fn'](tmp / 'game.bin', tmp / 'back.a78')
    check('A78 header added back byte-exact', (tmp / 'back.a78').read_bytes() == bytes(head) + body)

    lnx_head = bytearray(64)
    lnx_head[0:4] = b'LYNX'
    struct.pack_into('<HHH', lnx_head, 4, 0x100, 0, 1)
    lnx_head[10:18] = b'Lynx Hit'
    lnx_body = os.urandom(64 * 1024)
    twomg = bytearray(64)
    twomg[0:4] = b'2IMG'
    twomg[4:8] = b'WOOF'
    disk = os.urandom(143360)
    struct.pack_into('<II', twomg, 0x18, 64, len(disk))
    sets = tmp / 'headered set'
    sets.mkdir()
    with zipfile.ZipFile(sets / 'Lynx Hit (World).zip', 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('Lynx Hit (World).lnx', bytes(lnx_head) + lnx_body)
    (sets / 'disk.2mg').write_bytes(bytes(twomg) + disk)
    (sets / 'notes.txt').write_text('not a ROM')
    counts = rt.learn_headers_from_folder(sets)
    check('learning a folder finds headers in zips and loose files',
          counts['lnx']['new'] == 1 and counts['2mg']['new'] == 1, str(counts))
    again = rt.learn_headers_from_folder(sets)
    check('learning the same folder twice adds nothing', again['lnx']['new'] == 0)
    (tmp / 'lynx.bin').write_bytes(lnx_body)
    rt.CONVERSIONS['lnx:headerless->headered']['fn'](tmp / 'lynx.bin', tmp / 'lynx.lnx')
    (tmp / 'disk.po').write_bytes(disk)
    rt.CONVERSIONS['apple:raw->2mg']['fn'](tmp / 'disk.po', tmp / 'disk.2mg')
    check('LNX and 2IMG headers added back from what was learned',
          (tmp / 'lynx.lnx').read_bytes() == bytes(lnx_head) + lnx_body and
          (tmp / 'disk.2mg').read_bytes() == bytes(twomg) + disk)
    st = {c['id']: c['status'] for s in rt.system_catalog() for c in s['conversions']}
    check('info view: add-header conversions ready once headers are known',
          st['lnx:headerless->headered'] == 'ready' and st['apple:raw->2mg'] == 'ready')

print('=' * 70)
print('ALL PASS' if all(results) else f'{results.count(False)} FAILED')
sys.exit(0 if all(results) else 1)

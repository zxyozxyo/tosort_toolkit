"""Batch 6: Apple II nibble images, CSO/ZSO index alignment, Vita licences.

    python tests/test_rom_batch6.py
"""
import inspect
import os
import struct
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import rom_tools as rt

results = []


def check(name, ok, detail=''):
    results.append(ok)
    print(f'{"PASS" if ok else "FAIL"}  {name}  {detail}')


def woz2_from_nib(nib):
    """A WOZ2 whose bit streams carry the NIB's nibbles (FF as 10-bit sync),
    each track rotated so a sector straddles the index."""
    tracks = []
    for t in range(35):
        bits = []
        for b in nib[t * 6656:(t + 1) * 6656]:
            bits += [int(x) for x in format(b, '08b')] + ([0, 0] if b == 0xFF else [])
        bits = bits[5000:] + bits[:5000]
        n = len(bits)
        tracks.append((int(''.join(map(str, bits + [0] * (-n % 8))), 2).to_bytes((n + 7) // 8, 'big'), n))
    info = bytearray(60)
    info[0], info[1] = 2, 1
    tmap = bytearray(b'\xff' * 160)
    for t in range(35):
        tmap[t * 4] = t
    head_len = 12 + 8 + 60 + 8 + 160 + 8 + 1280
    cur = (head_len + 511) // 512
    trks, data = bytearray(1280), bytearray()
    for t, (by, n) in enumerate(tracks):
        nb = (len(by) + 511) // 512
        struct.pack_into('<HHI', trks, t * 8, cur, nb, n)
        data += by + bytes(nb * 512 - len(by))
        cur += nb
    woz = bytearray(b'WOZ2\xff\n\r\n' + bytes(4))
    woz += b'INFO' + struct.pack('<I', 60) + info + b'TMAP' + struct.pack('<I', 160) + tmap
    woz += b'TRKS' + struct.pack('<I', 1280) + trks
    return bytes(woz + bytes(((head_len + 511) // 512) * 512 - len(woz)) + data)


with tempfile.TemporaryDirectory() as tmp:
    tmp = Path(tmp)

    # ── Apple II ───────────────────────────────────────────────────────────
    disk = os.urandom(rt.APPLE_DISK)
    (tmp / 'a.dsk').write_bytes(disk)
    rt.apple_dsk_to_nib(tmp / 'a.dsk', tmp / 'a.nib')
    check('DSK -> NIB is 35 x 6656', (tmp / 'a.nib').stat().st_size == 232960)
    rt.apple_nib_to_dsk(tmp / 'a.nib', tmp / 'b.dsk')
    check('NIB -> DSK gives the sectors back', (tmp / 'b.dsk').read_bytes() == disk)
    (tmp / 'a.woz').write_bytes(woz2_from_nib((tmp / 'a.nib').read_bytes()))
    rt.apple_woz_to_dsk(tmp / 'a.woz', tmp / 'c.dsk')
    check('WOZ2 bit streams -> DSK', (tmp / 'c.dsk').read_bytes() == disk)
    check('WOZ detected with a conversion',
          'apple:woz->dsk' in rt.identify(str(tmp / 'a.woz'))['conversions'])
    # the physical -> DOS 3.3 interleave (dsk2woz: sector * 7 % 15)
    check('DOS 3.3 interleave table', list(rt.A2_DOS_ORDER) ==
          [(s * 7) % 15 if s < 15 else 15 for s in range(16)])
    nib = bytearray((tmp / 'a.nib').read_bytes())
    nib[17 * 6656:18 * 6656] = b'\xff' * 6656
    (tmp / 'p.nib').write_bytes(nib)
    try:
        rt.apple_nib_to_dsk(tmp / 'p.nib', tmp / 'p.dsk')
        check('unreadable track refused', False, 'converted anyway')
    except rt.ConversionError as e:
        check('unreadable track refused', 'copy-protected' in str(e), str(e))

    # ── CSO / ZSO index alignment (images past 2 GiB) ───────────────────────
    src = inspect.getsource(rt._pack_blocks).replace('>= 0x80000000:', '>= 0x1000:')
    ns = dict(vars(rt))
    exec(src, ns)
    data = os.urandom(50000) + bytes(300000) + os.urandom(7777)
    (tmp / 'z.iso').write_bytes(data)
    ns['_pack_blocks'](tmp / 'z.iso', tmp / 'z.cso', rt.CSO_MAGIC, 2048,
                       lambda raw: rt.zlib.compress(raw, 9)[2:-4])
    align = (tmp / 'z.cso').read_bytes()[21]
    rt.cso_to_iso(tmp / 'z.cso', tmp / 'z2.iso')
    check('CSO with an index shift round-trips', align > 0 and (tmp / 'z2.iso').read_bytes() == data,
          f'align {align}')
    rt.iso_to_cso(tmp / 'z.iso', tmp / 'small.cso')
    check('small CSO keeps align 0', (tmp / 'small.cso').read_bytes()[21] == 0)

    # ── Vita ─────────────────────────────────────────────────────────────────
    key, iv = os.urandom(16), os.urandom(16)
    for n in (32, 37, 5):
        blob = os.urandom(n)
        enc = rt._pfs_cbc(key, iv, blob, decrypt=False)
        check(f'PFS CBC with a {n % 16}-byte tail is reversible',
              rt._pfs_cbc(key, iv, enc, decrypt=True) == blob and len(enc) == n)
    tsv = rt.KEYS_DIR / 'nps' / 'PSV_GAMES.tsv'
    if tsv.exists():
        rif, where = rt.vita_licence(tmp / 'none.pkg', 'EP0082-PCSB00395_00-FFX0PKG0PSV0SCEE')
        import hashlib
        check('zRIF decodes to No-Intro\'s work.bin', rif is not None and
              hashlib.sha1(rif).hexdigest() == 'f0645601f5072244d9e1b062f8321e4d33d96bda', where)
    else:
        print('SKIP  zRIF check (no keys/nps/PSV_GAMES.tsv)')

print('=' * 70)
print('ALL PASS' if all(results) else f'{results.count(False)} FAILED')
sys.exit(0 if all(results) else 1)

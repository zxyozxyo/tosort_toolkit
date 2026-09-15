"""Build a synthetic ROM corpus with structurally-correct headers, then assert
that rom_tools.identify() classifies every one of them the way it should.

These are headers only - no copyrighted content. Padding is deterministic
pseudo-random so 'encrypted' cases do not accidentally look like anything.
"""
import os, struct, sys, hashlib
from pathlib import Path

sys.path.insert(0, r'B:\User\ClaudeCode\tosort_toolkit')
import rom_tools

OUT = Path(sys.argv[1] if len(sys.argv) > 1 else 'corpus')


def noise(n, seed):
    """Deterministic filler that is not all-zero and has no magic in it."""
    out = bytearray()
    h = hashlib.sha256(str(seed).encode()).digest()
    while len(out) < n:
        h = hashlib.sha256(h).digest()
        out += h
    return bytes(out[:n])


def blob(size, seed=1):
    return bytearray(noise(size, seed))


def write(rel, data):
    p = OUT / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(bytes(data))
    return p


# ── N64 ───────────────────────────────────────────────────────────────────────
z64 = blob(0x1000, 'n64')
z64[0:4] = b'\x80\x37\x12\x40'
z64[0x20:0x34] = b'SUPER MARIO 64      '
write('N64/mario.z64', z64)

v64 = bytearray(rom_tools._swap16(bytes(z64)))
write('N64/mario.v64', v64)

n64le = bytearray(rom_tools._swap32(bytes(z64)))
write('N64/mario.n64', n64le)

# ── NES ───────────────────────────────────────────────────────────────────────
prg, chr_ = 2, 1
ines = bytearray(b'NES\x1a' + bytes([prg, chr_, 0x12, 0x00]) + b'\x00' * 8)
ines += noise(prg * 16384 + chr_ * 8192, 'nes')
write('NES/game.nes', ines)
write('NES/headerless.nes', ines[16:])          # same data, header stripped

# ── NDS ───────────────────────────────────────────────────────────────────────
def nds(secure, seed):
    d = blob(0x8000, seed)
    d[0x00:0x0C] = b'ZELDA PH\x00\x00\x00\x00'
    d[0x0C:0x10] = b'AZEP'
    d[0x12] = 0x00
    struct.pack_into('<I', d, 0x20, 0x4000)     # arm9_rom_offset
    struct.pack_into('<H', d, 0x15C, 0xCF56)    # licensed logo CRC
    d[0x4000:0x4008] = secure
    return d

write('NDS/zelda_dec.nds', nds(b'encryObj', 'ndsdec'))
write('NDS/zelda_enc.nds', nds(noise(8, 'sec'), 'ndsenc'))

# ── 3DS ───────────────────────────────────────────────────────────────────────
def cci(nocrypto, seed):
    d = blob(0x9000, seed)
    d[0x100:0x104] = b'NCSD'
    struct.pack_into('<Q', d, 0x108, 0x0004000000123400)   # media id
    struct.pack_into('<I', d, 0x120, 0x40)                 # part0 @ 0x8000
    struct.pack_into('<I', d, 0x124, 0x8)
    base = 0x8000
    d[base + 0x100:base + 0x104] = b'NCCH'
    d[base + 0x150:base + 0x160] = b'CTR-P-AZEP\x00\x00\x00\x00\x00\x00'
    flags = bytearray(8)
    flags[3] = 0x00 if nocrypto else 0x0A
    flags[7] = 0x04 if nocrypto else 0x00
    d[base + 0x188:base + 0x190] = flags
    return d

write('3DS/game_dec.3ds', cci(True, '3dsdec'))
write('3DS/game_enc.3ds', cci(False, '3dsenc'))

cxi = blob(0x1000, 'cxi')
cxi[0x100:0x104] = b'NCCH'
cxi[0x150:0x160] = b'CTR-P-TEST\x00\x00\x00\x00\x00\x00'
f = bytearray(8); f[7] = 0x00; f[3] = 0x0B
cxi[0x188:0x190] = f
write('3DS/app.cxi', cxi)

cia = blob(0x1000, 'cia')
struct.pack_into('<I', cia, 0x00, 0x2020)
struct.pack_into('<H', cia, 0x04, 0)
struct.pack_into('<H', cia, 0x06, 0)
write('3DS/title.cia', cia)

# ── GameCube / Wii ────────────────────────────────────────────────────────────
gc = blob(0x1000, 'gc')
gc[0x00:0x06] = b'GALE01'
struct.pack_into('>I', gc, 0x1C, 0xC2339F3D)
write('GC/melee.iso', gc)

gcn = bytearray(gc); gcn[0x200:0x204] = b'NKIT'
write('GC/melee.nkit.iso', gcn)

wii = blob(0x1000, 'wii')
wii[0x00:0x06] = b'RMGE01'
struct.pack_into('>I', wii, 0x18, 0x5D1C9EA3)
write('WII/galaxy.iso', wii)

wbfs = blob(0x1000, 'wbfs')
wbfs[0:4] = b'WBFS'
write('WII/galaxy.wbfs', wbfs)

# RVZ: WIA header is big-endian; disc header copy (dhead) sits at 0x58.
rvz = blob(0x1000, 'rvz')
rvz[0:4] = b'RVZ\x01'
struct.pack_into('>I', rvz, 0x4C, 5)        # compression = zstd
struct.pack_into('>I', rvz, 0x50, 19)       # level
struct.pack_into('>I', rvz, 0x54, 131072)   # 128 KiB chunks
dhead = bytearray(0x80)
struct.pack_into('>I', dhead, 0x18, 0x5D1C9EA3)
dhead[0:6] = b'RMGE01'
rvz[0x58:0x58 + 0x80] = dhead
write('WII/galaxy.rvz', rvz)

ciso = blob(0x1000, 'ciso')
ciso[0:4] = b'CISO'
struct.pack_into('<I', ciso, 4, 0x8000)     # GC block size
write('GC/melee.ciso', ciso)

# ── PSP ───────────────────────────────────────────────────────────────────────
cso = blob(0x1000, 'cso')
cso[0:4] = b'CISO'
struct.pack_into('<I', cso, 4, 0x18)        # PSP header size
struct.pack_into('<Q', cso, 8, 1800 * 1024 * 1024)
struct.pack_into('<I', cso, 16, 2048)
write('PSP/game.cso', cso)

pbp = blob(0x40000, 'pbp')
pbp[0:4] = b'\x00PBP'
struct.pack_into('<I', pbp, 0x24, 0x20000)  # PSAR offset
pbp[0x20000:0x20008] = b'PSISOIMG'
write('PSP/EBOOT.PBP', pbp)

pkg = blob(0x1000, 'pkg')
pkg[0:4] = b'\x7fPKG'
struct.pack_into('<H', pkg, 4, 0x8000)
struct.pack_into('>H', pkg, 6, 2)             # platform: PSP
pkg[0xE7] = 1                                 # key type 1: PSP (2-4 would be Vita)
write('PSP/title.pkg', pkg)

iso = blob(0x9000, 'pspiso')
iso[0x8001:0x8006] = b'CD001'
iso[0x8028:0x8048] = b'PSP GAME                        '
write('PSP/umd.iso', iso)

# ── archive ───────────────────────────────────────────────────────────────────
write('ARCHIVE/pack.zip', b'PK\x03\x04' + noise(200, 'zip'))
write('ARCHIVE/junk.bin', noise(5000, 'junk'))

print(f'corpus written to {OUT}')

# ══════════════════════════════════════════════════════════════════════════════
#  assertions: (relative path, system, format, variant)
# ══════════════════════════════════════════════════════════════════════════════
EXPECT = [
    ('N64/mario.z64',       'N64',  'Z64',       'big-endian'),
    ('N64/mario.v64',       'N64',  'V64',       'byteswapped'),
    ('N64/mario.n64',       'N64',  'N64',       'little-endian'),
    ('NES/game.nes',        'NES',  'iNES',      'headered'),
    ('NES/headerless.nes',  'NES',  'raw',       'headerless'),
    ('NDS/zelda_dec.nds',   'NDS',  'NDS ROM',   'decrypted'),
    ('NDS/zelda_enc.nds',   'NDS',  'NDS ROM',   'encrypted'),
    ('3DS/game_dec.3ds',    '3DS',  'CCI',       'decrypted'),
    ('3DS/game_enc.3ds',    '3DS',  'CCI',       'encrypted'),
    ('3DS/app.cxi',         '3DS',  'NCCH',      'encrypted'),
    ('3DS/title.cia',       '3DS',  'CIA',       None),
    ('GC/melee.iso',        'GC',   'ISO',       'plain'),
    ('GC/melee.nkit.iso',   'GC',   'NKit ISO',  'nkit'),
    ('WII/galaxy.iso',      'WII',  'ISO',       'plain'),
    ('WII/galaxy.wbfs',     'WII',  'WBFS',      'plain'),
    ('WII/galaxy.rvz',      'WII',  'RVZ',       'zstd-19-128k'),
    ('GC/melee.ciso',       'GC',   'CISO',      'nkit'),
    ('PSP/game.cso',        'PSP',  'CSO',       'compressed'),
    ('PSP/EBOOT.PBP',       'PSX',  'EBOOT.PBP', 'encrypted'),   # PSISOIMG = PS one Classic
    ('PSP/title.pkg',       'PSP',  'PKG',       'encrypted'),
    ('PSP/umd.iso',         'PSP',  'ISO',       'plain'),
    ('ARCHIVE/pack.zip',    'ARCHIVE', 'ZIP',    None),
    ('ARCHIVE/junk.bin',    'UNKNOWN', 'unknown', None),
]

print('\n' + '=' * 78)
fails = 0
for rel, esys, efmt, evar in EXPECT:
    r = rom_tools.identify(OUT / rel)
    ok = (r['system'], r['format'], r['variant']) == (esys, efmt, evar)
    fails += not ok
    mark = 'PASS' if ok else 'FAIL'
    print(f'{mark}  {rel:24s} -> {r["system"]:8s} {r["format"]:10s} '
          f'{str(r["variant"]):12s} [{r["confidence"]}]')
    if not ok:
        print(f'      expected {esys}/{efmt}/{evar}')
    if r.get('detail'):
        print(f'      {r["detail"]}')
    if r.get('conversions'):
        print(f'      offers: {", ".join(r["conversions"])}')

print('=' * 78)
print(f'{len(EXPECT) - fails}/{len(EXPECT)} passed')
if rom_tools.detector_failures():
    print('DETECTOR EXCEPTIONS:')
    for f in rom_tools.detector_failures():
        print('  ', f)
sys.exit(1 if fails else 0)

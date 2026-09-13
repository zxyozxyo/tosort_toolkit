"""Detection and round-trip proofs for the SNES / Mega Drive / Atari / FDS /
NDS-trim / ZSO conversions."""
import os, sys, struct, hashlib, shutil
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import rom_tools as rt

TMP = Path(os.environ.get('TEMP', '.')) / 'romtier1'
shutil.rmtree(TMP, ignore_errors=True)
TMP.mkdir(parents=True)
fails = 0


def sha(p):
    return hashlib.sha1(Path(p).read_bytes()).hexdigest()


def check(label, ok, extra=''):
    global fails
    fails += not ok
    print(f'{"PASS" if ok else "FAIL"}  {label}{("  " + extra) if extra else ""}')


def noise(n, seed):
    out = bytearray()
    h = hashlib.sha256(str(seed).encode()).digest()
    while len(out) < n:
        h = hashlib.sha256(h).digest()
        out += h
    return bytes(out[:n])


def roundtrip(label, src, fwd, back):
    """Convert forward then back and require the original bytes."""
    a, b = TMP / f'{label}.fwd', TMP / f'{label}.back'
    extra = rt.CONVERSIONS[fwd]['fn'](src, a)
    spec = rt.CONVERSIONS[fwd]
    if spec.get('rebuild') and extra is not None:
        spec['rebuild'](a, b, extra)
    else:
        rt.CONVERSIONS[back]['fn'](a, b)
    check(label, sha(b) == sha(src),
          f'{os.path.getsize(src):,} -> {os.path.getsize(a):,} bytes')


# ── SNES ──────────────────────────────────────────────────────────────────────
def snes_body(size=0x8000):
    d = bytearray(noise(size, 'snes'))
    base = 0x7FC0
    d[base:base + 21] = b'SUPER TEST GAME      '
    d[base + 21] = 0x20                       # LoROM
    checksum = 0x1234
    struct.pack_into('<H', d, base + 28, checksum ^ 0xFFFF)
    struct.pack_into('<H', d, base + 30, checksum)
    return bytes(d)


plain = TMP / 'game.sfc'
plain.write_bytes(snes_body())
headered = TMP / 'game.smc'
headered.write_bytes(bytes(512) + snes_body())

r = rt.identify(plain)
check('SNES headerless detected', (r['system'], r['variant']) == ('SNES', 'headerless'), r['detail'])
r = rt.identify(headered)
check('SNES headered detected', (r['system'], r['variant']) == ('SNES', 'headered'), r['detail'])
roundtrip('snes', headered, 'snes:headered->headerless', 'snes:headerless->headered')
stripped = TMP / 'snes.fwd'
check('SNES strip is exactly 512 bytes smaller',
      os.path.getsize(headered) - os.path.getsize(stripped) == 512)

# ── Mega Drive ────────────────────────────────────────────────────────────────
md = bytearray(noise(0x8000, 'md'))
md[0x100:0x104] = b'SEGA'
md[0x120:0x130] = b'SONIC THE HEDGE '
mdbin = TMP / 'sonic.bin'
mdbin.write_bytes(bytes(md))
r = rt.identify(mdbin)
check('Mega Drive BIN detected', (r['system'], r['format']) == ('MD', 'BIN'), r['detail'])
roundtrip('md', mdbin, 'md:bin->smd', 'md:smd->bin')
smd = TMP / 'md.fwd'
r = rt.identify(smd)
check('Mega Drive SMD detected', (r['system'], r['format']) == ('MD', 'SMD'), r['detail'])

# ── Atari 7800 / Lynx ─────────────────────────────────────────────────────────
a78 = bytearray(noise(0x8000 + 128, 'a78'))
a78[0] = 1
a78[1:10] = b'ATARI7800'
p78 = TMP / 'game.a78'; p78.write_bytes(bytes(a78))
r = rt.identify(p78)
check('Atari 7800 detected', (r['system'], r['variant']) == ('A7800', 'headered'), r['detail'])
roundtrip('a78', p78, 'a78:headered->headerless', 'a78:headerless->headered')

lnx = bytearray(noise(0x8000 + 64, 'lnx'))
lnx[0:4] = b'LYNX'
lnx[10:22] = b'CHIPS CHALL\x00'
plnx = TMP / 'game.lnx'; plnx.write_bytes(bytes(lnx))
r = rt.identify(plnx)
check('Atari Lynx detected', (r['system'], r['variant']) == ('LYNX', 'headered'), r['detail'])
roundtrip('lnx', plnx, 'lnx:headered->headerless', 'lnx:headerless->headered')

# refusing a bogus header is the whole point of the add-side guard
try:
    rt.add_a78_header(TMP / 'a78.fwd', TMP / 'bad.a78', b'\x00' * 128)
    check('Atari 7800 refuses a header without its signature', False)
except rt.ConversionError:
    check('Atari 7800 refuses a header without its signature', True)

# ── FDS ───────────────────────────────────────────────────────────────────────
fds_raw = noise(65500 * 2, 'fds')
fds = TMP / 'disk.fds'
fds.write_bytes(b'FDS\x1a' + bytes([2]) + bytes(11) + fds_raw)
r = rt.identify(fds)
check('FDS headered detected', (r['system'], r['format']) == ('NES', 'FDS'), r['detail'])
roundtrip('fds', fds, 'nes:fds-headered->headerless', 'nes:fds-headerless->headered')

bad_fds = TMP / 'odd.fds'
bad_fds.write_bytes(fds_raw[:-100])
try:
    rt.add_fds_header(bad_fds, TMP / 'x.fds')
    check('FDS refuses a non-whole-side image', False)
except rt.ConversionError:
    check('FDS refuses a non-whole-side image', True)

# ── NDS trim ──────────────────────────────────────────────────────────────────
used, cap = 0x5000, 0x20000
nds = bytearray(noise(used, 'nds'))
nds[0x0C:0x10] = b'ATRP'
struct.pack_into('<H', nds, 0x15C, 0xCF56)
struct.pack_into('<I', nds, 0x20, 0x4000)
nds[0x14] = 0                                  # capacity: 128 KiB << 0
struct.pack_into('<I', nds, 0x80, used)
nds[0x4000:0x4008] = b'encryObj'
untrimmed = TMP / 'game.nds'
untrimmed.write_bytes(bytes(nds) + b'\xFF' * (cap - used))
roundtrip('ndstrim', untrimmed, 'nds:untrimmed->trimmed', None)

# real data past the end must block the trim, not be silently discarded
dirty = TMP / 'dirty.nds'
dirty.write_bytes(bytes(nds) + b'\xFF' * 1000 + b'REALDATA' + b'\xFF' * 100)
try:
    rt.nds_trim(dirty, TMP / 'x.nds')
    check('NDS refuses to trim non-uniform padding', False)
except rt.ConversionError as e:
    check('NDS refuses to trim non-uniform padding', 'destroy real data' in str(e))

# ── ZSO ───────────────────────────────────────────────────────────────────────
iso = TMP / 'game.iso'
iso.write_bytes(bytes(200000) + noise(300000, 'z') + b'AB' * 50000 + noise(4321, 'w'))
roundtrip('zso', iso, 'iso:iso->zso', 'iso:zso->iso')
z = TMP / 'zso.fwd'
r = rt.identify(z)
check('ZSO detected', r['format'] == 'ZSO', r['detail'])

# ── Wii U ─────────────────────────────────────────────────────────────────────
wux = TMP / 'game.wux'
wux.write_bytes(b'WUX0' + bytes(4) + struct.pack('<I', 32768) + noise(1000, 'wux'))
r = rt.identify(wux)
check('Wii U WUX detected', (r['system'], r['format']) == ('WIIU', 'WUX'), r['detail'])

print('=' * 70)
print('ALL PASS' if not fails else f'{fails} FAILURES')
sys.exit(1 if fails else 0)

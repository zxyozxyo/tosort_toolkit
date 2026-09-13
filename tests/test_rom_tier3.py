"""Detection and round-trip proofs for the Tier-3 systems:
PC Engine, Apple II, Amiga and Xbox."""
import os, sys, struct, hashlib, shutil
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import rom_tools as rt

TMP = Path(os.environ.get('TEMP', '.')) / 'romtier3'
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
    a, b = TMP / f'{label}.fwd', TMP / f'{label}.back'
    spec = rt.CONVERSIONS[fwd]
    extra = spec['fn'](src, a)
    if spec.get('rebuild') and extra is not None:
        spec['rebuild'](a, b, extra)
    else:
        rt.CONVERSIONS[back]['fn'](a, b)
    check(label, sha(b) == sha(src),
          f'{os.path.getsize(src):,} -> {os.path.getsize(a):,} bytes')
    return a


# ── PC Engine ─────────────────────────────────────────────────────────────────
body = noise(rt.PCE_BANK * 32, 'pce')
plain = TMP / 'game.pce'; plain.write_bytes(body)
headered = TMP / 'game_h.pce'; headered.write_bytes(bytes(512) + body)

r = rt.identify(plain)
check('PC Engine headerless detected',
      (r['system'], r['variant']) == ('PCE', 'headerless'), r['detail'])
r = rt.identify(headered)
check('PC Engine headered detected',
      (r['system'], r['variant']) == ('PCE', 'headered'), r['detail'])
roundtrip('pce', headered, 'pce:headered->headerless', 'pce:headerless->headered')

# ── Apple II sector order ─────────────────────────────────────────────────────
disk = TMP / 'disk.do'
disk.write_bytes(noise(rt.APPLE_DISK, 'apple'))
r = rt.identify(disk)
check('Apple II DOS-order detected',
      (r['system'], r['variant']) == ('APPLE2', 'dos-order'), r['detail'])

po = roundtrip('apple', disk, 'apple:do->po', 'apple:po->do')
check('Apple II reorder actually changes the data', sha(po) != sha(disk))
check('Apple II reorder preserves size',
      os.path.getsize(po) == os.path.getsize(disk))

# the map must be an involution, or the two directions would disagree
inv_ok = all(rt.DO_PO_MAP[rt.DO_PO_MAP[i]] == i for i in range(16))
check('Apple II sector map is self-inverse', inv_ok)

# a non-track-multiple image must be refused, not silently mangled
odd = TMP / 'odd.do'; odd.write_bytes(noise(rt.APPLE_DISK + 7, 'odd'))
try:
    rt._reorder_apple(odd, TMP / 'x.po')
    check('Apple II refuses a partial-track image', False)
except rt.ConversionError:
    check('Apple II refuses a partial-track image', True)

# ── Apple II 2IMG header ──────────────────────────────────────────────────────
hdr = bytearray(rt.TWOMG_HEADER)
hdr[0:4] = b'2IMG'
hdr[4:8] = b'XGS!'
struct.pack_into('<I', hdr, 0x0C, 1)          # ProDOS order
two = TMP / 'disk.2mg'
two.write_bytes(bytes(hdr) + noise(rt.APPLE_DISK, 'twomg'))
r = rt.identify(two)
check('Apple II 2IMG detected', (r['system'], r['format']) == ('APPLE2', '2MG'),
      r['detail'])
roundtrip('twomg', two, 'apple:2mg->raw', 'apple:raw->2mg')

try:
    rt.add_2mg_header(TMP / 'twomg.fwd', TMP / 'bad.2mg', bytes(64))
    check('2IMG refuses a header without its magic', False)
except rt.ConversionError:
    check('2IMG refuses a header without its magic', True)

# ── Amiga ─────────────────────────────────────────────────────────────────────
dms = TMP / 'disk.dms'
dms.write_bytes(b'DMS!' + noise(2000, 'dms'))
r = rt.identify(dms)
check('Amiga DMS detected', (r['system'], r['format']) == ('AMIGA', 'DMS'),
      r['detail'])

adf = TMP / 'disk.adf'
adf.write_bytes(b'DOS' + bytes(1) + noise(901120 - 4, 'adf'))
r = rt.identify(adf)
check('Amiga ADF detected', (r['system'], r['format']) == ('AMIGA', 'ADF'),
      r['detail'])

# xdms-rs must reject a bogus archive rather than emit a broken ADF
ok, why = rt.dms_verify(dms)
check('xdms-rs rejects a fake DMS', not ok, why[:60])

# ── Xbox ──────────────────────────────────────────────────────────────────────
xiso = TMP / 'game.iso'
buf = bytearray(noise(0x10100, 'xbox'))
buf[0x10000:0x10014] = rt.XBOX_MAGIC
xiso.write_bytes(bytes(buf))
r = rt.identify(xiso)
check('Xbox XISO detected', (r['system'], r['format']) == ('XBOX', 'XISO'),
      r['detail'])

print('=' * 70)
print('ALL PASS' if not fails else f'{fails} FAILURES')
sys.exit(1 if fails else 0)

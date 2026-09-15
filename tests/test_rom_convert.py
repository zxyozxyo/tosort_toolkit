"""Round-trip proofs for the native converters: convert, convert back, and
require the result to be byte-identical to the source.

Self-contained - it builds its own fixtures rather than depending on a corpus
directory, so it runs from anywhere."""
import sys, hashlib, os, struct, shutil
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import rom_tools as rt
import tempfile as _tempfile
# stripping headers teaches the header library; keep test fixtures out of the real one
rt.HEADER_LIBRARY_DIR = Path(_tempfile.mkdtemp(prefix='romtest_headers_'))

ROOT = Path(os.environ.get('TEMP', '.')) / 'romconvert'
shutil.rmtree(ROOT, ignore_errors=True)
TMP = ROOT / 'rt'; TMP.mkdir(parents=True)
CORPUS = ROOT / 'corpus'
(CORPUS / 'N64').mkdir(parents=True); (CORPUS / 'NES').mkdir(parents=True)
fails = 0


def _noise(n, seed):
    out = bytearray(); h = hashlib.sha256(str(seed).encode()).digest()
    while len(out) < n:
        h = hashlib.sha256(h).digest(); out += h
    return bytes(out[:n])


# N64: one ROM written in all three byte orders
_z = bytearray(_noise(0x1000, 'n64'))
_z[0:4] = bytes([0x80, 0x37, 0x12, 0x40])
_z[0x20:0x34] = b'SUPER MARIO 64      '
(CORPUS / 'N64/mario.z64').write_bytes(bytes(_z))
(CORPUS / 'N64/mario.v64').write_bytes(rt._swap16(bytes(_z)))
(CORPUS / 'N64/mario.n64').write_bytes(rt._swap32(bytes(_z)))

# NES: a headered ROM and the same data without its header
_ines = bytearray(b'NES' + bytes([0x1a]) + bytes([2, 1, 0x12, 0x00]) + bytes(8))
_ines += _noise(2 * 16384 + 8192, 'nes')
(CORPUS / 'NES/game.nes').write_bytes(bytes(_ines))
(CORPUS / 'NES/headerless.nes').write_bytes(bytes(_ines[16:]))


def sha(p):
    return hashlib.sha1(Path(p).read_bytes()).hexdigest()


def check(label, ok, extra=''):
    global fails
    fails += not ok
    print(f'{"PASS" if ok else "FAIL"}  {label}{("  " + extra) if extra else ""}')


# ── N64: every ordered pair, both directions ──────────────────────────────────
src = CORPUS / 'N64/mario.z64'
orders = ['big-endian', 'byteswapped', 'little-endian']
srcs = {'big-endian': CORPUS / 'N64/mario.z64',
        'byteswapped': CORPUS / 'N64/mario.v64',
        'little-endian': CORPUS / 'N64/mario.n64'}

for a in orders:
    for b in orders:
        if a == b:
            continue
        out = TMP / f'n64_{a}_{b}.bin'
        rt.convert_n64(srcs[a], out, a, b)
        check(f'N64 {a:14s} -> {b:14s}', sha(out) == sha(srcs[b]))
        back = TMP / f'n64_{a}_{b}_back.bin'
        rt.convert_n64(out, back, b, a)
        check(f'  round trip back to {a:12s}', sha(back) == sha(srcs[a]))

# odd-length tail must survive a round trip
odd = TMP / 'odd.z64'
odd.write_bytes((CORPUS / 'N64/mario.z64').read_bytes() + b'\xAB\xCD\xEF')
o1, o2 = TMP / 'odd1.bin', TMP / 'odd2.bin'
rt.convert_n64(odd, o1, 'big-endian', 'byteswapped')
rt.convert_n64(o1, o2, 'byteswapped', 'big-endian')
check('N64 odd-length tail round trip', sha(o2) == sha(odd))

# chunk boundary: force many small chunks and confirm identity holds
saved = rt.CHUNK
rt.CHUNK = 4096
c1, c2 = TMP / 'c1.bin', TMP / 'c2.bin'
rt.convert_n64(srcs['big-endian'], c1, 'big-endian', 'little-endian')
rt.convert_n64(c1, c2, 'little-endian', 'big-endian')
check('N64 4 KiB chunking round trip', sha(c2) == sha(srcs['big-endian']))
check('N64 chunked == unchunked', sha(c1) == sha(srcs['little-endian']))
rt.CHUNK = saved

# ── NES ───────────────────────────────────────────────────────────────────────
nes = CORPUS / 'NES/game.nes'
stripped = TMP / 'stripped.nes'
hdr = rt.strip_nes_header(nes, stripped)
check('NES strip header', sha(stripped) == sha(CORPUS / 'NES/headerless.nes'))
rehead = TMP / 'rehead.nes'
rt.add_nes_header(stripped, rehead, hdr)
check('NES re-head round trip', sha(rehead) == sha(nes))

try:
    rt.add_nes_header(stripped, TMP / 'bad.nes', b'\x00' * 16)
    check('NES refuses invalid header', False, 'no exception raised')
except rt.ConversionError:
    check('NES refuses invalid header', True)

# ── PSP CSO ───────────────────────────────────────────────────────────────────
# Build an ISO-ish payload with compressible and incompressible regions so both
# the deflate path and the store-uncompressed path are exercised.
iso = TMP / 'test.iso'
body = (b'\x00' * 200000) + os.urandom(300000) + (b'AB' * 100000) + os.urandom(7777)
iso.write_bytes(body)

for bs in (2048, 16384):
    cso = TMP / f'test_{bs}.cso'
    back = TMP / f'test_{bs}_back.iso'
    rt.iso_to_cso(iso, cso, block_size=bs)
    rt.cso_to_iso(cso, back)
    same = sha(back) == sha(iso)
    ratio = os.path.getsize(cso) * 100 // os.path.getsize(iso)
    check(f'CSO round trip @ {bs} B blocks', same,
          f'({ratio}% of original, size {"exact" if os.path.getsize(back)==len(body) else "WRONG"})')
    d = rt.identify(cso)
    check(f'  CSO detected @ {bs}',
          (d['system'], d['format']) == ('PSP', 'CSO'), d['detail'])

print('=' * 70)
print(f'{"ALL PASS" if not fails else str(fails) + " FAILURES"}')
sys.exit(1 if fails else 0)

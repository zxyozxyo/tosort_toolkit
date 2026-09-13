"""3DS seed crypto and per-file ExeFS keys.

The encrypted fixtures are built here by an INDEPENDENT encryptor: it picks the
key per byte from two whole-ExeFS keystreams, rather than splitting spans the
way rom_tools does. So a wrong span boundary, counter or mid-block skip in the
tool shows up as a mismatch instead of cancelling itself out."""
import os, sys, struct, hashlib, shutil
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import rom_tools as rt
from Crypto.Cipher import AES
from Crypto.Util import Counter

TMP = Path(os.environ.get('TEMP', '.')) / 'rom3dsseed'
shutil.rmtree(TMP, ignore_errors=True)
TMP.mkdir(parents=True)
fails = 0


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


def refused(label, fn, needle):
    try:
        fn()
        check(label, False, 'was not refused')
    except rt.ConversionError as e:
        check(label, needle in str(e), str(e)[:70])


# ── fake key files ────────────────────────────────────────────────────────────
SLOTS = (0x2C, 0x25, 0x18, 0x1B)
KEYX = {s: int.from_bytes(noise(16, f'keyx{s}'), 'big') for s in SLOTS}
# boot9 carries 0x2C only; the next blob entry is a DIFFERENT slot (0x30), which
# is exactly the bytes the old loader wrongly took for 0x25
boot9 = bytearray(0x10000)
base = 0x8000 + 0x5860 + 0x170
boot9[base:base + 16] = KEYX[0x2C].to_bytes(16, 'big')
boot9[base + 16:base + 32] = noise(16, 'slot0x30')
(TMP / 'boot9.bin').write_bytes(bytes(boot9))
rt.KEY_FILES['boot9'] = (TMP / 'boot9.bin', 'test boot9')
(TMP / 'aes_keys.txt').write_text(''.join(
    f'slot0x{s:02X}KeyX={KEYX[s]:032X}\n' for s in SLOTS))
rt.KEY_FILES['aes_keys'] = (TMP / 'aes_keys.txt', 'test aes_keys')

PROGRAM_ID = 0x0004000000123400
SEED = noise(16, 'the-seed')


def write_seeddb(path, entries):
    out = struct.pack('<I', len(entries)) + bytes(12)
    for tid, seed in entries:
        out += struct.pack('<Q', tid) + seed + bytes(8)
    path.write_bytes(out)


write_seeddb(TMP / 'seeddb.bin', [(0x0004000000999900, noise(16, 'other')),
                                  (PROGRAM_ID, SEED)])
rt.KEY_FILES['seeddb'] = (TMP / 'seeddb.bin', 'test seeddb')


# ── independent reference encryptor ───────────────────────────────────────────
def scramble(x, y):
    m = (1 << 128) - 1
    rol = lambda v, b: ((v << b) | (v >> (128 - b))) & m
    return rol(((rol(x, 2) ^ y) + 0x1FF9E9AAC5FE0408024591DC5D52768A) & m, 87)


def keystream(key, pid, section, n):
    iv = pid.to_bytes(8, 'big') + bytes([section]) + bytes(7)
    ctr = Counter.new(128, initial_value=int.from_bytes(iv, 'big'))
    return AES.new(key.to_bytes(16, 'big'), AES.MODE_CTR,
                   counter=ctr).encrypt(bytes(n))


EXH, EXEFS, ROMFS = 0x200, 0xA00, 0x1600
EXEFS_LEN, ROMFS_LEN = 0xC00, 0x1000
FILES = [(b'.code', 0x000, 0x3F3),       # ends mid AES block on purpose
         (b'icon', 0x400, 0x100),
         (b'banner', 0x500, 0x2A1),
         (b'logo', 0x800, 0x1F9)]        # secondary, then primary padding mid-block


def build(method=0x00, seeded=False, seed_for_key=SEED, pid=0x0004000000123400):
    """Return (encrypted, expected_decrypted) for one CXI."""
    plain = bytearray(ROMFS + ROMFS_LEN)
    plain[0:0x100] = noise(0x100, 'sig')
    plain[0x100:0x104] = b'NCCH'
    struct.pack_into('<Q', plain, 0x108, pid)
    struct.pack_into('<H', plain, 0x112, 0)
    plain[0x114:0x118] = hashlib.sha256(
        SEED + pid.to_bytes(8, 'little')).digest()[:4]
    struct.pack_into('<Q', plain, 0x118, pid)
    struct.pack_into('<I', plain, 0x180, 0x400)
    plain[0x188 + 3] = method
    plain[0x188 + 7] = rt.NCCH_FLAG_SEED if seeded else 0
    struct.pack_into('<II', plain, 0x1A0, EXEFS // 0x200, EXEFS_LEN // 0x200)
    struct.pack_into('<II', plain, 0x1B0, ROMFS // 0x200, ROMFS_LEN // 0x200)
    plain[EXH:EXH + 0x800] = noise(0x800, 'exh')
    for i, (name, off, size) in enumerate(FILES):
        struct.pack_into('<8sII', plain, EXEFS + i * 16, name, off, size)
        plain[EXEFS + 0x200 + off:EXEFS + 0x200 + off + size] = noise(size, name)
    plain[ROMFS:ROMFS + 4] = b'IVFC'
    plain[ROMFS + 4:ROMFS + ROMFS_LEN] = noise(ROMFS_LEN - 4, 'romfs')

    key_y = int.from_bytes(plain[0:16], 'big')
    y2 = key_y
    if seeded:
        y2 = int.from_bytes(
            hashlib.sha256(bytes(plain[0:16]) + seed_for_key).digest()[:16], 'big')
    k1 = scramble(KEYX[0x2C], key_y)
    k2 = scramble(KEYX[rt.CRYPTO_METHOD_SLOT[method]], y2)

    enc = bytearray(plain)
    ks = keystream(k1, pid, 1, 0x800)
    enc[EXH:EXH + 0x800] = bytes(a ^ b for a, b in zip(plain[EXH:EXH + 0x800], ks))
    ks1 = keystream(k1, pid, 2, EXEFS_LEN)
    ks2 = keystream(k2, pid, 2, EXEFS_LEN)
    secondary = set()
    for name, off, size in FILES:
        if name not in (b'icon', b'banner'):
            secondary.update(range(0x200 + off, 0x200 + off + size))
    for i in range(EXEFS_LEN):
        enc[EXEFS + i] = plain[EXEFS + i] ^ (ks2 if i in secondary else ks1)[i]
    ks = keystream(k2, pid, 3, ROMFS_LEN)
    enc[ROMFS:] = bytes(a ^ b for a, b in zip(plain[ROMFS:], ks))

    expect = bytearray(plain)
    expect[0x188 + 3] = 0
    expect[0x188 + 7] = (plain[0x188 + 7] & ~rt.NCCH_FLAG_SEED) | rt.NCCH_NOCRYPTO
    return bytes(enc), bytes(expect)


def decrypt(label, enc):
    src, dst = TMP / f'{label}.cxi', TMP / f'{label}.dec'
    src.write_bytes(enc)
    info = rt.ncch_crypt(src, dst, False)
    return dst.read_bytes(), info


# ── seed crypto, method 0x00 ──────────────────────────────────────────────────
enc, expect = build(seeded=True)
out, info = decrypt('seeded', enc)
check('seeded title decrypts byte-exact', out == expect, info['oracle'])
check('oracle reports the seed', 'seed-crypto' in info['oracle'])

# ── seed crypto on a 7.x-keyed title (both mechanisms at once) ────────────────
enc, expect = build(method=0x01, seeded=True)
out, info = decrypt('seeded_m1', enc)
check('seeded + method 0x01 decrypts byte-exact', out == expect)

# ── non-seeded 7.x / 9.x keys: previously refused as "not implemented" ────────
for m in (0x01, 0x0A, 0x0B):
    enc, expect = build(method=m)
    out, info = decrypt(f'method{m:02X}', enc)
    check(f'method 0x{m:02X} decrypts byte-exact', out == expect)

# ── the standard path is unchanged and still round-trips ─────────────────────
enc, expect = build()
out, info = decrypt('standard', enc)
check('standard title decrypts byte-exact', out == expect)
rt.ncch_crypt(TMP / 'standard.dec', TMP / 'standard.enc', True)
check('standard title re-encrypts byte-exact',
      (TMP / 'standard.enc').read_bytes() == enc)

# ── DAT-guided re-encryption recovers the original key ────────────────────────
class FakeDats:
    def __init__(self, blobs):
        self.hashes = {hashlib.sha1(b).hexdigest(): n for n, b in blobs.items()}

    def lookup(self, path):
        g = self.hashes.get(hashlib.sha1(Path(path).read_bytes()).hexdigest())
        return ({'game': g, 'dat': 'fake'}, 'sha1') if g else (None, '')


for label, kw in (('7.x key', dict(method=0x01)), ('New 3DS key', dict(method=0x0B)),
                  ('seed', dict(seeded=True))):
    enc, _ = build(**kw)
    (TMP / 'orig.cxi').write_bytes(enc)
    rt.ncch_crypt(TMP / 'orig.cxi', TMP / 'orig.dec', False)
    plain_back = rt.ncch_crypt(TMP / 'orig.dec', TMP / 'std.enc', True)
    info = rt.ncch_encrypt_matching(TMP / 'orig.dec', TMP / 'match.enc',
                                    dats=FakeDats({'the game': enc}))
    check(f'{label}: standard re-encrypt differs from the original (the problem)',
          (TMP / 'std.enc').read_bytes() != enc)
    check(f'{label}: DAT-guided re-encrypt is byte-exact',
          (TMP / 'match.enc').read_bytes() == enc, info['oracle'])

# ── refusals ──────────────────────────────────────────────────────────────────
enc, _ = build(seeded=True, pid=0x0004000000ABCD00)
(TMP / 'unknown.cxi').write_bytes(enc)
refused('title missing from seeddb is refused',
        lambda: rt.ncch_crypt(TMP / 'unknown.cxi', TMP / 'unknown.dec', False),
        'not in seeddb.bin')

write_seeddb(TMP / 'badseed.bin', [(PROGRAM_ID, noise(16, 'wrong'))])
rt.KEY_FILES['seeddb'] = (TMP / 'badseed.bin', 'bad')
enc, _ = build(seeded=True)
(TMP / 'badseed.cxi').write_bytes(enc)
refused('wrong seed is caught by the header checksum',
        lambda: rt.ncch_crypt(TMP / 'badseed.cxi', TMP / 'badseed.dec', False),
        'does not match')

rt.KEY_FILES['seeddb'] = (TMP / 'missing.bin', 'absent')
refused('seeded title without seeddb.bin is refused',
        lambda: rt.ncch_crypt(TMP / 'badseed.cxi', TMP / 'nodb.dec', False),
        'seeddb.bin')
rt.KEY_FILES['seeddb'] = (TMP / 'seeddb.bin', 'test seeddb')

# header says seed, checksum is valid, but the content was keyed WITHOUT it:
# only the new RomFS oracle can notice, since the ExeFS header is primary-keyed
enc, expect = build(seeded=True)
wrong, _ = build(seeded=False)
bad = bytearray(wrong)
bad[0x188:0x190] = enc[0x188:0x190]
(TMP / 'mislabelled.cxi').write_bytes(bytes(bad))
refused('secondary-key mismatch is caught by the RomFS IVFC check',
        lambda: rt.ncch_crypt(TMP / 'mislabelled.cxi', TMP / 'mis.dec', False),
        'IVFC')
check('no output left behind after a failed decrypt',
      not (TMP / 'mis.dec').exists())

# ── a 7.x title without aes_keys.txt must be refused by name, not mis-keyed ───
rt.KEY_FILES['aes_keys'] = (TMP / 'no_aes_keys.txt', 'absent')
enc, _ = build(method=0x01)
(TMP / 'nokey.cxi').write_bytes(enc)
refused('method 0x01 without aes_keys.txt names the missing slot',
        lambda: rt.ncch_crypt(TMP / 'nokey.cxi', TMP / 'nokey.dec', False),
        'slot0x25KeyX')
rt.KEY_FILES['aes_keys'] = (TMP / 'aes_keys.txt', 'test aes_keys')

(TMP / 'bad_aes.txt').write_text(f'slot0x2CKeyX={1:032X}\n')
refused('aes_keys.txt disagreeing with boot9 on 0x2C is refused',
        lambda: rt.load_boot9_keyx(aes_keys_path=TMP / 'bad_aes.txt'), 'disagree')

# ── truncated seeddb ──────────────────────────────────────────────────────────
(TMP / 'short.bin').write_bytes(struct.pack('<I', 50) + bytes(40))
refused('truncated seeddb is refused',
        lambda: rt.load_seeddb(TMP / 'short.bin'), 'truncated')

# ── the user's real seeddb.bin, if present ────────────────────────────────────
real = rt.KEYS_DIR / 'seeddb.bin'
if real.exists():
    seeds = rt.load_seeddb(real)
    size_ok = real.stat().st_size >= 0x10 + len(seeds) * 0x20
    check('real seeddb.bin parses', len(seeds) > 0 and size_ok,
          f'{len(seeds):,} seeds')
    check('real seeddb title IDs look like 3DS titles',
          all(tid >> 32 == 0x00040000 for tid in seeds),
          f'{sum(tid >> 32 == 0x00040000 for tid in seeds)} of {len(seeds)}')

print('=' * 70)
print('ALL PASS' if not fails else f'{fails} FAILURES')
sys.exit(1 if fails else 0)

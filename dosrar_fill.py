"""RAR 2.x for DOS reads memory it never wrote -- and the scene's PC had
something there.

The 16-bit packer (a hand-written asm module, the same code in every build
from 2.00 to 2.50) keeps its hash-chain links in two 64 KB segments: one for
window positions below 32768, one for the rest. The loop that inserts the
positions INSIDE a match keeps the segment it chose for the match's START. So
a match that runs across 32768 files the links of its tail in the lower
segment, and their slots in the upper one are never written on the first pass
through the window. The next chain walk that reaches one of those positions
reads whatever the memory held:

  * DOSBox hands out zeroed memory, the walk reads 0 and stops;
  * a real DOS box had leftovers from earlier programs, and the walk jumped
    to that "position" and carried on from there.

It changes a handful of decisions just past 32768 -- three in 100 KB on a
Dreamcast GD-ROM image -- and every byte after them. Measured 2026-09-28 on
Puyo_Puyo_DA!_JAP_DC-KALISTO: stock dosrar200 -m5 -s -mm reproduced 794
tokens; the same build with the upper segment pre-set to 6944..9000 is
byte-identical in all five volumes (90 MB), header times aside.

The garbage itself is unknowable, but it only matters where a walk used it:
where a stock probe first parts company with the original between 32768 and
65536, the original's match there was reached through a stale slot, and its
source position is a value that sends the walk to the same place. The sweep
reads it that way (RsrToolAPI._fill_from_look); fill_exe() builds the program
that uses it. fill_candidates() is the original-only approximation (a match
that skips a nearer copy of the same bytes) -- right on the Dreamcast sets,
but stock RAR skips nearer matches too, so on PSX images it also flags spots
the stock build reproduces. Kept for surveys, not used by the sweep.
"""
import struct

# ── the patched program ───────────────────────────────────────────────────────

# The packer's init: clear the hash table (32K or 16K words), then fill the
# 2-byte table with C0C0. 39 bytes, identical in every LZEXE 2.00-2.50 build.
_INIT_OLD = bytes.fromhex("2e8e0693012bc0b900802e803ec300ff7403b900402bfffcf3ab"
                          "b8c0c00e07bfa301b90008f3ab")


def _init_new(value: int) -> bytes:
    """The same 39 bytes, re-packed so they also fill the upper link segment
    (cs:[0x197]) with `value`. The 16K-table case is dropped: it is only taken
    below 377 KB free, which makes these originals WORSE, not better."""
    code = (bytes.fromhex("FC"              # cld
                          "2E8E069301"      # mov es, cs:[0x193]   hash table
                          "31C0" "31FF"     # xor ax,ax / xor di,di
                          "B90080" "F3AB"   # mov cx,8000h / rep stosw
                          "2E8E069701"      # mov es, cs:[0x197]   upper links
                          "B8") + struct.pack("<H", value & 0xFFFF)
            + bytes.fromhex("B580" "F3AB"   # mov ch,80h (cl is 0) / rep stosw
                            "B8C0C0" "0E07" "BFA301" "B508" "F3AB"))
    assert len(code) == len(_INIT_OLD)
    return code


def unlzexe(d: bytes):
    """LZEXE 0.91 -> (load image, [(seg, off)] relocations, (ip, cs, sp, ss))."""
    hp = struct.unpack_from("<H", d, 8)[0] * 16
    base = hp + struct.unpack_from("<H", d, 0x16)[0] * 16
    regs = struct.unpack_from("<4H", d, base)
    pos = hp
    w = struct.unpack_from("<H", d, pos)[0]
    pos += 2
    n = 16
    out = bytearray()

    def bit():
        nonlocal w, n, pos
        b = w & 1
        n -= 1
        if n == 0:
            w = struct.unpack_from("<H", d, pos)[0]
            pos += 2
            n = 16
        else:
            w >>= 1
        return b

    while True:
        if bit():
            out.append(d[pos])
            pos += 1
            continue
        if not bit():
            ln = (bit() << 1 | bit()) + 2
            span = d[pos] - 0x100
            pos += 1
        else:
            lo, hi = d[pos], d[pos + 1]
            pos += 2
            span = (lo | ((hi & 0xF8) << 5) | 0xE000) - 0x10000
            ln = hi & 7
            if ln == 0:
                ln = d[pos]
                pos += 1
                if ln == 0:
                    break
                if ln == 1:
                    continue
                ln += 1
            else:
                ln += 2
        for _ in range(ln):
            out.append(out[span])
    p = base + 0x158
    seg = off = 0
    rel = []
    while True:
        sp = d[p]
        p += 1
        if sp == 0:
            sp = struct.unpack_from("<H", d, p)[0]
            p += 2
            if sp == 0:
                seg += 0xFFF
                continue
            if sp == 1:
                break
        off += sp
        while off > 0xF:
            seg += 1
            off -= 0x10
        rel.append((seg, off))
    return bytes(out), rel, regs


def _build_mz(packed: bytes, img: bytes, rel, regs) -> bytes:
    ip, ocs, osp, oss = regs
    p_last, p_pages, _, p_hdr, p_min, _ = struct.unpack_from("<6H", packed, 2)
    p_img = p_pages * 512 - (512 - p_last if p_last else 0) - p_hdr * 16
    # the unpacked program must get at least the memory the packed one asked for
    need = max(p_img + p_min * 16, oss * 16 + osp)
    minalloc = max(0, -(-(need - len(img)) // 16))
    hdr_len = (0x1C + 4 * len(rel) + 15) // 16 * 16
    total = hdr_len + len(img)
    h = bytearray(hdr_len)
    struct.pack_into("<2s13H", h, 0, b"MZ", total % 512, -(-total // 512),
                     len(rel), hdr_len // 16, minalloc, 0xFFFF, oss, osp, 0,
                     ip, ocs, 0x1C, 0)
    for i, (s, o) in enumerate(rel):
        struct.pack_into("<2H", h, 0x1C + 4 * i, o, s)
    return bytes(h) + img


def fill_exe(stock: bytes, value: int) -> bytes:
    """`stock` with its upper link segment pre-set to `value`. Raises
    ValueError for a build that does not carry the 2.x packer's init block
    (1.5x, and the non-LZEXE repacks). With value 0 the output equals stock's,
    measured -- DOSBox memory is zero already."""
    new = _init_new(value)
    if stock[0x1C:0x20] != b"LZ91":
        at = stock.find(_INIT_OLD)
        if at < 0 or stock.find(_INIT_OLD, at + 1) >= 0:
            raise ValueError("not a RAR 2.x DOS packer")
        return stock[:at] + new + stock[at + len(new):]
    img, rel, regs = unlzexe(stock)
    at = img.find(_INIT_OLD)
    if at < 0 or img.find(_INIT_OLD, at + 1) >= 0:
        raise ValueError("not a RAR 2.x DOS packer")
    img = img[:at] + new + img[at + len(new):]
    return _build_mz(stock, img, rel, regs)


# ── where the original used a stale slot ─────────────────────────────────────

_LDEC = (0, 1, 2, 3, 4, 5, 6, 7, 8, 10, 12, 14, 16, 20, 24, 28, 32, 40, 48, 56,
         64, 80, 96, 112, 128, 160, 192, 224)
_LBIT = (0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 2, 2, 2, 2, 3, 3, 3, 3, 4, 4, 4, 4,
         5, 5, 5, 5)
_DDEC = (0, 1, 2, 3, 4, 6, 8, 12, 16, 24, 32, 48, 64, 96, 128, 192, 256, 384,
         512, 768, 1024, 1536, 2048, 3072, 4096, 6144, 8192, 12288, 16384,
         24576, 32768, 49152, 65536, 98304, 131072, 196608, 262144, 327680,
         393216, 458752, 524288, 589824, 655360, 720896, 786432, 851968,
         917504, 983040)
_DBIT = (0, 0, 0, 0, 1, 1, 2, 2, 3, 3, 4, 4, 5, 5, 6, 6, 7, 7, 8, 8, 9, 9, 10,
         10, 11, 11, 12, 12, 13, 13, 14, 14, 15, 15, 16, 16, 16, 16, 16, 16,
         16, 16, 16, 16, 16, 16, 16, 16)
_SDDEC = (0, 4, 8, 16, 32, 64, 128, 192)
_SDBIT = (2, 2, 3, 4, 5, 6, 6, 6)


def rar20_tokens(data: bytes, max_out: int, blocks: list | None = None,
                 tokens: bool = True):
    """[(output position, kind, detail)] of a RAR 2.0 stream up to max_out
    bytes of output. kind: L literal, M new match (len, dist), OLD/REP/S the
    distance-reuse forms, A audio. After unrar's Unpack20; no window needed.

    `blocks`, when given, collects (output position, audio?, channels) at
    every table read -- where each block starts and what it is. With
    tokens=False nothing else is kept, which is what a whole CD image needs
    (hundreds of millions of tokens would not fit in memory)."""
    buf = bytes(data) + b"\0" * 8
    nbits = len(data) * 8
    pos = 0

    def peek(n):
        i = pos >> 3
        return (int.from_bytes(buf[i:i + 4], "big") >> (32 - (pos & 7) - n)) & ((1 << n) - 1)

    def huff(lengths):
        look = [(0, 0)] * (1 << 15)
        code = 0
        for L in range(1, 16):
            for sym, ln in enumerate(lengths):
                if ln == L:
                    pre = code << (15 - L)
                    for i in range(1 << (15 - L)):
                        look[pre | i] = (sym, L)
                    code += 1
            code <<= 1
        return look

    def dec(tab):
        nonlocal pos
        s, ln = tab[peek(15)]
        if not ln:
            raise ValueError("bad code")
        pos += ln
        return s

    def get(n):
        nonlocal pos
        v = peek(n)
        pos += n
        return v

    old = [0] * (257 * 4)
    st = {"audio": False, "chan": 1, "cur": 0}
    tab = {}

    def tables():
        bf = peek(16)
        audio = bool(bf & 0x8000)
        if not bf & 0x4000:
            old[:] = [0] * len(old)
        get(2)
        if audio:
            st["chan"] = ((bf >> 12) & 3) + 1
            if st["cur"] >= st["chan"]:
                st["cur"] = 0
            get(2)
            size = 257 * st["chan"]
        else:
            size = 298 + 48 + 28
        st["audio"] = audio
        bd = huff([get(4) for _ in range(19)])
        t = [0] * size
        i = 0
        while i < size:
            n = dec(bd)
            if n < 16:
                t[i] = (n + old[i]) & 15
                i += 1
            elif n == 16:
                c = get(2) + 3
                while c and i < size:
                    t[i] = t[i - 1]
                    i += 1
                    c -= 1
            else:
                c = get(3) + 3 if n == 17 else get(7) + 11
                while c and i < size:
                    t[i] = 0
                    i += 1
                    c -= 1
        if audio:
            tab["MD"] = [huff(t[k * 257:(k + 1) * 257]) for k in range(st["chan"])]
        else:
            tab["LD"] = huff(t[:298])
            tab["DD"] = huff(t[298:346])
            tab["RD"] = huff(t[346:])
        old[:size] = t
        if blocks is not None:
            blocks.append((out, audio, st["chan"] if audio else 0))

    out = 0
    ev = []
    olds = [0, 0, 0, 0]
    optr = 0
    last = (0, 0)
    try:
        tables()
        while out < max_out and pos < nbits:
            if st["audio"]:
                n = dec(tab["MD"][st["cur"]])
                if n == 256:
                    tables()
                    continue
                if tokens:
                    ev.append((out, "A", n))
                out += 1
                st["cur"] = (st["cur"] + 1) % st["chan"]
                continue
            n = dec(tab["LD"])
            if n < 256:
                if tokens:
                    ev.append((out, "L", n))
                out += 1
                continue
            if n == 269:
                tables()
                continue
            if n > 269:
                n -= 270
                ln = _LDEC[n] + 3 + (get(_LBIT[n]) if _LBIT[n] else 0)
                dn = dec(tab["DD"])
                d = _DDEC[dn] + 1 + (get(_DBIT[dn]) if _DBIT[dn] else 0)
                ln += (d >= 0x2000) + (d >= 0x40000)
                kind = "M"
            elif n == 256:
                ln, d = last
                kind = "REP"
            elif n < 261:
                d = olds[(optr - (n - 256)) & 3]
                rn = dec(tab["RD"])
                ln = _LDEC[rn] + 2 + (get(_LBIT[rn]) if _LBIT[rn] else 0)
                ln += (d >= 0x101) + (d >= 0x2000) + (d >= 0x40000)
                kind = "OLD"
            else:
                n -= 261
                d = _SDDEC[n] + 1 + (get(_SDBIT[n]) if _SDBIT[n] else 0)
                ln = 2
                kind = "S"
            # Every copy pushes its distance, the repeat included -- unrar's
            # CopyString20 does it for all four kinds. Skipping REP here gave
            # later OLD tokens the wrong distance, hence the wrong length
            # bonus, and the output position drifted: 11,779 B by 17 MB into
            # Oni_Zero, which made every block look misplaced.
            olds[optr & 3] = d
            optr += 1
            last = (ln, d)
            if tokens:
                ev.append((out, kind, (ln, d)))
            out += ln
    except (ValueError, IndexError):
        pass
    return ev


def fill_candidates(stream: bytes, src: bytes, limit: int = 65536) -> list[int]:
    """Values for the upper link segment that reproduce `stream`, best first,
    or [] when the original shows no stale-slot jump.

    `stream` is the compressed data of the archive's FIRST file (it is the one
    whose window crosses 32768 first) and `src` at least `limit` + 300 bytes of
    that file. A stale slot only exists behind a match that crosses 32768,
    and only matters until the window wraps at 65536."""
    toks = rar20_tokens(stream, limit)
    cross = next((t for t in toks if t[1] in ("M", "OLD", "REP", "S")
                  and t[0] < 32768 < t[0] + t[2][0]), None)
    if cross is None:
        return []
    start = cross[0] + cross[2][0]
    out = []
    for p, kind, det in toks:
        if p < start or kind != "M":
            continue
        ln, d = det
        pat = src[p:p + ln]
        if len(pat) < ln:
            break
        # a nearer copy of the same bytes -- stock RAR could not have skipped it
        near = src.find(pat, p - d + 1, p + ln - 1)
        if 0 <= near < p and p - d not in out:
            out.append(p - d)
    return out

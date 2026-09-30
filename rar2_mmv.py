"""RAR 2.x (Win32) multimedia-analyser verdicts, forced per call.

WHY. With -mm, RAR 2.x asks an analyser about every ~1 KB chunk whether it is
audio. Its final test reads the match finder's hash heads, so the answer can
depend on the state the packing PC left in memory, not on the data alone --
the Windows twin of the DOS leftover-memory case (dosrar_fill). Measured on
two HOOLiGANS PSX walls whose .bin never reproduced at any build:

  Oni_Zero_Fukkatsu   582,175 analyser calls, ONE disagrees with the original
                      (call 344,061: original audio, ours LZ). Forced: the
                      281,460,921 B stream is byte-identical.
  FURAIKI             692,951 calls, ONE disagrees (call 279,549: original
                      LZ, ours audio). Forced: 518,112,167 B identical.

So a recipe can carry the verdicts that went the other way, `@MMV<call>:<v>`
(call numbers are 1-based across the whole command), and the replay runs the
same build patched to answer those calls that way. Every other call is
untouched and the build is otherwise byte-for-byte the stock one.

HOW THE PATCH WORKS. Every call to the analyser is redirected into a code cave
(CODE's raw data grown into its virtual slack): count the call, run the real
analyser, and if the count is in the table replace AL. With a trace path it
also appends a record per call -- ring position, call number, chunk length,
verdict, channels, window mask, the mode before the call and the chunk's
first 16 bytes -- which is how the capture finds the calls to force.

FINDING THEM (capture). The ring position is NOT the output position (it
drifts from it), so each call is placed in its file by its 16 data bytes;
the original's block starts come from decoding its stream
(dosrar_fill.rar20_tokens(blocks=...)). A call whose verdict differs from the
original's block type at that position is the one to force. One at a time,
repacking between: a flip changes what follows it.
"""
from __future__ import annotations

import bisect
import struct

REC = 44                                  # bytes per trace record
_SITE = bytes.fromhex("5653E8")           # push esi; push ebx; call ANALYSER
_AFTER = bytes.fromhex("0FBED08993B4010000")  # movsx edx, al; mov [ebx+1b4], edx
_CAVE = 0xC00
_TAB, _NAME, _DATA, _CNT, _OVR = 0x300, 0x900, 0xB00, 0xB80, 0xB84
_NTAB = (_NAME - _TAB - 4) // 8
_NAME_MAX = _DATA - _NAME - 0x40          # DATA-0x40 is where the code looks


def _sections(d):
    pe = struct.unpack_from('<I', d, 0x3c)[0]
    if d[pe:pe + 4] != b"PE\0\0":
        raise ValueError("not a PE image")
    nsec = struct.unpack_from('<H', d, pe + 6)[0]
    opt = struct.unpack_from('<H', d, pe + 20)[0]
    so = pe + 24 + opt
    secs = []
    for i in range(nsec):
        name, vs, va, rs, ro = struct.unpack_from('<8sIIII', d, so + 40 * i)
        secs.append(dict(off=so + 40 * i, name=name.rstrip(b'\0'), vs=vs,
                         va=va, rs=rs, ro=ro))
    ib = struct.unpack_from('<I', d, pe + 24 + 28)[0]
    return pe, secs, ib


def _rva2off(secs, rva):
    for s in secs:
        if s['va'] <= rva < s['va'] + max(s['vs'], s['rs']):
            return s['ro'] + rva - s['va']
    raise ValueError(f"rva {rva:#x} outside every section")


def _iat(d, secs, ib, pe, want):
    imp_rva = struct.unpack_from('<I', d, pe + 24 + 104)[0]
    out = {}
    p = _rva2off(secs, imp_rva)
    while True:
        oft, _, _, name_rva, ft = struct.unpack_from('<5I', d, p)
        if not name_rva:
            break
        look = oft or ft
        i = 0
        while True:
            ent = struct.unpack_from('<I', d, _rva2off(secs, look + 4 * i))[0]
            if not ent:
                break
            if not ent & 0x80000000:
                n = _rva2off(secs, ent) + 2
                nm = d[n:d.index(b'\0', n)].decode('latin1')
                if nm in want:
                    out[nm] = ib + ft + 4 * i
            i += 1
        p += 20
    return out


def _target(d, secs, ib):
    code = secs[0]
    at = d.find(_SITE, code['ro'], code['ro'] + code['rs'])
    while at >= 0 and d[at + 7:at + 7 + len(_AFTER)] != _AFTER:
        at = d.find(_SITE, at + 1, code['ro'] + code['rs'])
    if at < 0:
        return None
    call_va = ib + code['va'] + (at + 2 - code['ro'])
    return call_va + 5 + struct.unpack_from('<i', d, at + 3)[0]


def supported(exe: bytes) -> bool:
    """Whether this build has the analyser this patch knows and room for it."""
    try:
        pe, secs, ib = _sections(exe)
        code = secs[0]
        return (_target(exe, secs, ib) is not None
                and code['vs'] - code['rs'] >= _CAVE
                and len(_iat(exe, secs, ib, pe,
                             {"CreateFileA", "WriteFile"})) == 2)
    except (ValueError, struct.error, IndexError):
        return False


def parse(switches) -> list[tuple[int, int]]:
    """[(call, verdict)] from recipe switches like '@MMV344061:1,500000:0'."""
    out = []
    for x in switches:
        x = str(x)
        if not x.startswith("@MMV"):
            continue
        for part in filter(None, x[4:].split(",")):
            n, v = part.split(":")
            out.append((int(n), int(v)))
    return out


def switch(entries) -> str:
    return "@MMV" + ",".join(f"{n}:{v}" for n, v in sorted(entries))


def build(exe: bytes, entries, trace_path: str | None = None) -> bytes:
    """The build with each (call number, verdict) in `entries` forced."""
    d = bytearray(exe)
    pe, secs, ib = _sections(d)
    code = secs[0]
    target = _target(d, secs, ib)
    if target is None:
        raise ValueError("analyser call site not found")
    if code['vs'] - code['rs'] < _CAVE:
        raise ValueError("no slack in CODE for the patch")
    if len(entries) > _NTAB:
        raise ValueError("too many forced verdicts")
    ins_at = code['ro'] + code['rs']
    cave = ib + code['va'] + code['rs']
    d[ins_at:ins_at] = bytes(_CAVE)
    struct.pack_into('<I', d, code['off'] + 16, code['rs'] + _CAVE)
    for s in secs[1:]:
        if s['ro'] >= ins_at:
            struct.pack_into('<I', d, s['off'] + 20, s['ro'] + _CAVE)
    ch = struct.unpack_from('<I', d, code['off'] + 36)[0]
    struct.pack_into('<I', d, code['off'] + 36, ch | 0x80000000)   # writable
    secs = _sections(d)[1]
    TAB, CNT, OVR, DATA = cave + _TAB, cave + _CNT, cave + _OVR, cave + _DATA
    c = bytearray()
    c += b"\xFF\x05" + struct.pack('<I', CNT)                        # inc [CNT]
    c += b"\xC6\x05" + struct.pack('<I', OVR) + b"\xFF"              # [OVR] = none
    c += b"\x60"                                                     # pushad
    c += b"\xA1" + struct.pack('<I', CNT)                            # eax = [CNT]
    c += b"\xBE" + struct.pack('<I', TAB)                            # esi = TAB
    c += b"\x8B\x0E\x83\xC6\x04"                                     # ecx = n
    c += b"\xE3\x00"; j0 = len(c) - 1                                # jecxz done
    lp = len(c)
    c += b"\x3B\x06" + b"\x75\x00"; jn = len(c) - 1                  # cmp eax,[esi]
    c += b"\x8A\x5E\x04\x88\x1D" + struct.pack('<I', OVR)            # [OVR] = verdict
    c += b"\xEB\x00"; jd = len(c) - 1
    nx = len(c)
    c += b"\x83\xC6\x08"                                             # next entry
    c += b"\xE2" + bytes([(lp - (len(c) + 2)) & 0xFF])               # loop
    done = len(c)
    c[j0] = done - (j0 + 1); c[jn] = nx - (jn + 1); c[jd] = done - (jd + 1)
    c += b"\x61"                                                     # popad
    c += bytes.fromhex("FF742408FF742408")                           # push len; push obj
    c += b"\xE8" + struct.pack('<i', target - (cave + len(c) + 5))   # the analyser
    c += b"\x8A\x0D" + struct.pack('<I', OVR)                        # cl = [OVR]
    c += b"\x80\xF9\xFF\x74\x02\x88\xC8"                             # forced? al = cl
    if trace_path:
        name = trace_path.encode("mbcs") + b"\0"
        if len(name) > _NAME_MAX:
            raise ValueError("trace path too long")
        imp = _iat(d, secs, ib, pe, {"CreateFileA", "WriteFile"})
        if len(imp) != 2:
            raise ValueError("CreateFileA/WriteFile not imported")
        c += b"\x60"                                                 # pushad
        c += b"\x8B\x74\x24\x24"                                     # esi = obj
        c += b"\xBF" + struct.pack('<I', DATA)                       # edi = DATA
        c += bytes.fromhex("8B8E681A0000894F10")                     # ring pos
        c += b"\xA1" + struct.pack('<I', CNT) + b"\x89\x47\x14"      # call number
        c += bytes.fromhex("8B4C2428894F18")                         # chunk length
        c += bytes.fromhex("0FB64C241C894F1C")                       # verdict (AL)
        c += bytes.fromhex("8B8EB0010000894F20")                     # channels
        c += bytes.fromhex("8B4E1C894F24")                           # window mask
        c += bytes.fromhex("8B8EB4010000894F28")                     # mode before
        c += bytes.fromhex("8B06" "0386681A0000")                    # eax = window+pos
        for k in range(4):
            c += bytes([0x8B, 0x48, 4 * k, 0x89, 0x4F, 0x2C + 4 * k])  # 16 data bytes
        c += b"\x83\x3F\x00" + b"\x75\x00"; jo = len(c) - 1          # file open?
        c += bytes.fromhex("6A006880000000" "6A026A006A01" "6800000040")
        c += b"\x8D\x87" + struct.pack('<i', -(_DATA - _NAME)) + b"\x50"  # lea eax, name
        c += b"\xFF\x15" + struct.pack('<I', imp["CreateFileA"]) + b"\x89\x07"
        c[jo] = len(c) - (jo + 1)
        c += bytes.fromhex("6A008D4708506A") + bytes([REC]) + bytes.fromhex("8D471050FF37")
        c += b"\xFF\x15" + struct.pack('<I', imp["WriteFile"])
        c += b"\x61"                                                 # popad
    c += b"\xC2\x08\x00"                                             # ret 8
    if len(c) > _TAB:
        raise AssertionError("patch code overflows its slot")
    d[ins_at:ins_at + len(c)] = c
    tab = struct.pack('<I', len(entries)) + b''.join(
        struct.pack('<II', int(n), int(v) & 0xFF) for n, v in sorted(entries))
    d[ins_at + _TAB:ins_at + _TAB + len(tab)] = tab
    if trace_path:
        d[ins_at + _NAME:ins_at + _NAME + len(name)] = name
    hooked = 0
    i, end = code['ro'], code['ro'] + code['rs']
    while True:
        i = d.find(b"\xE8", i, end)
        if i < 0:
            break
        va = ib + code['va'] + (i - code['ro'])
        if (va + 5 + struct.unpack_from('<i', d, i + 1)[0] == target
                and 0x50 <= d[i - 1] <= 0x57 and 0x50 <= d[i - 2] <= 0x57):
            struct.pack_into('<i', d, i + 1, cave - (va + 5))
            hooked += 1
        i += 1
    if not hooked:
        raise ValueError("no analyser call hooked")
    return bytes(d)


def read_trace(raw: bytes):
    """[(call, unwrapped ring position, len, verdict, channels, mode, fp16)]."""
    out = []
    base = prev = 0
    for i in range(len(raw) // REC):
        pos, cnt, ln, v, ch, mask, mode = struct.unpack_from('<7I', raw, i * REC)
        if pos < prev:
            base += mask + 1
        prev = pos
        out.append((cnt, base + pos, ln, v & 0xFF, ch, mode,
                    raw[i * REC + 28:i * REC + 44]))
    return out


def place(calls, src: bytes, win: int = 1 << 17, samples: int = 48):
    """Yield (call, file position, verdict) for the calls whose 16 data bytes
    are FOUND in `src` -- one file's content, or a solid run's. Calibrated on
    data bytes that occur exactly once in the file, then followed call to call
    (the ring position drifts from the file position, slowly, so a narrow
    window almost always finds it and the wide one is only the fallback)."""
    good = [c for c in calls if len(set(c[6])) > 8]
    if not good:
        return
    votes = {}
    step = max(1, len(good) // samples)
    for c in good[::step][:samples * 2]:
        j = src.find(c[6])
        if j >= 0 and src.find(c[6], j + 1) < 0:
            votes[j - c[1]] = votes.get(j - c[1], 0) + 1
    if not votes:
        return
    off = max(votes, key=votes.get)
    for cnt, u, ln, v, ch, mode, fp in calls:
        est = u + off
        if not (0 <= est < len(src)) or len(set(fp)) <= 2:
            continue
        best = None
        for w in (4096, win):
            j = src.find(fp, max(0, est - w), est + w + 16)
            while j >= 0:
                if best is None or abs(j - est) < abs(best - est):
                    best = j
                j = src.find(fp, j + 1, est + w + 16)
            if best is not None:
                break
        if best is None:
            continue
        off = best - u
        yield cnt, best, v


def first_disagreement(placed, blocks):
    """(call, verdict the original implies) for the first call whose verdict
    differs from the original's block type at its position, or None.
    `blocks` is [(output position, audio?, channels)] of the ORIGINAL."""
    starts = [b[0] for b in blocks]
    for cnt, at, v in placed:
        i = bisect.bisect_right(starts, at) - 1
        if i < 0:
            continue
        want = 1 if blocks[i][1] else 0
        if v != want:
            return cnt, want
    return None

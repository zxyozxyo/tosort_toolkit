"""Unpack a representative SAMPLE of the zipped sets in !WIP into !WIP/_staged.

The user's zips are never modified. Each staged folder is named like its DAT so
tests/conversion_matrix.py can attach expectations to it. Paired sets (the same
games in two formats) are sampled by shared name, so both sides of a conversion
exist for the same title.

    python tests/stage_wip.py            # carts, computers, disk images
    python tests/stage_wip.py --discs    # also the big optical discs
"""
import argparse, os, subprocess, zipfile
from pathlib import Path

WIP = Path(__file__).resolve().parent.parent / '!WIP'
STAGED = WIP / '_staged'

# (source folder, sub-path filter or None, staged name, how many)
SETS = [
    ('Atari - Atari 2600', None, 'Atari - Atari 2600', 10),
    ('Atari - Atari 5200', None, 'Atari - Atari 5200', 10),
    ('Atari - Atari Lynx (BLL)', None, 'Atari - Atari Lynx (BLL)', 3),
    ('NEC - PC Engine - TurboGrafx-16', None, 'NEC - PC Engine - TurboGrafx-16', 20),
    ('NEC - PC Engine SuperGrafx', None, 'NEC - PC Engine SuperGrafx', 5),
    ('Commodore AMIGA', 'ADF', 'Commodore - Amiga [ADF]', 15),
] + [('Commodore C64', f'[{f}]', f'Commodore - C64 [{f}]', 10)
     for f in ('D64', 'T64', 'PRG', 'P00', 'CRT')] \
  + [('ZX Spectrum', f'[{f}]', f'Sinclair - ZX Spectrum [{f}]', 10)
     for f in ('TAP', 'TZX', 'Z80', 'TRD')] \
  + [('Apple', f'[{f}]', f'Apple - II [{f}]', n)
     for f, n in (('DSK', 10), ('PO', 10), ('2MG', 10), ('WOZ', 5), ('NIB', 5),
                  ('EDD', 3), ('HDV', 3), ('A2R', 2))]   + [('Atari', '[ATR]', 'Atari - 8bit [ATR]', 10), ('Atari', '[XFD]', 'Atari - 8bit [XFD]', 5),
     ('Atari', '[ST]', 'Atari - ST [ST]', 10), ('Sinclair', '[SCL]', 'Sinclair - ZX Spectrum [SCL]', 10),
     ('Commodore', '[D81]', 'Commodore - C64 [D81]', 10),
     ('IBM PC Compatibles', '[TD0]', 'IBM - PC [TD0]', 15),
     ('IBM PC Compatibles', '[IMD]', 'IBM - PC [IMD]', 6),
     ('Nintendo - Nintendo DSi (Encrypted)', None, 'Nintendo - Nintendo DSi (Encrypted)', 17),
     ('Nintendo - Nintendo DSi (Decrypted)', None, 'Nintendo - Nintendo DSi (Decrypted)', 15),
     ('Nintendo - Nintendo DSi (Digital) (CDN) (Encrypted)', None,
      'Nintendo - Nintendo DSi (Digital) (CDN) (Encrypted)', 10),
     ('Nintendo - Nintendo DSi (Digital) (CDN) (Decrypted)', None,
      'Nintendo - Nintendo DSi (Digital) (CDN) (Decrypted)', 11)]

# PSN PKG zips: every Mini and theme (small), plus a few full games
PSN_PKG = ('Sony - PlayStation Portable (PSN) (Encrypted)', 99)

# (folder A, folder B, how many shared titles)
PAIRS = [
    ('Atari - Atari 7800 (A78)', 'Atari - Atari 7800 (BIN)', 8),
    ('Atari - Atari Lynx (LNX)', 'Atari - Atari Lynx (LYX)', 18),
    ('Nintendo - Family Computer Disk System (FDS)',
     'Nintendo - Family Computer Disk System (QD)', 20),
    ('Casio - Loopy (BigEndian)', 'Casio - Loopy (LittleEndian)', 12),
    ('Seta - Aleck64 (BigEndian)', 'Seta - Aleck64 (ByteSwapped)', 14),
    ('Atari - Atari Jaguar (J64)', 'Atari - Atari Jaguar (ROM)', 12),
]

# Apple II bit/nibble-level sets: every sampled title that also exists as a
# [DSK] is staged with its DSK, so NIB/WOZ -> DSK can be judged by the DSK DAT.
# (folder, sub, staged name, shared titles, extra unshared titles)
APPLE_LEVELS = [
    ('Apple II', '[NIB]', 'Apple - II [NIB]', 999, 40),
    ('Apple II', '[WOZ]', 'Apple - II [WOZ]', 60, 20),
]

# optical discs: smallest N zips, one sub-folder per game (cue sheets need it)
DISCS = [
    ('Nintendo GameCube', 'Nintendo - GameCube', 2),
    ('Nintendo WII', 'Nintendo - Wii', 1),
    ('Sony Playstation', 'Sony - PlayStation', 3),
    ('Sony Playstation 2', 'Sony - PlayStation 2', 1),
    ('Microsoft XBOX', 'Microsoft - Xbox', 1),
    ('3DO Interactive Multiplayer', '3DO Interactive Multiplayer', 2),
    ('Sega - Saturn', 'Sega - Saturn', 2),
    ('Sega - Mega CD & Sega CD', 'Sega - Mega CD & Sega CD', 2),
    ('Sega - Dreamcast', 'Sega - Dreamcast', 2),
    ('SNK - Neo Geo CD', 'SNK - Neo Geo CD', 2),
    ('NEC - PC Engine CD & TurboGrafx CD', 'NEC - PC Engine CD & TurboGrafx CD', 2),
]


def spread(items, n):
    """n items evenly spaced through a sorted list, so a sample is not just
    the titles starting with 'A'."""
    items = sorted(items)
    if len(items) <= n:
        return items
    step = len(items) / n
    return [items[int(i * step)] for i in range(n)]


def zips_under(folder, sub=None):
    base = WIP / folder
    out = []
    for root, _, files in os.walk(base):
        if sub and sub not in Path(root).relative_to(base).parts:
            continue
        out += [Path(root) / f for f in files if f.lower().endswith('.zip')]
    return out


SEVENZIP = r'C:\Program Files\7-Zip\7z.exe'


def extract(zpath, dest, per_game=False):
    """The sets are RomVault zstd zips (method 93), which Python's zipfile
    cannot read - 7-Zip can. Already-unpacked files are skipped."""
    target = dest / zpath.stem if per_game else dest
    target.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zpath) as zf:
        members = [i for i in zf.infolist() if not i.is_dir()]
    if all((target / Path(i.filename).name).exists() and
           (target / Path(i.filename).name).stat().st_size == i.file_size
           for i in members):
        return 0
    r = subprocess.run([SEVENZIP, 'e', '-y', f'-o{target}', str(zpath)],
                       capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(f'7-Zip failed on {zpath.name}: {r.stderr.strip()[-200:]}')
    return len(members)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--discs', action='store_true')
    ap.add_argument('--only', help='stage only sets whose staged name contains this')
    args = ap.parse_args()
    STAGED.mkdir(exist_ok=True)
    want = lambda name: not args.only or args.only.lower() in name.lower()

    for folder, sub, name, shared_n, extra_n in APPLE_LEVELS:
        if not want(name):
            continue
        dsk = {z.stem: z for z in zips_under(folder, '[DSK]')}
        lvl = {z.stem: z for z in zips_under(folder, sub)}
        shared = spread(set(lvl) & set(dsk), shared_n)
        extra = spread(set(lvl) - set(dsk), extra_n)
        for stem in shared:
            extract(lvl[stem], STAGED / name)
            extract(dsk[stem], STAGED / 'Apple - II [DSK]')
        for stem in extra:
            extract(lvl[stem], STAGED / name)
        print(f'{name:<45} {len(shared)} titles with a [DSK] + {len(extra)} without')
    if args.only:
        return

    for folder, sub, name, n in SETS:
        zs = zips_under(folder, sub)
        pick = spread(zs, n)
        written = sum(extract(z, STAGED / name) for z in pick)
        print(f'{name:<45} {len(pick):>3} of {len(zs):>6} zips  ({written} new files)')

    folder, games = PSN_PKG
    zs = zips_under(folder)
    small = [z for z in zs if z.stat().st_size < 50 * 2**20]
    big = sorted((z for z in zs if z.stat().st_size >= 50 * 2**20),
                 key=lambda z: z.stat().st_size)[:games]
    for z in small + big:
        extract(z, STAGED / folder)
    print(f'{folder}: {len(small)} small + {len(big)} full-size PKGs')

    for a, b, n in PAIRS:
        za = {z.stem: z for z in zips_under(a)}
        zb = {z.stem: z for z in zips_under(b)}
        shared = spread(set(za) & set(zb), n)
        for stem in shared:
            extract(za[stem], STAGED / a)
            extract(zb[stem], STAGED / b)
        print(f'{a} + {b.split("(")[-1][:-1]:<10} {len(shared)} shared titles')

    if args.discs:
        for folder, name, n in DISCS:
            zs = sorted(zips_under(folder), key=lambda z: z.stat().st_size)[:n]
            for z in zs:
                print(f'{name}: unpacking {z.name} ({z.stat().st_size / 2**30:.1f} GiB)',
                      flush=True)
                extract(z, STAGED / name, per_game=True)


if __name__ == '__main__':
    main()

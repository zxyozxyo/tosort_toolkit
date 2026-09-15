"""Conversion test matrix: run every ROM conversion against real files.

For every file in the WIP tree it
  1. checks what the detector says against what the folder says the file is,
  2. runs every conversion the detector offers PLUS every conversion that
     folder is expected to support (so a detection bug cannot hide an engine),
  3. hashes the output and looks it up in the DATs - an encrypted NDS converted
     to decrypted must land in the "(Decrypted)" DAT, not just "look right",
  4. converts back and compares to the original byte for byte,
  5. records whether rom_tools' OWN verification agreed with the DAT/round trip.

Everything goes into SQLite (rom_test_matrix/matrix.db) so reruns after a fix
only redo what is asked for, and REPORT.md is regenerated from the database.

    python tests/conversion_matrix.py                   # run everything not yet run
    python tests/conversion_matrix.py --only "Nintendo 64" --redo
    python tests/conversion_matrix.py --retry-failed
    python tests/conversion_matrix.py --report          # rebuild REPORT.md only
"""
import argparse, collections, hashlib, os, pickle, shutil, sqlite3, sys, tempfile, time, traceback
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import rom_tools as rt

WIP = ROOT / '!WIP'
DATS = WIP / '!DATS!'           # named to sort to the top of the folder
SCRATCH = WIP / '_scratch'
RESULTS = ROOT / 'rom_test_matrix'
DB = RESULTS / 'matrix.db'
LOGS = RESULTS / 'logs'

# folder -> what every file in it IS, and which conversions it should support.
# Each conversion maps to a substring the OUTPUT's DAT name must contain, or
# None where no DAT exists for the output format (CSO, ZSO, CHD, SMD...).
EXPECT = {
    'Nintendo - Nintendo 3DS (Decrypted)': ('3DS', 'decrypted', {
        '3ds:decrypted->encrypted': 'Nintendo 3DS (Encrypted)'}),
    'Nintendo - Nintendo 3DS (Encrypted)': ('3DS', 'encrypted', {
        '3ds:encrypted->decrypted': 'Nintendo 3DS (Decrypted)'}),
    'Nintendo - Nintendo 64 (BigEndian)': ('N64', 'big-endian', {
        'n64:big-endian->byteswapped': 'Nintendo 64 (ByteSwapped)',
        'n64:big-endian->little-endian': None}),
    'Nintendo - Nintendo 64 (ByteSwapped)': ('N64', 'byteswapped', {
        'n64:byteswapped->big-endian': 'Nintendo 64 (BigEndian)',
        'n64:byteswapped->little-endian': None}),
    'Nintendo - Nintendo DS (Decrypted)': ('NDS', 'decrypted', {
        'nds:decrypted->encrypted': 'Nintendo DS (Encrypted)',
        'nds:untrimmed->trimmed': None}),
    'Nintendo - Nintendo DS (Encrypted)': ('NDS', 'encrypted', {
        'nds:encrypted->decrypted': 'Nintendo DS (Decrypted)',
        'nds:untrimmed->trimmed': None}),
    'Nintendo - Nintendo Entertainment System (Headered)': ('NES', 'headered', {
        'nes:headered->headerless': 'Entertainment System (Headerless)'}),
    'Nintendo - Nintendo Entertainment System (Headerless)': ('NES', 'headerless', {
        'nes:headerless->headered': 'Entertainment System (Headered)'}),
    'Nintendo - Super Nintendo Entertainment System': ('SNES', 'headerless', {
        'snes:headerless->headered': None}),
    'Sega - Mega Drive - Genesis': ('MD', 'plain', {
        'md:bin->smd': None}),
    'Sony - PlayStation Portable (PSN) (Decrypted)': ('PSP', None, {
        'psp:iso->cso': None, 'iso:iso->zso': None, 'chd:iso->chd': None}),
    'Sony - PlayStation Portable (PSN) (Encrypted)': ('PSP', 'encrypted', {
        # games and minis come out as ISOs (Minis DAT or main DAT), DLC as files
        'psp:pkg->decrypted': '~Decrypted'}),

    # ── staged from the zipped sets (tests/stage_wip.py) ──
    'Atari - Atari 7800 (A78)': ('A7800', 'headered', {
        'a78:headered->headerless': 'Atari 7800 (BIN)'}),
    'Atari - Atari 7800 (BIN)': ('A7800', None, {}),
    'Atari - Atari Lynx (LNX)': ('LYNX', 'headered', {
        'lnx:headered->headerless': 'Atari Lynx (LYX)'}),
    'Atari - Atari Lynx (LYX)': ('LYNX', 'headerless', {}),
    'Atari - Atari Lynx (BLL)': ('LYNX', None, {}),
    'Atari - Atari 2600': ('A2600', None, {}),
    'Atari - Atari 5200': ('A5200', None, {}),
    'Casio - Loopy (BigEndian)': ('LOOPY', 'big-endian', {
        'loopy:big-endian->little-endian': 'Loopy (LittleEndian)'}),
    'Casio - Loopy (LittleEndian)': ('LOOPY', 'little-endian', {
        'loopy:little-endian->big-endian': 'Loopy (BigEndian)'}),
    'Seta - Aleck64 (BigEndian)': ('N64', 'big-endian', {
        'n64:big-endian->byteswapped': 'Aleck64 (ByteSwapped)'}),
    'Seta - Aleck64 (ByteSwapped)': ('N64', 'byteswapped', {
        'n64:byteswapped->big-endian': 'Aleck64 (BigEndian)'}),
    # '~' = soft: the FDS and QD sets are often DIFFERENT physical disks of the
    # same game (the disk-info dates differ), so a miss is noted, not failed
    'Nintendo - Family Computer Disk System (FDS)': ('FDS', 'headerless', {
        'fds:fds->qd': '~Disk System (QD)',
        'nes:fds-headerless->headered': None}),
    'Nintendo - Family Computer Disk System (QD)': ('FDS', 'qd', {
        'fds:qd->fds': '~Disk System (FDS)'}),
    'NEC - PC Engine - TurboGrafx-16': ('PCE', 'headerless', {
        'pce:headerless->headered': None}),
    'NEC - PC Engine SuperGrafx': ('PCE', 'headerless', {
        'pce:headerless->headered': None}),
    'Commodore - Amiga [ADF]': ('AMIGA', None, {}),
    # loose scene DMS files; their ADFs are in the Non-TOSEC Amiga Warez DAT
    'Amiga DMS': ('AMIGA', None, {'amiga:dms->adf': '~Amiga Warez'}),
    'Commodore - C64 [CRT]': ('C64', None, {}),
    'Commodore - C64 [D64]': ('C64', None, {'c64:d64->files': '~C64'}),
    'Commodore - C64 [P00]': ('C64', None, {'c64:p00->prg': None}),
    'Commodore - C64 [PRG]': ('C64', None, {}),
    'Commodore - C64 [T64]': ('C64', None, {'c64:t64->prg': '~C64'}),
    'Commodore - C64 [D81]': ('C64', None, {'c64:d81->files': '~C64'}),
    'IBM - PC [IMD]': ('PCFLOPPY', None, {'pc:imd->img': '~IBM PC Compatibles'}),
    'IBM - PC [TD0]': ('PCFLOPPY', None, {'pc:td0->img': '~IBM PC Compatibles'}),
    'Sinclair - ZX Spectrum [TAP]': ('ZX', None, {'zx:tap->tzx': '~ZX Spectrum'}),
    'Sinclair - ZX Spectrum [TZX]': ('ZX', None, {'zx:tzx->tap': '~ZX Spectrum'}),
    'Sinclair - ZX Spectrum [Z80]': ('ZX', None, {}),
    'Sinclair - ZX Spectrum [TRD]': ('ZX', None, {}),
    'Apple - II [DSK]': ('APPLE2', None, {'apple:do->po': None}),
    'Apple - II [PO]': ('APPLE2', None, {}),
    'Apple - II [2MG]': ('APPLE2', None, {'apple:2mg->raw': None}),
    'Apple - II [WOZ]': ('APPLE2', None, {'apple:woz->dsk': '~- [DSK]'}),
    'Apple - II [NIB]': ('APPLE2', None, {'apple:nib->dsk': '~- [DSK]'}),
    'Apple - II [EDD]': ('APPLE2', None, {}),
    'Apple - II [HDV]': ('APPLE2', None, {}),
    'Apple - II [A2R]': ('APPLE2', None, {}),

    # ── optical discs (last: big and CPU-heavy) ──
    'Nintendo - GameCube': ('GC', None, {
        'disc:gc:iso->ciso': None, 'disc:gc:iso->rvz': None}),
    'Nintendo - GameCube - NKit RVZ [zstd-19-128k]': ('GC', None, {
        'disc:gc:rvz->iso': 'Nintendo - GameCube', 'disc:gc:rvz->ciso': None}),
    'Nintendo - Wii': ('WII', None, {
        'disc:wii:iso->wbfs': None, 'disc:wii:iso->rvz': None}),
    'Nintendo - Wii - NKit RVZ [zstd-19-128k]': ('WII', None, {
        'disc:wii:rvz->iso': 'Nintendo - Wii', 'disc:wii:rvz->wbfs': '~Wii - NKit WBFS'}),
    'Nintendo - Wii U - WUX': ('WIIU', None, {'wiiu:wux->wud': '~Nintendo - Wii U'}),
    'Sony - PlayStation': ('CD', None, {'chd:cd->chd': None}),
    'Sony - PlayStation 2': (None, None, {'chd:iso->chd': None, 'iso:iso->zso': None}),
    'Microsoft - Xbox': ('XBOX', None, {}),
    'Atari - 8bit [ATR]': ('A8BIT', None, {'a8:atr->xfd': None}),
    'Atari - 8bit [XFD]': ('A8BIT', None, {'a8:xfd->atr': None}),
    'Atari - ST [ST]': ('ATARIST', None, {'st:st->msa': None}),
    'Sinclair - ZX Spectrum [SCL]': ('ZX', None, {'zx:scl->trd': '~ZX Spectrum'}),
    'Atari - Atari Jaguar (J64)': ('JAGUAR', 'headered', {'jag:j64->rom': 'Jaguar (ROM)'}),
    'Atari - Atari Jaguar (ROM)': ('JAGUAR', 'headerless', {'jag:rom->j64': 'Jaguar (J64)'}),
    # retail DSi carts: No-Intro's Encrypted/Decrypted axis is the DS Secure Area
    # (KEY1) only - modcrypt stays encrypted in both dumps (measured)
    'Nintendo - Nintendo DSi (Decrypted)': ('DSi', None, {
        'nds:decrypted->encrypted': 'Nintendo DSi (Encrypted)'}),
    'Nintendo - Nintendo DSi (Encrypted)': ('DSi', None, {
        'nds:encrypted->decrypted': 'Nintendo DSi (Decrypted)'}),
    'NonGoodNES-[UNIF]': ('NES', None, {'nes:unif->nes': '~Entertainment System (Headered)'}),
    '3DO Interactive Multiplayer': ('CD', None, {'chd:cd->chd': None}),
    'Sega - Saturn': ('CD', None, {'chd:cd->chd': None}),
    'Sega - Mega CD & Sega CD': ('CD', None, {'chd:cd->chd': None}),
    'Sega - Dreamcast': ('CD', None, {'chd:cd->chd': None}),
    'SNK - Neo Geo CD': ('CD', None, {'chd:cd->chd': None}),
    'NEC - PC Engine CD & TurboGrafx CD': ('CD', None, {'chd:cd->chd': None}),
    'Sony - PlayStation 3': (None, None, {'ps3:iso->deciso': None}),
    # PS one Classics PKG -> Redump bin (data-only games can match exactly)
    'Sony - PlayStation (PS one Classics) (PSN)': ('PSP', 'encrypted', {
        'psp:pkg->decrypted': '~Sony - PlayStation'}),
    'Sony - PlayStation 3 (PSN)': ('PS3', 'encrypted', {'psp:pkg->decrypted': '~Decrypted'}),
    # Vita PSN package (+ work.bin) -> files with the PFS layer removed
    'Sony - PlayStation Vita (PSN) (Content)': ('VITA', 'encrypted', {
        'vita:pkg->decrypted': '~Vita (NoNpDrm)', 'vita:pkg->nonpdrm': None}),
    'Nintendo 3DS CIA': ('3DS', 'encrypted', {
        'cia:encrypted->decrypted': None,
        'cia:cia->cdn': '~Nintendo 3DS (Digital) (CDN)'}),
}

# folder -> only these extensions are test subjects (a cue's track .bins are
# inputs to it, not separate files to convert)
ONLY_EXTS = {'Sony - PlayStation': ('.cue',), '3DO Interactive Multiplayer': ('.cue',),
             'Sega - Saturn': ('.cue',), 'Sega - Mega CD & Sega CD': ('.cue',),
             'Sega - Dreamcast': ('.cue',), 'SNK - Neo Geo CD': ('.cue',),
             'NEC - PC Engine CD & TurboGrafx CD': ('.cue',),
             # the zips also carry .rap licence keys (16 bytes) - not packages
             'Sony - PlayStation Portable (PSN) (Encrypted)': ('.pkg',),
             'Sony - PlayStation 3 (PSN)': ('.pkg',), 'Sony - PlayStation 3': ('.iso',),
             'Sony - PlayStation (PS one Classics) (PSN)': ('.pkg',),
             'Sony - PlayStation Vita (PSN) (Content)': ('.pkg',)}

# folder -> at most this many files (each Wii U WUX expands to ~23 GB)
FOLDER_LIMIT = {'Nintendo - Wii U - WUX': 1, 'Nintendo - Wii - NKit RVZ [zstd-19-128k]': 1,
                'Nintendo - GameCube - NKit RVZ [zstd-19-128k]': 2}

# ConversionError texts meaning "this file is already in the target state"
NOT_APPLICABLE = ('already trimmed', 'already decrypted', 'already encrypted',
                  'nothing to remove', 'nothing to encrypt or decrypt',
                  'is not in nes20db.xml', 'no header can be proven',
                  'does not use the common Jaguar boot header',
                  'probably copy-protected', 'no known')

# conversions whose registry entry has no inverse, but a sibling undoes them
ROUNDTRIP_VIA = {
    '3ds:encrypted->decrypted': '3ds:decrypted->encrypted',
    'nds:trimmed->untrimmed': 'nds:untrimmed->trimmed',
}

# Small, fast systems first so problems surface in minutes, 3DS last.
ORDER = ['Nintendo 64', 'Aleck64', 'Loopy', 'Mega Drive', 'Super Nintendo',
         'Entertainment System', 'Disk System', 'Atari', 'PC Engine', 'C64',
         'Amiga', 'ZX Spectrum', 'Apple', 'Nintendo DS', 'PlayStation Portable',
         'Nintendo 3DS', 'Sony - PlayStation', 'GameCube', 'Wii', 'Xbox',
         'PlayStation 2', 'Wii U']


# ── storage ───────────────────────────────────────────────────────────────────
SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    rel TEXT PRIMARY KEY, folder TEXT, size INTEGER, mtime REAL,
    crc TEXT, md5 TEXT, sha1 TEXT,
    dat TEXT, dat_game TEXT, dat_how TEXT,
    det_system TEXT, det_format TEXT, det_variant TEXT, det_confidence TEXT,
    det_detail TEXT, det_convs TEXT,
    exp_system TEXT, exp_variant TEXT, detect_ok INTEGER, checked_at TEXT,
    det_code_sha TEXT);
CREATE TABLE IF NOT EXISTS tests (
    rel TEXT, conv TEXT, status TEXT, stage TEXT, error TEXT,
    offered_by_detector INTEGER, expected_by_folder INTEGER,
    fwd_seconds REAL, out_size INTEGER, out_sha1 TEXT,
    out_dat TEXT, out_game TEXT, expected_dat TEXT, dat_ok INTEGER,
    tool_verified INTEGER, tool_detail TEXT,
    roundtrip TEXT, roundtrip_ok INTEGER, notes TEXT,
    code_sha TEXT, run_id TEXT, tested_at TEXT,
    PRIMARY KEY (rel, conv));
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY, started TEXT, finished TEXT, code_sha TEXT,
    args TEXT, tests_run INTEGER);
"""


def db():
    RESULTS.mkdir(exist_ok=True)
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    cols = {r[1] for r in con.execute('PRAGMA table_info(files)')}
    if 'det_code_sha' not in cols:
        con.execute('ALTER TABLE files ADD COLUMN det_code_sha TEXT')
    return con


def code_sha():
    return hashlib.sha1((ROOT / 'rom_tools.py').read_bytes()).hexdigest()[:10]


class Log:
    def __init__(self, run_id):
        LOGS.mkdir(parents=True, exist_ok=True)
        self.path = LOGS / f'run-{run_id}.log'
        self.fh = open(self.path, 'a', encoding='utf-8')

    def __call__(self, msg):
        line = f'[{datetime.now():%H:%M:%S}] {msg}'
        print(line, flush=True)
        self.fh.write(line + '\n')
        self.fh.flush()


# ── DAT index (No-Intro + Redump at the top of DATS; TOSEC is not needed here) ─
# The RomVault DAT tree is ~13 GB (MAME, media, arcade...). Only the sources a
# ROM conversion can be proven against are indexed; TOSEC only for systems the
# matrix has files for.
INDEX_DIRS = ['Tosec/TOSEC/Apple/II', 'Others/Non-TOSEC/Amiga Warez', 'NoIntro', 'ReDump/ReDump', 'ReDumpPlus', 'IBM-NoIntro', 'N-Library',
              'Tosec/TOSEC/Sinclair/ZX Spectrum', 'Tosec/TOSEC/Commodore/C64',
              'Tosec/TOSEC/Atari/8bit', 'Tosec/TOSEC/Atari/ST',
              'Tosec/TOSEC/IBM/PC Compatibles']


def load_index(log):
    dats = sorted(DATS.glob('*.dat'))                # loose DATs, if any
    for sub in INDEX_DIRS:
        dats += sorted((DATS / sub).rglob('*.dat'))
    stamp = [(p.name, p.stat().st_size, p.stat().st_mtime) for p in dats]
    cache = RESULTS / 'dat_index.pickle'
    if cache.exists():
        try:
            saved_stamp, index = pickle.loads(cache.read_bytes())
            if saved_stamp == stamp:
                log(f'DAT index: {len(index):,} ROMs from {len(dats)} DATs (cached)')
                return index
        except Exception:
            pass
    t = time.time()
    index = rt.build_dat_index(dats)
    cache.write_bytes(pickle.dumps((stamp, index)))
    log(f'DAT index: {len(index):,} ROMs from {len(index.dats)} of {len(dats)} '
        f'DATs, built in {time.time() - t:.0f}s')
    return index


def dat_lookup(index, crc, md5, sha1, size):
    for table, key, how in ((index.by_sha1, sha1, 'sha1'),
                            (index.by_md5, md5, 'md5'),
                            (index.by_crc_size, f'{crc}_{size}', 'crc+size')):
        rec = table.get(key)
        if rec:
            return rec, how
    return None, ''


# ── one file ──────────────────────────────────────────────────────────────────
def check_file(con, index, path, folder):
    rel = str(path.relative_to(WIP))
    st = path.stat()
    row = con.execute('SELECT * FROM files WHERE rel=?', (rel,)).fetchone()
    if row and row['size'] == st.st_size and row['mtime'] == st.st_mtime:
        if row['det_code_sha'] == code_sha():
            return row
        # same file, newer rom_tools: keep the hashes, redo only the detection
        crc, md5, sha1, size = row['crc'], row['md5'], row['sha1'], row['size']
    else:
        crc, md5, sha1, size = rt.hash_file(path)
    rec, how = dat_lookup(index, crc, md5, sha1, size)
    det = rt.identify(path)
    exp_system, exp_variant, _ = EXPECT[folder]
    # a prototype with a zeroed NDS Secure Area has no crypto state at all, so
    # "n/a" is the right answer whichever DAT folder it was filed under
    no_axis = det['variant'] is None and 'secure area n/a' in (det['detail'] or '')
    detect_ok = int((exp_system is None or det['system'] == exp_system) and
                    (exp_variant is None or det['variant'] == exp_variant or no_axis))
    con.execute('INSERT OR REPLACE INTO files VALUES '
                '(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (rel, folder, size, st.st_mtime, crc, md5, sha1,
                 rec['dat'] if rec else None, rec['game'] if rec else None, how,
                 det['system'], det['format'], det['variant'], det['confidence'],
                 det['detail'], ','.join(det['conversions']),
                 exp_system, exp_variant, detect_ok, datetime.now().isoformat(),
                 code_sha()))
    con.commit()
    return con.execute('SELECT * FROM files WHERE rel=?', (rel,)).fetchone()


def clean_scratch():
    shutil.rmtree(SCRATCH, ignore_errors=True)
    (SCRATCH / 'tmp').mkdir(parents=True, exist_ok=True)


def run_test(index, frow, conv, expected_dat):
    """Returns a dict of test columns. Never raises."""
    src = WIP / frow['rel']
    spec = rt.CONVERSIONS.get(conv)
    r = {'status': None, 'stage': '', 'error': '', 'notes': [],
         'expected_dat': expected_dat}
    if spec is None:
        return dict(r, status='UNKNOWN', error='not in the conversion registry')
    avail = rt.conversion_status(conv)
    if not avail['available']:
        return dict(r, status='UNAVAILABLE', error=avail['reason'])

    clean_scratch()
    ext = spec.get('ext') or src.suffix
    out = SCRATCH / f'fwd{ext}'
    try:
        t = time.time()
        extra = spec['fn'](src, out, None)
        r['fwd_seconds'] = round(time.time() - t, 2)
        if not out.exists():
            raise RuntimeError('conversion returned but wrote no output file')
    except rt.ConversionError as e:
        # The engine declining a file that is already in the target state is
        # correct behaviour, not a failure (the folder forced the attempt).
        if any(k in str(e) for k in NOT_APPLICABLE):
            return dict(r, status='N/A', stage='convert', error=str(e))
        return dict(r, status='FAIL', stage='convert',
                    error=f'ConversionError: {e}',
                    notes=[traceback.format_exc(limit=3)])
    except Exception as e:
        return dict(r, status='FAIL', stage='convert',
                    error=f'{type(e).__name__}: {e}',
                    notes=[traceback.format_exc(limit=3)])

    crc, md5, sha1, size = rt.hash_file(out)
    r.update(out_size=size, out_sha1=sha1)
    # multi-file outputs (CDN sets, extracted disk files): check every companion
    if isinstance(extra, dict) and extra.get('sidecars'):
        hits = collections.Counter()
        for side in extra['sidecars']:
            srec, _ = dat_lookup(index, *rt.hash_file(side))
            if srec:
                hits[srec['dat']] += 1
        r['notes'].append(f"{sum(hits.values())} of {len(extra['sidecars'])} companion "
                          'files are in a DAT' + (f" ({', '.join(hits)})" if hits else ''))
    rec, _ = dat_lookup(index, crc, md5, sha1, size)
    r['out_dat'] = rec['dat'] if rec else None
    r['out_game'] = rec['game'] if rec else None
    soft = expected_dat is not None and expected_dat.startswith('~')
    if soft:
        want = expected_dat[1:]
        r['expected_dat'] = want
        if rec and want in rec['dat']:
            r['dat_ok'] = 1              # a hit proves it; only a miss is soft
            r['notes'].append(f'output matched {rec["dat"]}')
        else:
            r['notes'].append(f'output not in {want} (soft check: that set is '
                              'often a different dump of the same title)')
    elif expected_dat is not None:
        if not frow['dat']:
            r['notes'].append('source is not in any DAT, so the output cannot '
                              'be expected to match one')
        else:
            r['dat_ok'] = int(bool(rec) and expected_dat in rec['dat'])
    elif rec:
        r['notes'].append(f'output unexpectedly matched {rec["dat"]}')

    # rom_tools' own verdict - what the GUI would have told the user
    try:
        vr = rt._verify(src, out, conv, extra)
        v = vr.get('verified')
        r['tool_verified'] = None if v is None else int(bool(v))
        r['tool_detail'] = vr.get('detail', '')
    except Exception as e:
        vr = {}
        r['tool_verified'] = 0
        r['tool_detail'] = f'verifier crashed: {type(e).__name__}: {e}'

    # independent round trip
    # same file name as the source, in its own folder: multi-file outputs (a
    # cue and its track bins) then carry names that compare 1:1 with the source
    back = SCRATCH / 'back' / src.name
    back.parent.mkdir(parents=True, exist_ok=True)
    try:
        if spec.get('verify_mode') == 'verifier':
            # the engine's own verifier knows whether the conversion is lossless
            # (and proves it) or lossy by nature (TZX timing, CIA re-keying...)
            v = vr.get('verified')
            r['roundtrip'] = f"{vr.get('detail', '')} (tool verifier)"
            r['roundtrip_ok'] = None if v is None else int(bool(v))
        elif vr.get('payload_sha1'):
            r['roundtrip'] = f"{vr['detail']} (tool payload check)"
            r['roundtrip_ok'] = int(bool(vr['verified']))
        elif vr.get('roundtrip_sha1'):
            r['roundtrip'] = 'byte-exact' if vr['roundtrip_sha1'] == frow['sha1'] \
                else 'differs (via tool verify)'
            r['roundtrip_ok'] = int(vr['roundtrip_sha1'] == frow['sha1'])
        else:
            via = ROUNDTRIP_VIA.get(conv) or spec.get('inverse')
            if spec.get('rebuild') and extra is not None:
                spec['rebuild'](out, back, extra, None)
                how = 'rebuild'
            elif via and rt.CONVERSIONS.get(via, {}).get('fn') and \
                    rt.conversion_status(via)['available']:
                rt.CONVERSIONS[via]['fn'](out, back, None)
                how = via
            else:
                how = None
            produced = sorted(p for p in back.parent.iterdir() if p.is_file())
            # decoding a compressed disc container (RVZ/WBFS/CISO -> ISO) cannot
            # be undone byte-for-byte: re-encoding makes a different container.
            # The DAT match on the ISO is the proof there.
            if conv.startswith('disc:') and conv.endswith('->iso') and \
                    not conv.split(':')[2].startswith('iso'):
                how = None
                r['roundtrip'] = 'n/a - a compressed container cannot be re-created'
            elif how is None:
                r['roundtrip'] = 'no way back'
            elif len(produced) > 1:
                # compare every produced file with the source file of that name
                bad_files = [p.name for p in produced
                             if not (src.parent / p.name).is_file()
                             or rt.hash_file(p)[2] != rt.hash_file(src.parent / p.name)[2]]
                r['roundtrip_ok'] = int(not bad_files)
                r['roundtrip'] = (f'all {len(produced)} files byte-exact' if not bad_files
                                  else f'{len(bad_files)} of {len(produced)} files differ: '
                                       + ', '.join(bad_files[:3])) + f' ({how})'
            else:
                bsha = rt.hash_file(back)[2]
                r['roundtrip_ok'] = int(bsha == frow['sha1'])
                r['roundtrip'] = ('byte-exact' if r['roundtrip_ok']
                                  else 'differs') + f' ({how})'
    except Exception as e:
        r['roundtrip'] = f'crashed: {type(e).__name__}: {e}'
        r['roundtrip_ok'] = 0

    # verdict
    bad = []
    if r.get('dat_ok') == 0:
        bad.append('output not in expected DAT'
                   + (f' (matched {r["out_dat"]})' if r['out_dat'] else ''))
    if r.get('roundtrip_ok') == 0:
        bad.append(f'round trip {r["roundtrip"]}')
    truth = r.get('dat_ok') if r.get('dat_ok') is not None else r.get('roundtrip_ok')
    if truth == 1 and r['tool_verified'] == 0:
        r['notes'].append('tool verification said NO but the output is correct '
                          '(false negative - GUI would discard a good file)')
    if truth == 0 and r['tool_verified']:
        r['notes'].append('tool verification said YES but the output is WRONG '
                          '(false positive - dangerous)')
    if bad:
        r.update(status='FAIL', stage='verify', error='; '.join(bad))
    elif r.get('dat_ok') is None and r.get('roundtrip_ok') is None:
        r['status'] = 'UNVERIFIED'
    else:
        r['status'] = 'PASS'
    return r


def save_test(con, frow, conv, r, offered, expected, run_id):
    con.execute(
        'INSERT OR REPLACE INTO tests VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (frow['rel'], conv, r['status'], r.get('stage', ''), r.get('error', ''),
         int(offered), int(expected), r.get('fwd_seconds'), r.get('out_size'),
         r.get('out_sha1'), r.get('out_dat'), r.get('out_game'),
         r.get('expected_dat'), r.get('dat_ok'), r.get('tool_verified'),
         r.get('tool_detail'), r.get('roundtrip'), r.get('roundtrip_ok'),
         '\n'.join(r.get('notes') or []), code_sha(), run_id,
         datetime.now().isoformat()))
    con.commit()


def describe(r):
    if r['status'] in ('UNAVAILABLE', 'N/A', 'UNKNOWN'):
        return r.get('error', '')
    bits = []
    if r.get('out_dat'):
        bits.append(f"out = {r['out_dat']}")
    elif r.get('expected_dat'):
        bits.append('out not in any DAT')
    if r.get('roundtrip'):
        bits.append(f"back {r['roundtrip']}")
    if r.get('error'):
        bits.append(r['error'])
    return ' | '.join(bits)


# ── main loop ─────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--only', help='substring of the folder name')
    ap.add_argument('--conv', help='substring of the conversion id')
    ap.add_argument('--redo', action='store_true', help='rerun tests already recorded')
    ap.add_argument('--retry-failed', action='store_true')
    ap.add_argument('--limit', type=int, help='max files per folder')
    ap.add_argument('--report', action='store_true', help='only rebuild REPORT.md')
    args = ap.parse_args()

    con = db()
    if args.report:
        write_report(con)
        return

    run_id = datetime.now().strftime('%Y%m%d-%H%M%S')
    log = Log(run_id)
    tempfile.tempdir = str(SCRATCH / 'tmp')        # keep verify temp files off C:
    clean_scratch()
    con.execute('INSERT INTO runs VALUES (?,?,?,?,?,?)',
                (run_id, datetime.now().isoformat(), None, code_sha(),
                 ' '.join(sys.argv[1:]), 0))
    con.commit()
    log(f'run {run_id}  rom_tools.py {code_sha()}  args: {" ".join(sys.argv[1:]) or "(none)"}')
    index = load_index(log)
    rt.REFERENCE_DATS = index        # lets 3DS re-encryption find the original key

    roots = [d for d in WIP.iterdir() if d.is_dir() and d != DATS and not d.name.startswith('_')]
    if (WIP / '_staged').is_dir():
        roots += [d for d in (WIP / '_staged').iterdir() if d.is_dir()]
    folders = [d for d in roots if d.name in EXPECT]
    unknown = sorted({d.name for d in roots if d.name not in EXPECT})
    if unknown:
        log(f'WARNING: no expectations defined for folders: {unknown}')
    folders.sort(key=lambda d: max((i for i, k in enumerate(ORDER) if k in d.name), default=99))
    if args.only:
        folders = [d for d in folders if args.only.lower() in d.name.lower()]

    count = 0
    for d in folders:
        files = sorted(p for p in d.rglob('*') if p.is_file()
                       and p.suffix.lower() != '.zip'
                       and (d.name not in ONLY_EXTS or p.suffix.lower() in ONLY_EXTS[d.name]))
        if not files:
            continue
        limit = args.limit or FOLDER_LIMIT.get(d.name)
        if limit:
            files = files[:limit]
        log(f'== {d.name}  ({len(files)} files)')
        _, _, expected = EXPECT[d.name]
        for n, path in enumerate(files, 1):
            frow = check_file(con, index, path, d.name)
            if not frow['detect_ok']:
                log(f'   DETECT  {path.name}: folder says {frow["exp_system"]} '
                    f'{frow["exp_variant"]}, detector says {frow["det_system"]} '
                    f'{frow["det_variant"]} ({frow["det_detail"]})')
            offered = [c for c in (frow['det_convs'] or '').split(',') if c]
            convs = list(dict.fromkeys(list(expected) + offered))
            if args.conv:
                convs = [c for c in convs if args.conv in c]
            else:
                # a conversion no longer offered for this file (detection was
                # fixed) must not linger in the report as a stale failure
                marks = ','.join('?' * len(convs)) or "''"
                con.execute(f'DELETE FROM tests WHERE rel=? AND conv NOT IN ({marks})',
                            (frow['rel'], *convs))
                con.commit()
            for conv in convs:
                prev = con.execute('SELECT status FROM tests WHERE rel=? AND conv=?',
                                   (frow['rel'], conv)).fetchone()
                if prev and not args.redo and not (
                        args.retry_failed and prev['status'] == 'FAIL'):
                    continue
                exp_dat = expected.get(conv)
                r = run_test(index, frow, conv, exp_dat)
                save_test(con, frow, conv, r, conv in offered, conv in expected, run_id)
                count += 1
                if r['status'] != 'UNAVAILABLE' or n <= 1:
                    log(f'   {r["status"]:<11} {conv:<32} [{n}/{len(files)}] '
                        f'{path.name[:60]}  {describe(r)}')
                for note in r.get('notes') or []:
                    if 'Traceback' not in note:
                        log(f'               note: {note}')
    clean_scratch()
    con.execute('UPDATE runs SET finished=?, tests_run=? WHERE run_id=?',
                (datetime.now().isoformat(), count, run_id))
    con.commit()
    log(f'done: {count} test(s) run')
    write_report(con)
    log(f'report: {RESULTS / "REPORT.md"}')


# ── report ────────────────────────────────────────────────────────────────────
def write_report(con):
    q = lambda sql, *a: con.execute(sql, a).fetchall()
    L = []
    L.append('# ROM conversion test matrix\n')
    L.append(f'Generated {datetime.now():%Y-%m-%d %H:%M}  ·  rom_tools.py `{code_sha()}`  ·  '
             f'database `{DB}`\n')
    L.append('**Status meanings:** PASS = output matched the expected DAT and/or '
             'converted back byte-exact · FAIL = crashed, wrong DAT, or round trip '
             'differs · UNVERIFIED = ran, but nothing could prove it right · '
             'UNAVAILABLE = engine not implemented or key/tool missing · '
             'N/A = file already in the target state.\n')

    L.append('## By conversion\n')
    L.append('| Conversion | Tested | PASS | FAIL | UNVERIFIED | UNAVAILABLE / N/A | DAT match | Round trip | Tool verify wrong | Code |')
    L.append('|---|---|---|---|---|---|---|---|---|---|')
    for row in q("""SELECT conv, COUNT(*) n,
            SUM(status='PASS') p, SUM(status='FAIL') f, SUM(status='UNVERIFIED') u,
            SUM(status='UNAVAILABLE') + SUM(status='N/A') na,
            SUM(dat_ok=1) dy, SUM(dat_ok IS NOT NULL) dn,
            SUM(roundtrip_ok=1) ry, SUM(roundtrip_ok IS NOT NULL) rn,
            SUM(notes LIKE '%false%') tv, GROUP_CONCAT(DISTINCT code_sha) cs
            FROM tests GROUP BY conv ORDER BY f DESC, conv"""):
        L.append(f"| `{row['conv']}` | {row['n']} | {row['p']} | {row['f']} | {row['u']} | "
                 f"{row['na']} | {row['dy']}/{row['dn']} | {row['ry']}/{row['rn']} | "
                 f"{row['tv']} | {row['cs']} |")

    L.append('\n## Detection (what the folder says vs what identify() says)\n')
    L.append('| Folder | Files | Detected correctly | In a DAT |')
    L.append('|---|---|---|---|')
    for row in q("""SELECT folder, COUNT(*) n, SUM(detect_ok) ok, SUM(dat IS NOT NULL) d
                    FROM files GROUP BY folder ORDER BY folder"""):
        L.append(f"| {row['folder']} | {row['n']} | {row['ok']} | {row['d']} |")
    wrong = q("""SELECT folder, det_system, det_format, det_variant, det_detail, COUNT(*) n,
                 MIN(rel) example FROM files WHERE detect_ok=0
                 GROUP BY folder, det_system, det_variant, det_detail ORDER BY n DESC""")
    if wrong:
        L.append('\n**Misdetections, grouped:**\n')
        for w in wrong:
            L.append(f"- {w['folder']}: **{w['n']}** detected as {w['det_system']} / "
                     f"{w['det_format']} / {w['det_variant']} — {w['det_detail']}  "
                     f"(e.g. `{Path(w['example']).name}`)")

    fails = q("""SELECT t.*, f.dat src_dat FROM tests t JOIN files f USING(rel)
                 WHERE status='FAIL' ORDER BY conv, rel""")
    L.append(f'\n## Failures ({len(fails)})\n')
    groups = {}
    for t in fails:
        groups.setdefault((t['conv'], t['stage'], t['error'][:160]), []).append(t)
    for (conv, stage, err), ts in groups.items():
        L.append(f"### `{conv}` — {stage} — {len(ts)} file(s)\n")
        L.append(f"> {err}\n")
        for t in ts[:15]:
            extra = f" · tool verify: {'yes' if t['tool_verified'] else 'no'} ({t['tool_detail']})"
            L.append(f"- `{Path(t['rel']).name}` · source DAT: {t['src_dat'] or 'none'}{extra}")
        if len(ts) > 15:
            L.append(f'- … and {len(ts) - 15} more (see the database)')
        L.append('')

    notes = q("""SELECT conv, notes, COUNT(*) n FROM tests WHERE notes != ''
                 AND notes NOT LIKE 'Traceback%' GROUP BY conv, notes ORDER BY n DESC""")
    if notes:
        L.append('## Notes\n')
        for n in notes:
            L.append(f"- `{n['conv']}` ×{n['n']}: {n['notes'].splitlines()[0]}")

    un = q("""SELECT conv, error, COUNT(*) n FROM tests WHERE status='UNAVAILABLE'
              GROUP BY conv, error""")
    if un:
        L.append('\n## Not implemented / unavailable, but files exist to test them\n')
        for u in un:
            L.append(f"- `{u['conv']}` ×{u['n']}: {u['error']}")

    tested = {r['conv'] for r in q('SELECT DISTINCT conv FROM tests')}
    untested = [c for c in rt.CONVERSIONS if c not in tested]
    L.append(f'\n## Registered conversions with NO test files yet ({len(untested)})\n')
    for c in untested:
        st = rt.conversion_status(c)
        L.append(f"- `{c}` — {rt.CONVERSIONS[c]['label']}"
                 + ('' if st['available'] else f"  *(unavailable: {st['reason']})*"))

    runs = q('SELECT * FROM runs ORDER BY started DESC LIMIT 10')
    L.append('\n## Recent runs\n')
    for r in runs:
        L.append(f"- `{r['run_id']}` code `{r['code_sha']}` · {r['tests_run'] or 0} tests · "
                 f"args `{r['args'] or '-'}` · finished {r['finished'] or 'NOT FINISHED'} · "
                 f"log `logs/run-{r['run_id']}.log`")
    (RESULTS / 'REPORT.md').write_text('\n'.join(L) + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()

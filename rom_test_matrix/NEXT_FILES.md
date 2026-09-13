# What to fetch next

## Current asks (2026-09-13, after CIA / PKG / ZX / C64 were built)

- **2–3 PSN PSP *game* PKGs** (not DLC), ideally ones whose decrypted EBOOT/ISO
  is in `Sony - PlayStation Portable (PSN) (Decrypted)`. The six PKGs so far were
  all DLC, so PKG -> EBOOT/ISO (NPUMDIMG + LZRC decompression) is untested.
- **A newer `nes20db.xml`** (the 2021 copy misses 22 titles and has 2 stale headers).
- Optional: a couple of 3DS DLC/update CIAs, and one CIA whose title has a
  `cetk` listed in the CDN DAT (free titles), to check the ticket side too.


Drop each set into `!WIP` as its own folder, named **exactly like the DAT**
(No-Intro / Redump style), e.g. `!WIP\Atari - Atari 7800 (A78)`. The harness
keys its expectations off the folder name, and the DAT folder already covers
every system below, so every output can be proven against a real DAT.

5–10 files per set is plenty. Prefer small titles; for disc systems pick the
smallest discs in the set.

---

## Tier 1 — engines that already exist but have never touched a real file

| Folder to create | What / how many | Tests |
|---|---|---|
| `Nintendo - Super Nintendo Entertainment System` **headered** copies (`.smc`) | 5–10 `.smc` with the 512-byte copier header | `snes:headered->headerless` — output must land in the No-Intro SNES DAT |
| `Nintendo - Family Computer Disk System (FDS)` | 5–10 `.fds` | FDS header strip/add |
| `Nintendo - Family Computer Disk System (QD)` | the SAME games as `.qd` | future FDS ↔ QD conversion (both DATs present) |
| `Atari - Atari 7800 (A78)` **and** `Atari - Atari 7800 (BIN)` | the same 5 games in both | `a78` strip → must match the BIN DAT |
| `Atari - Atari Lynx (LNX)` **and** `(LYX)` | the same 5 games in both | `lnx` strip → must match the LYX DAT |
| `NEC - PC Engine - TurboGrafx-16` | 5–10, plus headered copies if you find any | PCE 512-byte header strip/add |
| `Nintendo - GameCube` (Redump) | 2–3 **small** discs as ISO, plus the NKit RVZ / NKit ISO versions if you can | CISO, RVZ, NKit round trips — the NKit DATs are already in the folder |
| `Nintendo - Wii` (Redump) | 1–2 **small** discs | WBFS, RVZ |
| `Sony - PlayStation` (Redump) | 3 bin/cue, **one multi-track** (audio tracks) | `chd:cd->chd`, `chd:chd->cd` |
| `Sega - Mega CD & Sega CD` or `NEC - PC Engine CD & TurboGrafx CD` | 2 bin/cue multi-track | CHD again, different track layouts |
| `Sony - PlayStation 2` (Redump) | 1–2 small DVD ISOs | `chd:iso->chd` (DVD mode), ZSO |
| `Sony - PlayStation Portable` (Redump, UMD) | 3 ISOs, plus any `.cso` / `.zso` you find | CSO/ZSO *from* compressed sources |
| Apple II | a few `.dsk` / `.do` / `.po` / `.2mg` | DO ↔ PO sector order, 2MG header |
| `Commodore - Amiga` | a few `.adf` and `.dms` | DMS → ADF |

## Tier 2 — new conversions the DATs are pointing at (cheap to build)

These systems have **two DATs for the same games in different formats**, which
is the strongest hint that a conversion is wanted, and it makes a perfect test
oracle.

| DAT pair | Conversion to build | Effort |
|---|---|---|
| `Casio - Loopy (BigEndian)` / `(LittleEndian)` | 16-bit byteswap (reuse the N64 swapper) | trivial |
| `Seta - Aleck64 (BigEndian)` / `(ByteSwapped)` | N64 byte order engine, new system | trivial |
| `Commodore - Commodore 64` / `(Headerless)` | CRT cartridge header strip/add | small |
| `Atari - Atari Jaguar` `(J64)` / `(JAG)` / `(ROM)` | Jaguar container/header conversions | small–medium |
| `Nintendo - Family Computer Disk System` `(FDS)` / `(QD)` | FDS ↔ QD (QD adds per-block CRCs and fixed side sizes) | medium |
| `Nintendo - New Nintendo 3DS (Encrypted)` / `(Decrypted)` | same 3DS engine, New 3DS-only titles (methods 0x0A/0x0B) | test only |
| `Nintendo - Nintendo DSi (Encrypted)` / `(Decrypted)` | DSi modcrypt (AES-CTR, needs DSi keys) | medium |
| `Sony - PlayStation Portable (PSN) (Encrypted)` / `(Decrypted)` | PKG → ISO/EBOOT (the 6 PKGs already in `!WIP` are waiting for this) | medium |
| `Nintendo - Nintendo 3DS (Digital) (CDN)` + `Unofficial ... (Updates and DLC) (Encrypted/Decrypted)` | CIA decrypt (the seed work just done is most of the key handling) | medium |

### 3DS CIA (asked about 2026-09-13)

Keys are already present: `aes_keys.txt` has `slot0x3DKeyX` and `common0-5`.

| Conversion | Byte-exact? | Proven against |
|---|---|---|
| CIA encrypted ↔ decrypted | yes (deterministic AES-CBC title-key layer + the NCCH work already done) | round trip; Unofficial 3DS Updates/DLC (Encrypted)/(Decrypted) DATs |
| CDN files (tmd + cetk + contents) ↔ CIA | yes ("legit CIA") | `Nintendo - Nintendo 3DS (Digital) (CDN)` DAT |
| cartridge .3ds → CIA | no — no valid signatures, update partition dropped | offer, but label lossy |
| CIA → cartridge .3ds | no — cart-only header data cannot be recreated | offer only as lossy, or not at all |

Files wanted: 3–5 eShop game CIAs (one seeded if possible) + one DLC or update
CIA, and the SAME titles as CDN folders (`tmd`, `cetk`, content files), in
`!WIP\Nintendo - Nintendo 3DS (Digital) (CDN)` and a CIA folder beside it.

For **headerless NES → headered** (181 files waiting), the header cannot come from
a DAT — only the hash does. The standard source is the NES 2.0 header database
(`nes20db.xml`). If you can get that file, that conversion becomes buildable
and all 181 `.unh` files become test cases immediately.

## Tier 3 — the old home computers you mentioned

TOSEC in the DAT folder shows which formats dominate. The high-value,
well-documented conversions:

| System | Formats in TOSEC (most common first) | Conversions worth building |
|---|---|---|
| **ZX Spectrum** | TAP 36 · TZX 34 · TRD 30 · SCL 26 · Z80 20 · DSK 16 · SNA 12 | TAP ↔ TZX, TRD ↔ SCL, Z80 ↔ SNA snapshots |
| **Commodore 64** | D64 116 · PRG 82 · T64 62 · TAP 58 · G64 58 · D81 56 · P00 46 · CRT 42 | P00 → PRG (26-byte header), CRT ↔ BIN, T64 → PRG, D64 ↔ G64 |
| **Amstrad CPC** | DSK 10 · CDT 8 · SNA · CPR | DSK standard ↔ extended, CDT ↔ TZX (same container family as Spectrum) |
| Atari 8-bit | ATR 24 · XEX 16 · CAS 12 · ATX 8 · XFD 4 | ATR ↔ XFD (16-byte header) — trivial |
| Atari ST | ST 22 · STX 18 · MSA | MSA ↔ ST (simple RLE) |
| Acorn BBC | UEF 22 · SSD 20 · DSD 14 | UEF ↔ CSW, SSD/DSD |
| MSX | DSK 20 · CAS 18 · ROM 16 | CAS ↔ WAV is lossy; ROM header detection |

Get 5–10 files of each format you care about, from **TOSEC-named** sets so the
TOSEC DATs in the folder can verify the *sources*. Be aware that for these
computer formats a converted file will usually **not** match the other
format's DAT: a TOSEC TZX is its own separate dump (with timing and turbo
blocks), not a conversion of the TAP. So these get proven by lossless round
trips instead - which also means conversions that throw information away
(TZX → TAP drops turbo loaders, G64 → D64 drops copy protection) must be
flagged as lossy in the tool, not silently offered.

BIOS/firmware for any of these: https://emulation.gametechwiki.com/index.php/Emulator_files

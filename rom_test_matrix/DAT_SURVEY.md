# DAT survey — what the RomVault set says is left to build

Surveyed 2026-09-13 from `!WIP\!DATS!` (12,156 files, 13 GB). Only sources a
ROM conversion can be proven against were read in depth: No-Intro (546 DATs),
Redump (117 + ReDumpPlus), IBM-NoIntro, N-Library, MISC/Others, TDC, and TOSEC
(6,301). MAME/arcade/media/music trees were skipped — they are not conversion
targets.

**How to read it:** when one system has DATs for the same games in two formats
(Encrypted/Decrypted, Headered/Headerless, A78/BIN…), that pair is a conversion
the preservation groups consider meaningful, and the second DAT is a ready-made
proof for it.

Noise, ignored: `(T-En)` DATs are fan translations (patched ROMs, not a format
conversion); `(2023)`/`(2024)` are dated snapshots; `(Aftermarket)`/`(Private)`
are subsets of the same format.

---

## Already done and proven

| Pair in the DATs | Conversion |
|---|---|
| NES Headered / Headerless | strip; add via No-Intro DAT headers (181/181) |
| N64 BigEndian / ByteSwapped, Aleck64 same | byte order |
| Loopy BigEndian / LittleEndian | byte order |
| NDS Encrypted / Decrypted | Secure Area KEY1 |
| 3DS Encrypted / Decrypted, Digital CDN, Updates & DLC | NCCH + CIA + CIA↔CDN |
| FDS / QD | FDS↔QD |
| Atari 7800 A78 / BIN, Lynx LNX / LYX | header strip |
| PSP PSN Encrypted / Decrypted, Minis Enc/Dec | PKG → ISO / EDAT / PTF |
| GameCube / Wii NKit RVZ, Wii NKit WBFS, Wii U WUX | disc containers |
| PS1 / PS2 / PSP Redump | CHD, CSO, ZSO |

## Build next — provable against a DAT, no keys needed

| Pair in the DATs | Conversion | Notes |
|---|---|---|
| **Sony - PlayStation (PS one Classics) (PSN)** | PSN PKG → EBOOT / ISO | Same pipeline as PSP games (PSISOIMG instead of NPUMDIMG). |
| **Lost Level Archive - PSP: CSO / EBOOT / ISO** | ISO ↔ EBOOT (unencrypted PBP), DAX/JSO → ISO | A DAT for *each* format, so every direction is provable. |
| **Lost Level Archive - NES: plain / UNF** | UNIF → iNES/NES 2.0 | UNIF board names map to mappers; the headered DAT proves the result. |
| **Lost Level Archive - Amstrad CPC: CDT / CPR / DSK / SNA** | CDT ↔ TZX-style tape, DSK standard ↔ extended | TOSEC CPC has DSK/CDT/SNA/CPR too. |
| **Commodore 64 / (Headerless)** | CRT → raw cartridge | Header and CHIP packets strip; lossy the other way. |
| **Atari Jaguar ABS / COF / J64 / JAG / ROM** | J64/ROM header, JAG/ABS/COF executables | Five No-Intro DATs of the same games. |
| **Apple Macintosh DC42 / raw** | DiskCopy 4.2 ↔ raw image | Simple 84-byte header + checksums. |
| **Nintendo Kiosk Video CF: CardImage / Extracted** | card image → files | Filesystem extraction. |
| **Toshiba Pasopia BIN / WAV** | cassette WAV → BIN | Decoding, one-way. |
| Redump **Saturn, Sega CD, PC Engine CD, 3DO, Neo Geo CD, Dreamcast (GDI)** | CHD round trip | Engine exists (proven on PS1); GDI is the new part. |

## Needs keys or licences (you have some already)

| Pair in the DATs | Conversion | What it needs |
|---|---|---|
| **PS Vita: PSN Content / NoNpDrm / VPK / PSN Decrypted (NoNpDrm, VPK) / PSVgameSD / BlackFinPSV** | PKG → NoNpDrm → VPK | **zRIF — now in `keys\nps\` (3,664 games, 9,979 DLC).** Six DATs = strong proof. |
| **Sony PlayStation Mobile (PSN)** | PKG → decrypted | zRIF — 244 titles in `keys\nps\`. |
| **PS3: PSN Content / DLC / Themes / Avatars / Updates / PSN Decrypted** | PKG → decrypted, EDAT/SDAT | **RAP — now in `keys\nps\`.** Decrypted DAT is Unofficial/2018. |
| **PS3 Redump** | disc ISO → decrypted ISO | per-disc `.dkey` (Redump publishes them). |
| **Nintendo DSi: Encrypted / Decrypted, Digital CDN Encrypted / Decrypted** | modcrypt, CDN TAD decrypt | DSi keys — check your `aes_keys.txt`/dumps. |
| **New Nintendo 3DS Encrypted / Decrypted** | same NCCH engine | Test files only — keys already work. |
| **Wii: Digital CDN / Digital WAD** | WAD ↔ CDN (like CIA ↔ CDN) | Wii common key. |
| **Wii U: Digital / Digital CDN** | CDN → decrypted game files | Wii U common key. |
| **iQue: CDN / Decrypted** | content decrypt | iQue common key. |
| **Panic Playdate: Catalog/Seasons Encrypted / Decrypted** | .pdx decrypt | Key situation unclear — research first. |

## Old computers — TOSEC formats worth converting (most common first)

| Machine | Formats in TOSEC | Worth doing |
|---|---|---|
| C64 | D64, T64, PRG, P00, G64, NIB, TAP, CRT, D81 | **done:** T64→PRG, D64→files, P00→PRG. Next: D81 files, G64/NIB → D64 (lossy), CRT → BIN |
| ZX Spectrum | TAP, TZX, TRD, SCL, Z80, SNA, DSK | **done:** TAP↔TZX. Next: TRD↔SCL, Z80↔SNA, +3 DSK |
| Apple II / IIGS | DSK, WOZ, A2R, 2MG, NIB, PO | **done:** DO↔PO, 2MG. Next: WOZ/NIB → DSK (lossy) |
| Atari 8-bit | ATR, XEX, CAS, ATX, XFD | ATR↔XFD (16-byte header) — trivial |
| Atari ST | ST, STX, IPF, MSA | MSA↔ST (RLE) |
| Acorn BBC | SSD, DSD, UEF | UEF ↔ tape, SSD/DSD |
| Amstrad CPC | DSK, CDT, SNA, CPR | DSK std↔ext, CDT |
| Thomson TO8/MO5 | FD, SAP, K7 | SAP ↔ FD |
| TRS-80 | DSK, DMK, CAS | DMK ↔ JV1/JV3 |
| IBM PC | IMG, IMD, TD0 | TD0/IMD → IMG (Teledisk decompress) |

## Not worth building (for this tool)

Android APK/XAPK/AAB, PC digital storefront sets, Xbox 360/One digital, music
(M4A/tracks), magazine scans, videos, MAME/arcade — these are file collections,
not ROM formats with a conversion between them.

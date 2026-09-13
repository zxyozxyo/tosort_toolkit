# ROM conversion test matrix

Generated 2026-09-13 20:15  ·  rom_tools.py `38f3f6ad14`  ·  database `B:\User\ClaudeCode\tosort_toolkit\rom_test_matrix\matrix.db`

**Status meanings:** PASS = output matched the expected DAT and/or converted back byte-exact · FAIL = crashed, wrong DAT, or round trip differs · UNVERIFIED = ran, but nothing could prove it right · UNAVAILABLE = engine not implemented or key/tool missing · N/A = file already in the target state.

## By conversion

| Conversion | Tested | PASS | FAIL | UNVERIFIED | UNAVAILABLE / N/A | DAT match | Round trip | Tool verify wrong | Code |
|---|---|---|---|---|---|---|---|---|---|
| `chd:cd->chd` | 15 | 14 | 1 | 0 | 0 | None/0 | 14/15 | 1 | 2ed1e069c5,5e1db6994c,e6cef9b724,ba1811571c,d02dd3fb76 |
| `iso:iso->zso` | 10 | 9 | 1 | 0 | 0 | None/0 | 9/9 | 0 | bf7f5382c2,d15202fe81,83de16a730 |
| `loopy:big-endian->little-endian` | 13 | 12 | 1 | 0 | 0 | 11/12 | 13/13 | 1 | d15202fe81 |
| `loopy:little-endian->big-endian` | 12 | 11 | 1 | 0 | 0 | 11/12 | 12/12 | 1 | d15202fe81 |
| `3ds:decrypted->encrypted` | 9 | 9 | 0 | 0 | 0 | 9/9 | 9/9 | 0 | bf7f5382c2 |
| `3ds:encrypted->decrypted` | 10 | 10 | 0 | 0 | 0 | 10/10 | 10/10 | 0 | bf7f5382c2 |
| `a78:headered->headerless` | 8 | 8 | 0 | 0 | 0 | 8/8 | 8/8 | 0 | d15202fe81 |
| `a8:atr->xfd` | 10 | 10 | 0 | 0 | 0 | None/0 | 10/10 | 0 | 7ea1b35567 |
| `a8:xfd->atr` | 1 | 1 | 0 | 0 | 0 | None/0 | 1/1 | 0 | 7ea1b35567 |
| `apple:2mg->raw` | 10 | 10 | 0 | 0 | 0 | None/0 | 10/10 | 0 | d15202fe81 |
| `apple:do->po` | 10 | 10 | 0 | 0 | 0 | None/0 | 10/10 | 0 | d15202fe81 |
| `c64:d64->files` | 10 | 7 | 0 | 3 | 0 | 7/7 | None/0 | 0 | 1ee9692bcd |
| `c64:d81->files` | 10 | 1 | 0 | 9 | 0 | 1/1 | None/0 | 0 | e6cef9b724 |
| `c64:p00->prg` | 10 | 10 | 0 | 0 | 0 | None/0 | 10/10 | 0 | d15202fe81 |
| `c64:t64->prg` | 10 | 8 | 0 | 2 | 0 | 8/8 | None/0 | 0 | 1ee9692bcd |
| `chd:iso->chd` | 11 | 11 | 0 | 0 | 0 | None/0 | 11/11 | 0 | bf7f5382c2,d15202fe81,83de16a730,38f3f6ad14 |
| `cia:cia->cdn` | 13 | 13 | 0 | 0 | 0 | 13/13 | 13/13 | 0 | e570be8db0 |
| `cia:encrypted->decrypted` | 13 | 13 | 0 | 0 | 0 | None/0 | 13/13 | 0 | e570be8db0 |
| `disc:gc:iso->ciso` | 2 | 2 | 0 | 0 | 0 | None/0 | 2/2 | 0 | 2ed1e069c5 |
| `disc:gc:iso->rvz` | 2 | 2 | 0 | 0 | 0 | None/0 | 2/2 | 0 | 2ed1e069c5 |
| `disc:gc:rvz->ciso` | 2 | 2 | 0 | 0 | 0 | None/0 | 2/2 | 0 | 2ed1e069c5 |
| `disc:gc:rvz->iso` | 2 | 2 | 0 | 0 | 0 | 2/2 | None/0 | 0 | 2ed1e069c5 |
| `disc:wii:iso->rvz` | 1 | 1 | 0 | 0 | 0 | None/0 | 1/1 | 0 | 2ed1e069c5 |
| `disc:wii:iso->wbfs` | 1 | 1 | 0 | 0 | 0 | None/0 | 1/1 | 0 | 2ed1e069c5 |
| `disc:wii:rvz->iso` | 1 | 1 | 0 | 0 | 0 | 1/1 | None/0 | 0 | 2ed1e069c5 |
| `disc:wii:rvz->wbfs` | 1 | 1 | 0 | 0 | 0 | None/0 | 1/1 | 0 | 2ed1e069c5 |
| `fds:fds->qd` | 20 | 20 | 0 | 0 | 0 | 7/7 | 20/20 | 0 | 2ed1e069c5 |
| `fds:qd->fds` | 20 | 20 | 0 | 0 | 0 | 7/7 | 20/20 | 0 | 2ed1e069c5 |
| `jag:j64->rom` | 12 | 12 | 0 | 0 | 0 | 12/12 | 12/12 | 0 | 7ea1b35567 |
| `jag:rom->j64` | 12 | 2 | 0 | 0 | 10 | 2/2 | 2/2 | 0 | 7ea1b35567 |
| `lnx:headered->headerless` | 18 | 18 | 0 | 0 | 0 | 18/18 | 18/18 | 0 | d15202fe81 |
| `md:bin->smd` | 13 | 13 | 0 | 0 | 0 | None/0 | 13/13 | 0 | bf7f5382c2 |
| `n64:big-endian->byteswapped` | 24 | 24 | 0 | 0 | 0 | 24/24 | 24/24 | 0 | bf7f5382c2,d15202fe81 |
| `n64:big-endian->little-endian` | 24 | 24 | 0 | 0 | 0 | None/0 | 24/24 | 0 | bf7f5382c2,d15202fe81 |
| `n64:byteswapped->big-endian` | 24 | 24 | 0 | 0 | 0 | 24/24 | 24/24 | 0 | bf7f5382c2,d15202fe81 |
| `n64:byteswapped->little-endian` | 24 | 24 | 0 | 0 | 0 | None/0 | 24/24 | 0 | bf7f5382c2,d15202fe81 |
| `nds:decrypted->encrypted` | 25 | 19 | 0 | 0 | 6 | 19/19 | 19/19 | 0 | bf7f5382c2,5e1db6994c |
| `nds:encrypted->decrypted` | 27 | 9 | 0 | 0 | 18 | 9/9 | 9/9 | 0 | bf7f5382c2,5e1db6994c |
| `nds:trimmed->untrimmed` | 2 | 2 | 0 | 0 | 0 | None/0 | 2/2 | 0 | bf7f5382c2 |
| `nds:untrimmed->trimmed` | 52 | 50 | 0 | 0 | 2 | None/0 | 50/50 | 0 | bf7f5382c2,5e1db6994c |
| `nes:fds-headerless->headered` | 20 | 20 | 0 | 0 | 0 | None/0 | 20/20 | 0 | 2ed1e069c5 |
| `nes:headered->headerless` | 1274 | 1274 | 0 | 0 | 0 | 1274/1274 | 1274/1274 | 0 | bf7f5382c2 |
| `nes:headerless->headered` | 181 | 181 | 0 | 0 | 0 | 181/181 | 181/181 | 0 | 8d5fac9c53 |
| `nes:unif->nes` | 161 | 26 | 0 | 72 | 63 | 26/26 | None/0 | 0 | 7ea1b35567 |
| `pc:imd->img` | 6 | 1 | 0 | 1 | 4 | 1/1 | None/0 | 0 | e6cef9b724 |
| `pc:td0->img` | 18 | 6 | 0 | 3 | 9 | 6/6 | None/0 | 0 | e6cef9b724 |
| `pce:headerless->headered` | 25 | 25 | 0 | 0 | 0 | None/0 | 25/25 | 0 | d15202fe81 |
| `ps3:iso->deciso` | 2 | 2 | 0 | 0 | 0 | None/0 | 2/2 | 0 | 83de16a730 |
| `psp:iso->cso` | 8 | 8 | 0 | 0 | 0 | None/0 | 8/8 | 0 | bf7f5382c2 |
| `psp:pkg->decrypted` | 44 | 43 | 0 | 1 | 0 | 43/43 | None/0 | 0 | 4e8421a87a,7aa604fe3a,46fedd0eb9,041567de78 |
| `snes:headerless->headered` | 34 | 34 | 0 | 0 | 0 | None/0 | 34/34 | 0 | bf7f5382c2 |
| `st:st->msa` | 10 | 10 | 0 | 0 | 0 | None/0 | 10/10 | 0 | dadf150eb3 |
| `vita:pkg->decrypted` | 1 | 1 | 0 | 0 | 0 | 1/1 | None/0 | 0 | 38f3f6ad14 |
| `vita:pkg->nonpdrm` | 1 | 0 | 0 | 1 | 0 | None/0 | None/0 | 0 | 38f3f6ad14 |
| `wiiu:wux->wud` | 1 | 1 | 0 | 0 | 0 | 1/1 | None/0 | 0 | 2ed1e069c5 |
| `zx:scl->trd` | 10 | 10 | 0 | 0 | 0 | None/0 | 10/10 | 0 | 7ea1b35567 |
| `zx:tap->tzx` | 10 | 10 | 0 | 0 | 0 | None/0 | 10/10 | 0 | 1ee9692bcd |
| `zx:tzx->tap` | 10 | 4 | 0 | 6 | 0 | 4/4 | None/0 | 0 | 1ee9692bcd |

## Detection (what the folder says vs what identify() says)

| Folder | Files | Detected correctly | In a DAT |
|---|---|---|---|
| 3DO Interactive Multiplayer | 2 | 2 | 2 |
| Apple - II [2MG] | 10 | 10 | 0 |
| Apple - II [A2R] | 2 | 2 | 1 |
| Apple - II [DSK] | 10 | 10 | 0 |
| Apple - II [EDD] | 3 | 3 | 0 |
| Apple - II [HDV] | 3 | 3 | 0 |
| Apple - II [NIB] | 5 | 5 | 0 |
| Apple - II [PO] | 10 | 9 | 0 |
| Apple - II [WOZ] | 5 | 5 | 0 |
| Atari - 8bit [ATR] | 10 | 10 | 10 |
| Atari - 8bit [XFD] | 1 | 1 | 1 |
| Atari - Atari 2600 | 10 | 10 | 10 |
| Atari - Atari 5200 | 10 | 10 | 10 |
| Atari - Atari 7800 (A78) | 8 | 8 | 8 |
| Atari - Atari 7800 (BIN) | 8 | 0 | 8 |
| Atari - Atari Jaguar (J64) | 12 | 12 | 12 |
| Atari - Atari Jaguar (ROM) | 12 | 12 | 12 |
| Atari - Atari Lynx (BLL) | 3 | 3 | 3 |
| Atari - Atari Lynx (LNX) | 18 | 18 | 18 |
| Atari - Atari Lynx (LYX) | 18 | 18 | 18 |
| Atari - ST [ST] | 10 | 10 | 10 |
| Casio - Loopy (BigEndian) | 12 | 10 | 12 |
| Casio - Loopy (LittleEndian) | 12 | 9 | 12 |
| Commodore - Amiga [ADF] | 15 | 15 | 0 |
| Commodore - C64 [CRT] | 10 | 10 | 10 |
| Commodore - C64 [D64] | 10 | 10 | 10 |
| Commodore - C64 [D81] | 10 | 10 | 10 |
| Commodore - C64 [P00] | 10 | 10 | 10 |
| Commodore - C64 [PRG] | 10 | 10 | 10 |
| Commodore - C64 [T64] | 10 | 10 | 10 |
| IBM - PC [IMD] | 6 | 6 | 6 |
| IBM - PC [TD0] | 18 | 18 | 18 |
| Microsoft - Xbox | 1 | 1 | 1 |
| NEC - PC Engine - TurboGrafx-16 | 20 | 20 | 20 |
| NEC - PC Engine CD & TurboGrafx CD | 2 | 2 | 2 |
| NEC - PC Engine SuperGrafx | 5 | 5 | 5 |
| Nintendo - Family Computer Disk System (FDS) | 20 | 20 | 20 |
| Nintendo - Family Computer Disk System (QD) | 20 | 20 | 20 |
| Nintendo - GameCube | 2 | 2 | 2 |
| Nintendo - GameCube - NKit RVZ [zstd-19-128k] | 2 | 2 | 2 |
| Nintendo - Nintendo 3DS (Decrypted) | 9 | 9 | 9 |
| Nintendo - Nintendo 3DS (Encrypted) | 10 | 10 | 10 |
| Nintendo - Nintendo 64 (BigEndian) | 10 | 10 | 10 |
| Nintendo - Nintendo 64 (ByteSwapped) | 10 | 10 | 10 |
| Nintendo - Nintendo DS (Decrypted) | 10 | 10 | 10 |
| Nintendo - Nintendo DS (Encrypted) | 10 | 10 | 10 |
| Nintendo - Nintendo DSi (Decrypted) | 15 | 15 | 15 |
| Nintendo - Nintendo DSi (Encrypted) | 17 | 17 | 17 |
| Nintendo - Nintendo Entertainment System (Headered) | 1274 | 1274 | 1274 |
| Nintendo - Nintendo Entertainment System (Headerless) | 181 | 181 | 181 |
| Nintendo - Super Nintendo Entertainment System | 34 | 34 | 34 |
| Nintendo - Wii | 1 | 1 | 1 |
| Nintendo - Wii - NKit RVZ [zstd-19-128k] | 1 | 1 | 1 |
| Nintendo - Wii U - WUX | 1 | 1 | 1 |
| Nintendo 3DS CIA | 13 | 13 | 0 |
| NonGoodNES-[UNIF] | 161 | 161 | 0 |
| SNK - Neo Geo CD | 2 | 2 | 2 |
| Sega - Dreamcast | 2 | 2 | 2 |
| Sega - Mega CD & Sega CD | 2 | 2 | 2 |
| Sega - Mega Drive - Genesis | 13 | 13 | 13 |
| Sega - Saturn | 2 | 2 | 2 |
| Seta - Aleck64 (BigEndian) | 14 | 14 | 14 |
| Seta - Aleck64 (ByteSwapped) | 14 | 14 | 14 |
| Sinclair - ZX Spectrum [SCL] | 10 | 10 | 10 |
| Sinclair - ZX Spectrum [TAP] | 10 | 10 | 10 |
| Sinclair - ZX Spectrum [TRD] | 10 | 10 | 10 |
| Sinclair - ZX Spectrum [TZX] | 10 | 10 | 10 |
| Sinclair - ZX Spectrum [Z80] | 10 | 10 | 10 |
| Sony - PlayStation | 3 | 3 | 3 |
| Sony - PlayStation (PS one Classics) (PSN) | 5 | 5 | 5 |
| Sony - PlayStation 2 | 1 | 1 | 1 |
| Sony - PlayStation 3 | 2 | 2 | 2 |
| Sony - PlayStation 3 (PSN) | 3 | 3 | 3 |
| Sony - PlayStation Portable (PSN) (Decrypted) | 8 | 8 | 8 |
| Sony - PlayStation Portable (PSN) (Encrypted) | 36 | 36 | 36 |
| Sony - PlayStation Vita (PSN) (Content) | 1 | 1 | 1 |

**Misdetections, grouped:**

- Atari - Atari 7800 (BIN): **8** detected as UNKNOWN / unknown / None — no known signature  (e.g. `Commando (USA) (Beta) (1988-04-29).bin`)
- Casio - Loopy (BigEndian): **2** detected as UNKNOWN / unknown / None — no known signature  (e.g. `[BIOS] Internal Thermal Printer (Japan).bin`)
- Casio - Loopy (LittleEndian): **2** detected as UNKNOWN / unknown / None — no known signature  (e.g. `[BIOS] Internal Thermal Printer (Japan).bin`)
- Apple - II [PO]: **1** detected as UNKNOWN / unknown / None — no known signature  (e.g. `8-bit Apple II Game Compilation - 32 Games in Total (1991)(cvxmelody)[b].po`)
- Casio - Loopy (LittleEndian): **1** detected as LOOPY / BIN / big-endian — Casio Loopy cartridge, big-endian  (e.g. `Chakrakun no Omajinai Paradise (Japan).bin`)

## Failures (4)

### `chd:cd->chd` — verify — 1 file(s)

> round trip 1 of 2 files differ: Makaroni Hourensou Interactive (Japan).cue (chd:chd->cd)

- `Makaroni Hourensou Interactive (Japan).cue` · source DAT: 3DO Interactive Multiplayer · tool verify: yes (chdman verify passed)

### `iso:iso->zso` — convert — 1 file(s)

> error: 'I' format requires 0 <= number <= 4294967295

- `Persona 4 Arena Ultimax (Europe).iso` · source DAT: Sony - PlayStation 3 · tool verify: no (None)

### `loopy:big-endian->little-endian` — verify — 1 file(s)

> output not in expected DAT

- `Chakrakun no Omajinai Paradise (Japan).bin` · source DAT: Casio - Loopy (LittleEndian) · tool verify: yes (byte-exact)

### `loopy:little-endian->big-endian` — verify — 1 file(s)

> output not in expected DAT

- `Chakrakun no Omajinai Paradise (Japan).bin` · source DAT: Casio - Loopy (LittleEndian) · tool verify: yes (byte-exact)

## Notes

- `nes:unif->nes` ×72: output not in Entertainment System (Headered) (soft check: that set is often a different dump of the same title)
- `nes:unif->nes` ×18: output matched Nintendo - Nintendo Entertainment System (Headered)
- `fds:fds->qd` ×13: output not in Disk System (QD) (soft check: that set is often a different dump of the same title)
- `fds:qd->fds` ×13: output not in Disk System (FDS) (soft check: that set is often a different dump of the same title)
- `cia:cia->cdn` ×12: 3 of 4 companion files are in a DAT (Nintendo - Nintendo 3DS (Digital) (CDN))
- `zx:scl->trd` ×10: output not in ZX Spectrum (soft check: that set is often a different dump of the same title)
- `zx:tap->tzx` ×10: output not in ZX Spectrum (soft check: that set is often a different dump of the same title)
- `psp:pkg->decrypted` ×9: 1 of 8 companion files are in a DAT (Unofficial - Sony - PlayStation 3 (PSN) (Decrypted))
- `c64:t64->prg` ×8: output matched Commodore C64 - Games - Arcade - [PRG]
- `psp:pkg->decrypted` ×8: 1 of 7 companion files are in a DAT (Unofficial - Sony - PlayStation 3 (PSN) (Decrypted))
- `c64:d64->files` ×7: output matched Commodore C64 - Games - Arcade - [PRG]
- `fds:fds->qd` ×7: output matched Nintendo - Family Computer Disk System (QD)
- `fds:qd->fds` ×7: output matched Nintendo - Family Computer Disk System (FDS)
- `pc:td0->img` ×6: output matched IBM PC Compatibles - Games - [IMG]
- `zx:tzx->tap` ×6: output not in ZX Spectrum (soft check: that set is often a different dump of the same title)
- `nes:unif->nes` ×5: output matched Nintendo - Nintendo Entertainment System (Headered) (Aftermarket)
- `psp:pkg->decrypted` ×4: 1 of 1 companion files are in a DAT (Sony - PlayStation Portable (PSN) (Decrypted))
- `psp:pkg->decrypted` ×4: 1 of 13 companion files are in a DAT (Unofficial - Sony - PlayStation 3 (PSN) (Decrypted))
- `zx:tzx->tap` ×4: output matched Sinclair ZX Spectrum - Games - [TAP]
- `c64:d64->files` ×3: output not in C64 (soft check: that set is often a different dump of the same title)
- `c64:d81->files` ×3: 0 of 2 companion files are in a DAT
- `nes:unif->nes` ×3: output matched Nintendo - Nintendo Entertainment System (Headered) (Private)
- `pc:td0->img` ×3: output not in IBM PC Compatibles (soft check: that set is often a different dump of the same title)
- `psp:pkg->decrypted` ×3: 1 of 8 companion files are in a DAT (Unofficial - Sony - PlayStation 3 (PSN) (Decrypted))
- `psp:pkg->decrypted` ×3: 4 of 9 companion files are in a DAT (Unofficial - Sony - PlayStation 3 (PSN) (Decrypted), Sony - PlayStation Portable (PSN) (Decrypted))
- `c64:d81->files` ×2: 0 of 41 companion files are in a DAT
- `c64:t64->prg` ×2: output not in C64 (soft check: that set is often a different dump of the same title)
- `psp:pkg->decrypted` ×2: 1 of 7 companion files are in a DAT (Unofficial - Sony - PlayStation 3 (PSN) (Decrypted))
- `psp:pkg->decrypted` ×2: 3 of 8 companion files are in a DAT (Unofficial - Sony - PlayStation 3 (PSN) (Decrypted), Sony - PlayStation Portable (PSN) (Decrypted))
- `c64:d81->files` ×1: 0 of 12 companion files are in a DAT
- `c64:d81->files` ×1: 0 of 17 companion files are in a DAT
- `c64:d81->files` ×1: 0 of 18 companion files are in a DAT
- `c64:d81->files` ×1: 0 of 47 companion files are in a DAT
- `c64:d81->files` ×1: output matched Commodore C64 - Games - Arcade - [PRG]
- `chd:cd->chd` ×1: tool verification said YES but the output is WRONG (false positive - dangerous)
- `cia:cia->cdn` ×1: 4 of 5 companion files are in a DAT (Nintendo - Nintendo 3DS (Digital) (CDN))
- `disc:wii:iso->wbfs` ×1: output unexpectedly matched Nintendo - Wii - NKit WBFS [lossless]
- `disc:wii:rvz->wbfs` ×1: output matched Nintendo - Wii - NKit WBFS [lossless]
- `loopy:big-endian->little-endian` ×1: tool verification said YES but the output is WRONG (false positive - dangerous)
- `loopy:little-endian->big-endian` ×1: tool verification said YES but the output is WRONG (false positive - dangerous)
- `pc:imd->img` ×1: output matched IBM PC Compatibles - Games - [IMG]
- `pc:imd->img` ×1: output not in IBM PC Compatibles (soft check: that set is often a different dump of the same title)
- `psp:pkg->decrypted` ×1: 1 of 13 companion files are in a DAT (Unofficial - Sony - PlayStation 3 (PSN) (Decrypted))
- `psp:pkg->decrypted` ×1: 1 of 14 companion files are in a DAT (Unofficial - Sony - PlayStation 3 (PSN) (Decrypted))
- `psp:pkg->decrypted` ×1: 1 of 17 companion files are in a DAT (Unofficial - Sony - PlayStation 3 (PSN) (Decrypted))
- `psp:pkg->decrypted` ×1: 1 of 17 companion files are in a DAT (Unofficial - Sony - PlayStation 3 (PSN) (Decrypted))
- `psp:pkg->decrypted` ×1: 1 of 307 companion files are in a DAT (Unofficial - Sony - PlayStation 3 (PSN) (Decrypted))
- `psp:pkg->decrypted` ×1: 1 of 34 companion files are in a DAT (Unofficial - Sony - PlayStation 3 (PSN) (Decrypted))
- `psp:pkg->decrypted` ×1: 168 of 173 companion files are in a DAT (Unofficial - Sony - PlayStation 3 (PSN) (Decrypted), Sony - PlayStation Portable (PSN) (Decrypted))
- `psp:pkg->decrypted` ×1: 72 of 77 companion files are in a DAT (Unofficial - Sony - PlayStation 3 (PSN) (Decrypted), Sony - PlayStation Portable (PSN) (Decrypted))
- `psp:pkg->decrypted` ×1: 8 of 13 companion files are in a DAT (Unofficial - Sony - PlayStation 3 (PSN) (Decrypted), Unofficial - Sony - PlayStation Portable (PSN) (Decrypted))
- `vita:pkg->decrypted` ×1: 1261 of 1261 companion files are in a DAT (Unofficial - Sony - PlayStation Vita (NoNpDrm), IBM - PC and Compatibles (Tiger Electronics - Net Jet))
- `vita:pkg->nonpdrm` ×1: 5 of 1270 companion files are in a DAT (Unofficial - Sony - PlayStation Vita (NoNpDrm), IBM - PC and Compatibles (Tiger Electronics - Net Jet), Sony - PlayStation Vita (PSN) (Content))
- `wiiu:wux->wud` ×1: output matched Nintendo - Wii U

## Registered conversions with NO test files yet (35)

- `n64:little-endian->big-endian` — N64: little-endian -> big-endian
- `n64:little-endian->byteswapped` — N64: little-endian -> byteswapped
- `psp:cso->iso` — PSP: CSO -> ISO (decompress)
- `psp:pbp->iso` — PSP: EBOOT.PBP -> ISO  *(unavailable: PSN content decryption is not implemented yet.)*
- `psp:dax->iso` — PSP: DAX -> ISO  *(unavailable: PSN content decryption is not implemented yet.)*
- `psp:jso->iso` — PSP: JSO -> ISO  *(unavailable: PSN content decryption is not implemented yet.)*
- `disc:gc:ciso->iso` — GC: CISO -> ISO
- `disc:gc:ciso->rvz` — GC: CISO -> RVZ
- `disc:gc:rvz->rvz` — GC: recompress to zstd-19-128k
- `disc:wii:wbfs->iso` — WII: WBFS -> ISO
- `disc:wii:wbfs->rvz` — WII: WBFS -> RVZ
- `disc:wii:rvz->rvz` — WII: recompress to zstd-19-128k
- `chd:chd->cd` — CHD: CHD -> CUE/BIN (extract)
- `chd:chd->dvd` — CHD: CHD -> ISO (DVD)
- `chd:chd->raw` — CHD: CHD -> ISO (extract raw)
- `snes:headered->headerless` — SNES: strip header
- `a78:headerless->headered` — Atari 7800: add header  *(unavailable: The original header bytes are needed; they cannot be derived from the ROM body. Strip and re-add in one run, or supply a DAT match.)*
- `lnx:headerless->headered` — Atari Lynx: add header  *(unavailable: The original header bytes are needed; they cannot be derived from the ROM body. Strip and re-add in one run, or supply a DAT match.)*
- `nes:fds-headered->headerless` — FDS: strip header
- `md:smd->bin` — Mega Drive: SMD -> BIN (deinterleave)
- `iso:zso->iso` — PSP/PS2: ZSO -> ISO
- `wiiu:wud->wux` — Wii U: WUD -> WUX (compress)
- `pce:headered->headerless` — PC Engine: strip header
- `apple:raw->2mg` — Apple II 2IMG: add header  *(unavailable: The original header bytes are needed; they cannot be derived from the ROM body. Strip and re-add in one run, or supply a DAT match.)*
- `apple:po->do` — Apple II: ProDOS order -> DOS order
- `amiga:dms->adf` — Amiga: DMS -> ADF (decompress)
- `cia:decrypted->encrypted` — 3DS CIA: encrypt (keys matched to a DAT when loaded)
- `cia:cdn->cia` — 3DS CDN files (tmd + cetk + contents) -> CIA
- `psp:edat->decrypted` — PSP: EDAT -> decrypted payload
- `st:msa->st` — Atari ST: MSA -> ST
- `zx:trd->scl` — ZX Spectrum: TRD -> SCL (files only; lossy for unused sectors)
- `ps3:deciso->iso` — PS3: decrypted ISO -> Redump ISO (disc key)
- `apple:nib->dsk` — Apple II: NIB nibble image -> DSK sectors (lossy)
- `apple:woz->dsk` — Apple II: WOZ bit-stream image -> DSK sectors (lossy)
- `apple:dsk->nib` — Apple II: DSK sectors -> NIB nibble image (standard format)

## Recent runs

- `20260913-201416` code `38f3f6ad14` · 2 tests · args `--only Vita (PSN) (Content)` · finished 2026-09-13T20:15:20.840109 · log `logs/run-20260913-201416.log`
- `20260913-195530` code `041567de78` · 0 tests · args `--only Sony - PlayStation 3` · finished NOT FINISHED · log `logs/run-20260913-195530.log`
- `20260913-194101` code `46fedd0eb9` · 5 tests · args `--only PS one Classics` · finished 2026-09-13T19:55:29.356387 · log `logs/run-20260913-194101.log`
- `20260913-191158` code `7aa604fe3a` · 3 tests · args `--only PlayStation 3 (PSN)` · finished 2026-09-13T19:12:08.571825 · log `logs/run-20260913-191158.log`
- `20260913-181525` code `d02dd3fb76` · 2 tests · args `--only SNK - Neo Geo CD --redo` · finished 2026-09-13T18:15:42.359025 · log `logs/run-20260913-181525.log`
- `20260913-181326` code `ba1811571c` · 2 tests · args `--only 3DO --redo` · finished 2026-09-13T18:14:19.835532 · log `logs/run-20260913-181326.log`
- `20260913-181305` code `ba1811571c` · 2 tests · args `--only PC Engine CD --redo` · finished 2026-09-13T18:13:25.130541 · log `logs/run-20260913-181305.log`
- `20260913-181248` code `ba1811571c` · 2 tests · args `--only SNK - Neo Geo CD --redo` · finished 2026-09-13T18:13:04.878928 · log `logs/run-20260913-181248.log`
- `20260913-181219` code `ba1811571c` · 2 tests · args `--only Sega - Mega CD --redo` · finished 2026-09-13T18:12:47.151264 · log `logs/run-20260913-181219.log`
- `20260913-181159` code `ba1811571c` · 2 tests · args `--only Sega - Saturn --redo` · finished 2026-09-13T18:12:18.843634 · log `logs/run-20260913-181159.log`

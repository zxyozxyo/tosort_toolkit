# Scene rulesets as a REPACK detector — tested and rejected (2026-09-18)

A measured negative result. Read this before rebuilding the idea; the tool that
produced every number here is `volume_signature_audit.py`.

## Where the rulesets came from

<https://scenerules.org/> carries 281 rulesets at `nfo/<YEAR>_<NAME>.nfo`,
complete and with no anti-bot (srrdb serves the same documents but sits behind
Anubis for plain fetches). The game/console ones that touch our corpus:

| ruleset | what it mandates |
|---|---|
| `UNK_ISOaRIP.nfo` | "Best compression, dictionary size 1024K, solid archive", 8.3 names, 15 MB volumes, **"Use the old winrar (pre 3.0)"** |
| `1998_GAMEiSO.nfo` | solid **off**, `.001 -> .0xx` naming, 15 meg segments |
| `2010_NDS.nfo`, `2010_oNDS.nfo` | "split into 5,000,000 byte parts" |
| `2011_PS3.nfo` | seven volume-size brackets keyed to folder size |
| `2021_GAMEiSO.nfo` | seven brackets; `m0` (store) forbidden outright (1.13) |
| `general_games.nfo` | CD 15,000,000 · DVD 50,000,000 · DVD9 100,000,000 |

There is **no 3DS, GBA or GBC ruleset in the archive at all**, which alone puts
2,682 + 34 + 7 of our releases outside any table.

## The theory

Every ruleset fixes an exact volume size for a payload size and era. We already
capture the true `recipe.volume_bytes` per set. So a release whose volume size
violates its era's table was not packed to the rules of its stated year, which
would be cheap offline evidence that the local source is a REPACK rather than
the scene original — the thing `rsr-srr-crosscheck-idea` wants, with no rebuild.

## Three formulations, three nulls

Corpus: 8,965 multi-volume RAR sets with a real volume size, from 15,184
manifests.

**1 — Ruleset compliance. Killed by the null check.** The applicable rulesets
put a CD at 15,000,000 bytes. Among *byte-exactly verified* PSX/PS1 releases —
releases we reproduced from the genuine scene rars, so their provenance is not
in question — the modal volume size is **20,000,000** (PSX 80.2%, PS1 59.6%),
with 15,000,000 a minority (12.5% / 32.4%). The rule does not govern these
groups. Flagging violations would flag the majority of genuine releases.

**2 — REPACK correlation on NDS**, the one platform with an unambiguous rule.
The association runs **backwards**:

| | n | exactly 5,000,000 |
|---|---|---|
| REPACK-tagged | 37 | **97.3%** |
| everything else | 4,900 | 78.7% |

**3 — Deviation from the group's own modal size**, all systems, which drops the
rulesets entirely and asks only whether a release broke its group's habit. 449
deviants carry a **0.45%** REPACK rate against a **0.59%** corpus base rate. No
signal.

## The follow-up guess, also dead

That volume-size inconsistency might mark a group who packed
non-deterministically, and so predict capture difficulty. Mean delta-verify
share is **52.4%** for groups below 90% purity vs **59.4%** for those at or
above — again the wrong way round.

## What the work did yield

**A sharp group fingerprint.** NDS group **LITE** packed 1,000 of 1,001 sets at
**5,000,192** bytes and none at 5,000,000. 5,000,192 is `4883 * 1024` — LITE
spelled the switch `-v4883k` where the rest of the scene wrote `-v5000000b`.
Treat it as a capture-integrity check (a LITE release landing on 5,000,000 is
the anomaly); it is useless as a sweep prior, because `rsr_tool.py` reads the
true volume size out of the archive headers and never has to guess.

**A correction to a claim I made before reading the documents.** The rulesets
cannot improve the sweep's shape priors at all: `rsr_tool.py:4770` already
derives `-m` level, `-md` dictionary, solid and `-k` from the archive headers,
authoritatively and per release. A document cannot beat reading the bytes. The
one prior that survives is `UNK_ISOaRIP`'s **"use the old winrar (pre 3.0)"**,
because it is an era gate that owes nothing to our own captures — the gap
`rsr-group-packer-host` warns about, where capture-derived surveys mispredict
reachability for groups we have never cracked.

**An explanation for the KALISTO shape split.** `1998_GAMEiSO` mandates solid
*off* while `UNK_ISOaRIP` mandates solid *on*, in the same era. The 23 KALISTO
walls are not one population with an unexplained split; they are two rulebooks.

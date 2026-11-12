# Validation

## What judges the APU

The hardware judges are test ROMs, run on gameboy-python (the APU needs a CPU to run them):

| Suite | Model | Result | Rerun (in gameboy-python) |
|---|---|---|---|
| blargg `dmg_sound` singles | DMG | 12 of 12 | `pytest tests/test_apu.py -k dmg_sound` |
| blargg `cgb_sound` singles | CGB | 12 of 12 | `pytest tests/test_apu.py -k cgb_sound` |
| SameSuite `apu/` (sample-accurate through PCM12/PCM34) | CGB E | the 63 that apply to a CGB E (of 70) | `scripts/cgb_suites.py same-suite` |
| Gambatte `_outaudio` ROMs | DMG | as gameboy-python lists them | `scripts/gambatte_audio.py` |

The blargg rows were rerun on 2026-11-12 with this repository at b8cab61 in place of the board's
pin (gameboy-python 36e14e1, PyPy): 24 of 24. SameSuite was last measured on the board itself.

## What this repository checks on its own

CI (PyPy) runs the board streams, the seam tests and ruff on every push to `main` and every pull
request; nothing in it needs a ROM.

- **Board streams** (`tests/test_board_streams.py`): `scripts/export_board_streams.py` records every
  call gameboy-python's board makes into the APU while it runs the 94 judge ROMs above (dmg_sound and
  cgb_sound singles on their models, all 70 of SameSuite apu/ on the CGB, passing or not), from the skip-boot hand-off. The
  replay must return every register and PCM read the board saw, the digital outputs after every
  sequencer step, and samples with the same SHA-256. The streams were recorded from gameboy-python
  3c8e378, before the extraction, so they hold the APU's behaviour as it was.
- **Seam tests** (`tests/test_apu.py`): the model rows, `run` against the per-M-cycle calls, the
  sequencer being the host's, the clock level at power-on, the outputs a mixing host reads, wave RAM,
  the snapshot.
- **The AGB row** has no hardware judge (docs/contract.md "The AGB row"): its rules are GBATEK's,
  each held by a seam test and a seeded fault. gba-python compares its whole sound output with
  NanoBoyAdvance's on commercial music, which hears the wave banks but not the shift register.
- **Seeded faults** (`scripts/mutation.py`): 30 mutants, one rule broken each (duty, noise divisors
  and taps, wave shift, nibble order and period, sequencer steps for length, sweep and envelope,
  NR52, length widths, the DMG/CGB power-off rules, the spent first step, wave start-up and
  corruption timing, DAC polarity, panning, PCM nibbles; for the AGB the bank the CPU reaches, the
  bank that plays, the 64-digit ring, the shift register, the force bit, the readable bits; the odd T `run` holds); each must fail
  the tests. 30 of 30 are caught (2026-11-11; rerun at b8cab61 on 2026-11-12, PyPy, same). The 7-bit noise width is heard by no judge ROM, so it has its own rule test
  (`test_noise_lfsr_periods`): without it that mutant went unseen.

## The extraction

gameboy-python's own APU results before (3c8e378, `apu/` in the board) and after (the board pinning
this repository), on CPython and PyPy, must be identical: the verdicts and, per ROM, the hashes of
the 44.1 kHz samples, of the digital outputs at every VBlank and of the last frame.

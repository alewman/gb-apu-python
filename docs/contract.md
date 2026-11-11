# The host contract

`gb_apu_python.APU` is driven entirely by its host: it has no clock, no timer and no bus of its
own. Everything here is the whole interface; anything else on the object is internal.

## Construction and models

```python
APU(sample_rate: int = 44100, model: Model = DMG)
```

`sample_rate` only matters to `take_samples()`. A `Model` is a frozen row of one revision's
differences; `MODELS` maps names to rows.

| Row | `cgb` | Revisions | Judged by |
|---|---|---|---|
| `DMG` | False | DMG-CPU (the revisions gameboy-python runs) | blargg `dmg_sound` 12/12 |
| `CGB` | True | CPU CGB E (gameboy-python's CGB) | blargg `cgb_sound` 12/12, SameSuite APU 63/63 |
| `AGB` | True | Game Boy Advance, in GBA mode (gba-python) | no hardware judge: see "The AGB row" |

`cgb` selects the CGB-family rules: powering off clears the length counters and keeps them
read-only; wave RAM is reachable at any time while CH3 plays and a trigger corrupts nothing; NRx2
writes while a channel plays take the CGB "zombie" path; the CGB sweep and noise counters
(SameBoy's model, which SameSuite measures).

A new row adds a `Model` field per difference, defaulting to the current behaviour so the other
rows stay as they are, and the rules behind it.

### The AGB row

`AGB` is the CGB-family rules plus one field, `wave_banks` (GBATEK "GBA Sound Channel 3"):

- Wave RAM is two banks of 32 digits. NR30 bit 6 selects the bank that plays and bit 5 chains
  both into 64 digits, starting with the selected one. Both bits read back.
- `read`/`write` of `WAVE_RAM` always reach the bank that is **not** selected, playing or not.
- Wave RAM is a shift register, not a memory with a pointer: each step the digit at the front of
  the playing ring is played and re-enters at the back, so the data moves. A host sees it move
  when it switches banks after a partial play, or reads the other bank in 64-digit mode. A
  trigger does not rewind it; from freshly written data the first digit played is the first
  written (the Game Boy's first is the second).
- NR32 bit 7 forces the volume to 75 % and reads back. `digital()` rounds that down
  (`digit * 3 >> 2`); `wave_digit()` and `wave_quarters()` give a mixing host the exact parts.
- `volumes()` gives the three envelope volumes, for a host that mixes each channel around zero
  as the AGB's digital mixer does (no DACs).
- The frame sequencer is the host's, as on every row: gba-python clocks it from its system
  clock.

What decides each rule: GBATEK for the registers and the shift register; mGBA
(`src/gb/audio.c`, `GB_AUDIO_GBA`) models the shift register too, NanoBoyAdvance a pointer. In
64-digit mode with bank 1 selected the three differ (GBATEK: start with the selected bank; mGBA:
always bank 0; NanoBoyAdvance: forces the bank bit to 0 on a trigger): GBATEK is followed. There
is no hardware test ROM for any of it, so these are documented rules with seeded faults, not
judged ones. Not modelled: what SameBoy records for an AGB running Game Boy software (wave RAM
reading $FF while playing, the NR43 glitch variants, the mixer's bias), which is another mode.

## The clock

- `tick(t)`: advance `t` T-cycles of the APU's 4,194,304 Hz clock. The Game Boy calls it once
  per M-cycle with `t = 4` (2 in CGB double speed).
- `run(t)`: catch up any number of T-cycles in those 4-T steps (the remainder last). A host with
  another master clock converts to these T-cycles and calls `run`; sequencer edges and register
  accesses go between calls, at the T they belong to.

## The frame sequencer

- `frame_tick()`: one sequencer step (512 Hz). The Game Boy calls it at each falling edge of its
  system counter's bit 12 (13 in double speed), and when a DIV write makes that bit fall.
- `secondary_tick()`: the rising edge half a period later (envelope reloads).
- `frame_clock_high`: the clock's level, which the host sets before it writes NR52. Powering on
  while it is high spends the next step (SameSuite `div_write_trigger`). A host without DIV keeps
  it in step with its own clock, or leaves it False.

## Registers

`read(address)` and `write(address, value)` take the Game Boy addresses: NR10-NR52
($FF10-$FF26, named `NR10` ... `NR52`) and wave RAM (`WAVE_RAM` = $FF30-$FF3F). Unused addresses
read $FF. A host maps its own register addresses onto these. `pcm(pair)` is the CGB's
PCM12/PCM34 ($FF76/$FF77).

## Outputs

- `digital()`: the four channels' digital outputs (0-15), the DACs' inputs.
- `dacs()`: which DACs are on (an off DAC contributes nothing at all).
- `nr50`, `nr51`: master volume and panning as written.
- `channel_on`: the four channel-on flags (NR52's low bits).
- `volumes()`, `wave_digit()`, `wave_quarters()`: the envelope volumes and the wave channel's
  digit and volume apart ("The AGB row").
- `take_samples()`: with `capture = True`, interleaved signed 16-bit stereo at `sample_rate`
  since the last call. The mix is this core's labelled fit (DACs, panning, volume, a high-pass);
  a host with its own mixer (the GBA's FIFOs and SOUNDBIAS) takes `digital()`, `dacs()`, `nr50`
  and `nr51` instead.

## State

`capture_state()` returns an `APUState`: a frozen snapshot (model, T, power, registers, wave RAM,
sequencer step, channel enables, lengths, envelope volumes, digital outputs) for comparison and
reporting, with `as_dict()`. Restoring is not offered, as with the board's `capture_board_state`.

## Hand-off

`skip_boot()` puts the APU in the state the Game Boy boot ROM leaves (the board's skip-boot
start).

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
| AGB | | Game Boy Advance | not modelled yet (see below) |

`cgb` selects the CGB-family rules: powering off clears the length counters and keeps them
read-only; wave RAM is reachable at any time while CH3 plays and a trigger corrupts nothing; NRx2
writes while a channel plays take the CGB "zombie" path; the CGB sweep and noise counters
(SameBoy's model, which SameSuite measures).

A new row (the AGB) adds a `Model` field per difference, defaulting to the current behaviour so
DMG and CGB stay as they are, and the rules behind it: for the AGB, the two wave banks and the
64-sample mode, the forced 75 % wave volume, and its own sequencer timing (the host already owns
the sequencer clock, below).

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

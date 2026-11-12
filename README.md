# gb-apu-python

A readable, pure-Python Game Boy APU: the two pulse channels, the wave channel, the noise channel,
the frame sequencer and a mixer. One class, with a row per hardware revision: the DMG, the CGB,
and the Game Boy Advance's four Game Boy channels (the AGB row: two wave banks in a shift
register, the forced 75 % volume). It runs on CPython and PyPy (Python 3.11 or newer) and imports
nothing.

It is one of a family of reference cores: chips are cores, boards compose them, and every
behaviour names the source it was judged against. Correctness and readability come before speed.
It was extracted, with its history, from the Game Boy board
[gameboy-python](https://github.com/alewman/gameboy-python) when a second board, gba-python,
needed the same channels. Both boards pin it by git revision.

## Status

| Judge | Model | Result |
| --- | --- | --- |
| blargg `dmg_sound` singles (hardware-verified) | DMG | 12 of 12, on gameboy-python |
| blargg `cgb_sound` singles (hardware-verified) | CGB | 12 of 12, on gameboy-python |
| SameSuite `apu/` (sample-accurate through PCM12/PCM34) | CGB E | the 63 of 70 that apply to a CGB E, on gameboy-python |
| Board streams: the board's calls into the APU while it ran those 94 ROMs | DMG, CGB | 94 of 94 replay identically |
| Seeded faults (`scripts/mutation.py`) | all | 30 of 30 caught |
| Hardware tests for the AGB row | AGB | none exist: its rules are GBATEK's, each held by a seam test and a seeded fault |

The test ROMs need a CPU, so they run on the board. This repository checks itself without one:
`tests/streams/` holds every register write, read and clock call gameboy-python made into the APU
while it ran the judge ROMs above (register traffic only, no ROM data), and the replay must give
the same reads, the same channel outputs after every sequencer step and samples with the same
hash. [docs/validation.md](docs/validation.md) has the details and how to rerun each judge.

## Use

Not on PyPI. From a clone:

```sh
pip install -e '.[dev]'
```

```python
from gb_apu_python import APU, CGB, NR11, NR12, NR13, NR14, NR51, NR52

apu = APU(sample_rate=44100, model=CGB)
apu.write(NR52, 0x80)  # power on; registers by their Game Boy address
apu.write(NR51, 0x11)  # channel 1 to both sides
apu.write(NR11, 0x80)  # 50 % duty
apu.write(NR12, 0xF0)  # full volume, no envelope
apu.write(NR13, 0x00)
apu.write(NR14, 0x87)  # trigger, period $700: 131072 / (2048 - $700) = 512 Hz
apu.capture = True
apu.run(70224)  # the clock: T-cycles of 4,194,304 Hz (one frame)
apu.frame_tick()  # the frame sequencer: the host's 512 Hz clock
apu.digital(), apu.dacs()  # per-channel DAC inputs (0-15) and DAC enables
samples = apu.take_samples()  # interleaved signed 16-bit stereo since capture began
apu.capture_state()  # an immutable snapshot
```

The APU has no clock, timer or bus of its own: the host calls `tick`/`run` for time,
`frame_tick`/`secondary_tick` for the sequencer, and `read`/`write` for registers. A host with its
own mixer (the GBA's) takes `digital()`, `dacs()`, `nr50` and `nr51` instead of the samples.
[docs/contract.md](docs/contract.md) is the whole interface, and what a new model row adds.

## Tests

```sh
python -m pytest              # the seam tests and the board streams (no ROMs needed; minutes)
python scripts/mutation.py    # seeded faults: each must fail the tests
python scripts/export_board_streams.py ../gameboy-python   # re-record the streams from the board
```

## Credits

The behaviour follows [Pan Docs](https://github.com/gbdev/pandocs) `Audio_details.md`, blargg's
`dmg_sound` and `cgb_sound`, LIJI's [SameSuite](https://github.com/LIJI32/SameSuite) and
[SameBoy](https://github.com/LIJI32/SameBoy)'s model of the CGB, and, for the AGB row, GBATEK
(with mGBA and NanoBoyAdvance compared where they differ). Each rule cites its source in the code.

## Licence

MIT (see `LICENSE`).

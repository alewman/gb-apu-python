# gb-apu-python

A readable, pure-Python Game Boy APU: the two pulse channels, the wave channel, the noise channel,
the frame sequencer and a mixer, for the DMG and the CGB. It runs on CPython and PyPy (Python 3.11
or newer) and imports nothing.

It was extracted, with its history, from [gameboy-python](../gameboy-python), which pins it by git
revision, when a second board (gba-python, whose Game Boy channels are this APU) needed it. The
behaviour is the board's, unchanged: blargg `dmg_sound` 12/12 (DMG) and `cgb_sound` 12/12 (CGB),
SameSuite's APU ROMs 63/63 (those that apply to a CGB E), measured on the board. This repository
replays the board's calls into the APU while it ran those ROMs (`tests/streams/`) and checks that
every read, every step's outputs and the samples come out the same.

```python
from gb_apu_python import APU, CGB, NR52

apu = APU(sample_rate=44100, model=CGB)
apu.write(NR52, 0x80)  # registers by their Game Boy address
apu.run(70224)  # the clock: T-cycles of 4,194,304 Hz
apu.frame_tick()  # the frame sequencer: the host's 512 Hz clock
apu.digital(), apu.dacs()  # per-channel DAC inputs (0-15) and DAC enables
apu.nr50, apu.nr51  # volume and panning, for a host that mixes
apu.capture_state()  # an immutable snapshot
```

- [docs/contract.md](docs/contract.md): the host interface, and what a new model row adds.
- [docs/validation.md](docs/validation.md): what judges it and how to rerun it.

## Tests

```sh
python -m pytest              # the seam tests and the board streams (no ROMs needed)
python scripts/mutation.py    # seeded faults: each must fail the tests
python scripts/export_board_streams.py ../gameboy-python   # re-record the streams from the board
```

MIT licensed.

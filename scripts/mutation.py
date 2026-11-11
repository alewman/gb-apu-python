#!/usr/bin/env python
"""Seed faults into the APU, one at a time, and check the gate catches each.

    python scripts/mutation.py [--streams N]    # N: replay only the first N streams (default all)

A gate that cannot fail proves nothing. Each mutant changes one place in
``src/gb_apu_python/__init__.py`` -- a rule the APU is known to have, broken the way a port might
break it -- and the gate is this core's own tests: the board streams (tests/test_board_streams.py)
and the seam tests (tests/test_apu.py). A mutant the gate misses is listed.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APU = ROOT / "src" / "gb_apu_python" / "__init__.py"

#: (what is broken, the exact text, its replacement)
MUTANTS = [
    ("duty 75% inverted", "        (0, 1, 1, 1, 1, 1, 1, 0))", "        (1, 0, 0, 0, 0, 0, 0, 1))"),
    ("noise divisors from 16", "NOISE_DIVISORS = (8, 16, 32, 48, 64, 80, 96, 112)", "NOISE_DIVISORS = (16, 16, 32, 48, 64, 80, 96, 112)"),
    ("wave volume shifts off by one", "WAVE_QUARTERS = (0, 4, 2, 1)", "WAVE_QUARTERS = (0, 2, 1, 0)"),
    ("wave nibbles swapped", "ch3.buffer = (byte >> 4) if not ch3.index & 1 else byte & 0xF", "ch3.buffer = (byte & 0xF) if not ch3.index & 1 else byte >> 4"),
    ("wave period at 1 MHz", "ch3.timer += (2048 - ch3.frequency) * 2", "ch3.timer += (2048 - ch3.frequency) * 4"),
    ("lengths clocked on the even steps", "        if divider & 1:\n", "        if not divider & 1:\n"),
    ("sweep on the wrong steps", "        if divider & 3 == 3:\n", "        if divider & 3 == 1:\n"),
    ("envelope on the wrong step", "            if divider & 7 == 7:\n", "            if divider & 7 == 6:\n"),
    ("DMG noise XOR, not XNOR", "            lfsr = ch4.lfsr\n            bit = ((lfsr ^ (lfsr >> 1)) & 1) ^ 1", "            lfsr = ch4.lfsr\n            bit = (lfsr ^ (lfsr >> 1)) & 1"),
    ("7-bit noise taps bit 5", "lfsr = (lfsr & ~0x40) | (bit << 6)", "lfsr = (lfsr & ~0x40) | (bit << 5)"),
    ("NR52 hides the power bit", "return 0x70 | (0x80 if self.power else 0) | status", "return 0x70 | status"),
    ("CH3 length is 6 bits", "self.ch3.length = 256 - value", "self.ch3.length = 256 - (value & 0x3F)"),
    ("DMG lengths read-only while off", "if not self.cgb and address in (0xFF11, 0xFF16, 0xFF1B, 0xFF20):", "if False and address in (0xFF11, 0xFF16, 0xFF1B, 0xFF20):"),
    ("CGB power-off keeps the lengths", "                if self.cgb:\n                    channel.length = 0\n", "                if False:\n                    channel.length = 0\n"),
    ("a high sequencer clock spends nothing", "                self.skip_event = 1\n", "                self.skip_event = 0\n"),
    ("wave starts 2 T early", "WAVE_TRIGGER_DELAY = 6", "WAVE_TRIGGER_DELAY = 4"),
    ("narrower DMG wave corruption", "CORRUPT_LO, CORRUPT_HI = 1, 3", "CORRUPT_LO, CORRUPT_HI = 1, 2"),
    ("DAC polarity flipped", "analog = (7.5 - digital) / 7.5", "analog = (digital - 7.5) / 7.5"),
    ("panning sides swapped", "            if nr51 & (0x10 << i):\n", "            if nr51 & (0x01 << i):\n"),
    ("AGB: the CPU reaches the playing bank", "return 0 if self.registers[0x0A] & 0x40 else 32", "return 32 if self.registers[0x0A] & 0x40 else 0"),
    ("AGB: bank 0 always plays", "        start = 32 if nr30 & 0x40 else 0\n", "        start = 0\n"),
    ("AGB: 64 digits never chain", "        if nr30 & 0x20:\n", "        if False:\n"),
    ("AGB: the 64-digit ring drops a digit", "            digits[other + 31] = out\n", "            digits[other + 31] = digits[other]\n"),
    ("AGB: a pointer, not a shift register", "            digits[start : start + 31] = digits[start + 1 : start + 32]\n            digits[start + 31] = out\n", "            digits[start + 31] = out\n"),
    ("AGB: force bit ignored", "        if self.wave_banks and nr32 & 0x80:\n", "        if False:\n"),
    ("AGB: force gives 50 %", "            return 3\n", "            return 2\n"),
    ("AGB: NR30's bank bits unreadable", "READ_MASKS_AGB = (*READ_MASKS[:0x0A], 0x1F, 0xFF, 0x1F,", "READ_MASKS_AGB = (*READ_MASKS[:0x0A], 0x7F, 0xFF, 0x1F,"),
    ("AGB: wave digits swapped on write", "            self.wave_digits[at] = value >> 4\n            self.wave_digits[at + 1] = value & 0xF\n", "            self.wave_digits[at + 1] = value >> 4\n            self.wave_digits[at] = value & 0xF\n"),
    ("run() drops an odd T", "        self._held_t = t & 1\n", "        self._held_t = 0\n"),
    ("PCM nibbles swapped", "return outputs[2 * pair] | (outputs[2 * pair + 1] << 4)", "return (outputs[2 * pair] << 4) | outputs[2 * pair + 1]"),
]  # fmt: skip


def gate(src: Path, python: str, streams: int | None) -> bool:
    """True when the gate passes (the mutant survives)."""
    env = dict(os.environ, PYTHONPATH=str(src))
    tests = ["tests/test_apu.py", "tests/test_board_streams.py"]
    if streams is not None:
        env["GB_APU_STREAMS"] = str(streams)
    result = subprocess.run(
        # -o pythonpath: pyproject puts the real src first; the mutated copy must win.
        [python, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", "-o", "pythonpath=tests scripts", *tests],
        cwd=ROOT, env=env, capture_output=True, text=True,
    )  # fmt: skip
    return result.returncode == 0


def main() -> int:
    streams = None
    if "--streams" in sys.argv:
        streams = int(sys.argv[sys.argv.index("--streams") + 1])
    python = sys.executable
    original = APU.read_text()
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "src"
        shutil.copytree(ROOT / "src", src)
        target = src / "gb_apu_python" / "__init__.py"
        if not gate(src, python, streams):
            print("the gate fails without a mutant: fix that first")
            return 2
        missed = []
        for name, old, new in MUTANTS:
            assert original.count(old) == 1, f"{name}: the text is not in the source once"
            target.write_text(original.replace(old, new))
            caught = not gate(src, python, streams)
            print(f"{'caught' if caught else 'MISSED'}  {name}", flush=True)
            if not caught:
                missed.append(name)
        target.write_text(original)
    print(f"{len(MUTANTS) - len(missed)} of {len(MUTANTS)} caught")
    return 1 if missed else 0


if __name__ == "__main__":
    sys.exit(main())

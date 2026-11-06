"""Replay the calls gameboy-python's board made into this APU while it passed (or failed) the APU's
test ROMs (scripts/export_board_streams.py): every read, every PCM read and the digital outputs
after every frame sequencer step must come out as the board saw them, and the captured 44.1 kHz
samples must hash the same."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
from pathlib import Path

import pytest

from gb_apu_python import APU, MODELS

STREAMS = sorted((Path(__file__).parent / "streams").glob("*.json.gz"))
if os.environ.get("GB_APU_STREAMS"):  # scripts/mutation.py --streams N: a quick subset
    STREAMS = STREAMS[: int(os.environ["GB_APU_STREAMS"])]


def load(path: Path) -> dict:
    with gzip.open(path, "rt") as f:
        return json.load(f)


def replay(stream: dict) -> str | None:
    """Run a stream; the first mismatch, or None."""
    apu = APU(model=MODELS[stream["header"]["model"]])
    apu.skip_boot()
    apu.capture = True
    tick, write, read, pcm = apu.tick, apu.write, apu.read, apu.pcm
    for i, event in enumerate(stream["events"]):
        kind = event[0]
        if kind == "t":
            t = event[1]
            for _ in range(event[2]):
                tick(t)
        elif kind == "w":
            apu.frame_clock_high = bool(event[3])
            write(event[1], event[2])
        elif kind == "r":
            if read(event[1]) != event[2]:
                got = read(event[1])
                return f"event {i}: read ${event[1]:04X} gave ${got:02X}, want ${event[2]:02X}"
        elif kind == "p":
            if pcm(event[1]) != event[2]:
                return f"event {i}: PCM{event[1]} gave ${pcm(event[1]):02X}, want ${event[2]:02X}"
        elif kind == "f":
            apu.frame_tick()
        elif kind == "s":
            apu.secondary_tick()
        elif kind == "c" and list(apu.digital()) != event[1:]:
            return f"event {i}: digital {apu.digital()}, want {tuple(event[1:])}"
    samples = hashlib.sha256(apu.take_samples().tobytes()).hexdigest()
    if samples != stream["header"]["samples_sha256"]:
        return "the samples differ"
    return None


@pytest.mark.skipif(not STREAMS, reason="no streams: run scripts/export_board_streams.py")
@pytest.mark.parametrize("path", STREAMS, ids=[p.name.removesuffix(".json.gz") for p in STREAMS])
def test_board_stream(path):
    assert replay(load(path)) is None

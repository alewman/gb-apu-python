#!/usr/bin/env python3
"""Record the calls gameboy-python's board makes into its APU while it runs a test ROM, as a stream
the core replays without the board (tests/test_board_streams.py).

    python scripts/export_board_streams.py GAMEBOY_PYTHON_CHECKOUT [--jobs 8] [NAME ...]

Run with an interpreter that has gameboy-python's dependencies (its venv). The ROMs are the ones
that judge the APU: blargg dmg_sound (DMG) and cgb_sound (CGB) by their $A000 protocol, SameSuite
apu/ on the CGB. Each stream is a gzip JSON list of events from the board's skip-boot hand-off:
``["t", t, n]`` n calls of ``tick(t)``; ``["w", address, value, frame_clock_high]``;
``["r", address, value]`` a read and what it returned; ``["p", pair, value]`` a PCM12/34 read;
``["f"]`` ``frame_tick()`` followed by ``["c", ch1, ch2, ch3, ch4]``, the digital outputs after it;
``["s"]`` ``secondary_tick()``. The header holds the model, the verdict and the hash of the
44.1 kHz samples captured from the hand-off on.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import sys
from multiprocessing import Pool
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "tests" / "streams"


def roms(board: Path) -> list[tuple[Path, str, str]]:
    gbtr = board / "tests" / "test_roms" / "gbtr"
    blargg = gbtr / "blargg"
    out = [(p, "dmg", "blargg") for p in sorted((blargg / "dmg_sound/rom_singles").glob("*.gb"))]
    out += [(p, "cgb", "blargg") for p in sorted((blargg / "cgb_sound/rom_singles").glob("*.gb"))]
    out += [(p, "cgb", "same") for p in sorted((gbtr / "same-suite/apu").rglob("*.gb"))]
    return out


def stream_name(path: Path, model: str) -> str:
    return f"{model}-{path.parent.name}-{path.stem}".replace(" ", "_")


def record(item: tuple[Path, str, str, Path]) -> str:
    path, model, kind, board = item
    sys.path[:0] = [str(board / "src")]
    from gameboy_python import MACHINES, GameBoy, load_cartridge
    from gameboy_python.testrom import run_blargg_ram, run_mooneye

    gb = GameBoy(load_cartridge(path.read_bytes()), MACHINES[model])
    apu = gb.apu
    apu.capture = True
    events: list[list] = []
    tick, write, read, pcm = apu.tick, apu.write, apu.read, apu.pcm
    frame_tick, secondary_tick = apu.frame_tick, apu.secondary_tick

    def t_hook(t: int = 4) -> None:
        last = events[-1] if events else None
        if last and last[0] == "t" and last[1] == t:
            last[2] += 1
        else:
            events.append(["t", t, 1])
        tick(t)

    def w_hook(address: int, value: int) -> None:
        # the board before the extraction called the input div_bit12
        level = getattr(apu, "frame_clock_high", False) or getattr(apu, "div_bit12", False)
        events.append(["w", address, value, int(level)])
        write(address, value)

    def r_hook(address: int) -> int:
        value = read(address)
        events.append(["r", address, value])
        return value

    def p_hook(pair: int) -> int:
        value = pcm(pair)
        events.append(["p", pair, value])
        return value

    def f_hook() -> None:
        events.append(["f"])
        frame_tick()
        events.append(["c", *apu.digital()])

    def s_hook() -> None:
        events.append(["s"])
        secondary_tick()

    apu.tick, apu.write, apu.read, apu.pcm = t_hook, w_hook, r_hook, p_hook
    apu.frame_tick, apu.secondary_tick = f_hook, s_hook
    result = run_blargg_ram(gb, max_frames=3000) if kind == "blargg" else run_mooneye(gb, 600)
    header = {
        "rom": f"{path.parent.name}/{path.name}",
        "model": model,
        "passed": bool(result.passed),
        "samples_sha256": hashlib.sha256(apu.take_samples().tobytes()).hexdigest(),
    }
    name = stream_name(path, model)
    OUT.mkdir(parents=True, exist_ok=True)
    with gzip.open(OUT / f"{name}.json.gz", "wt", compresslevel=9) as f:
        json.dump({"header": header, "events": events}, f, separators=(",", ":"))
    return f"{name} {'pass' if header['passed'] else 'FAIL'} {len(events)} events"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("board", type=Path, help="a gameboy-python checkout with its test ROMs")
    parser.add_argument("--jobs", type=int, default=8)
    parser.add_argument("names", nargs="*", help="only streams whose name contains one of these")
    args = parser.parse_intermixed_args()
    work = [
        (p, m, k, args.board.resolve())
        for p, m, k in roms(args.board.resolve())
        if not args.names or any(n in stream_name(p, m) for n in args.names)
    ]
    with Pool(args.jobs) as pool:
        for line in pool.imap_unordered(record, work):
            print(line, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

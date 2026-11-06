"""The APU on its own: the host seams (clock, sequencer, registers, outputs, state) and the model
rows. The behaviour against hardware is judged through the board's streams
(test_board_streams.py)."""

from __future__ import annotations

import pytest

from gb_apu_python import (
    APU,
    CGB,
    DMG,
    MODELS,
    NR11,
    NR12,
    NR13,
    NR14,
    NR42,
    NR43,
    NR44,
    NR50,
    NR51,
    NR52,
    WAVE_RAM,
)


def square(apu: APU) -> None:
    apu.write(NR52, 0x80)  # power
    apu.write(NR50, 0x77)
    apu.write(NR51, 0xFF)
    apu.write(NR11, 0x80)  # duty 50%
    apu.write(NR12, 0xF0)  # volume 15, no envelope
    apu.write(NR13, 0x00)
    apu.write(NR14, 0x87)  # trigger, frequency 0x700: about 2 kHz


def test_a_pulse_channel_makes_a_square_wave():
    apu = APU(sample_rate=44100)
    apu.capture = True
    square(apu)
    for _ in range(100_000):  # 0.095 s of the 4 MHz clock
        apu.tick(4)
    samples = apu.take_samples()
    assert len(samples) > 2 * 4000  # about 4,200 stereo frames
    assert max(samples) > 2000 and min(samples) < -2000


def test_power_off_clears_registers_but_not_length_counters():
    apu = APU()
    apu.write(NR52, 0x80)
    apu.write(NR12, 0xF0)
    apu.write(NR11, 0x3F)
    apu.write(NR52, 0x00)
    assert apu.read(NR12) == 0x00
    assert apu.ch1.length == 1  # DMG: the length counter survives
    apu.write(NR12, 0xF0)  # ignored while off
    assert apu.read(NR12) == 0x00


def test_cgb_power_off_clears_the_length_counters():
    apu = APU(model=CGB)
    apu.write(NR52, 0x80)
    apu.write(NR11, 0x3F)
    apu.write(NR52, 0x00)
    assert apu.ch1.length == 0


def test_the_model_rows():
    assert MODELS == {"dmg": DMG, "cgb": CGB}
    assert not DMG.cgb and CGB.cgb
    assert APU().model is DMG


@pytest.mark.parametrize("model", [DMG, CGB])
def test_run_is_the_board_steps(model):
    """run(t) catches up in the Game Boy's 4-T steps, so a host with another master clock gets the
    same result as the Game Boy's per-M-cycle calls."""
    a, b = APU(model=model), APU(model=model)
    for apu in (a, b):
        apu.capture = True
        square(apu)
    for _ in range(1000):
        a.tick(4)
    a.tick(2)
    b.run(4002)
    assert a.capture_state() == b.capture_state()
    assert a.take_samples() == b.take_samples()


def test_the_sequencer_is_the_hosts():
    """The frame sequencer only moves when the host says so: no clock of its own."""
    apu = APU()
    apu.write(NR52, 0x80)
    apu.write(NR11, 0x3E)  # length 2
    apu.write(NR12, 0xF0)
    apu.write(NR14, 0xC0)  # trigger with the length enabled
    apu.run(1 << 20)
    assert apu.digital()[0] or apu.channel_on[0]  # no sequencer step yet: still playing
    for _ in range(4):
        apu.frame_tick()
    assert not apu.channel_on[0]  # two length clocks (every other step) ran it out


def test_the_clock_level_at_power_on_spends_the_first_step():
    """Powering on while the sequencer clock is high spends its next step (SameSuite
    div_write_trigger): the host tells the APU the level before the NR52 write."""
    low, high = APU(), APU()
    high.frame_clock_high = True
    for apu in (low, high):
        apu.write(NR52, 0x80)
        apu.frame_tick()
    assert low.capture_state().divider == 1
    assert high.capture_state().divider == 1  # the spent step set it, the real one did not count
    for apu in (low, high):
        apu.frame_tick()
    assert (low.capture_state().divider, high.capture_state().divider) == (2, 1)


def test_outputs_for_a_host_that_mixes():
    apu = APU()
    square(apu)
    apu.run(4096)
    assert apu.dacs() == (True, False, False, False)
    assert (apu.nr50, apu.nr51) == (0x77, 0xFF)
    assert all(0 <= v <= 15 for v in apu.digital())


def test_wave_ram_by_address():
    apu = APU(model=CGB)
    apu.write(NR52, 0x80)
    for i in range(16):
        apu.write(WAVE_RAM + i, i * 0x11)
    assert [apu.read(WAVE_RAM + i) for i in range(16)] == [i * 0x11 for i in range(16)]


def test_state_is_a_snapshot():
    apu = APU()
    square(apu)
    state = apu.capture_state()
    assert state.model == "dmg" and state.power and state.enabled[0]
    assert state.registers[NR12 - 0xFF10] == 0xF0
    apu.write(NR52, 0x00)
    assert state.power  # unchanged by later writes
    assert set(state.as_dict()) >= {"model", "registers", "wave_ram", "digital"}


@pytest.mark.parametrize("model", [DMG, CGB])
@pytest.mark.parametrize(("nr43", "period"), [(0x00, 32767), (0x08, 127)])
def test_noise_lfsr_periods(model, nr43, period):
    """NR43 bit 3 narrows the LFSR to 7 bits: the sequence repeats every 127 steps, not 32,767
    (Pan Docs Audio_details.md). No judge ROM listens to the narrow mode, so this rule is tested
    here."""
    apu = APU(model=model)
    apu.write(NR52, 0x80)
    apu.write(NR42, 0xF0)
    apu.write(NR43, nr43)  # the fastest clock
    apu.write(NR44, 0x80)
    states, last = [], apu.ch4.lfsr
    for _ in range(64 * (2 * period + 200)):  # a stuck LFSR fails below instead of hanging
        apu.tick(2)
        if apu.ch4.lfsr != last:
            last = apu.ch4.lfsr
            states.append(last & 0x7F)
            if len(states) == 2 * period + 200:
                break
    assert len(states) == 2 * period + 200, "the LFSR stopped stepping"
    tail = states[200:]
    assert all(tail[i] == tail[i + period] for i in range(period))
    if period == 127:
        assert any(tail[i] != tail[i + 63] for i in range(period))

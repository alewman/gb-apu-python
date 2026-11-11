"""The APU on its own: the host seams (clock, sequencer, registers, outputs, state) and the model
rows. The behaviour against hardware is judged through the board's streams
(test_board_streams.py)."""

from __future__ import annotations

import pytest

from gb_apu_python import (
    AGB,
    APU,
    CGB,
    DMG,
    MODELS,
    NR11,
    NR12,
    NR13,
    NR14,
    NR30,
    NR32,
    NR33,
    NR34,
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
    assert MODELS == {"dmg": DMG, "cgb": CGB, "agb": AGB}
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


# ----- the AGB row: wave RAM as two banks in a shift register ---------------------------------

BANK_A = bytes(range(0x01, 0x11))  # digits 0,1,0,2,...: every byte differs
BANK_B = bytes(range(0xF0, 0xE0, -1))


def agb_wave(nr30: int, nr32: int = 0x20) -> APU:
    """An AGB APU with BANK_A in bank 0 and BANK_B in bank 1, then ``nr30`` and a trigger at the
    fastest rate (one digit per 2 T)."""
    apu = APU(model=AGB)
    apu.write(NR52, 0x80)
    apu.write(NR30, 0x40)  # bank 1 selected: the CPU reaches bank 0
    for i, value in enumerate(BANK_A):
        apu.write(WAVE_RAM + i, value)
    apu.write(NR30, 0x00)
    for i, value in enumerate(BANK_B):
        apu.write(WAVE_RAM + i, value)
    apu.write(NR30, nr30)
    apu.write(NR32, nr32)
    apu.write(NR33, 0xFF)
    apu.write(NR34, 0x87)
    return apu


def played(apu: APU, count: int) -> list[int]:
    """The next ``count`` digits the wave channel plays."""
    digits = []
    apu.tick(8)  # the start-up delay and one period: the first digit
    for _ in range(count):
        digits.append(apu.wave_digit())
        apu.tick(2)
    return digits


def digits_of(data: bytes) -> list[int]:
    return [digit for byte in data for digit in (byte >> 4, byte & 0xF)]


def test_agb_is_a_cgb_family_row_with_wave_banks():
    assert MODELS["agb"] is AGB
    assert AGB.cgb and AGB.wave_banks and not CGB.wave_banks and not DMG.wave_banks


def test_agb_cpu_reaches_the_bank_that_is_not_selected():
    apu = agb_wave(0x00)  # bank 0 selected
    apu.write(NR30, 0x40)
    assert bytes(apu.read(WAVE_RAM + i) for i in range(16)) == BANK_A
    apu = agb_wave(0x40)
    apu.write(NR30, 0x00)
    assert bytes(apu.read(WAVE_RAM + i) for i in range(16)) == BANK_B


def test_agb_plays_the_selected_bank_from_its_first_digit():
    assert played(agb_wave(0x80), 32) == digits_of(BANK_A)
    assert played(agb_wave(0xC0), 32) == digits_of(BANK_B)
    assert played(agb_wave(0x80), 64) == digits_of(BANK_A) * 2  # one bank repeats


def test_agb_64_digit_mode_starts_with_the_selected_bank_then_the_other():
    assert played(agb_wave(0xA0), 64) == digits_of(BANK_A + BANK_B)
    assert played(agb_wave(0xE0), 128) == digits_of(BANK_B + BANK_A) * 2


def test_agb_wave_ram_is_a_shift_register_the_cpu_sees_move():
    apu = agb_wave(0xA0)  # 64 digits from bank 0: the CPU reaches bank 1
    apu.tick(8 + 2)  # two digits played: one byte has left the front and gone to the back
    assert apu.read(WAVE_RAM) == BANK_B[1]
    assert apu.read(WAVE_RAM + 15) == BANK_A[0]
    assert apu.capture_state().wave_ram == BANK_A[1:] + BANK_B + BANK_A[:1]
    apu = agb_wave(0x80)  # one bank: stopping part way leaves it turned
    apu.tick(8 + 2 * 3)  # four digits
    apu.write(NR30, 0x40)
    assert bytes(apu.read(WAVE_RAM + i) for i in range(16)) == BANK_A[2:] + BANK_A[:2]


def test_agb_force_bit_is_75_percent_and_reads_back():
    apu = agb_wave(0x80, nr32=0x80)
    assert apu.wave_quarters() == 3
    assert apu.read(NR32) == 0x9F
    apu.tick(8 + 2 * 29)  # the 30th digit: the 15 of byte 0Fh
    assert apu.wave_digit() == 15
    assert apu.digital()[2] == 11  # 15 x 3/4, rounded down
    assert [agb_wave(0x80, nr32=n << 5).wave_quarters() for n in range(4)] == [0, 4, 2, 1]
    cgb = APU(model=CGB)
    cgb.write(NR52, 0x80)
    cgb.write(NR32, 0x80)
    assert cgb.wave_quarters() == 0 and cgb.read(NR32) == 0x9F | 0x80


def test_agb_nr30_reads_its_bank_bits():
    apu = APU(model=AGB)
    apu.write(NR52, 0x80)
    apu.write(NR30, 0xE0)
    assert apu.read(NR30) == 0xFF
    apu.write(NR30, 0x00)
    assert apu.read(NR30) == 0x1F


def test_volumes_are_the_envelopes():
    apu = APU(model=AGB)
    apu.write(NR52, 0x80)
    apu.write(NR12, 0xA0)
    apu.write(NR14, 0x80)
    assert apu.volumes() == (10, 0, 0)

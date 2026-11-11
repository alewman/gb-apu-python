"""The Game Boy APU (the four channels, the frame sequencer and the mixer) behind a pin-shaped
boundary that any host drives (docs/contract.md):

- the clock: ``tick(t)`` advances ``t`` T-cycles of the APU's 4,194,304 Hz clock (the Game Boy
  calls it every M-cycle; ``t`` is even), ``run(t)`` catches up any number of them in those
  steps;
- the frame sequencer: ``frame_tick()`` at each step (512 Hz) and ``secondary_tick()`` half a period
  later; the Game Boy takes both edges from its system counter's bit 12, a host without DIV clocks
  them itself. ``frame_clock_high`` is that clock's level, read when NR52 powers the APU on;
- registers by their Game Boy address: ``read``/``write`` of NR10-NR52 ($FF10-$FF26) and wave RAM
  ($FF30-$FF3F), the names below; a host maps its own addresses onto these;
- out: ``digital()`` (each channel's 0-15 DAC input), ``dacs()``, ``nr50``/``nr51`` (panning and
  volume) for a host that mixes them itself, or ``take_samples()`` (this core's labelled-fit mix);
  ``capture_state()`` is an immutable snapshot.

A ``Model`` row holds a revision's differences: DMG, CGB and AGB (the Game Boy Advance's four
Game Boy channels, in its own mode; docs/contract.md "The AGB row"). The behaviour follows Pan Docs
``Audio_details.md``, blargg ``dmg_sound``/``cgb_sound`` and SameSuite's APU ROMs (SameBoy's model
of the CGB); where a constant is fitted to a test it says so. Extracted from gameboy-python, which
pins it.
"""

from __future__ import annotations

from array import array
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Model:
    """A hardware revision's APU: ``cgb`` selects the CGB-family rules (powering off clears the
    length counters and keeps them read-only; wave RAM is reachable at any time while CH3 plays
    and a trigger corrupts nothing; NRx2 writes while playing take the CGB "zombie" path; the CGB
    sweep and noise counters; blargg cgb_sound 08-11, SameSuite)."""

    name: str
    cgb: bool
    #: AGB: wave RAM is two banks of 32 digits held in a shift register (NR30 bit 6 picks the
    #: bank that plays, bit 5 chains both into 64 digits; the CPU reaches the bank that is not
    #: selected), and NR32 bit 7 forces the wave volume to 75 % (GBATEK "GBA Sound Channel 3")
    wave_banks: bool = False


DMG = Model("dmg", cgb=False)  # DMG-CPU (the revisions gameboy-python runs)
CGB = Model("cgb", cgb=True)  # CPU CGB E (gameboy-python's CGB)
# The Game Boy Advance's Game Boy channels, in GBA mode (gba-python): the CGB-family rules plus
# the wave channel's two banks. The host clocks the frame sequencer from its own system clock.
AGB = Model("agb", cgb=True, wave_banks=True)
MODELS = {"dmg": DMG, "cgb": CGB, "agb": AGB}

#: register addresses (the Game Boy's; a host maps its own onto them)
NR10, NR11, NR12, NR13, NR14 = 0xFF10, 0xFF11, 0xFF12, 0xFF13, 0xFF14
NR21, NR22, NR23, NR24 = 0xFF16, 0xFF17, 0xFF18, 0xFF19
NR30, NR31, NR32, NR33, NR34 = 0xFF1A, 0xFF1B, 0xFF1C, 0xFF1D, 0xFF1E
NR41, NR42, NR43, NR44 = 0xFF20, 0xFF21, 0xFF22, 0xFF23
NR50, NR51, NR52 = 0xFF24, 0xFF25, 0xFF26
WAVE_RAM = 0xFF30  # to $FF3F

#: bits that always read 1 per register $FF10-$FF26 (Pan Docs Audio_Registers.md, DMG)
READ_MASKS = (
    0x80, 0x3F, 0x00, 0xFF, 0xBF,  # NR10-NR14
    0xFF, 0x3F, 0x00, 0xFF, 0xBF,  # $FF15 (unused), NR21-NR24
    0x7F, 0xFF, 0x9F, 0xFF, 0xBF,  # NR30-NR34
    0xFF, 0xFF, 0x00, 0x00, 0xBF,  # $FF1F (unused), NR41-NR44
    0x00, 0x00, 0x70,  # NR50, NR51, NR52
)  # fmt: skip

#: AGB: NR30's bank bits (6-5) and NR32's force bit (7) read back too
READ_MASKS_AGB = (*READ_MASKS[:0x0A], 0x1F, 0xFF, 0x1F, *READ_MASKS[0x0D:])
#: the wave channel's volume in quarters by NR32 bits 6-5; the AGB's force bit gives 3
WAVE_QUARTERS = (0, 4, 2, 1)

#: NRx4 address -> channel index (the trigger bit is bit 7)
TRIGGERS = {0xFF14: 0, 0xFF19: 1, 0xFF1E: 2, 0xFF23: 3}

DUTY = ((0, 0, 0, 0, 0, 0, 0, 1), (1, 0, 0, 0, 0, 0, 0, 1), (1, 0, 0, 0, 0, 1, 1, 1),
        (0, 1, 1, 1, 1, 1, 1, 0))  # fmt: skip
NOISE_DIVISORS = (8, 16, 32, 48, 64, 80, 96, 112)
#: DMG wave RAM at power-on (Pan Docs; the board's skip-boot state)
WAVE_POWER_ON = bytes.fromhex("84 40 43 AA 2D 78 92 3C 60 59 59 B0 34 B8 2E DA".replace(" ", ""))

WAVE_TRIGGER_DELAY = 6  # FITTED (blargg dmg_sound 09/12): T before the wave channel's first read
WAVE_WINDOW = 2  # FITTED: the wave RAM is reachable for this many T after a channel read
CORRUPT_LO, CORRUPT_HI = 1, 3  # FITTED (blargg dmg_sound 10): ages (T) at which a trigger corrupts
HPF_CHARGE = 0.999958  # per T at 4194304 Hz (Pan Docs Audio_details.md, DMG)
CLOCK_HZ = 4194304


class Envelope:
    """Volume envelope shared by the pulse and noise channels (NRx2): a 3-bit countdown that the
    64 Hz DIV-APU event decrements, a reload on the next (secondary) event after it reaches zero
    that arms the clock, and the clock's tick on the event after that; the clock locks while the
    volume cannot move. The model SameSuite's volume ROMs measure."""

    def __init__(self) -> None:
        self.volume = 0
        self.countdown = 0
        self.clock = False
        self.should_lock = False
        self.locked = False

    def trigger(self, nrx2: int) -> None:
        self.locked = False
        self.clock = False
        self.countdown = nrx2 & 7
        self.volume = nrx2 >> 4

    def set_clock(self, value: bool, up: bool, volume: int) -> None:
        if self.clock == value:
            return
        if value:
            self.clock = True
            self.should_lock = (volume == 0xF and up) or (volume == 0 and not up)
        else:
            self.clock = False
            self.locked |= self.should_lock

    def count(self) -> None:
        """The event that is the eighth since the last: the countdown runs unless a tick is
        armed."""
        if not self.clock:
            self.countdown = (self.countdown - 1) & 7

    def tick(self, nrx2: int) -> bool:
        """The armed clock fires: the volume steps. Returns whether it did."""
        self.set_clock(False, False, 0)
        if self.locked or not nrx2 & 7:
            return False
        self.volume = (self.volume + (1 if nrx2 & 8 else -1)) & 0xF
        return True

    def secondary(self, nrx2: int) -> None:
        """The event half a DIV-APU period after the tick: reload the countdown and arm the
        clock."""
        if self.countdown == 0:
            self.countdown = nrx2 & 7
            self.set_clock(self.countdown != 0, bool(nrx2 & 8), self.volume)

    def _glitch(self, value: int, old: int) -> None:
        if self.clock:
            self.countdown = value & 7
        should_tick = bool(value & 7) and not old & 7 and not self.locked
        if value & 0xF == 8 and old & 0xF == 8 and not self.locked:
            should_tick = True
        if (value ^ old) & 8:
            if value & 8:
                if not old & 7 and not self.locked:
                    self.volume ^= 0xF
                else:
                    self.volume = (0xE - self.volume) & 0xF
                should_tick = False
            else:
                self.volume = (0x10 - self.volume) & 0xF
        if should_tick:
            self.volume = (self.volume + (1 if value & 8 else -1)) & 0xF
        elif not value & 7 and self.clock:
            self.set_clock(False, False, 0)

    def write(self, old: int, new: int, cgb: bool) -> None:
        """NRx2 written while the channel plays (the "zombie mode" glitches)."""
        if cgb:
            self._glitch(new, old)
        else:
            self._glitch(0xFF, old)
            self._glitch(new, 0xFF)


class Pulse:
    """A pulse channel clocked in 2 MHz ticks, the unit SameSuite's PCM12 reads resolve (SameBoy's
    model of the CGB: a countdown per sample, a start-up ``delay`` and a suppressed first
    sample)."""

    def __init__(self, sweep: bool) -> None:
        self.has_sweep = sweep
        self.reset()
        self.length = 0  # survives power-off on the DMG

    def reset(self) -> None:
        self.enabled = False
        self.length_enabled = False
        self.frequency = 0
        self.env = Envelope()
        self.sweep_shadow = 0
        self.sweep_timer = 0
        self.sweep_enabled = False
        self.sweep_negated = False
        self.index = 0  # position in the duty pattern; only turning the APU off resets it
        self.countdown = 0xFFFF  # 2 MHz ticks to the next step
        self.delay = 0  # ticks of start-up left (the phantom step of CGB D/E)
        self.suppressed = False  # the first sample after a start is held
        self.just_reloaded = False
        self.did_tick = False
        self.sample = 0  # the last digital value (what PCM12 shows while the channel is on)

    def output(self) -> int:
        return self.sample if self.enabled else 0


class Wave:
    def __init__(self) -> None:
        self.reset()
        self.length = 0

    def reset(self) -> None:
        self.enabled = False
        self.length_enabled = False
        self.frequency = 0
        self.timer = 0
        self.index = 0
        self.buffer = 0
        self.last_read = -(1 << 30)  # apu T of the last wave RAM read by the channel


class Noise:
    def __init__(self) -> None:
        self.reset()
        self.length = 0

    def reset(self) -> None:
        self.enabled = False
        self.length_enabled = False
        self.timer = 0
        self.lfsr = 0
        self.env = Envelope()
        # CGB: the 14-bit counter whose bit NR43[7:4] clocks the LFSR, in 2 MHz ticks
        self.counter = 0
        self.countdown = 0
        self.alignment = 0  # 2 MHz ticks since power-on; its low bits set the start-up delay
        self.counter_active = False
        self.background_active = False  # the counter keeps running after the channel stops
        self.started_with_dac_off = False
        self.did_step_counter = False
        self.reloaded = False

    def output(self) -> int:
        return self.env.volume if self.enabled and self.lfsr & 1 else 0


@dataclass(frozen=True, slots=True)
class APUState:
    """An immutable snapshot, for comparison and reporting (restoring is not offered)."""

    model: str
    t: int  # T-cycles since construction
    power: bool
    registers: bytes  # $FF10-$FF26 as written
    wave_ram: bytes  # AGB: both banks (32 bytes) as the shift register holds them now
    divider: int  # frame sequencer steps counted (mod 256)
    enabled: tuple[bool, bool, bool, bool]
    lengths: tuple[int, int, int, int]
    volumes: tuple[int, int, int]  # CH1, CH2, CH4 envelopes
    digital: tuple[int, int, int, int]

    def as_dict(self) -> dict[str, object]:
        return {name: getattr(self, name) for name in self.__slots__}  # type: ignore[attr-defined]


class APU:
    def __init__(self, sample_rate: int = 44100, model: Model = DMG) -> None:
        self.model = model
        self.cgb = model.cgb
        self.wave_banks = model.wave_banks
        self._read_masks = READ_MASKS_AGB if model.wave_banks else READ_MASKS
        self.registers = bytearray(0x17)  # $FF10-$FF26 as written
        self.wave_ram = bytearray(WAVE_POWER_ON)
        # AGB: the shift register, one digit per entry: bank 0 then bank 1, each in play order.
        # Playing rotates the selected bank (both, in 64-digit mode), so digits move.
        self.wave_digits = bytearray(64)
        self.power = False
        self.ch1 = Pulse(True)
        self.ch2 = Pulse(False)
        self.ch3 = Wave()
        self.ch4 = Noise()
        self.divider = 0  # DIV-APU events counted (mod 256); its low bits pick what each one clocks
        # 1: the first event after a power-on with the clock high is dropped, 2: and the next does
        # not count
        self.skip_event = 0
        # CGB sweep: a 3-bit period counter stepped by the DIV-APU events, and the calculation it
        # starts, which finishes a few 1 MHz ticks later (SameSuite channel_1_sweep*)
        self.sweep_countdown = 0
        self.calc_countdown = 0
        self.calc_reload = 0
        self.calc_instant = False
        self.sweep_unshifted = False
        self.sweep_addend = 0
        self.sweep_shadow = 0
        self.sweep_completed = 0
        self.restart_hold = 0
        self.lf_div = 1  # the 2 MHz tick's phase against the 1 MHz the pulse channels start on
        self.t = 0  # T since reset
        self._held_t = 0  # an odd T run() has not passed on yet
        # output
        self.sample_rate = sample_rate
        self.capture = False
        self._pending = array("h")
        self._acc_l = self._acc_r = 0.0
        self._acc_t = 0
        self._capacitor = [0.0, 0.0]
        self._per_sample = CLOCK_HZ / sample_rate
        self._charge = HPF_CHARGE ** (CLOCK_HZ / sample_rate)
        self._owed = 0.0

    # ----- state the rest of the board reads ----------------------------------------------

    @property
    def channel_on(self) -> list[bool]:
        return [self.ch1.enabled, self.ch2.enabled, self.ch3.enabled, self.ch4.enabled]

    def dac_on(self, channel: int) -> bool:
        """A channel's DAC is on when NRx2's top five bits (NR30 bit 7 for CH3) are not zero."""
        if channel == 2:
            return bool(self.registers[0x0A] & 0x80)
        return bool(self.registers[(0x02, 0x07, 0, 0x11)[channel]] & 0xF8)

    # ----- registers ----------------------------------------------------------------------

    def read(self, address: int) -> int:
        if address >= 0xFF30:
            return self._read_wave(address - 0xFF30)
        if address >= 0xFF27:
            return 0xFF
        if address == 0xFF26:
            status = sum(1 << i for i, on in enumerate(self.channel_on) if on)
            return 0x70 | (0x80 if self.power else 0) | status
        index = address - 0xFF10
        return self.registers[index] | self._read_masks[index]

    def write(self, address: int, value: int) -> None:
        if address >= 0xFF30:
            self._write_wave(address - 0xFF30, value)
            return
        if address >= 0xFF27:
            return
        if address == 0xFF26:
            self._write_power(bool(value & 0x80))
            return
        if not self.power:
            # DMG: the length counters stay writable (their duty bits do not)
            if not self.cgb and address in (0xFF11, 0xFF16, 0xFF1B, 0xFF20):
                self._write_length(address, value)
            return
        self._write_register(address, value)

    def _write_power(self, on: bool) -> None:
        if self.power and not on:
            self.registers = bytearray(0x17)
            for channel in (self.ch1, self.ch2, self.ch3, self.ch4):
                channel.reset()
                if self.cgb:
                    channel.length = 0
            self.divider = 0
            self.skip_event = 0
            self.sweep_countdown = self.calc_countdown = self.calc_reload = 0
            self.calc_instant = self.sweep_unshifted = False
            self.sweep_addend = self.sweep_shadow = self.sweep_completed = self.restart_hold = 0
        elif on and not self.power:
            if self.frame_clock_high:  # the sequencer clock is high already: a step is spent
                self.divider = 1
                self.skip_event = 1
            self.lf_div = 1
        self.power = on

    frame_clock_high = False  # set by the host: the frame sequencer clock's level (DIV bit 12)

    def _write_length(self, address: int, value: int) -> None:
        if address == 0xFF11:
            self.ch1.length = 64 - (value & 0x3F)
        elif address == 0xFF16:
            self.ch2.length = 64 - (value & 0x3F)
        elif address == 0xFF1B:
            self.ch3.length = 256 - value
        else:
            self.ch4.length = 64 - (value & 0x3F)

    def _write_register(self, address: int, value: int) -> None:
        index = address - 0xFF10
        old = self.registers[index]
        self.registers[index] = value
        ch1, ch2, ch3, ch4 = self.ch1, self.ch2, self.ch3, self.ch4
        if address == 0xFF10 and self.cgb:
            self._write_nr10(old, value)
        elif address == 0xFF10:
            if ch1.sweep_negated and old & 8 and not value & 8:
                ch1.enabled = False  # sweep direction cleared after a negated calculation
        elif address in (0xFF11, 0xFF16, 0xFF1B, 0xFF20):
            self._write_length(address, value)
        elif address in (0xFF12, 0xFF17, 0xFF21):
            channel = {0xFF12: ch1, 0xFF17: ch2, 0xFF21: ch4}[address]
            if not value & 0xF8:
                if channel is ch4 and self.cgb:
                    if ch4.enabled and self.registers[0x12] & 7:
                        if ch4.countdown <= 2:
                            ch4.counter = (ch4.counter + 1) & 0x3FFF
                        ch4.background_active = False
                    ch4.counter_active = False
                channel.enabled = False  # DAC off
            elif channel.enabled:
                channel.env.write(old, value, self.cgb)
                if channel is not ch4:
                    self._refresh(channel)
        elif address == 0xFF22 and self.cgb:
            self._write_nr43(old, value)
        elif address in (0xFF13, 0xFF18):
            channel = ch1 if address == 0xFF13 else ch2
            channel.frequency = (channel.frequency & 0x700) | value
            if channel.just_reloaded:
                channel.countdown = (channel.frequency ^ 0x7FF) * 2 + 1
        elif address == 0xFF1D:
            ch3.frequency = (ch3.frequency & 0x700) | value
        elif address == 0xFF1A:
            if not value & 0x80:
                ch3.enabled = False
        elif address in (0xFF14, 0xFF19, 0xFF1E, 0xFF23):
            self._write_nrx4(address, value, old)

    def _write_nrx4(self, address: int, value: int, old: int) -> None:
        channel = (self.ch1, self.ch2, self.ch3, self.ch4)[TRIGGERS[address]]
        self._old_frequency = getattr(channel, "frequency", 0)
        if TRIGGERS[address] < 2:
            self._pulse_frequency_high(channel, value, old)
        elif address != 0xFF23:
            channel.frequency = (channel.frequency & 0xFF) | ((value & 7) << 8)
        was_enabled = channel.length_enabled
        channel.length_enabled = bool(value & 0x40)
        # extra length clock: length enabled in the half period the next step does not clock
        if channel.length_enabled and not was_enabled and self.divider & 1 and channel.length:
            channel.length -= 1
            if channel.length == 0 and not value & 0x80:
                channel.enabled = False
        if value & 0x80:
            self._trigger(TRIGGERS[address])

    def _trigger(self, index: int) -> None:
        channel = (self.ch1, self.ch2, self.ch3, self.ch4)[index]
        dac = self.dac_on(index)
        if index == 2:
            self._trigger_wave_corruption()
        if not channel.length:
            channel.length = 256 if index == 2 else 64
            if channel.length_enabled and self.divider & 1:
                channel.length -= 1
        if index in (0, 1):
            nrx2 = self.registers[0x02 if index == 0 else 0x07]
            channel.env.trigger(nrx2)
            was_active = channel.enabled
            self._pulse_start(channel, index, dac)
            if index == 0:
                if self.cgb:
                    self._sweep_restart(was_active)
                else:
                    self._trigger_sweep()
            return
        if index == 2:
            channel.enabled = dac
            channel.timer = (2048 - channel.frequency) * 2 + WAVE_TRIGGER_DELAY
            channel.index = 0
        else:
            nr43 = self.registers[0x12]
            channel.env.trigger(self.registers[0x11])
            if self.cgb:
                self._noise_start(channel)  # reads whether the channel was running
                channel.enabled = dac
            else:
                channel.enabled = dac
                channel.lfsr = 0
                channel.timer = self._noise_period(nr43)

    # ----- the sweep on the CGB ------------------------------------------------------------

    def _sweep_done(self) -> None:
        """A sweep calculation finishes: the overflow check uses the addend twice."""
        nr10 = self.registers[0]
        if self.restart_hold == 0:
            self.sweep_shadow = self.ch1.frequency
        if nr10 & 8:
            self.sweep_addend ^= 0x7FF
        if self.sweep_shadow + self.sweep_addend > 0x7FF and not nr10 & 8:
            self.ch1.enabled = False
        self.sweep_completed = self.sweep_addend

    def _sweep_calculation(self) -> None:
        nr10 = self.registers[0]
        ch1 = self.ch1
        if nr10 & 0x70 and self.sweep_countdown == 7:
            if nr10 & 7:
                ch1.frequency = (
                    self.sweep_addend + self.sweep_shadow + (1 if nr10 & 8 else 0)
                ) & 0x7FF
            if self.restart_hold == 0:
                self.sweep_addend = ch1.frequency >> (nr10 & 7)
            self.calc_countdown = nr10 & 7  # recalculation and the overflow check come later
            self.calc_reload = 1 + self.lf_div
            self.sweep_unshifted = not nr10 & 7
            self.sweep_countdown = ((nr10 >> 4) & 7) ^ 7
            if self.calc_countdown == 0:
                self.calc_instant = True

    def _sweep_restart(self, was_active: bool) -> None:
        nr10 = self.registers[0]
        self.calc_instant = False
        self.sweep_shadow = 0
        self.sweep_completed = 0
        if nr10 & 7:  # the overflow check also happens on trigger
            self.calc_countdown = nr10 & 7
            self.calc_reload = 2 + (0 if was_active else 1)
            self.sweep_unshifted = False
            self.sweep_addend = self.ch1.frequency >> (nr10 & 7)
        else:
            self.sweep_addend = 0
        self.restart_hold = 4 - self.lf_div
        self.sweep_countdown = ((nr10 >> 4) & 7) ^ 7

    def _write_nr10(self, old: int, value: int) -> None:
        if self.calc_countdown or self.calc_reload:
            if self.calc_reload == 2:  # the countdown just reloaded: reload it again
                self.calc_countdown = value & 7
                if not self.calc_countdown:
                    self.calc_reload = 0
            if value & 7 and not old & 7 and not self.lf_div and self.calc_countdown > 1:
                self.calc_countdown -= 1
                if not self.calc_countdown:
                    self._sweep_done()
        if (
            self.sweep_shadow + self.sweep_completed + (1 if old & 8 else 0) > 0x7FF
            and not value & 8
        ):
            self.ch1.enabled = False
        self._sweep_calculation()

    def _run_sweep(self, cycles: int) -> None:
        ticks = cycles // 2
        if cycles & 1 and not self.lf_div:
            ticks += 1
        if self.calc_reload > ticks:
            self.calc_reload -= ticks
            ticks = 0
        else:
            if self.calc_reload and not self.calc_countdown and self.calc_instant:
                self._sweep_done()
            self.calc_instant = False
            ticks -= self.calc_reload
            self.calc_reload = 0
        if self.calc_countdown and (self.registers[0] & 7 or self.sweep_unshifted):
            if self.calc_countdown > ticks:
                self.calc_countdown -= ticks
            else:
                self.calc_countdown = 0
                self._sweep_done()
        if self.restart_hold:
            self.restart_hold = 0 if self.restart_hold <= cycles else self.restart_hold - cycles

    # ----- the pulse channels in 2 MHz ticks ----------------------------------------------

    def _refresh(self, channel: Pulse) -> None:
        """The digital value for the current duty step and volume (not while the first sample
        after a start is held)."""
        if channel.suppressed:
            return
        duty = self.registers[0x01 if channel is self.ch1 else 0x06] >> 6
        channel.sample = DUTY[duty][channel.index] * channel.env.volume

    @staticmethod
    def _half(value: int) -> int:
        """C integer division by two (towards zero)."""
        return value // 2 if value >= 0 else -((-value) // 2)

    def _pulse_frequency_high(self, channel: Pulse, value: int, old: int) -> None:
        """NRx4's frequency bits and what a change of them does to a running channel."""
        if (
            not value & 0x80
            and channel.enabled
            and old & 7 == 7
            and value & 7 != 7
            and (self.cgb or channel.countdown & 1)
            and channel.did_tick
            and channel.countdown >> 1 == channel.frequency ^ 0x7FF
        ):  # the length just changed from >= $700 to < $700: undo the step it took too early
            channel.index = (channel.index - 1) & 7
            channel.suppressed = False
        channel.frequency = (channel.frequency & 0xFF) | ((value & 7) << 8)
        if channel.just_reloaded:
            channel.countdown = (channel.frequency ^ 0x7FF) * 2 + 1

    def _pulse_start(self, channel: Pulse, index: int, dac: bool) -> None:
        """A trigger: the countdown and start-up delay depend on whether the channel was running
        and on the tick phase (``lf_div``)."""
        value = self.registers[0x04 if index == 0 else 0x09]
        was_active = channel.enabled
        channel.did_tick = False
        unsuppress = False
        if not was_active:
            if (
                self.cgb
                and not value & 4
                and not self._half(channel.countdown - channel.delay) & 0x400
            ):
                channel.index = (channel.index + 1) & 7  # CGB D/E: a phantom step
                unsuppress = True
            channel.delay = 6 - self.lf_div
        else:
            extra = 0
            if self.cgb:
                if (
                    not channel.just_reloaded
                    and not value & 4
                    and not self._half(channel.countdown - 1 - channel.delay) & 0x400
                ):
                    channel.index = (channel.index + 1) & 7
                    channel.suppressed = False
                elif (
                    channel.frequency == 0x7FF
                    and self._old_frequency != 0x7FF
                    and channel.suppressed
                ):
                    extra = 2
            channel.delay = 4 - self.lf_div + extra
        channel.countdown = ((channel.frequency ^ 0x7FF) * 2 + channel.delay) & 0xFFFF
        if was_active:
            self._refresh(channel)  # the new volume takes effect at once, the new start does not
        if dac and not was_active:
            channel.enabled = True
            channel.sample = 0
            channel.suppressed = not unsuppress
        elif not dac:
            channel.enabled = False

    def _run_pulse(self, channel: Pulse, cycles: int) -> None:
        if not channel.enabled:
            return
        left = cycles
        if channel.delay:
            channel.delay = 0 if channel.delay < left else channel.delay - left
        while left > channel.countdown:
            left -= channel.countdown + 1
            channel.countdown = (channel.frequency ^ 0x7FF) * 2 + 1
            channel.index = (channel.index + 1) & 7
            channel.suppressed = False
            channel.did_tick = True
            self._refresh(channel)
        channel.just_reloaded = left == 0
        if left:
            channel.countdown -= left

    # ----- the noise channel in 2 MHz ticks (CGB) ------------------------------------------

    def _step_lfsr(self) -> None:
        ch4 = self.ch4
        lfsr = ch4.lfsr
        bit = ((lfsr ^ (lfsr >> 1)) & 1) ^ 1  # XNOR of bits 0 and 1
        mask = 0x4040 if self.registers[0x12] & 8 else 0x4000
        lfsr >>= 1
        ch4.lfsr = lfsr | mask if bit else lfsr & ~mask

    def _noise_start(self, ch4: Noise) -> None:
        """A trigger: the counter's countdown to its next step depends on the divisor, on the
        2 MHz tick's alignment against the start, and on whether the counter was running (SameSuite
        channel_4_align, _delay, _frequency_alignment measure these)."""
        nr43 = self.registers[0x12]
        ch4.counter_active = bool(self.registers[0x11] & 0xF8)
        was_dac_off = ch4.started_with_dac_off
        ch4.started_with_dac_off = not ch4.counter_active
        divisor = nr43 & 7
        was_background = ch4.background_active
        ch4.background_active = True
        align = ch4.alignment & 3
        instant_step = div_1_glitch = False
        if divisor > 1 and ch4.countdown == 1:
            ch4.counter = (ch4.counter + 1) & 0x3FFF
        elif ch4.countdown == 2 and align == 0 and ch4.enabled:
            if divisor == 0:
                divisor = 8
            elif divisor == 1:
                if not ch4.did_step_counter:
                    div_1_glitch = True
                mask = 1 << (nr43 >> 4)
                old_bit = ch4.counter & mask
                ch4.counter = (ch4.counter + 1) & 0x3FFF
                instant_step = bool(ch4.counter & mask) and not old_bit
        countdown = 6 if divisor == 0 else divisor * 4 + 6
        if ch4.alignment & 1:
            if not divisor:
                countdown += -1 if was_background else 1
            elif ch4.alignment & 2:
                countdown += 1 if divisor == 1 and not ch4.enabled else -3
            else:
                countdown -= 1
                if divisor == 1 and ch4.enabled:
                    countdown -= 4
        elif divisor:
            if ch4.alignment & 2:
                countdown -= 2
            elif divisor > 1 or (divisor == 1 and ch4.enabled and not nr43 & 0xF0):
                countdown -= 4
        if divisor > 1:
            if not ch4.counter_active and not align:
                countdown += 4
        elif was_background and not ch4.enabled and not align:
            if divisor == 0:
                if was_dac_off:
                    countdown += 28
            else:
                countdown -= 4
        if div_1_glitch:
            countdown -= 4
        ch4.countdown = countdown
        ch4.lfsr = 0x55 if not divisor and ch4.enabled and align == 3 else 0
        ch4.did_step_counter = align == 2
        if instant_step:
            self._step_lfsr()

    def _run_noise(self, cycles: int) -> None:
        ch4 = self.ch4
        ch4.alignment += cycles
        if not (ch4.counter_active or ch4.background_active):
            return
        nr43 = self.registers[0x12]
        divisor = (nr43 & 7) << 2 or 2
        left = cycles
        if ch4.countdown == 0:
            ch4.countdown = divisor
        while left >= ch4.countdown:
            left -= ch4.countdown
            ch4.countdown = divisor
            mask = 1 << (nr43 >> 4)
            old_bit = ch4.counter & mask
            ch4.counter = (ch4.counter + 1) & 0x3FFF
            ch4.did_step_counter = True
            if ch4.counter & mask and not old_bit and ch4.enabled:
                self._step_lfsr()
        if left:
            ch4.countdown -= left
            ch4.reloaded = False
        else:
            ch4.reloaded = True

    def _write_nr43(self, old: int, new: int) -> None:
        """NR43 written: a changed clock-select bit glitches the LFSR (the plain, deterministic
        variants of what the CGB E does; the revision- and instance-specific ones are not here)."""
        ch4 = self.ch4
        if ch4.reloaded:
            divisor = (new & 7) << 2 or 2
            ch4.countdown = divisor + (0 if divisor == 2 else (2, 1, 0, 3)[ch4.alignment & 3])
        if old & 0xF0 == new & 0xF0:
            return
        counter = ch4.counter
        old_bit = (counter >> (old >> 4)) & 1
        glitch_bit = (counter >> (((old & 0x7F) | (new & 0x80)) >> 4)) & 1
        new_bit = (counter >> (new >> 4)) & 1
        if old_bit == new_bit and new_bit != glitch_bit:
            if not new_bit or not new & 0x80:
                self._step_lfsr()
        elif not old_bit and new_bit:
            self._step_lfsr()

    @staticmethod
    def _noise_period(nr43: int) -> int:
        shift = nr43 >> 4
        return NOISE_DIVISORS[nr43 & 7] << shift if shift < 14 else 1 << 30

    # ----- the sweep ----------------------------------------------------------------------

    def _sweep_calc(self) -> int:
        nr10 = self.registers[0]
        delta = self.ch1.sweep_shadow >> (nr10 & 7)
        if nr10 & 8:
            self.ch1.sweep_negated = True
            return self.ch1.sweep_shadow - delta
        return self.ch1.sweep_shadow + delta

    def _trigger_sweep(self) -> None:
        ch1 = self.ch1
        nr10 = self.registers[0]
        ch1.sweep_shadow = ch1.frequency
        ch1.sweep_timer = ((nr10 >> 4) & 7) or 8
        ch1.sweep_enabled = bool(nr10 & 0x77)
        ch1.sweep_negated = False
        if nr10 & 7 and self._sweep_calc() > 2047:
            ch1.enabled = False

    def _clock_sweep(self) -> None:
        ch1 = self.ch1
        ch1.sweep_timer -= 1
        if ch1.sweep_timer > 0:
            return
        period = (self.registers[0] >> 4) & 7
        ch1.sweep_timer = period or 8
        if not (ch1.sweep_enabled and period):
            return
        new = self._sweep_calc()
        if new > 2047:
            ch1.enabled = False
        elif self.registers[0] & 7:
            ch1.sweep_shadow = new
            ch1.frequency = new
            self.registers[0x03] = new & 0xFF
            self.registers[0x04] = (self.registers[0x04] & ~7) | (new >> 8)
            if self._sweep_calc() > 2047:
                ch1.enabled = False

    # ----- wave RAM -----------------------------------------------------------------------

    def _wave_reachable(self) -> bool:
        return self.t - self.ch3.last_read < WAVE_WINDOW

    def _other_bank(self) -> int:
        """AGB: where the bank the CPU reaches starts in the shift register: the one NR30 bit 6
        does not select, playing or not (GBATEK; with the APU off NR30 is 0, so bank 1)."""
        return 0 if self.registers[0x0A] & 0x40 else 32

    def _read_wave(self, offset: int) -> int:
        if self.wave_banks:
            at = self._other_bank() + 2 * offset
            return (self.wave_digits[at] << 4) | self.wave_digits[at + 1]
        ch3 = self.ch3
        if not (ch3.enabled and self.power):
            return self.wave_ram[offset]
        if self.cgb or self._wave_reachable():
            return self.wave_ram[ch3.index >> 1]
        return 0xFF

    def _write_wave(self, offset: int, value: int) -> None:
        if self.wave_banks:
            at = self._other_bank() + 2 * offset
            self.wave_digits[at] = value >> 4
            self.wave_digits[at + 1] = value & 0xF
            return
        ch3 = self.ch3
        if not (ch3.enabled and self.power):
            self.wave_ram[offset] = value
        elif self.cgb or self._wave_reachable():
            self.wave_ram[ch3.index >> 1] = value

    def _trigger_wave_corruption(self) -> None:
        """DMG: triggering CH3 while it reads wave RAM rewrites the first bytes (Pan Docs)."""
        ch3 = self.ch3
        if self.cgb or not (ch3.enabled and CORRUPT_LO <= self.t - ch3.last_read < CORRUPT_HI):
            return
        position = ((ch3.index + 1) & 31) >> 1
        if position < 4:
            self.wave_ram[0] = self.wave_ram[position]
        else:
            start = position & ~3
            self.wave_ram[0:4] = self.wave_ram[start : start + 4]

    # ----- time ---------------------------------------------------------------------------

    def frame_tick(self) -> None:
        """A falling edge of DIV's bit 12 (DIV-APU): the next frame sequencer step."""
        if not self.power:
            return
        if self.skip_event == 1:
            self.skip_event = 2
            return
        if self.skip_event == 2:
            self.skip_event = 0
        else:
            self.divider = (self.divider + 1) & 0xFF
        divider = self.divider
        for channel, nrx2 in self._envelopes():
            env = channel.env
            if divider & 7 == 7:
                env.count()
            if (
                env.clock
                and env.tick(self.registers[nrx2])
                and channel.enabled
                and channel is not self.ch4
            ):
                self._refresh(channel)
        if divider & 1:
            self._clock_lengths()
        if divider & 3 == 3:
            if self.cgb:
                self.sweep_countdown = (self.sweep_countdown + 1) & 7
                self._sweep_calculation()
            else:
                self._clock_sweep()

    def _envelopes(self):
        return ((self.ch1, 0x02), (self.ch2, 0x07), (self.ch4, 0x11))

    def secondary_tick(self) -> None:
        """A rising edge of DIV's bit 12: half a period after the falling one."""
        if not self.power:
            return
        for channel, nrx2 in self._envelopes():
            if channel.enabled:
                channel.env.secondary(self.registers[nrx2])

    def _clock_lengths(self) -> None:
        for channel in (self.ch1, self.ch2, self.ch3, self.ch4):
            if channel.length_enabled and channel.length:
                channel.length -= 1
                if channel.length == 0:
                    channel.enabled = False

    def run(self, t: int) -> None:
        """Catch up ``t`` T-cycles in the Game Boy's steps: 4 at a time (one M-cycle), the rest
        last. Sequencer edges and register accesses go between calls. The channels count 2 MHz
        ticks, two T each: an odd T is held and joins the next call, so a host whose clock is
        not the Game Boy's loses no time."""
        t += self._held_t
        self._held_t = t & 1
        t -= self._held_t
        while t >= 4:
            self.tick(4)
            t -= 4
        if t:
            self.tick(t)

    def tick(self, t: int = 4) -> None:
        """Advance ``t`` T-cycles (the Game Boy passes 4, one M-cycle; 2 in CGB double speed)."""
        base = self.t
        self.t = base + t
        if self.power:
            cycles = t >> 1
            self.lf_div ^= cycles & 1
            if self.cgb:
                self._run_sweep(cycles)
            self._run_pulse(self.ch1, cycles)
            self._run_pulse(self.ch2, cycles)
            self._tick_wave(base, t)
            if self.cgb:
                self._run_noise(cycles)
            else:
                self._tick_noise(t)
        if self.capture:
            self._sample(t)

    def _tick_wave(self, base: int, t: int) -> None:
        ch3 = self.ch3
        if not ch3.enabled:
            return
        ch3.timer -= t
        if self.wave_banks:
            period = (2048 - ch3.frequency) * 2
            while ch3.timer <= 0:
                ch3.timer += period
                self._shift_wave()
            return
        while ch3.timer <= 0:
            at = base + t + ch3.timer  # when inside this tick the period ran out
            ch3.timer += (2048 - ch3.frequency) * 2
            ch3.index = (ch3.index + 1) & 31
            byte = self.wave_ram[ch3.index >> 1]
            ch3.buffer = (byte >> 4) if not ch3.index & 1 else byte & 0xF
            ch3.last_read = at

    def _shift_wave(self) -> None:
        """AGB: the shift register moves one digit and the digit that leaves its end is played
        (and re-enters at the other end). One bank is a ring of 32; in 64-digit mode both are
        one ring that starts with the selected bank (GBATEK)."""
        digits = self.wave_digits
        nr30 = self.registers[0x0A]
        start = 32 if nr30 & 0x40 else 0
        out = digits[start]
        if nr30 & 0x20:
            other = start ^ 32
            digits[start : start + 31] = digits[start + 1 : start + 32]
            digits[start + 31] = digits[other]
            digits[other : other + 31] = digits[other + 1 : other + 32]
            digits[other + 31] = out
        else:
            digits[start : start + 31] = digits[start + 1 : start + 32]
            digits[start + 31] = out
        self.ch3.buffer = out

    def _tick_noise(self, t: int) -> None:
        ch4 = self.ch4
        if not ch4.enabled:
            return
        ch4.timer -= t
        while ch4.timer <= 0:
            ch4.timer += self._noise_period(self.registers[0x12])
            lfsr = ch4.lfsr
            bit = ((lfsr ^ (lfsr >> 1)) & 1) ^ 1  # XNOR of bits 0 and 1
            lfsr = (lfsr >> 1) | (bit << 14)
            if self.registers[0x12] & 8:
                lfsr = (lfsr & ~0x40) | (bit << 6)
            ch4.lfsr = lfsr

    # ----- output -------------------------------------------------------------------------

    def wave_output(self) -> int:
        ch3 = self.ch3
        if not ch3.enabled:
            return 0
        return (ch3.buffer * self.wave_quarters()) >> 2

    def wave_quarters(self) -> int:
        """The wave channel's volume in quarters (NR32): 0, 4 (100 %), 2 or 1; on the AGB bit 7
        forces 3 (75 %)."""
        nr32 = self.registers[0x0C]
        if self.wave_banks and nr32 & 0x80:
            return 3
        return WAVE_QUARTERS[(nr32 >> 5) & 3]

    def wave_digit(self) -> int:
        """The wave channel's current digit (0-15) before its volume; 0 while it is off. With
        ``wave_quarters()``, what a host needs to mix the AGB's 75 % exactly."""
        return self.ch3.buffer if self.ch3.enabled else 0

    def volumes(self) -> tuple[int, int, int]:
        """The envelope volumes (0-15) of channels 1, 2 and 4: a host that mixes around zero (the
        AGB's digital mixer) needs them beside ``digital()``."""
        return (self.ch1.env.volume, self.ch2.env.volume, self.ch4.env.volume)

    def audible(self) -> bool:
        """Whether any channel routed to an output (NR51) is producing a non-zero digital value
        through an enabled DAC: Gambatte's "outaudio" criterion (no DC, no filter)."""
        nr51 = self.registers[0x15]
        return self.power and any(
            value and self.dac_on(i) and nr51 & (0x11 << i)
            for i, value in enumerate(self.digital())
        )

    def digital(self) -> tuple[int, int, int, int]:
        """The four channels' digital outputs (0-15), the DACs' inputs."""
        return (self.ch1.output(), self.ch2.output(), self.wave_output(), self.ch4.output())

    def dacs(self) -> tuple[bool, bool, bool, bool]:
        """Which channels' DACs are on (an off DAC outputs nothing at all)."""
        return (self.dac_on(0), self.dac_on(1), self.dac_on(2), self.dac_on(3))

    @property
    def nr50(self) -> int:
        """Master volume (bits 6-4 left, 2-0 right) and the VIN bits, as written."""
        return self.registers[0x14]

    @property
    def nr51(self) -> int:
        """Panning: bit 4 + n sends channel n + 1 left, bit n sends it right."""
        return self.registers[0x15]

    def capture_state(self) -> APUState:
        return APUState(
            model=self.model.name,
            t=self.t,
            power=self.power,
            registers=bytes(self.registers),
            wave_ram=self._wave_bytes(),
            divider=self.divider,
            enabled=(self.ch1.enabled, self.ch2.enabled, self.ch3.enabled, self.ch4.enabled),
            lengths=(self.ch1.length, self.ch2.length, self.ch3.length, self.ch4.length),
            volumes=(self.ch1.env.volume, self.ch2.env.volume, self.ch4.env.volume),
            digital=self.digital(),
        )

    def _wave_bytes(self) -> bytes:
        if not self.wave_banks:
            return bytes(self.wave_ram)
        digits = self.wave_digits
        return bytes((digits[i] << 4) | digits[i + 1] for i in range(0, 64, 2))

    def _mix(self) -> tuple[float, float]:
        """Analogue left and right, -1..1 before the volume: DAC maps digital 0 to +1 and 15 to
        -1, an off DAC to 0; NR51 pans; NR50 scales by (volume + 1) / 8. LABELLED FIT: the host
        output is this sum divided by 4, high-passed; the real DMG is analogue."""
        outputs = self.digital()
        nr50, nr51 = self.registers[0x14], self.registers[0x15]
        left = right = 0.0
        for i, digital in enumerate(outputs):
            if not self.dac_on(i):
                continue
            analog = (7.5 - digital) / 7.5
            if nr51 & (0x10 << i):
                left += analog
            if nr51 & (1 << i):
                right += analog
        return left * (((nr50 >> 4) & 7) + 1) / 8 / 4, right * ((nr50 & 7) + 1) / 8 / 4

    def _sample(self, t: int) -> None:
        left, right = self._mix()
        self._acc_l += left * t
        self._acc_r += right * t
        self._acc_t += t
        self._owed += t
        while self._owed >= self._per_sample:
            self._owed -= self._per_sample
            span = self._acc_t or 1
            out = []
            any_dac = any(self.dac_on(i) for i in range(4)) and self.power
            for k, value in enumerate((self._acc_l / span, self._acc_r / span)):
                if any_dac:
                    filtered = value - self._capacitor[k]
                    self._capacitor[k] = value - filtered * self._charge
                else:
                    filtered = 0.0
                out.append(max(-32768, min(32767, int(filtered * 32767))))
            self._pending.extend(out)
            self._acc_l = self._acc_r = 0.0
            self._acc_t = 0

    def take_samples(self) -> array:
        """Interleaved signed 16-bit stereo since the last call (capture must be on)."""
        out, self._pending = self._pending, array("h")
        return out

    # ----- boot hand-off ------------------------------------------------------------------

    def pcm(self, pair: int) -> int:
        """PCM12/PCM34 ($FF76/$FF77, CGB): the channels' current 4-bit digital outputs, channel
        1 or 3 in the low nibble and 2 or 4 in the high one (Pan Docs Audio_Registers.md)."""
        outputs = self.digital() if self.power else (0, 0, 0, 0)
        return outputs[2 * pair] | (outputs[2 * pair + 1] << 4)

    def skip_boot(self) -> None:
        """The state the DMG boot ROM leaves: powered, the registers it writes, channel 1 on."""
        self.power = True
        self.divider = 0
        for address, value in (
            (0x10, 0x80), (0x11, 0xBF), (0x12, 0xF3), (0x13, 0xFF), (0x14, 0xBF),
            (0x16, 0x3F), (0x17, 0x00), (0x18, 0xFF), (0x19, 0xBF),
            (0x1A, 0x7F), (0x1B, 0xFF), (0x1C, 0x9F), (0x1D, 0xFF), (0x1E, 0xBF),
            (0x20, 0xFF), (0x21, 0x00), (0x22, 0x00), (0x23, 0xBF),
            (0x24, 0x77), (0x25, 0xF3),
        ):  # fmt: skip
            self.registers[address - 0x10] = value
        self.ch1.enabled = True  # NR52 reads $F1 at $0100
        self.ch1.frequency = 0x7FF
        self.ch1.countdown = 1
        self.ch1.env.volume = 0
        self.ch1.length = 64


__all__ = [
    "AGB",
    "APU",
    "CGB",
    "DMG",
    "MODELS",
    "NR10",
    "NR11",
    "NR12",
    "NR13",
    "NR14",
    "NR21",
    "NR22",
    "NR23",
    "NR24",
    "NR30",
    "NR31",
    "NR32",
    "NR33",
    "NR34",
    "NR41",
    "NR42",
    "NR43",
    "NR44",
    "NR50",
    "NR51",
    "NR52",
    "WAVE_RAM",
    "APUState",
    "Model",
]

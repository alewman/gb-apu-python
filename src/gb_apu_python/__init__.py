"""The APU behind a pin-shaped boundary (docs/handoff-brief.md "Decisions").

In: register reads and writes ($FF10-$FF3F), ``tick(t)`` for the clock (the board calls it every
M-cycle with t = 4) and ``frame_tick()`` for every falling edge of the system counter's bit 12
(DIV-APU, 512 Hz). Out: ``take_samples()`` (host-rate stereo, labelled analogue fit below) and
the per-channel digital outputs. This package imports nothing from the rest of the board
(tests/test_import_boundaries.py).

The model is the DMG-CPU one (docs/start-here.md): Pan Docs ``Audio_details.md`` and the
Blargg ``dmg_sound`` behaviours it names. Where a constant is fitted to a test it says so.
"""

from __future__ import annotations

from array import array

#: bits that always read 1 per register $FF10-$FF26 (Pan Docs Audio_Registers.md, DMG)
READ_MASKS = (
    0x80, 0x3F, 0x00, 0xFF, 0xBF,  # NR10-NR14
    0xFF, 0x3F, 0x00, 0xFF, 0xBF,  # $FF15 (unused), NR21-NR24
    0x7F, 0xFF, 0x9F, 0xFF, 0xBF,  # NR30-NR34
    0xFF, 0xFF, 0x00, 0x00, 0xBF,  # $FF1F (unused), NR41-NR44
    0x00, 0x00, 0x70,  # NR50, NR51, NR52
)  # fmt: skip

#: NRx4 address -> channel index (the trigger bit is bit 7)
TRIGGERS = {0xFF14: 0, 0xFF19: 1, 0xFF1E: 2, 0xFF23: 3}

DUTY = ((0, 0, 0, 0, 0, 0, 0, 1), (1, 0, 0, 0, 0, 0, 0, 1), (1, 0, 0, 0, 1, 1, 1, 1),
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
    """Volume envelope shared by the pulse and noise channels (NRx2)."""

    def __init__(self) -> None:
        self.volume = 0
        self.timer = 0
        self.enabled = False

    def reload(self, nrx2: int) -> int:
        period = nrx2 & 7
        self.timer = period or 8
        return period

    def trigger(self, nrx2: int, next_step: int) -> None:
        self.volume = nrx2 >> 4
        self.reload(nrx2)
        if next_step == 7:  # triggered just before the envelope clock: its timer is one longer
            self.timer += 1
        self.enabled = True

    def clock(self, nrx2: int) -> None:
        if not self.enabled:
            return
        self.timer -= 1
        if self.timer <= 0:
            period = self.reload(nrx2)
            if period:
                volume = self.volume + (1 if nrx2 & 8 else -1)
                if 0 <= volume <= 15:
                    self.volume = volume
                else:
                    self.enabled = False

    def write(self, old: int, new: int, active: bool) -> None:
        """ "Zombie mode": NRx2 written while the channel plays (Pan Docs Obscure Behavior)."""
        if not active:
            return
        if (old & 7) == 0 and self.enabled:
            self.volume += 1
        elif not old & 8:
            self.volume += 2
        if (old ^ new) & 8:
            self.volume = 16 - self.volume
        self.volume &= 0xF


class Pulse:
    def __init__(self, sweep: bool) -> None:
        self.has_sweep = sweep
        self.reset()
        self.length = 0  # survives power-off on the DMG

    def reset(self) -> None:
        self.enabled = False
        self.length_enabled = False
        self.duty = 0
        self.duty_step = 0
        self.timer = 0
        self.frequency = 0
        self.env = Envelope()
        self.sweep_shadow = 0
        self.sweep_timer = 0
        self.sweep_enabled = False
        self.sweep_negated = False
        self.silent_step = True  # after power-on the first step after a trigger plays as 0

    def output(self) -> int:
        if not self.enabled or self.silent_step:
            return 0
        return DUTY[self.duty][self.duty_step] * self.env.volume

    def tick(self, t: int) -> None:
        if not self.enabled:
            return
        self.timer -= t
        while self.timer <= 0:
            self.timer += (2048 - self.frequency) * 4
            self.duty_step = (self.duty_step + 1) & 7
            self.silent_step = False


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

    def output(self) -> int:
        return self.env.volume if self.enabled and self.lfsr & 1 else 0


class APU:
    def __init__(self, sample_rate: int = 44100) -> None:
        self.registers = bytearray(0x17)  # $FF10-$FF26 as written
        self.wave_ram = bytearray(WAVE_POWER_ON)
        self.power = False
        self.ch1 = Pulse(True)
        self.ch2 = Pulse(False)
        self.ch3 = Wave()
        self.ch4 = Noise()
        self.step = 0  # the frame sequencer step the next DIV-APU tick runs
        self.t = 0  # T since reset
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
        return self.registers[index] | READ_MASKS[index]

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
            if address in (0xFF11, 0xFF16, 0xFF1B, 0xFF20):
                self._write_length(address, value)
            return
        self._write_register(address, value)

    def _write_power(self, on: bool) -> None:
        if self.power and not on:
            self.registers = bytearray(0x17)
            for channel in (self.ch1, self.ch2, self.ch3, self.ch4):
                channel.reset()
            self.step = 0
        elif on and not self.power:
            self.step = 1 if self.div_bit12 else 0  # DIV-APU's bit is already high: step 0 is spent
        self.power = on

    div_bit12 = False  # set by the board: DIV's bit 12 as the write happens

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
        if address == 0xFF10:
            if ch1.sweep_negated and old & 8 and not value & 8:
                ch1.enabled = False  # sweep direction cleared after a negated calculation
        elif address in (0xFF11, 0xFF16):
            self._write_length(address, value)
            (ch1 if address == 0xFF11 else ch2).duty = value >> 6
        elif address in (0xFF1B, 0xFF20):
            self._write_length(address, value)
        elif address in (0xFF12, 0xFF17, 0xFF21):
            channel = {0xFF12: ch1, 0xFF17: ch2, 0xFF21: ch4}[address]
            channel.env.write(old, value, channel.enabled)
            if not value & 0xF8:
                channel.enabled = False  # DAC off
        elif address == 0xFF13:
            ch1.frequency = (ch1.frequency & 0x700) | value
        elif address == 0xFF18:
            ch2.frequency = (ch2.frequency & 0x700) | value
        elif address == 0xFF1D:
            ch3.frequency = (ch3.frequency & 0x700) | value
        elif address == 0xFF1A:
            if not value & 0x80:
                ch3.enabled = False
        elif address in (0xFF14, 0xFF19, 0xFF1E, 0xFF23):
            self._write_nrx4(address, value)

    def _write_nrx4(self, address: int, value: int) -> None:
        channel = (self.ch1, self.ch2, self.ch3, self.ch4)[TRIGGERS[address]]
        if address != 0xFF23:
            channel.frequency = (channel.frequency & 0xFF) | ((value & 7) << 8)
        was_enabled = channel.length_enabled
        channel.length_enabled = bool(value & 0x40)
        # extra length clock: length enabled in the half period the next step does not clock
        if channel.length_enabled and not was_enabled and self.step & 1 and channel.length:
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
            if channel.length_enabled and self.step & 1:
                channel.length -= 1
        channel.enabled = dac
        if index in (0, 1):
            nrx2 = self.registers[0x02 if index == 0 else 0x07]
            channel.env.trigger(nrx2, self.step)
            channel.timer = (2048 - channel.frequency) * 4
            if index == 0:
                self._trigger_sweep()
        elif index == 2:
            channel.timer = (2048 - channel.frequency) * 2 + WAVE_TRIGGER_DELAY
            channel.index = 0
        else:
            nr43 = self.registers[0x12]
            channel.env.trigger(self.registers[0x11], self.step)
            channel.lfsr = 0
            channel.timer = self._noise_period(nr43)

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

    def _read_wave(self, offset: int) -> int:
        ch3 = self.ch3
        if not (ch3.enabled and self.power):
            return self.wave_ram[offset]
        return self.wave_ram[ch3.index >> 1] if self._wave_reachable() else 0xFF

    def _write_wave(self, offset: int, value: int) -> None:
        ch3 = self.ch3
        if not (ch3.enabled and self.power):
            self.wave_ram[offset] = value
        elif self._wave_reachable():
            self.wave_ram[ch3.index >> 1] = value

    def _trigger_wave_corruption(self) -> None:
        """DMG: triggering CH3 while it reads wave RAM rewrites the first bytes (Pan Docs)."""
        ch3 = self.ch3
        if not (ch3.enabled and CORRUPT_LO <= self.t - ch3.last_read < CORRUPT_HI):
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
        step = self.step
        self.step = (step + 1) & 7
        if not step & 1:
            self._clock_lengths()
        if step in (2, 6):
            self._clock_sweep()
        if step == 7:
            self.ch1.env.clock(self.registers[0x02])
            self.ch2.env.clock(self.registers[0x07])
            self.ch4.env.clock(self.registers[0x11])

    def _clock_lengths(self) -> None:
        for channel in (self.ch1, self.ch2, self.ch3, self.ch4):
            if channel.length_enabled and channel.length:
                channel.length -= 1
                if channel.length == 0:
                    channel.enabled = False

    def tick(self, t: int = 4) -> None:
        """Advance ``t`` T-cycles (the board passes 4: one M-cycle)."""
        base = self.t
        self.t = base + t
        if self.power:
            self.ch1.tick(t)
            self.ch2.tick(t)
            self._tick_wave(base, t)
            self._tick_noise(t)
        if self.capture:
            self._sample(t)

    def _tick_wave(self, base: int, t: int) -> None:
        ch3 = self.ch3
        if not ch3.enabled:
            return
        ch3.timer -= t
        while ch3.timer <= 0:
            at = base + t + ch3.timer  # when inside this tick the period ran out
            ch3.timer += (2048 - ch3.frequency) * 2
            ch3.index = (ch3.index + 1) & 31
            byte = self.wave_ram[ch3.index >> 1]
            ch3.buffer = (byte >> 4) if not ch3.index & 1 else byte & 0xF
            ch3.last_read = at

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
        shift = (0, 0, 1, 2)[(self.registers[0x0C] >> 5) & 3]
        return 0 if (self.registers[0x0C] >> 5) & 3 == 0 else ch3.buffer >> shift

    def audible(self) -> bool:
        """Whether any channel routed to an output (NR51) is producing a non-zero digital value
        through an enabled DAC: Gambatte's "outaudio" criterion (no DC, no filter)."""
        nr51 = self.registers[0x15]
        return self.power and any(
            value and self.dac_on(i) and nr51 & (0x11 << i)
            for i, value in enumerate(self.digital())
        )

    def digital(self) -> tuple[int, int, int, int]:
        """The four channels' digital outputs (0-15)."""
        return (self.ch1.output(), self.ch2.output(), self.wave_output(), self.ch4.output())

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
        self.step = 0
        for address, value in (
            (0x10, 0x80), (0x11, 0xBF), (0x12, 0xF3), (0x13, 0xFF), (0x14, 0xBF),
            (0x16, 0x3F), (0x17, 0x00), (0x18, 0xFF), (0x19, 0xBF),
            (0x1A, 0x7F), (0x1B, 0xFF), (0x1C, 0x9F), (0x1D, 0xFF), (0x1E, 0xBF),
            (0x20, 0xFF), (0x21, 0x00), (0x22, 0x00), (0x23, 0xBF),
            (0x24, 0x77), (0x25, 0xF3),
        ):  # fmt: skip
            self.registers[address - 0x10] = value
        self.ch1.enabled = True  # NR52 reads $F1 at $0100
        self.ch1.silent_step = False
        self.ch1.frequency = 0x7FF
        self.ch1.duty = 2
        self.ch1.env.volume = 0
        self.ch1.length = 64

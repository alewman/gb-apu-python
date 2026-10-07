"""The APU behind a pin-shaped boundary (docs/handoff-brief.md "Decisions").

Milestone 1 has only the register file: reads return the stored value OR the register's unused
bits (Pan Docs Audio_Registers.md; Mooneye bits/unused_hwio-GS), NR52 carries the power bit, and
powering off clears the registers. The channels, mixer and frame sequencer are milestone 4. This
package imports nothing from the rest of the board (tests/test_boundaries.py).
"""

from __future__ import annotations

#: bits that always read 1 per register $FF10-$FF26 (Pan Docs Audio_Registers.md, DMG)
READ_MASKS = (
    0x80, 0x3F, 0x00, 0xFF, 0xBF,  # NR10-NR14
    0xFF, 0x3F, 0x00, 0xFF, 0xBF,  # $FF15 (unused), NR21-NR24
    0x7F, 0xFF, 0x9F, 0xFF, 0xBF,  # NR30-NR34
    0xFF, 0xFF, 0x00, 0x00, 0xBF,  # $FF1F (unused), NR41-NR44
    0x00, 0x00, 0x70,  # NR50, NR51, NR52
)  # fmt: skip

#: registers that stay writable with the APU off (DMG: the length counters; Pan Docs
#: Audio_details.md "DMG: length counters are writable while off"; refined in milestone 4)
LENGTH_REGISTERS = (0x11, 0x16, 0x1B, 0x20)


class APU:
    def __init__(self) -> None:
        self.registers = bytearray(0x17)  # $FF10-$FF26
        self.wave_ram = bytearray(16)
        self.power = False

    def read(self, address: int) -> int:
        if address >= 0xFF30:
            return self.wave_ram[address - 0xFF30]
        if address >= 0xFF27:
            return 0xFF
        index = address - 0xFF10
        if address == 0xFF26:
            return 0x70 | (0x80 if self.power else 0)
        return self.registers[index] | READ_MASKS[index]

    def write(self, address: int, value: int) -> None:
        if address >= 0xFF30:
            self.wave_ram[address - 0xFF30] = value
            return
        if address >= 0xFF27:
            return
        if address == 0xFF26:
            power = bool(value & 0x80)
            if self.power and not power:
                self.registers = bytearray(0x17)
            self.power = power
            return
        if self.power or address in LENGTH_REGISTERS:
            self.registers[address - 0xFF10] = value

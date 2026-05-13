#!/usr/bin/env python3.14
# -*- coding: utf-8 -*-
# cython: language_level=3, boundscheck=False, wraparound=False, cdivision=True
# =============================================================================
#  acnesemu 0.1  -  cat's NES emulator (single file)
#  Cython-compatible pure Python  /  tkinter  /  FCEUX-style GUI
#  Theme: black bg, blue text, blue hue accents  /  600x400
#  by Flames / Samsoft / Team Flames                                       owo
# =============================================================================
#
#  Build (optional Cython):
#     cythonize -i acnesemu0_1.py    # produces .so / .pyd
#
#  Pure Python run:
#     python3.14 acnesemu0.1.py
#
#  Status:
#     [x] iNES + common mappers 0,1,2,3,4,7 (+ loose ID aliases)
#     [x] 6502 CPU core (official opcodes, NMI/IRQ vectors)
#     [x] memory bus + 2KB internal RAM mirror
#     [x] PPU pattern-table viewer (real CHR decode, 2bpp planar)
#     [x] FCEUX-style menu: File / NES / Config / Tools / Debug / Help
#     [x] step / run / reset / power buttons
#     [x] PPU: nametables + scroll + BG + 8x8 sprites → 256×240 canvas
#     [ ] APU                                              ( ditto )
# =============================================================================

from __future__ import annotations
import os, sys, struct, time, threading, tempfile
import tkinter as tk
from tkinter import filedialog, messagebox

# -----------------------------------------------------------------------------
#  theme   ( black bg / blue text  -  fceux-ish )
# -----------------------------------------------------------------------------
BG       = "#000000"
FG       = "#00aaff"     # blue text
ACCENT   = "#0066aa"     # darker blue hue
DIM      = "#003355"     # disabled / inset
EDGE     = "#0088cc"     # button border-ish
HI       = "#00ddff"     # highlight
FONT_MONO  = ("Courier", 9)
FONT_MONO_B= ("Courier", 9, "bold")
FONT_UI    = ("Courier", 8)

# =============================================================================
#  iNES ROM loader
# =============================================================================
class INES:
    """Parse an iNES (.nes) ROM file; mapper field selects cartridge logic."""
    __slots__ = ("prg", "chr", "mapper", "mirror", "battery",
                 "prg_banks", "chr_banks", "trainer", "path")

    def __init__(self, path: str):
        self.path = path
        with open(path, "rb") as f:
            data = f.read()
        if len(data) < 16 or data[:4] != b"NES\x1a":
            raise ValueError("not an iNES rom")
        prg_banks = data[4]                 # 16KB units
        chr_banks = data[5]                 #  8KB units
        flags6    = data[6]
        flags7    = data[7]
        self.prg_banks = prg_banks
        self.chr_banks = chr_banks
        self.mirror    = "V" if (flags6 & 1) else "H"
        self.battery   = bool(flags6 & 2)
        self.trainer   = bool(flags6 & 4)
        self.mapper    = ((flags6 >> 4) & 0x0F) | (flags7 & 0xF0)
        off = 16 + (512 if self.trainer else 0)
        self.prg = bytearray(data[off : off + prg_banks * 16384])
        off += prg_banks * 16384
        if chr_banks:
            self.chr = bytearray(data[off : off + chr_banks * 8192])
        else:
            self.chr = bytearray(8192)      # CHR-RAM

    def info(self) -> str:
        return (f"PRG: {self.prg_banks*16}KB  CHR: {self.chr_banks*8}KB  "
                f"mapper: {self.mapper}  mirror: {self.mirror}  "
                f"battery: {'yes' if self.battery else 'no'}")


# =============================================================================
#  Cartridge mappers (common commercial iNES IDs — not exhaustive)
# =============================================================================
class _MapperBase:
    __slots__ = ("bus", "rom")

    def __init__(self, bus: "Bus", rom: INES):
        self.bus = bus
        self.rom = rom
        bus.ppu_mirror = rom.mirror

    def prg_read(self, addr: int) -> int:
        raise NotImplementedError

    def prg_write(self, addr: int, val: int) -> None:
        pass

    def chr_read(self, addr: int) -> int:
        c = self.rom.chr
        return c[addr % len(c)] if c else 0

    def chr_write(self, addr: int, val: int) -> None:
        if self.rom.chr_banks == 0 and self.rom.chr:
            self.rom.chr[addr % len(self.rom.chr)] = val & 0xFF


class Mapper0(_MapperBase):
    """NROM / fixed banking."""

    def prg_read(self, addr: int) -> int:
        idx = addr - 0x8000
        if self.rom.prg_banks == 1:
            idx &= 0x3FFF
        return self.rom.prg[idx % len(self.rom.prg)]


class Mapper2(_MapperBase):
    """UNROM / UxROM — PRG 16K switch @ $8000, last 16K fixed."""

    __slots__ = ("bank",)

    def __init__(self, bus: "Bus", rom: INES):
        super().__init__(bus, rom)
        self.bank = 0

    def prg_read(self, addr: int) -> int:
        prg = self.rom.prg
        L = len(prg)
        if addr < 0xC000:
            return prg[self.bank * 0x4000 + (addr - 0x8000)]
        return prg[L - 0x4000 + (addr - 0xC000)]

    def prg_write(self, addr: int, val: int) -> None:
        if addr >= 0x8000:
            n = max(1, len(self.rom.prg) // 0x4000)
            self.bank = val & (n - 1)


class Mapper3(_MapperBase):
    """CNROM — CHR 8K bank switch."""

    __slots__ = ("chr_bank",)

    def __init__(self, bus: "Bus", rom: INES):
        super().__init__(bus, rom)
        self.chr_bank = 0

    def prg_read(self, addr: int) -> int:
        prg = self.rom.prg
        return prg[(addr - 0x8000) % len(prg)]

    def prg_write(self, addr: int, val: int) -> None:
        if addr >= 0x8000:
            n = max(1, len(self.rom.chr) // 0x2000)
            self.chr_bank = val & (n - 1)

    def chr_read(self, addr: int) -> int:
        c = self.rom.chr
        if not c:
            return 0
        return c[(self.chr_bank * 0x2000 + (addr & 0x1FFF)) % len(c)]

    def chr_write(self, addr: int, val: int) -> None:
        if self.rom.chr_banks == 0 and self.rom.chr:
            c = self.rom.chr
            c[(self.chr_bank * 0x2000 + (addr & 0x1FFF)) % len(c)] = val & 0xFF


class Mapper7(_MapperBase):
    """AxROM — 32K PRG + one-screen mirroring."""

    __slots__ = ("bank",)

    def __init__(self, bus: "Bus", rom: INES):
        super().__init__(bus, rom)
        self.bank = 0

    def prg_read(self, addr: int) -> int:
        prg = self.rom.prg
        L = len(prg)
        off = self.bank * 0x8000 + (addr - 0x8000)
        return prg[off % L]

    def prg_write(self, addr: int, val: int) -> None:
        if addr >= 0x8000:
            self.bank = val & 7
            self.bus.ppu_mirror = "1" if (val & 0x10) else "0"


class Mapper1(_MapperBase):
    """MMC1 — PRG/CHR/mirroring via serial shift (common subset)."""

    __slots__ = ("shift", "control", "chr0", "chr1", "prg")

    def __init__(self, bus: "Bus", rom: INES):
        super().__init__(bus, rom)
        self.shift = 0x10
        self.control = 0x0C
        self.chr0 = self.chr1 = self.prg = 0
        self._sync_mirror()

    def _sync_mirror(self) -> None:
        m = self.control & 3
        if m == 0:
            self.bus.ppu_mirror = "0"
        elif m == 1:
            self.bus.ppu_mirror = "1"
        elif m == 2:
            self.bus.ppu_mirror = "V"
        else:
            self.bus.ppu_mirror = "H"

    def prg_write(self, addr: int, val: int) -> None:
        if val & 0x80:
            self.shift = 0x10
            self.control |= 0x0C
            self._sync_mirror()
            return
        complete = ((self.shift >> 1) | ((val & 1) << 4)) & 0x1F
        self.shift = complete
        if complete & 1:
            data = (complete >> 1) & 0x0F
            slot = (addr >> 13) & 3
            if slot == 0:
                self.control = data & 0x1F
                self._sync_mirror()
            elif slot == 1:
                self.chr0 = data & 0x1F
            elif slot == 2:
                self.chr1 = data & 0x1F
            else:
                self.prg = data & 0x0F
            self.shift = 0x10

    def prg_read(self, addr: int) -> int:
        prg = self.rom.prg
        L = len(prg)
        banks_16 = max(1, L // 0x4000)
        if self.control & 0x08:
            # 16K mode
            if self.control & 0x04:
                # $C000 switchable
                bank = self.prg & (banks_16 - 1)
                if addr >= 0xC000:
                    return prg[bank * 0x4000 + (addr - 0xC000)]
                return prg[((banks_16 - 1) * 0x4000 + (addr - 0x8000)) % L]
            # $8000 switchable
            bank = self.prg & (banks_16 - 1)
            if addr < 0xC000:
                return prg[bank * 0x4000 + (addr - 0x8000)]
            return prg[((banks_16 - 1) * 0x4000 + (addr - 0xC000)) % L]
        # 32K
        b32 = (self.prg >> 1) & (max(1, L // 0x8000) - 1)
        return prg[b32 * 0x8000 + (addr - 0x8000)]

    def chr_read(self, addr: int) -> int:
        c = self.rom.chr
        if not c:
            return 0
        cl = len(c)
        a = addr & 0x1FFF
        if self.control & 0x10:
            off = (self.chr0 if a < 0x1000 else self.chr1) * 0x1000 + (a & 0x0FFF)
        else:
            off = (self.chr0 >> 1) * 0x2000 + a
        return c[off % cl]

    def chr_write(self, addr: int, val: int) -> None:
        if self.rom.chr_banks != 0:
            return
        c = self.rom.chr
        if not c:
            return
        cl = len(c)
        a = addr & 0x1FFF
        if self.control & 0x10:
            off = (self.chr0 if a < 0x1000 else self.chr1) * 0x1000 + (a & 0x0FFF)
        else:
            off = (self.chr0 >> 1) * 0x2000 + a
        c[off % cl] = val & 0xFF


class Mapper4(_MapperBase):
    """MMC3 — PRG/CHR banking (no A12 IRQ; some games still run)."""

    __slots__ = ("r", "bank_sel", "prg_mode", "chr_mode", "wram")

    def __init__(self, bus: "Bus", rom: INES):
        super().__init__(bus, rom)
        self.r = [0] * 8
        self.bank_sel = 0
        self.prg_mode = self.chr_mode = 0
        self.wram = bytearray(8192)
        self._sync()

    def _sync(self) -> None:
        pass

    def prg_write(self, addr: int, val: int) -> None:
        if addr < 0x8000:
            return
        val &= 0xFF
        if addr < 0xA000:
            if (addr & 1) == 0:
                self.bank_sel = val & 7
                self.prg_mode = (val >> 6) & 1
                self.chr_mode = (val >> 7) & 1
            else:
                self.r[self.bank_sel] = val
        elif addr < 0xC000:
            if addr & 1:
                self.bus.ppu_mirror = "V" if (val & 1) else "H"
        elif addr < 0xE000:
            pass  # IRQ / RAM protect stubs
        else:
            pass

    def prg_read(self, addr: int) -> int:
        prg = self.rom.prg
        L = len(prg)
        n8 = max(1, L // 0x2000)
        R = self.r
        if self.prg_mode == 0:
            b8000 = (R[6] % n8) * 0x2000
            bA000 = (R[7] % n8) * 0x2000
            bC000 = (n8 - 2) * 0x2000
            bE000 = (n8 - 1) * 0x2000
        else:
            b8000 = (n8 - 2) * 0x2000
            bA000 = (R[7] % n8) * 0x2000
            bC000 = (R[6] % n8) * 0x2000
            bE000 = (n8 - 1) * 0x2000
        if 0x8000 <= addr < 0xA000:
            return prg[b8000 + addr - 0x8000]
        if 0xA000 <= addr < 0xC000:
            return prg[bA000 + addr - 0xA000]
        if 0xC000 <= addr < 0xE000:
            return prg[bC000 + addr - 0xC000]
        return prg[bE000 + addr - 0xE000]

    def chr_read(self, addr: int) -> int:
        c = self.rom.chr
        if not c:
            return 0
        cl = len(c)
        nk = max(1, cl // 1024)
        R = self.r
        a = addr & 0x1FFF
        if self.chr_mode == 0:
            if a < 0x800:
                off = ((R[0] & ~1) % nk) * 1024 + (a & 0x7FF)
            elif a < 0x1000:
                off = ((R[1] & ~1) % nk) * 1024 + (a & 0x7FF)
            else:
                slot = (a - 0x1000) // 0x400
                off = (R[2 + slot] % nk) * 1024 + (a & 0x3FF)
        else:
            slot = a // 0x400
            off = (R[slot] % nk) * 1024 + (a & 0x3FF)
        return c[off % cl]

    def chr_write(self, addr: int, val: int) -> None:
        if self.rom.chr_banks != 0:
            return
        c = self.rom.chr
        if not c:
            return
        cl = len(c)
        nk = max(1, cl // 1024)
        R = self.r
        a = addr & 0x1FFF
        if self.chr_mode == 0:
            if a < 0x800:
                off = ((R[0] & ~1) % nk) * 1024 + (a & 0x7FF)
            elif a < 0x1000:
                off = ((R[1] & ~1) % nk) * 1024 + (a & 0x7FF)
            else:
                slot = (a - 0x1000) // 0x400
                off = (R[2 + slot] % nk) * 1024 + (a & 0x3FF)
        else:
            slot = a // 0x400
            off = (R[slot] % nk) * 1024 + (a & 0x3FF)
        c[off % cl] = val & 0xFF


def build_mapper(bus: "Bus", rom: INES) -> _MapperBase:
    """Pick mapper by iNES mapper number (covers a large share of retail carts)."""
    m = rom.mapper
    if m == 1:
        return Mapper1(bus, rom)
    if m in (2, 71, 94, 180, 206, 207, 245):
        return Mapper2(bus, rom)
    if m == 3:
        return Mapper3(bus, rom)
    if m in (4, 76, 118, 119, 191, 192, 194, 195, 209, 215, 249, 250, 254):
        return Mapper4(bus, rom)
    if m in (7, 11, 34, 47):
        return Mapper7(bus, rom)
    return Mapper0(bus, rom)


# =============================================================================
#  Memory bus — delegates PRG/CHR to cartridge mapper
# =============================================================================

class Bus:
    """CPU memory bus + mapper-backed PRG/CHR (common commercial mappers)."""
    __slots__ = ("ram", "rom", "ppu", "controller", "controller_shift",
                 "mapper", "ppu_mirror")

    def __init__(self):
        self.ram = bytearray(0x0800)
        self.rom: INES | None = None
        self.ppu: "PPU | None" = None
        self.controller = 0
        self.controller_shift = 0
        self.mapper: _MapperBase | None = None
        self.ppu_mirror = "H"

    def attach(self, rom: INES, ppu: "PPU"):
        self.rom = rom
        self.ppu = ppu
        ppu.bus = self
        self.mapper = build_mapper(self, rom)

    def chr_read(self, addr: int) -> int:
        addr &= 0x1FFF
        if self.mapper:
            return self.mapper.chr_read(addr)
        if not self.rom or not self.rom.chr:
            return 0
        return self.rom.chr[addr % len(self.rom.chr)]

    def chr_write(self, addr: int, val: int) -> None:
        addr &= 0x1FFF
        val &= 0xFF
        if self.mapper:
            self.mapper.chr_write(addr, val)
            return
        if self.rom and self.rom.chr and self.rom.chr_banks == 0:
            self.rom.chr[addr % len(self.rom.chr)] = val

    def read(self, addr: int) -> int:
        addr &= 0xFFFF
        if addr < 0x2000:
            return self.ram[addr & 0x07FF]
        if addr < 0x4000:
            return self.ppu.reg_read(0x2000 + (addr & 7)) if self.ppu else 0
        if addr == 0x4016:
            v = (self.controller_shift & 1)
            self.controller_shift >>= 1
            return v
        if addr < 0x4020:
            return 0
        if addr < 0x6000:
            return 0
        if addr < 0x8000:
            if isinstance(self.mapper, Mapper4) and len(self.mapper.wram):
                return self.mapper.wram[(addr - 0x6000) & 0x1FFF]
            return 0
        if self.rom is None or self.mapper is None:
            return 0
        return self.mapper.prg_read(addr)

    def write(self, addr: int, val: int) -> None:
        addr &= 0xFFFF
        val &= 0xFF
        if addr < 0x2000:
            self.ram[addr & 0x07FF] = val
            return
        if addr < 0x4000:
            if self.ppu:
                self.ppu.reg_write(0x2000 + (addr & 7), val)
            return
        if addr == 0x4014 and self.ppu:
            base = val << 8
            for i in range(256):
                self.ppu.oam[i] = self.read(base + i)
            return
        if addr == 0x4016:
            if val & 1:
                self.controller_shift = self.controller
            return
        if 0x6000 <= addr < 0x8000:
            if isinstance(self.mapper, Mapper4):
                self.mapper.wram[(addr - 0x6000) & 0x1FFF] = val
            return
        if addr >= 0x8000 and self.mapper is not None:
            self.mapper.prg_write(addr, val)


# =============================================================================
#  6502 CPU  -  official opcodes
# =============================================================================
# flag bits
C, Z, I, D, B, U, V, N = 1, 2, 4, 8, 16, 32, 64, 128

class CPU:
    """MOS 6502.  Not cycle-accurate; instruction-accurate enough to boot."""
    __slots__ = ("a", "x", "y", "sp", "pc", "p", "bus", "cycles", "halted",
                 "_addr", "_page_crossed")

    def __init__(self, bus: Bus):
        self.bus = bus
        self.a = 0; self.x = 0; self.y = 0
        self.sp = 0xFD
        self.pc = 0xC000
        self.p  = U | I
        self.cycles = 0
        self.halted = False
        self._addr = 0
        self._page_crossed = False

    # --------- helpers -----------------------------------------------------
    def r(self, a):  return self.bus.read(a)
    def w(self, a, v): self.bus.write(a, v)

    def r16(self, a):
        return self.r(a) | (self.r((a + 1) & 0xFFFF) << 8)

    def push(self, v):
        self.w(0x100 + self.sp, v & 0xFF)
        self.sp = (self.sp - 1) & 0xFF

    def pop(self):
        self.sp = (self.sp + 1) & 0xFF
        return self.r(0x100 + self.sp)

    def setF(self, flag, on):
        if on: self.p |=  flag
        else:  self.p &= ~flag & 0xFF

    def setNZ(self, v):
        v &= 0xFF
        self.setF(Z, v == 0)
        self.setF(N, v & 0x80)

    def reset(self):
        self.sp = 0xFD
        self.p  = U | I
        self.a = self.x = self.y = 0
        self.pc = self.r16(0xFFFC)
        self.cycles = 0
        self.halted = False

    def nmi(self):
        self.push((self.pc >> 8) & 0xFF)
        self.push(self.pc & 0xFF)
        self.push((self.p | U) & ~B & 0xFF)
        self.setF(I, True)
        self.pc = self.r16(0xFFFA)
        self.cycles += 7

    def irq(self):
        if self.p & I:
            return
        self.push((self.pc >> 8) & 0xFF)
        self.push(self.pc & 0xFF)
        self.push((self.p | U) & ~B & 0xFF)
        self.setF(I, True)
        self.pc = self.r16(0xFFFE)
        self.cycles += 7

    # --------- addressing modes  (set self._addr) --------------------------
    def am_imp(self): pass
    def am_acc(self): pass
    def am_imm(self): self._addr = self.pc; self.pc = (self.pc + 1) & 0xFFFF
    def am_zp (self): self._addr = self.r(self.pc); self.pc = (self.pc + 1) & 0xFFFF
    def am_zpx(self): self._addr = (self.r(self.pc) + self.x) & 0xFF; self.pc = (self.pc + 1) & 0xFFFF
    def am_zpy(self): self._addr = (self.r(self.pc) + self.y) & 0xFF; self.pc = (self.pc + 1) & 0xFFFF
    def am_abs(self):
        self._addr = self.r16(self.pc); self.pc = (self.pc + 2) & 0xFFFF
    def am_abx(self):
        base = self.r16(self.pc); self.pc = (self.pc + 2) & 0xFFFF
        self._addr = (base + self.x) & 0xFFFF
    def am_aby(self):
        base = self.r16(self.pc); self.pc = (self.pc + 2) & 0xFFFF
        self._addr = (base + self.y) & 0xFFFF
    def am_ind(self):
        ptr = self.r16(self.pc); self.pc = (self.pc + 2) & 0xFFFF
        # 6502 indirect-jump page-wrap bug
        if (ptr & 0xFF) == 0xFF:
            self._addr = self.r(ptr) | (self.r(ptr & 0xFF00) << 8)
        else:
            self._addr = self.r16(ptr)
    def am_izx(self):
        z = (self.r(self.pc) + self.x) & 0xFF
        self.pc = (self.pc + 1) & 0xFFFF
        self._addr = self.r(z) | (self.r((z + 1) & 0xFF) << 8)
    def am_izy(self):
        z = self.r(self.pc); self.pc = (self.pc + 1) & 0xFFFF
        base = self.r(z) | (self.r((z + 1) & 0xFF) << 8)
        self._addr = (base + self.y) & 0xFFFF
    def am_rel(self):
        off = self.r(self.pc); self.pc = (self.pc + 1) & 0xFFFF
        if off & 0x80: off -= 0x100
        self._addr = (self.pc + off) & 0xFFFF

    # --------- operations  (operate on self._addr) -------------------------
    def op_lda(self): self.a = self.r(self._addr); self.setNZ(self.a)
    def op_ldx(self): self.x = self.r(self._addr); self.setNZ(self.x)
    def op_ldy(self): self.y = self.r(self._addr); self.setNZ(self.y)
    def op_sta(self): self.w(self._addr, self.a)
    def op_stx(self): self.w(self._addr, self.x)
    def op_sty(self): self.w(self._addr, self.y)
    def op_tax(self): self.x = self.a; self.setNZ(self.x)
    def op_tay(self): self.y = self.a; self.setNZ(self.y)
    def op_txa(self): self.a = self.x; self.setNZ(self.a)
    def op_tya(self): self.a = self.y; self.setNZ(self.a)
    def op_tsx(self): self.x = self.sp; self.setNZ(self.x)
    def op_txs(self): self.sp = self.x
    def op_pha(self): self.push(self.a)
    def op_php(self): self.push(self.p | B | U)
    def op_pla(self): self.a = self.pop(); self.setNZ(self.a)
    def op_plp(self): self.p = (self.pop() | U) & ~B & 0xFF

    def op_and(self): self.a &= self.r(self._addr); self.setNZ(self.a)
    def op_ora(self): self.a |= self.r(self._addr); self.setNZ(self.a)
    def op_eor(self): self.a ^= self.r(self._addr); self.setNZ(self.a)

    def op_bit(self):
        m = self.r(self._addr)
        self.setF(Z, (self.a & m) == 0)
        self.setF(V, m & 0x40)
        self.setF(N, m & 0x80)

    def op_adc(self):
        m = self.r(self._addr)
        s = self.a + m + (1 if self.p & C else 0)
        self.setF(C, s > 0xFF)
        self.setF(V, (~(self.a ^ m) & (self.a ^ s) & 0x80) != 0)
        self.a = s & 0xFF
        self.setNZ(self.a)

    def op_sbc(self):
        m = self.r(self._addr) ^ 0xFF
        s = self.a + m + (1 if self.p & C else 0)
        self.setF(C, s > 0xFF)
        self.setF(V, (~(self.a ^ m) & (self.a ^ s) & 0x80) != 0)
        self.a = s & 0xFF
        self.setNZ(self.a)

    def _cmp(self, reg):
        m = self.r(self._addr)
        d = (reg - m) & 0x1FF
        self.setF(C, reg >= m)
        self.setNZ(d & 0xFF)
    def op_cmp(self): self._cmp(self.a)
    def op_cpx(self): self._cmp(self.x)
    def op_cpy(self): self._cmp(self.y)

    def op_inc(self):
        v = (self.r(self._addr) + 1) & 0xFF
        self.w(self._addr, v); self.setNZ(v)
    def op_dec(self):
        v = (self.r(self._addr) - 1) & 0xFF
        self.w(self._addr, v); self.setNZ(v)
    def op_inx(self): self.x = (self.x + 1) & 0xFF; self.setNZ(self.x)
    def op_iny(self): self.y = (self.y + 1) & 0xFF; self.setNZ(self.y)
    def op_dex(self): self.x = (self.x - 1) & 0xFF; self.setNZ(self.x)
    def op_dey(self): self.y = (self.y - 1) & 0xFF; self.setNZ(self.y)

    def op_asl_a(self):
        self.setF(C, self.a & 0x80); self.a = (self.a << 1) & 0xFF; self.setNZ(self.a)
    def op_asl(self):
        m = self.r(self._addr); self.setF(C, m & 0x80)
        m = (m << 1) & 0xFF;    self.w(self._addr, m); self.setNZ(m)
    def op_lsr_a(self):
        self.setF(C, self.a & 1);  self.a >>= 1;       self.setNZ(self.a)
    def op_lsr(self):
        m = self.r(self._addr); self.setF(C, m & 1)
        m >>= 1;                self.w(self._addr, m); self.setNZ(m)
    def op_rol_a(self):
        c = 1 if self.p & C else 0
        self.setF(C, self.a & 0x80)
        self.a = ((self.a << 1) | c) & 0xFF; self.setNZ(self.a)
    def op_rol(self):
        m = self.r(self._addr); c = 1 if self.p & C else 0
        self.setF(C, m & 0x80)
        m = ((m << 1) | c) & 0xFF; self.w(self._addr, m); self.setNZ(m)
    def op_ror_a(self):
        c = 0x80 if self.p & C else 0
        self.setF(C, self.a & 1)
        self.a = (self.a >> 1) | c; self.setNZ(self.a)
    def op_ror(self):
        m = self.r(self._addr); c = 0x80 if self.p & C else 0
        self.setF(C, m & 1)
        m = (m >> 1) | c; self.w(self._addr, m); self.setNZ(m)

    def op_jmp(self): self.pc = self._addr
    def op_jsr(self):
        ret = (self.pc - 1) & 0xFFFF
        self.push((ret >> 8) & 0xFF); self.push(ret & 0xFF)
        self.pc = self._addr
    def op_rts(self):
        lo = self.pop(); hi = self.pop()
        self.pc = ((hi << 8) | lo) + 1
    def op_rti(self):
        self.p = (self.pop() | U) & ~B & 0xFF
        lo = self.pop(); hi = self.pop()
        self.pc = (hi << 8) | lo
    def op_brk(self):
        self.pc = (self.pc + 1) & 0xFFFF
        self.push((self.pc >> 8) & 0xFF); self.push(self.pc & 0xFF)
        self.push(self.p | B | U)
        self.setF(I, True)
        self.pc = self.r16(0xFFFE)

    def _branch(self, cond):
        if cond:
            self.cycles += 1
            self.pc = self._addr

    def op_bpl(self): self._branch(not self.p & N)
    def op_bmi(self): self._branch( bool(self.p & N))
    def op_bvc(self): self._branch(not self.p & V)
    def op_bvs(self): self._branch( bool(self.p & V))
    def op_bcc(self): self._branch(not self.p & C)
    def op_bcs(self): self._branch( bool(self.p & C))
    def op_bne(self): self._branch(not self.p & Z)
    def op_beq(self): self._branch( bool(self.p & Z))

    def op_clc(self): self.setF(C, False)
    def op_sec(self): self.setF(C, True)
    def op_cli(self): self.setF(I, False)
    def op_sei(self): self.setF(I, True)
    def op_clv(self): self.setF(V, False)
    def op_cld(self): self.setF(D, False)
    def op_sed(self): self.setF(D, True)
    def op_nop(self): pass

    # opcode table built lazily below (after class def)
    _table = None

    def step(self):
        if self.halted: return 0
        op = self.r(self.pc)
        self.pc = (self.pc + 1) & 0xFFFF
        entry = CPU._table[op]
        if entry is None:
            self.halted = True
            return 0
        am, fn, cyc = entry
        am(self); fn(self)
        self.cycles += cyc
        return cyc

# ----- opcode dispatch table  (am, op, cycles) -------------------------------
def _build_table():
    t = [None] * 256
    C = CPU
    def s(code, am, op, cyc):
        t[code] = (getattr(C, am), getattr(C, op), cyc)

    # loads
    s(0xA9,"am_imm","op_lda",2); s(0xA5,"am_zp", "op_lda",3)
    s(0xB5,"am_zpx","op_lda",4); s(0xAD,"am_abs","op_lda",4)
    s(0xBD,"am_abx","op_lda",4); s(0xB9,"am_aby","op_lda",4)
    s(0xA1,"am_izx","op_lda",6); s(0xB1,"am_izy","op_lda",5)
    s(0xA2,"am_imm","op_ldx",2); s(0xA6,"am_zp", "op_ldx",3)
    s(0xB6,"am_zpy","op_ldx",4); s(0xAE,"am_abs","op_ldx",4)
    s(0xBE,"am_aby","op_ldx",4)
    s(0xA0,"am_imm","op_ldy",2); s(0xA4,"am_zp", "op_ldy",3)
    s(0xB4,"am_zpx","op_ldy",4); s(0xAC,"am_abs","op_ldy",4)
    s(0xBC,"am_abx","op_ldy",4)
    # stores
    s(0x85,"am_zp", "op_sta",3); s(0x95,"am_zpx","op_sta",4)
    s(0x8D,"am_abs","op_sta",4); s(0x9D,"am_abx","op_sta",5)
    s(0x99,"am_aby","op_sta",5); s(0x81,"am_izx","op_sta",6)
    s(0x91,"am_izy","op_sta",6)
    s(0x86,"am_zp", "op_stx",3); s(0x96,"am_zpy","op_stx",4)
    s(0x8E,"am_abs","op_stx",4)
    s(0x84,"am_zp", "op_sty",3); s(0x94,"am_zpx","op_sty",4)
    s(0x8C,"am_abs","op_sty",4)
    # transfers
    s(0xAA,"am_imp","op_tax",2); s(0xA8,"am_imp","op_tay",2)
    s(0x8A,"am_imp","op_txa",2); s(0x98,"am_imp","op_tya",2)
    s(0xBA,"am_imp","op_tsx",2); s(0x9A,"am_imp","op_txs",2)
    # stack
    s(0x48,"am_imp","op_pha",3); s(0x08,"am_imp","op_php",3)
    s(0x68,"am_imp","op_pla",4); s(0x28,"am_imp","op_plp",4)
    # logic
    s(0x29,"am_imm","op_and",2); s(0x25,"am_zp", "op_and",3)
    s(0x35,"am_zpx","op_and",4); s(0x2D,"am_abs","op_and",4)
    s(0x3D,"am_abx","op_and",4); s(0x39,"am_aby","op_and",4)
    s(0x21,"am_izx","op_and",6); s(0x31,"am_izy","op_and",5)
    s(0x09,"am_imm","op_ora",2); s(0x05,"am_zp", "op_ora",3)
    s(0x15,"am_zpx","op_ora",4); s(0x0D,"am_abs","op_ora",4)
    s(0x1D,"am_abx","op_ora",4); s(0x19,"am_aby","op_ora",4)
    s(0x01,"am_izx","op_ora",6); s(0x11,"am_izy","op_ora",5)
    s(0x49,"am_imm","op_eor",2); s(0x45,"am_zp", "op_eor",3)
    s(0x55,"am_zpx","op_eor",4); s(0x4D,"am_abs","op_eor",4)
    s(0x5D,"am_abx","op_eor",4); s(0x59,"am_aby","op_eor",4)
    s(0x41,"am_izx","op_eor",6); s(0x51,"am_izy","op_eor",5)
    s(0x24,"am_zp", "op_bit",3); s(0x2C,"am_abs","op_bit",4)
    # arithmetic
    s(0x69,"am_imm","op_adc",2); s(0x65,"am_zp", "op_adc",3)
    s(0x75,"am_zpx","op_adc",4); s(0x6D,"am_abs","op_adc",4)
    s(0x7D,"am_abx","op_adc",4); s(0x79,"am_aby","op_adc",4)
    s(0x61,"am_izx","op_adc",6); s(0x71,"am_izy","op_adc",5)
    s(0xE9,"am_imm","op_sbc",2); s(0xE5,"am_zp", "op_sbc",3)
    s(0xF5,"am_zpx","op_sbc",4); s(0xED,"am_abs","op_sbc",4)
    s(0xFD,"am_abx","op_sbc",4); s(0xF9,"am_aby","op_sbc",4)
    s(0xE1,"am_izx","op_sbc",6); s(0xF1,"am_izy","op_sbc",5)
    # compares
    s(0xC9,"am_imm","op_cmp",2); s(0xC5,"am_zp", "op_cmp",3)
    s(0xD5,"am_zpx","op_cmp",4); s(0xCD,"am_abs","op_cmp",4)
    s(0xDD,"am_abx","op_cmp",4); s(0xD9,"am_aby","op_cmp",4)
    s(0xC1,"am_izx","op_cmp",6); s(0xD1,"am_izy","op_cmp",5)
    s(0xE0,"am_imm","op_cpx",2); s(0xE4,"am_zp", "op_cpx",3); s(0xEC,"am_abs","op_cpx",4)
    s(0xC0,"am_imm","op_cpy",2); s(0xC4,"am_zp", "op_cpy",3); s(0xCC,"am_abs","op_cpy",4)
    # inc/dec
    s(0xE6,"am_zp", "op_inc",5); s(0xF6,"am_zpx","op_inc",6)
    s(0xEE,"am_abs","op_inc",6); s(0xFE,"am_abx","op_inc",7)
    s(0xC6,"am_zp", "op_dec",5); s(0xD6,"am_zpx","op_dec",6)
    s(0xCE,"am_abs","op_dec",6); s(0xDE,"am_abx","op_dec",7)
    s(0xE8,"am_imp","op_inx",2); s(0xC8,"am_imp","op_iny",2)
    s(0xCA,"am_imp","op_dex",2); s(0x88,"am_imp","op_dey",2)
    # shifts
    s(0x0A,"am_acc","op_asl_a",2); s(0x06,"am_zp", "op_asl",5)
    s(0x16,"am_zpx","op_asl",6);   s(0x0E,"am_abs","op_asl",6)
    s(0x1E,"am_abx","op_asl",7)
    s(0x4A,"am_acc","op_lsr_a",2); s(0x46,"am_zp", "op_lsr",5)
    s(0x56,"am_zpx","op_lsr",6);   s(0x4E,"am_abs","op_lsr",6)
    s(0x5E,"am_abx","op_lsr",7)
    s(0x2A,"am_acc","op_rol_a",2); s(0x26,"am_zp", "op_rol",5)
    s(0x36,"am_zpx","op_rol",6);   s(0x2E,"am_abs","op_rol",6)
    s(0x3E,"am_abx","op_rol",7)
    s(0x6A,"am_acc","op_ror_a",2); s(0x66,"am_zp", "op_ror",5)
    s(0x76,"am_zpx","op_ror",6);   s(0x6E,"am_abs","op_ror",6)
    s(0x7E,"am_abx","op_ror",7)
    # jumps
    s(0x4C,"am_abs","op_jmp",3); s(0x6C,"am_ind","op_jmp",5)
    s(0x20,"am_abs","op_jsr",6); s(0x60,"am_imp","op_rts",6)
    s(0x40,"am_imp","op_rti",6); s(0x00,"am_imp","op_brk",7)
    # branches
    s(0x10,"am_rel","op_bpl",2); s(0x30,"am_rel","op_bmi",2)
    s(0x50,"am_rel","op_bvc",2); s(0x70,"am_rel","op_bvs",2)
    s(0x90,"am_rel","op_bcc",2); s(0xB0,"am_rel","op_bcs",2)
    s(0xD0,"am_rel","op_bne",2); s(0xF0,"am_rel","op_beq",2)
    # flag ops + nop
    s(0x18,"am_imp","op_clc",2); s(0x38,"am_imp","op_sec",2)
    s(0x58,"am_imp","op_cli",2); s(0x78,"am_imp","op_sei",2)
    s(0xB8,"am_imp","op_clv",2); s(0xD8,"am_imp","op_cld",2)
    s(0xF8,"am_imp","op_sed",2); s(0xEA,"am_imp","op_nop",2)
    return t
CPU._table = _build_table()

# =============================================================================
#  PPU stub  -  enough to expose CHR tiles via Tools > PPU Viewer
# =============================================================================
NES_PALETTE = [
    (84,84,84),(0,30,116),(8,16,144),(48,0,136),(68,0,100),(92,0,48),
    (84,4,0),(60,24,0),(32,42,0),(8,58,0),(0,64,0),(0,60,0),(0,50,60),
    (0,0,0),(0,0,0),(0,0,0),
    (152,150,152),(8,76,196),(48,50,236),(92,30,228),(136,20,176),
    (160,20,100),(152,34,32),(120,60,0),(84,90,0),(40,114,0),(8,124,0),
    (0,118,40),(0,102,120),(0,0,0),(0,0,0),(0,0,0),
    (236,238,236),(76,154,236),(120,124,236),(176,98,236),(228,84,236),
    (236,88,180),(236,106,100),(212,136,32),(160,170,0),(116,196,0),
    (76,208,32),(56,204,108),(56,180,204),(60,60,60),(0,0,0),(0,0,0),
    (236,238,236),(168,204,236),(188,188,236),(212,178,236),(236,174,236),
    (236,174,212),(236,180,176),(228,196,144),(204,210,120),(180,222,120),
    (168,226,144),(152,226,180),(160,214,228),(160,162,160),(0,0,0),(0,0,0),
]

class PPU:
    """PPU: nametable VRAM, palettes, loopy scroll, PPUDATA, BG + 8x8 sprites → framebuffer."""
    __slots__ = (
        "oam", "regs", "rom", "palette", "nametable", "v", "t", "w", "x",
        "_fine_x", "oam_addr", "read_buffer", "_vblank", "nes", "bus",
    )

    def __init__(self):
        self.oam = bytearray(256)
        self.regs = bytearray(8)
        self.rom: INES | None = None
        self.palette = bytearray(32)
        self.nametable = bytearray(2048)
        self.v = self.t = 0
        self.w = 0
        self.x = 0
        self._fine_x = 0
        self.oam_addr = 0
        self.read_buffer = 0
        self._vblank = False
        self.nes: "NES | None" = None
        self.bus: "Bus | None" = None

    def attach(self, rom: INES):
        self.rom = rom

    def bind_nes(self, nes: "NES") -> None:
        self.nes = nes

    def reset(self) -> None:
        self.oam[:] = [0] * 256
        self.regs[:] = [0] * 8
        self.palette[:] = [0] * 32
        self.nametable[:] = [0] * 2048
        self.v = self.t = 0
        self.w = 0
        self.x = 0
        self._fine_x = 0
        self.oam_addr = 0
        self.read_buffer = 0
        self._vblank = False

    # --- nametable mirroring (mapper 0) ------------------------------------
    def _nt_phys(self, addr: int) -> int:
        addr = (addr - 0x2000) & 0x0FFF
        tb = addr // 0x400
        off = addr & 0x3FF
        m = self.bus.ppu_mirror if self.bus else (self.rom.mirror if self.rom else "H")
        if m in ("0", "1"):
            return (0x400 if m == "1" else 0) + off
        if m == "H":
            return (tb // 2) * 0x400 + off
        return (tb & 1) * 0x400 + off

    def _pal_addr(self, a: int) -> int:
        a &= 0x1F
        if (a & 0x13) == 0x10:
            a &= 0x0F
        return a

    def ppu_read(self, a: int) -> int:
        a &= 0x3FFF
        if a < 0x2000:
            if self.bus:
                return self.bus.chr_read(a)
            if not self.rom or not self.rom.chr:
                return 0
            return self.rom.chr[a & (len(self.rom.chr) - 1)]
        if a < 0x3F00:
            return self.nametable[self._nt_phys(a)]
        return self.palette[self._pal_addr(a)] & 0x3F

    def ppu_write(self, a: int, val: int) -> None:
        a &= 0x3FFF
        val &= 0xFF
        if a < 0x2000:
            if self.bus:
                self.bus.chr_write(a, val)
                return
            if self.rom and self.rom.chr and self.rom.chr_banks == 0:
                self.rom.chr[a & (len(self.rom.chr) - 1)] = val
            return
        if a < 0x3F00:
            self.nametable[self._nt_phys(a)] = val
            return
        self.palette[self._pal_addr(a)] = val

    def reg_read(self, addr: int) -> int:
        r = addr & 7
        if r == 2:
            out = (self.regs[2] & 0x1F) | (0x80 if self._vblank else 0)
            self._vblank = False
            self.w = 0
            return out & 0xFF
        if r == 4:
            return self.oam[self.oam_addr]
        if r == 7:
            tmp = self.v & 0x3FFF
            inc = 32 if (self.regs[0] & 4) else 1
            if tmp < 0x3F00:
                ret = self.read_buffer
                self.read_buffer = self.ppu_read(tmp)
            else:
                ret = self.ppu_read(tmp)
                self.read_buffer = self.ppu_read(tmp - 0x1000)
            self.v = (self.v + inc) & 0x7FFF
            return ret & 0xFF
        return self.regs[r] & 0xFF

    def reg_write(self, addr: int, val: int) -> None:
        r = addr & 7
        val &= 0xFF
        self.regs[r] = val
        if r == 0:
            self.t = (self.t & 0xF3FF) | ((val & 0x03) << 10)
        elif r == 1:
            pass
        elif r == 3:
            self.oam_addr = val
        elif r == 4:
            self.oam[self.oam_addr] = val
            self.oam_addr = (self.oam_addr + 1) & 0xFF
        elif r == 5:
            if self.w == 0:
                self.t = (self.t & 0xFFE0) | (val >> 3)
                self._fine_x = val & 7
                self.w = 1
            else:
                self.t = (self.t & 0x8FFF) | ((val & 0x07) << 12)
                self.t = (self.t & 0xFC1F) | ((val & 0xF8) << 2)
                self.w = 0
        elif r == 6:
            if self.w == 0:
                self.t = (self.t & 0x80FF) | ((val & 0x3F) << 8)
                self.w = 1
            else:
                self.t = (self.t & 0xFF00) | val
                self.v = self.t
                self.w = 0
        elif r == 7:
            self.ppu_write(self.v, val)
            inc = 32 if (self.regs[0] & 4) else 1
            self.v = (self.v + inc) & 0x7FFF

    def begin_frame(self) -> None:
        self._vblank = False

    def end_frame(self) -> None:
        self._vblank = True
        if self.nes and (self.regs[0] & 0x80):
            self.nes.cpu.nmi()

    def _chr_byte(self, off: int) -> int:
        off &= 0x1FFF
        if self.bus:
            return self.bus.chr_read(off)
        if self.rom and self.rom.chr:
            return self.rom.chr[off % len(self.rom.chr)]
        return 0

    def decode_tile(self, table: int, tile_idx: int, palette_idx: int = 0):
        if not self.rom or len(self.rom.chr) < 0x2000:
            return [(0, 0, 0)] * 64
        base = (table & 1) * 0x1000 + (tile_idx & 0xFF) * 16
        out = []
        pal = (palette_idx & 3) * 4
        for row in range(8):
            lo = self._chr_byte(base + row)
            hi = self._chr_byte(base + row + 8)
            for col in range(8):
                bit = ((lo >> (7 - col)) & 1) | (((hi >> (7 - col)) & 1) << 1)
                if bit == 0:
                    out.append(NES_PALETTE[0x0F])
                else:
                    out.append(NES_PALETTE[(pal + bit) & 0x3F])
        return out

    def _scroll_xy(self):
        v = self.v
        sx = (v & 0x1F) * 8 + self._fine_x
        sy = ((v >> 5) & 0x1F) * 8 + ((v >> 12) & 7)
        sx += ((v >> 10) & 1) * 256
        sy += ((v >> 11) & 1) * 240
        return sx, sy

    def _pal_rgb(self, idx: int):
        return NES_PALETTE[idx & 0x3F]

    def render_rgb(self) -> bytes | None:
        if not self.rom or len(self.rom.chr) < 0x2000:
            return None
        mask = self.regs[1]
        ctrl = self.regs[0]
        bg_on = mask & 0x08
        sp_on = mask & 0x10
        bg_base = 0x1000 if (ctrl & 0x10) else 0
        sp_base = 0x1000 if (ctrl & 0x08) else 0
        tall = bool(ctrl & 0x20)

        sx0, sy0 = self._scroll_xy()
        W, H = 256, 240
        buf = bytearray(W * H * 3)
        backdrop = self._pal_rgb(self.palette[self._pal_addr(0x3F00)])
        bd = (backdrop[0], backdrop[1], backdrop[2])

        for py in range(H):
            for px in range(W):
                wx = (px + sx0) % 512
                wy = (py + sy0) % 512
                nt_i = ((wx // 256) + (wy // 256) * 2) & 3
                lx = wx % 256
                ly = wy % 256
                tc = lx // 8
                tr = ly // 8
                fx = lx % 8
                fy = ly % 8
                nt_base = 0x2000 + nt_i * 0x400
                taddr = nt_base + tr * 32 + tc
                tile_id = self.ppu_read(taddr)
                at_addr = nt_base + 0x3C0 + (tr // 4) * 8 + (tc // 4)
                attr = self.ppu_read(at_addr)
                shift = ((tr & 2) << 1) | (tc & 2)
                subpal = (attr >> shift) & 3
                pidx = 0
                if bg_on:
                    tb = bg_base + tile_id * 16 + fy
                    lo = self._chr_byte(tb)
                    hi = self._chr_byte(tb + 8)
                    pidx = ((lo >> (7 - fx)) & 1) | (((hi >> (7 - fx)) & 1) << 1)
                if pidx == 0:
                    r, g, b = backdrop
                else:
                    pi = self.palette[self._pal_addr(0x3F00 + (subpal << 2) + pidx)]
                    r, g, b = self._pal_rgb(pi)
                q = (py * W + px) * 3
                buf[q] = r
                buf[q + 1] = g
                buf[q + 2] = b

        if sp_on and not tall:
            for si in range(63, -1, -1):
                base = si * 4
                oy = self.oam[base + 0]
                tile = self.oam[base + 1] & 0xFF
                attr = self.oam[base + 2]
                ox = self.oam[base + 3]
                if oy > 239:
                    continue
                flip_v = attr & 0x80
                flip_h = attr & 0x40
                behind = attr & 0x20
                spal = attr & 0x03
                tbase = sp_base + tile * 16
                for ry in range(8):
                    row = 7 - ry if flip_v else ry
                    tb = tbase + row
                    lo = self._chr_byte(tb)
                    hi = self._chr_byte(tb + 8)
                    py = oy + ry
                    if py < 0 or py >= H:
                        continue
                    for fx in range(8):
                        col = 7 - fx if flip_h else fx
                        pv = ((lo >> (7 - col)) & 1) | (((hi >> (7 - col)) & 1) << 1)
                        if pv == 0:
                            continue
                        px = ox + fx
                        if px < 0 or px >= W:
                            continue
                        pi = self.palette[self._pal_addr(0x3F10 + (spal << 2) + pv)]
                        r, g, b = self._pal_rgb(pi)
                        q = (py * W + px) * 3
                        if behind:
                            if (buf[q], buf[q + 1], buf[q + 2]) == bd:
                                buf[q] = r
                                buf[q + 1] = g
                                buf[q + 2] = b
                        else:
                            buf[q] = r
                            buf[q + 1] = g
                            buf[q + 2] = b
        return bytes(buf)

# =============================================================================
#  NES system  -  glue
# =============================================================================
class NES:
    def __init__(self):
        self.bus = Bus()
        self.cpu = CPU(self.bus)
        self.ppu = PPU()
        self.rom: INES | None = None

    def load(self, path: str):
        self.rom = INES(path)
        self.ppu.attach(self.rom)
        self.ppu.bind_nes(self)
        self.ppu.reset()
        self.bus.attach(self.rom, self.ppu)
        self.cpu.reset()

    def reset(self):
        if self.rom: self.cpu.reset()

    def step(self):  return self.cpu.step()

# =============================================================================
#  FCEUX-style GUI
# =============================================================================
class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.nes  = NES()
        self._emu_lock = threading.Lock()
        self.running = False
        self.run_thread: threading.Thread | None = None

        root.title("acnesemu 0.1")
        root.geometry("600x400")
        root.configure(bg=BG)
        root.resizable(False, False)
        root.option_add("*Font", FONT_UI)

        self._build_menu()
        self._build_body()
        self._build_status()
        self._refresh_state()
        self._pattern_img_ref = None  # tk.PhotoImage / ImageTk — keep alive for canvas

    # --------- styling helpers --------------------------------------------
    def _btn(self, parent, text, cmd):
        b = tk.Button(parent, text=text, command=cmd,
                      bg=BG, fg=FG,
                      activebackground=ACCENT, activeforeground=HI,
                      disabledforeground=DIM,
                      bd=1, relief="solid",
                      highlightbackground=EDGE, highlightthickness=1,
                      font=FONT_MONO_B, padx=8, pady=2,
                      cursor="hand2")
        return b

    def _lbl(self, parent, text, **kw):
        opts = {"bg": BG, "fg": FG, "font": FONT_MONO}
        opts.update(kw)
        return tk.Label(parent, text=text, **opts)

    # --------- menu (fceux layout) ----------------------------------------
    def _build_menu(self):
        bar = tk.Menu(self.root, bg=BG, fg=FG,
                      activebackground=ACCENT, activeforeground=HI,
                      bd=0, tearoff=0)

        m_file = tk.Menu(bar, bg=BG, fg=FG, tearoff=0,
                         activebackground=ACCENT, activeforeground=HI)
        m_file.add_command(label="Open ROM...", command=self.open_rom)
        m_file.add_command(label="Close ROM",   command=self.close_rom)
        m_file.add_separator()
        m_file.add_command(label="Recent ROMs", state="disabled")
        m_file.add_separator()
        m_file.add_command(label="Save State",  command=self._todo)
        m_file.add_command(label="Load State",  command=self._todo)
        m_file.add_separator()
        m_file.add_command(label="Exit", command=self.root.destroy)
        bar.add_cascade(label="File", menu=m_file)

        m_nes = tk.Menu(bar, bg=BG, fg=FG, tearoff=0,
                        activebackground=ACCENT, activeforeground=HI)
        m_nes.add_command(label="Power",  command=self.power)
        m_nes.add_command(label="Reset",  command=self.reset)
        m_nes.add_separator()
        m_nes.add_command(label="Pause",  command=self.pause)
        m_nes.add_command(label="Resume", command=self.resume)
        bar.add_cascade(label="NES", menu=m_nes)

        m_cfg = tk.Menu(bar, bg=BG, fg=FG, tearoff=0,
                        activebackground=ACCENT, activeforeground=HI)
        m_cfg.add_command(label="Input...",  command=self._todo)
        m_cfg.add_command(label="Video...",  command=self._todo)
        m_cfg.add_command(label="Sound...",  command=self._todo)
        m_cfg.add_command(label="Paths...",  command=self._todo)
        bar.add_cascade(label="Config", menu=m_cfg)

        m_tools = tk.Menu(bar, bg=BG, fg=FG, tearoff=0,
                          activebackground=ACCENT, activeforeground=HI)
        m_tools.add_command(label="PPU Viewer",   command=self.ppu_viewer)
        m_tools.add_command(label="Hex Editor",   command=self.hex_editor)
        m_tools.add_command(label="Cheats...",    command=self._todo)
        m_tools.add_command(label="RAM Watch",    command=self._todo)
        bar.add_cascade(label="Tools", menu=m_tools)

        m_dbg = tk.Menu(bar, bg=BG, fg=FG, tearoff=0,
                        activebackground=ACCENT, activeforeground=HI)
        m_dbg.add_command(label="Debugger",   command=self.debugger)
        m_dbg.add_command(label="Step",       command=self.step_one)
        m_dbg.add_command(label="Trace Log",  command=self._todo)
        bar.add_cascade(label="Debug", menu=m_dbg)

        m_help = tk.Menu(bar, bg=BG, fg=FG, tearoff=0,
                         activebackground=ACCENT, activeforeground=HI)
        m_help.add_command(label="About acnesemu", command=self.about)
        bar.add_cascade(label="Help", menu=m_help)

        self.root.config(menu=bar)

    # --------- body --------------------------------------------------------
    def _build_body(self):
        # video frame (NES native 256x240, centered)
        vid_wrap = tk.Frame(self.root, bg=BG, bd=1, relief="solid",
                            highlightbackground=EDGE, highlightthickness=1)
        vid_wrap.place(x=8, y=4, width=350, height=320)
        self.canvas = tk.Canvas(vid_wrap, width=256, height=240,
                                bg=BG, bd=0, highlightthickness=0)
        self.canvas.place(x=46, y=38)
        self.canvas.create_text(128, 120, text="NO SIGNAL", fill=ACCENT,
                                font=("Courier", 16, "bold"))
        self.canvas.create_text(128, 142, text="(load a rom)", fill=DIM,
                                font=FONT_MONO)

        # right panel: control buttons
        side = tk.Frame(self.root, bg=BG)
        side.place(x=370, y=4, width=222, height=320)

        self._lbl(side, "─ control ─").pack(anchor="w", pady=(2, 4))
        for label, cmd in (("Power",  self.power),
                           ("Reset",  self.reset),
                           ("Pause",  self.pause),
                           ("Resume", self.resume),
                           ("Step",   self.step_one)):
            self._btn(side, label, cmd).pack(fill="x", pady=1)

        self._lbl(side, "").pack()
        self._lbl(side, "─ tools ─").pack(anchor="w", pady=(0, 4))
        for label, cmd in (("PPU Viewer", self.ppu_viewer),
                           ("Hex Editor", self.hex_editor),
                           ("Debugger",   self.debugger),
                           ("Open ROM",   self.open_rom)):
            self._btn(side, label, cmd).pack(fill="x", pady=1)

    # --------- status bar --------------------------------------------------
    def _build_status(self):
        sb = tk.Frame(self.root, bg=BG, bd=1, relief="solid",
                      highlightbackground=ACCENT, highlightthickness=1)
        sb.place(x=8, y=328, width=584, height=64)

        self.lbl_rom   = self._lbl(sb, "ROM: <none>")
        self.lbl_rom.place(x=6, y=2)
        self.lbl_info  = self._lbl(sb, "")
        self.lbl_info.place(x=6, y=18)
        self.lbl_cpu   = self._lbl(sb, "CPU: -")
        self.lbl_cpu.place(x=6, y=34)
        self.lbl_state = self._lbl(sb, "● stopped", fg=DIM)
        self.lbl_state.place(x=460, y=2)

    # --------- actions -----------------------------------------------------
    def open_rom(self):
        path = filedialog.askopenfilename(
            title="open .nes rom",
            filetypes=[("iNES rom", "*.nes"), ("all files", "*.*")])
        if not path: return
        try:
            self.nes.load(path)
        except Exception as e:
            messagebox.showerror("acnesemu", f"load failed:\n{e}")
            return
        self._refresh_state()
        with self._emu_lock:
            rgb = self.nes.ppu.render_rgb() if self.nes.rom else None
        if rgb:
            self._canvas_show_rgb_buffer(256, 240, rgb, "")
        else:
            self._draw_pattern_preview()

    def close_rom(self):
        self.pause()
        self.nes = NES()
        self._pattern_img_ref = None
        self.canvas.delete("all")
        self.canvas.create_text(128, 120, text="NO SIGNAL", fill=ACCENT,
                                font=("Courier", 16, "bold"))
        self.canvas.create_text(128, 142, text="(load a rom)", fill=DIM,
                                font=FONT_MONO)
        self._refresh_state()

    def power(self):
        if self.nes.rom:
            with self._emu_lock:
                self.nes.cpu.reset()
                rgb = self.nes.ppu.render_rgb()
            self._present_frame(rgb)
        else:
            self._refresh_state()

    def reset(self):
        with self._emu_lock:
            self.nes.reset()
            rgb = self.nes.ppu.render_rgb() if self.nes.rom else None
        self._present_frame(rgb)

    def pause(self):
        self.running = False
        self.lbl_state.configure(text="● paused", fg=ACCENT)

    def resume(self):
        if not self.nes.rom or self.running: return
        self.running = True
        self.lbl_state.configure(text="● running", fg=HI)
        self.run_thread = threading.Thread(target=self._run_loop, daemon=True)
        self.run_thread.start()

    def step_one(self):
        if not self.nes.rom: return
        with self._emu_lock:
            self.nes.step()
            rgb = self.nes.ppu.render_rgb()
        self._present_frame(rgb)

    # --------- core run loop  (instruction-paced) --------------------------
    def _run_loop(self):
        cpu = self.nes.cpu
        last = time.time()
        while self.running and not cpu.halted:
            # ~29780 cycles per NTSC frame, 60 fps  -> ~1.79 MHz
            target = 29780
            count = 0
            rgb = None
            with self._emu_lock:
                self.nes.ppu.begin_frame()
                while count < target and self.running and not cpu.halted:
                    count += cpu.step() or 1
                self.nes.ppu.end_frame()
                if self.nes.rom:
                    rgb = self.nes.ppu.render_rgb()
            now = time.time()
            dt = now - last
            if dt < 1/60:
                time.sleep(1/60 - dt)
            last = time.time()
            self.root.after(0, lambda r=rgb: self._present_frame(r))

    def _present_frame(self, rgb: bytes | None) -> None:
        """Main-thread: status line + PPU framebuffer."""
        self._refresh_state()
        if rgb is None or not self.nes.rom:
            return
        self._canvas_show_rgb_buffer(256, 240, rgb, "")

    # --------- state readout ----------------------------------------------
    def _refresh_state(self):
        r = self.nes.rom
        if r:
            self.lbl_rom.configure(text=f"ROM: {os.path.basename(r.path)}")
            self.lbl_info.configure(text=r.info())
        else:
            self.lbl_rom.configure(text="ROM: <none>")
            self.lbl_info.configure(text="")
        c = self.nes.cpu
        self.lbl_cpu.configure(
            text=(f"PC:{c.pc:04X}  A:{c.a:02X} X:{c.x:02X} Y:{c.y:02X}  "
                  f"SP:{c.sp:02X}  P:{c.p:02X}  cyc:{c.cycles}"))

    # --------- pattern-table preview (CHR → canvas) ------------------------
    def _pattern_tables_rgb_bytes(self) -> bytes | None:
        """256×128 RGB: left $0000 pattern table, right $1000 (Cython-friendly loops)."""
        rom = self.nes.rom
        if not rom or len(rom.chr) < 0x2000:
            return None
        w, h = 256, 128
        buf = bytearray(w * h * 3)
        chr_ = rom.chr
        pal = 0
        for table in (0, 1):
            ox = table * 128
            for ti in range(256):
                base = (table & 1) * 0x1000 + ti * 16
                tx = (ti & 15) * 8 + ox
                ty = (ti >> 4) * 8
                for row in range(8):
                    lo = chr_[base + row]
                    hi = chr_[base + row + 8]
                    for col in range(8):
                        bit = ((lo >> (7 - col)) & 1) | (((hi >> (7 - col)) & 1) << 1)
                        if bit == 0:
                            r, g, b = NES_PALETTE[0x0F]
                        else:
                            r, g, b = NES_PALETTE[(pal + bit) & 0x3F]
                        x = tx + col
                        y = ty + row
                        p = (y * w + x) * 3
                        buf[p] = r
                        buf[p + 1] = g
                        buf[p + 2] = b
        return bytes(buf)

    def _canvas_show_rgb_buffer(self, width: int, height: int, rgb: bytes, subtitle: str) -> None:
        """One PhotoImage on the main canvas (fast); PIL preferred, else PPM + tempfile."""
        self.canvas.delete("all")
        photo = None
        try:
            from PIL import Image, ImageTk
            im = Image.frombytes("RGB", (width, height), rgb)
            photo = ImageTk.PhotoImage(im)
        except (ImportError, OSError, ValueError, TypeError):
            hdr = f"P6\n{width} {height}\n255\n".encode("ascii")
            with tempfile.NamedTemporaryFile(suffix=".ppm", delete=False) as tmp:
                tmp.write(hdr)
                tmp.write(rgb)
                path = tmp.name
            try:
                photo = tk.PhotoImage(file=path)
            finally:
                try:
                    os.unlink(path)
                except OSError:
                    pass
        self._pattern_img_ref = photo
        y0 = max(0, (240 - height) // 2 - 6)
        self.canvas.create_image(0, y0, anchor="nw", image=photo)
        if subtitle:
            self.canvas.create_text(128, 228, text=subtitle, fill=ACCENT, font=FONT_MONO)

    def _draw_pattern_preview(self):
        """Draw both pattern tables from loaded CHR onto the video canvas."""
        rgb = self._pattern_tables_rgb_bytes()
        if rgb is None:
            self.canvas.delete("all")
            self.canvas.create_text(128, 120, text="NO SIGNAL", fill=ACCENT,
                                    font=("Courier", 16, "bold"))
            return
        self._canvas_show_rgb_buffer(256, 128, rgb, "CHR pattern tables — full PPU later")

    # --------- tools windows ----------------------------------------------
    def ppu_viewer(self):
        if not self.nes.rom:
            messagebox.showinfo("acnesemu", "load a rom first")
            return
        w = tk.Toplevel(self.root, bg=BG)
        w.title("PPU Viewer")
        w.configure(bg=BG)
        w.resizable(False, False)
        scale = 2
        cv = tk.Canvas(w, width=256*scale, height=128*scale, bg=BG,
                       bd=0, highlightthickness=1,
                       highlightbackground=EDGE)
        cv.pack(padx=6, pady=6)
        for table in (0, 1):
            ox = table * 128
            for ti in range(256):
                pixels = self.nes.ppu.decode_tile(table, ti, 0)
                tx = (ti & 15) * 8 + ox
                ty = (ti >> 4) * 8
                for py in range(8):
                    for px in range(8):
                        r,g,b = pixels[py*8 + px]
                        color = f"#{r:02x}{g:02x}{b:02x}"
                        x0 = (tx+px)*scale; y0 = (ty+py)*scale
                        cv.create_rectangle(x0, y0, x0+scale, y0+scale,
                                            fill=color, outline=color)
        tk.Label(w, text="left: $0000   right: $1000",
                 bg=BG, fg=FG, font=FONT_MONO).pack(pady=(0,6))

    def hex_editor(self):
        w = tk.Toplevel(self.root, bg=BG)
        w.title("Hex Editor — CPU RAM ($0000-$07FF)")
        w.configure(bg=BG); w.resizable(False, False)
        txt = tk.Text(w, width=68, height=20, bg=BG, fg=FG,
                      insertbackground=FG, font=FONT_MONO,
                      bd=1, relief="solid",
                      highlightbackground=EDGE, highlightthickness=1)
        txt.pack(padx=6, pady=6)
        ram = self.nes.bus.ram
        lines = []
        for row in range(0, 0x0800, 16):
            hexs = " ".join(f"{ram[row+i]:02X}" for i in range(16))
            asc  = "".join(chr(ram[row+i]) if 32<=ram[row+i]<127 else "."
                           for i in range(16))
            lines.append(f"{row:04X}  {hexs}  {asc}")
        txt.insert("1.0", "\n".join(lines))
        txt.configure(state="disabled")

    def debugger(self):
        w = tk.Toplevel(self.root, bg=BG)
        w.title("6502 Debugger")
        w.configure(bg=BG); w.resizable(False, False)
        info = tk.Text(w, width=58, height=18, bg=BG, fg=FG,
                       font=FONT_MONO, bd=1, relief="solid",
                       highlightbackground=EDGE, highlightthickness=1,
                       insertbackground=FG)
        info.pack(padx=6, pady=6)

        def refresh():
            c = self.nes.cpu
            lines = [
                f"PC: {c.pc:04X}   A: {c.a:02X}   X: {c.x:02X}   Y: {c.y:02X}",
                f"SP: {c.sp:02X}   P: {c.p:02X}   cycles: {c.cycles}",
                f"flags: N={int(bool(c.p&N))} V={int(bool(c.p&V))} "
                f"B={int(bool(c.p&B))} D={int(bool(c.p&D))} "
                f"I={int(bool(c.p&I))} Z={int(bool(c.p&Z))} "
                f"C={int(bool(c.p&C))}",
                "",
                "─ disasm @ PC ─",
            ]
            pc = c.pc
            for _ in range(12):
                op = c.r(pc)
                entry = CPU._table[op]
                if entry is None:
                    lines.append(f"{pc:04X}  {op:02X}      ???")
                    pc = (pc + 1) & 0xFFFF
                    continue
                am, fn, _ = entry
                mnem = fn.__name__.replace("op_","").upper()
                size = {  # rough size by addressing mode
                    "am_imp":1,"am_acc":1,"am_imm":2,"am_zp":2,
                    "am_zpx":2,"am_zpy":2,"am_abs":3,"am_abx":3,
                    "am_aby":3,"am_ind":3,"am_izx":2,"am_izy":2,
                    "am_rel":2}.get(am.__name__,1)
                raw = " ".join(f"{c.r((pc+i)&0xFFFF):02X}" for i in range(size))
                lines.append(f"{pc:04X}  {raw:<8}  {mnem}")
                pc = (pc + size) & 0xFFFF
            info.configure(state="normal")
            info.delete("1.0","end")
            info.insert("1.0", "\n".join(lines))
            info.configure(state="disabled")

        bar = tk.Frame(w, bg=BG); bar.pack(fill="x", padx=6, pady=(0,6))
        for label, cmd in (("step", lambda: (self.step_one(), refresh())),
                           ("reset", lambda: (self.reset(), refresh())),
                           ("refresh", refresh)):
            self._btn(bar, label, cmd).pack(side="left", padx=2)
        refresh()

    # --------- misc --------------------------------------------------------
    def _todo(self):
        messagebox.showinfo("acnesemu",
                            "TODO — not in 0.1 yet meow")

    def about(self):
        messagebox.showinfo(
            "About acnesemu",
            "acnesemu 0.1\n"
            "cat's NES emulator — single file, tkinter / cython-ready\n"
            "Flames / Team Flames / Samsoft\n\n"
            "FCEUX-style GUI, 6502 core, mapper 0, pattern viewer.\n"
            "PPU rendering + APU coming in 0.2  owo")

# =============================================================================
#  main
# =============================================================================
def main():
    root = tk.Tk()
    App(root)
    root.mainloop()

if __name__ == "__main__":
    main()

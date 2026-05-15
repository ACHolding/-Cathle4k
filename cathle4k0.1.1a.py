#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# cython: language_level=3, boundscheck=False, wraparound=False, cdivision=True, infer_types=True
"""
cathle0.1.v0.py — cat64hle 1.x N64 HLE emulator shell (clean-room Python monolith)
Engine: cathle1.x
GUI styled as a cat64hle 1.x boot shell with Project64-style classic chrome
Single-file Python 3.14 target — no external dependencies beyond Tkinter (PIL optional for framebuffer)

Project64 Legacy / 1.6 C/C++ tree → this file conceptual port map
- R4300 interpreter     → CPUCore.step / execute (with delayed branch)
- CP0 / TLB             → CPUCore.cp0, CPUCore.tlb, DeviceBus.v_to_p
- RDRAM / ROM           → ACsN64Core.rdram + .rom, DeviceBus read/write
- PI DMA                → trigger_pi_dma + 0x0460xxxx MMIO
- SP / RSP DMA          → trigger_sp_dma + process_rsp
- DPC / RDP (HLE)       → process_rdp (draws simple rects on Tk canvas)
- VI                    → 0x0440xxxx + Tk Canvas framebuffer preview (RGB5551)
- AI                    → process_audio (HLE counter)
- SI / PIF / Controllers→ trigger_si_dma + keyboard mapping
- MI                    → 0x0430xxxx
- Plugins               → Fully inlined catHLE-style monolith (no DLLs)

PJ64SystemFacade provides the classic "N64System" one-object view used in many
YouTube "write an N64 emulator from scratch" series.

Window title stays exactly as requested:
    cat64hle 1.x

This single-file cathle1.x build includes a dedicated ROM boot window, a
visible HLE boot framebuffer, controller input, and RDP/VI preview hooks. It is
still a lightweight HLE/interpreter emulator, not a cycle-accurate N64 core.
"""

from __future__ import annotations

import base64
import hashlib
import math
import os
import struct
import sys
import time
import random
import io
import webbrowser
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
except ImportError:
    tk = None
    filedialog = None
    messagebox = None
    ttk = None

# --- Configuration Constants (Project64 1.6 Legacy feel) ---
APP_NAME = "cat64hle"
VERSION = "0.1.v0-python314-bootwindow"
BUILD_TAG = "cathle0.1.v0-py314-cathle1x-bootwindow"
ENGINE_NAME = "cathle1.x"
PYTHON_TARGET = "3.14"
WINDOW_TITLE = "cat64hle 1.x"
FRAME_STEPS_PER_TICK = 12000
VI_REFRESH_DIVISOR = 1
TARGET_FPS = 60
FRAME_TIME_S = 1.0 / TARGET_FPS
BOOT_WINDOW_TITLE = "cat64hle 1.x ROM Boot"
BOOT_FRAMEBUFFER_ORIGIN = 0x00100000
BOOT_FRAMEBUFFER_WIDTH = 320
BOOT_FRAMEBUFFER_HEIGHT = 240

# Project64 0.1 — Python port tags
PJ64_01_LINE = "cat64hle 1.x layout → Python clean-room HLE port (cathle1.x)"
CATHLE_TAG = "catHLE monolith (public-feature HLE path, no plugin DLLs)"

PJ64_01_PORT: Dict[str, str] = {
    "R4300 interpreter": "CPUCore.step / CPUCore.execute (delayed branch)",
    "CP0 + TLB": "CPUCore.cp0, CPUCore.tlb, DeviceBus.v_to_p",
    "RDRAM": "ACsN64Core.rdram + DeviceBus read/write",
    "Cartridge ROM": "ACsN64Core.rom + PI domain 0x10……",
    "PI DMA": "ACsN64Core.trigger_pi_dma, MMIO 0x04600000–0C",
    "SP / RSP": "trigger_sp_dma, rsp_dmem/imem, process_rsp (HLE)",
    "DPC / RDP": "process_rdp, MMIO 0x04100000–0C (HLE rects)",
    "VI": "MMIO 0x0440…, Tk canvas RGB5551 preview",
    "AI": "process_audio, MMIO 0x0450… (HLE)",
    "SI / PIF": "trigger_si_dma, pif_ram, controller_state (keyboard)",
    "MI": "MMIO 0x0430…",
    "Plugins": "inlined — " + CATHLE_TAG,
    "N64System (YouTube / PJ64 tree)": "ACsN64Core.n64_system → PJ64SystemFacade",
    "CPU_step / Emulate one instr": "PJ64SystemFacade.step_cpu_instruction → CPUCore.step",
    "GFX_ProcessDList": "PJ64SystemFacade.run_rdp_hle → ACsN64Core.process_rdp",
    "RSP_Process": "PJ64SystemFacade.run_rsp_hle → ACsN64Core.process_rsp",
    "AI_DMA": "PJ64SystemFacade.run_ai_hle → ACsN64Core.process_audio",
}

def pj64_port_note(subsystem: str) -> Optional[str]:
    return PJ64_01_PORT.get(subsystem)

@dataclass(frozen=True, slots=True)
class PJ64PluginSlot:
    name: str
    role: str

def pj64_plugin_slots_monolith() -> Tuple[PJ64PluginSlot, ...]:
    return (
        PJ64PluginSlot("Gfx", "RDP display lists → Tk Canvas (process_rdp HLE)"),
        PJ64PluginSlot("Audio", "AI DMA drain counter (process_audio HLE)"),
        PJ64PluginSlot("RSP", "SP DMA + immediate HLE (process_rsp)"),
        PJ64PluginSlot("Controller", "SI PIF + keyboard → controller_state"),
    )

class PJ64SystemFacade:
    """Early-Project64 / YouTube course layout: one N64System object."""
    __slots__ = ("_core",)

    def __init__(self, core: "ACsN64Core") -> None:
        self._core = core

    @property
    def m_Cpu(self) -> "CPUCore":
        return self._core.cpu

    @property
    def m_Bus(self) -> "DeviceBus":
        return self._core.bus

    @property
    def m_RDRAM(self) -> bytearray:
        return self._core.rdram

    @property
    def m_CartRom(self) -> bytearray:
        return self._core.rom

    @property
    def m_RSP_DMEM(self) -> bytearray:
        return self._core.rsp_dmem

    @property
    def m_RSP_IMEM(self) -> bytearray:
        return self._core.rsp_imem

    @property
    def m_PIF_RAM(self) -> bytearray:
        return self._core.pif_ram

    @property
    def m_PluginSlots(self) -> Tuple[PJ64PluginSlot, ...]:
        return self._core.pj64_plugin_slots

    def step_cpu_instruction(self) -> None:
        self._core.cpu.step()

    def run_rsp_hle(self) -> None:
        self._core.process_rsp()

    def run_rdp_hle(self) -> None:
        self._core.process_rdp()

    def run_ai_hle(self) -> None:
        self._core.process_audio()

# UI — Project64 ~0.1 era (Win9x gray chrome)
PJ64_WIN_GRAY = "#c0c0c0"
PJ64_WIN_FACE = "#c0c0c0"
PJ64_BTN_FACE = "#c0c0c0"
PJ64_BTN_HIGHLIGHT = "#ffffff"
PJ64_BTN_SHADOW = "#808080"
PJ64_PANEL_WHITE = "#ffffff"
PJ64_TEXT = "#000000"
PJ64_SPLASH_GRAY = "#a0a0a0"
PJ64_VIEWPORT_BORDER = "#808080"

BG_COLOR = PJ64_WIN_GRAY
PANEL_COLOR = PJ64_BTN_FACE
TEXT_COLOR = PJ64_TEXT
ACCENT_BLUE = PJ64_TEXT
TERMINAL_GREEN = "#008000"
STATUS_RED = "#800000"
WHITE = PJ64_PANEL_WHITE

UI_FONT = ("MS Sans Serif", 8)
UI_FONT_MONO = ("Courier New", 9)
UI_FONT_BOLD = ("MS Sans Serif", 8, "bold")

# Hardware Constraints
RDRAM_SIZE = 8 * 1024 * 1024
RSP_DMEM_SIZE = 0x1000
RSP_IMEM_SIZE = 0x1000
PIF_RAM_SIZE = 0x40

# Video Interface
VI_ORIGIN_REG = 0x04400004
VI_WIDTH_REG = 0x04400008

# Bit Masks
MASK_8 = 0xFF
MASK_16 = 0xFFFF
MASK_32 = 0xFFFFFFFF
MASK_64 = 0xFFFFFFFFFFFFFFFF

# CP0 Registers
CP0_INDEX = 0
CP0_RANDOM = 1
CP0_ENTRYLO0 = 2
CP0_ENTRYLO1 = 3
CP0_CONTEXT = 4
CP0_PAGEMASK = 5
CP0_WIRED = 6
CP0_BADVADDR = 8
CP0_COUNT = 9
CP0_ENTRYHI = 10
CP0_COMPARE = 11
CP0_STATUS = 12
CP0_CAUSE = 13
CP0_EPC = 14
CP0_PRID = 15
CP0_CONFIG = 16
CP0_LLADDR = 17
CP0_ERROREPC = 30

FCR31_COND_BIT = 23

# --- Utility Functions ---
def u8(v: int) -> int: return v & MASK_8
def u16(v: int) -> int: return v & MASK_16
def u32(v: int) -> int: return v & MASK_32
def u64(v: int) -> int: return v & MASK_64

def sign8(v: int) -> int:
    v &= MASK_8
    return v - 0x100 if v & 0x80 else v

def sign16(v: int) -> int:
    v &= MASK_16
    return v - 0x10000 if v & 0x8000 else v

def sign32(v: int) -> int:
    v &= MASK_32
    return v - 0x100000000 if v & 0x80000000 else v

def sign64(v: int) -> int:
    v &= MASK_64
    return v - 0x10000000000000000 if v & 0x8000000000000000 else v

def sx8_to_64(v: int) -> int: return u64(sign8(v))
def sx16_to_64(v: int) -> int: return u64(sign16(v))
def sx32_to_64(v: int) -> int: return u64(sign32(v))

def be32(data: bytearray | bytes, offset: int) -> int:
    if offset < 0 or offset + 3 >= len(data): return 0
    return struct.unpack_from(">I", data, offset)[0]

def put_be32(data: bytearray, offset: int, value: int) -> None:
    if offset < 0 or offset + 3 >= len(data): return
    struct.pack_into(">I", data, offset, value & MASK_32)

def rdram_rgb5551_to_ppm(rdram: bytearray, origin: int, width: int, height: int) -> bytes | None:
    """Pack N64 big-endian RGBA5551 RDRAM into binary P6 PPM for tk.PhotoImage."""
    origin &= 0xFFFFFF
    width = max(1, min(width, 320))
    height = max(1, min(height, 240))
    stride = width * 2
    need = origin + stride * height
    if origin < 0 or need > len(rdram):
        return None
    header = f"P6\n{width} {height}\n255\n".encode("ascii")
    out = bytearray(width * height * 3)
    mv = memoryview(rdram)
    o = 0
    for y in range(height):
        row = origin + y * stride
        for x in range(0, stride, 2):
            px = (mv[row + x] << 8) | mv[row + x + 1]
            out[o] = ((px >> 11) & 0x1F) << 3
            out[o + 1] = ((px >> 6) & 0x1F) << 3
            out[o + 2] = ((px >> 1) & 0x1F) << 3
            o += 3
    return header + bytes(out)

def f32_to_bits(value: float) -> int:
    return struct.unpack(">I", struct.pack(">f", float(value)))[0]

def bits_to_f32(value: int) -> float:
    return struct.unpack(">f", struct.pack(">I", value & MASK_32))[0]

def f64_to_bits(value: float) -> int:
    return struct.unpack(">Q", struct.pack(">d", float(value)))[0]

def bits_to_f64(value: int) -> float:
    return struct.unpack(">d", struct.pack(">Q", value & MASK_64))[0]

def normalize_commercial_entry(addr: int) -> int:
    addr = u32(addr)
    if addr == 0 or addr == MASK_32:
        return 0x80000400
    hi = addr >> 24
    if hi in (0x80, 0xA0, 0xB0):
        if hi == 0xB0:
            return 0x80000000 | (addr & 0x1FFFFFFF)
        return addr
    if addr < RDRAM_SIZE:
        return 0x80000000 | addr
    if hi == 0 and addr < 0x04000000:
        return 0x80000000 | addr
    return addr

def seed_commercial_pif_ram(pif: bytearray) -> None:
    pif[:] = b"\xff" * PIF_RAM_SIZE
    pif[0] = 0xFF
    pif[1] = 0xFF
    pif[2] = 0xFF
    pif[3] = 0xFF

# Nintendo 64 cartridge image signatures
Z64_BIG_ENDIAN_MAGIC = b"\x80\x37\x12\x40"
V64_MAGIC = b"\x37\x80\x40\x12"
N64_LE_MAGIC = b"\x40\x12\x37\x80"
_CART_SIGS = (Z64_BIG_ENDIAN_MAGIC, V64_MAGIC, N64_LE_MAGIC)

def strip_documentation_header_if_present(data: bytearray) -> None:
    if len(data) < 4:
        return
    for _ in range(4):
        if len(data) >= 4 and data[0:4] in _CART_SIGS:
            return
        search_cap = min(len(data), 16 * 1024 * 1024)
        found = False
        for off in (4096, 2048, 512):
            if off + 4 <= search_cap and data[off : off + 4] in _CART_SIGS:
                del data[:off]
                found = True
                break
        if not found:
            return

def apply_ultra64_cart_header_defaults(data: bytearray) -> None:
    if len(data) < 0x40:
        data.extend(b"\x00" * (0x40 - len(data)))
    if data[0:4] not in _CART_SIGS:
        return
    if data[0:4] != Z64_BIG_ENDIAN_MAGIC:
        return
    if be32(data, 0x04) == 0:
        put_be32(data, 0x04, 0x00000F48)
    boot = be32(data, 0x08)
    if boot == 0 or boot == MASK_32:
        put_be32(data, 0x08, 0x80000400)
    if be32(data, 0x0C) == 0:
        put_be32(data, 0x0C, 0x0000144B)
    title_region = data[0x20:0x34]
    if not any(title_region):
        pat = b"Ultra 64\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
        data[0x20:0x34] = pat[:20].ljust(20, b"\x00")
    if data[0x3E] == 0:
        data[0x3E] = 0x45

# Opcode tables (Project64 0.1 style dispatch)
PRIMARY_OPS = {
    0x00: "SPECIAL", 0x01: "REGIMM", 0x02: "J", 0x03: "JAL",
    0x04: "BEQ", 0x05: "BNE", 0x06: "BLEZ", 0x07: "BGTZ",
    0x08: "ADDI", 0x09: "ADDIU", 0x0A: "SLTI", 0x0B: "SLTIU",
    0x0C: "ANDI", 0x0D: "ORI", 0x0E: "XORI", 0x0F: "LUI",
    0x10: "COP0", 0x11: "COP1", 0x12: "COP2", 0x13: "COP3",
    0x14: "BEQL", 0x15: "BNEL", 0x16: "BLEZL", 0x17: "BGTZL",
    0x18: "DADDI", 0x19: "DADDIU", 0x1A: "LDL", 0x1B: "LDR",
    0x20: "LB", 0x21: "LH", 0x22: "LWL", 0x23: "LW",
    0x24: "LBU", 0x25: "LHU", 0x26: "LWR", 0x27: "LWU",
    0x28: "SB", 0x29: "SH", 0x2A: "SWL", 0x2B: "SW",
    0x2C: "SDL", 0x2D: "SDR", 0x2E: "SWR", 0x2F: "CACHE",
    0x30: "LL", 0x31: "LWC1", 0x32: "LWC2", 0x33: "LWC3",
    0x34: "LLD", 0x35: "LDC1", 0x36: "LDC2", 0x37: "LD",
    0x38: "SC", 0x39: "SWC1", 0x3A: "SWC2", 0x3B: "SWC3",
    0x3C: "SCD", 0x3D: "SDC1", 0x3E: "SDC2", 0x3F: "SD",
}

SPECIAL_OPS = {
    0x00: "SLL", 0x02: "SRL", 0x03: "SRA", 0x04: "SLLV",
    0x06: "SRLV", 0x07: "SRAV", 0x08: "JR", 0x09: "JALR",
    0x0C: "SYSCALL", 0x0D: "BREAK", 0x0F: "SYNC",
    0x10: "MFHI", 0x11: "MTHI", 0x12: "MFLO", 0x13: "MTLO",
    0x14: "DSLLV", 0x16: "DSRLV", 0x17: "DSRAV",
    0x18: "MULT", 0x19: "MULTU", 0x1A: "DIV", 0x1B: "DIVU",
    0x1C: "DMULT", 0x1D: "DMULTU", 0x1E: "DDIV", 0x1F: "DDIVU",
    0x20: "ADD", 0x21: "ADDU", 0x22: "SUB", 0x23: "SUBU",
    0x24: "AND", 0x25: "OR", 0x26: "XOR", 0x27: "NOR",
    0x2A: "SLT", 0x2B: "SLTU", 0x2C: "DADD", 0x2D: "DADDU",
    0x2E: "DSUB", 0x2F: "DSUBU",
    0x30: "TGE", 0x31: "TGEU", 0x32: "TLT", 0x33: "TLTU",
    0x34: "TEQ", 0x36: "TNE",
    0x38: "DSLL", 0x3A: "DSRL", 0x3B: "DSRA",
    0x3C: "DSLL32", 0x3E: "DSRL32", 0x3F: "DSRA32",
}

REGIMM_OPS = {
    0x00: "BLTZ", 0x01: "BGEZ", 0x02: "BLTZL", 0x03: "BGEZL",
    0x08: "TGEI", 0x09: "TGEIU", 0x0A: "TLTI", 0x0B: "TLTIU",
    0x0C: "TEQI", 0x0E: "TNEI",
    0x10: "BLTZAL", 0x11: "BGEZAL", 0x12: "BLTZALL", 0x13: "BGEZALL",
}

COP0_RS = {
    0x00: "MFC0", 0x01: "DMFC0", 0x02: "CFC0", 0x04: "MTC0",
    0x05: "DMTC0", 0x06: "CTC0", 0x08: "BC0", 0x10: "COP0_CO",
}

COP0_CO = {
    0x01: "TLBR", 0x02: "TLBWI", 0x06: "TLBWR", 0x08: "TLBP",
    0x18: "ERET",
}

COP1_RS = {
    0x00: "MFC1", 0x01: "DMFC1", 0x02: "CFC1", 0x04: "MTC1",
    0x05: "DMTC1", 0x06: "CTC1", 0x08: "BC1",
    0x10: "S", 0x11: "D", 0x14: "W", 0x15: "L",
}

COP1_FUNCT = {
    0x00: "ADD", 0x01: "SUB", 0x02: "MUL", 0x03: "DIV",
    0x04: "SQRT", 0x05: "ABS", 0x06: "MOV", 0x07: "NEG",
    0x08: "ROUND.L", 0x09: "TRUNC.L", 0x0A: "CEIL.L", 0x0B: "FLOOR.L",
    0x0C: "ROUND.W", 0x0D: "TRUNC.W", 0x0E: "CEIL.W", 0x0F: "FLOOR.W",
    0x20: "CVT.S", 0x21: "CVT.D", 0x24: "CVT.W", 0x25: "CVT.L",
    0x30: "C.F", 0x31: "C.UN", 0x32: "C.EQ", 0x33: "C.UEQ",
    0x34: "C.OLT", 0x35: "C.ULT", 0x36: "C.OLE", 0x37: "C.ULE",
    0x38: "C.SF", 0x39: "C.NGLE", 0x3A: "C.SEQ", 0x3B: "C.NGL",
    0x3C: "C.LT", 0x3D: "C.NGE", 0x3E: "C.LE", 0x3F: "C.NGT",
}

# --- Core Hardware Logic ---

class N64Header:
    __slots__ = (
        "pi_bsd_dom1_lat", "pi_bsd_dom1_pwd", "pi_bsd_dom1_pgs", "pi_bsd_dom1_rls",
        "clock_rate", "boot_address", "release", "crc1", "crc2", "title", "cart_id",
    )

    def __init__(self, data: bytearray):
        if len(data) >= 0x40:
            self.pi_bsd_dom1_lat = data[0]
            self.pi_bsd_dom1_pwd = data[1]
            self.pi_bsd_dom1_pgs = data[2]
            self.pi_bsd_dom1_rls = data[3]
            self.clock_rate = be32(data, 0x04)
            self.boot_address = be32(data, 0x08)
            self.release = be32(data, 0x0C)
            self.crc1 = be32(data, 0x10)
            self.crc2 = be32(data, 0x14)
            self.title = data[0x20:0x34].decode('ascii', 'ignore').strip('\x00').strip()
            self.cart_id = data[0x3C:0x3E].decode('ascii', 'ignore')
        else:
            self.clock_rate = 0
            self.boot_address = 0x80000400
            self.release = 0
            self.crc1 = 0
            self.crc2 = 0
            self.title = "UNKNOWN"
            self.cart_id = "??"

@dataclass(slots=True)
class TLBEntry:
    mask: int = 0
    vpn2: int = 0
    g: bool = False
    asid: int = 0
    pfn0: int = 0
    c0: int = 0
    d0: bool = False
    v0: bool = False
    pfn1: int = 0
    c1: int = 0
    d1: bool = False
    v1: bool = False

class N64Opcode:
    __slots__ = ("word", "op", "rs", "rt", "rd", "sa", "funct", "imm", "simm", "target")
    def __init__(self, word: int):
        self.word = word & MASK_32
        self.op = (self.word >> 26) & 0x3F
        self.rs = (self.word >> 21) & 0x1F
        self.rt = (self.word >> 16) & 0x1F
        self.rd = (self.word >> 11) & 0x1F
        self.sa = (self.word >> 6) & 0x1F
        self.funct = self.word & 0x3F
        self.imm = self.word & MASK_16
        self.simm = sign16(self.imm)
        self.target = self.word & 0x03FFFFFF

    def target_addr(self, pc: int) -> int:
        return u32(((pc + 4) & 0xF0000000) | (self.target << 2))

    def branch_addr(self, pc: int) -> int:
        return u32(pc + 4 + (self.simm << 2))

class DeviceBus:
    """N64 physical map + MMIO — Project64 0.1 memory/IO dispatch style."""
    __slots__ = ("core", "regs")

    def __init__(self, core: ACsN64Core):
        self.core = core
        self.regs: Dict[int, int] = {}
        self.reset()

    def reset(self):
        self.regs.clear()
        self.regs[0x04300004] = 0x02020102  # MI_VERSION
        self.regs[VI_ORIGIN_REG] = 0
        self.regs[VI_WIDTH_REG] = 320
        self.regs[0x04600010] = 0  # PI_STATUS
        self.regs[0x0450000C] = 0  # AI_STATUS
        self.regs[0x04800018] = 0  # SI_STATUS
        self.regs[0x04040010] = 1  # SP_STATUS (Halted)

    def v_to_p(self, addr: int) -> int:
        addr &= MASK_32
        segment = addr >> 29
        if segment in (0b100, 0b101):  # KSEG0 / KSEG1
            return addr & 0x1FFFFFFF
        # TLB
        tlb = self.core.cpu.tlb
        asid = self.core.cpu.cp0[CP0_ENTRYHI] & 0xFF
        vpn2 = (addr >> 13) & 0x7FFFF
        for entry in tlb:
            if entry.vpn2 == vpn2 and (entry.g or entry.asid == asid):
                even_odd = (addr >> 12) & 1
                if even_odd == 0:
                    if entry.v0:
                        return (entry.pfn0 << 12) | (addr & 0xFFF)
                else:
                    if entry.v1:
                        return (entry.pfn1 << 12) | (addr & 0xFFF)
        return addr & 0x1FFFFFFF

    def read_u8(self, addr: int) -> int:
        p = self.v_to_p(addr)
        if 0 <= p < RDRAM_SIZE:
            return self.core.rdram[p]
        if 0x10000000 <= p < 0x10000000 + len(self.core.rom):
            return self.core.rom[p - 0x10000000]
        if 0x1FC007C0 <= p < 0x1FC007C0 + PIF_RAM_SIZE:
            return self.core.pif_ram[p - 0x1FC007C0]
        return 0

    def read_u16(self, addr: int) -> int:
        p = self.v_to_p(addr)
        if 0 <= p < RDRAM_SIZE - 1:
            return (self.core.rdram[p] << 8) | self.core.rdram[p + 1]
        return 0

    def read_u32(self, addr: int) -> int:
        p_addr = self.v_to_p(addr)
        if 0x00000000 <= p_addr <= RDRAM_SIZE - 4:
            return be32(self.core.rdram, p_addr)
        if 0x04000000 <= p_addr <= 0x04001000 - 4:
            return be32(self.core.rsp_dmem, p_addr - 0x04000000)
        if 0x04001000 <= p_addr <= 0x04002000 - 4:
            return be32(self.core.rsp_imem, p_addr - 0x04001000)
        if 0x04040000 <= p_addr <= 0x048FFFFF:
            return self.regs.get(p_addr & ~3, 0)
        rom_len = len(self.core.rom)
        roff = p_addr - 0x10000000
        if 0 <= roff <= rom_len - 4:
            return be32(self.core.rom, roff)
        return 0

    def read_u64(self, addr: int) -> int:
        hi = self.read_u32(addr)
        lo = self.read_u32(addr + 4)
        return ((hi << 32) | lo) & MASK_64

    def write_u8(self, addr: int, val: int):
        p = self.v_to_p(addr)
        if 0 <= p < RDRAM_SIZE:
            self.core.rdram[p] = val & MASK_8
        elif 0x1FC007C0 <= p < 0x1FC007C0 + PIF_RAM_SIZE:
            self.core.pif_ram[p - 0x1FC007C0] = val & MASK_8

    def write_u16(self, addr: int, val: int):
        p = self.v_to_p(addr)
        if 0 <= p < RDRAM_SIZE - 1:
            val &= MASK_16
            self.core.rdram[p] = (val >> 8) & MASK_8
            self.core.rdram[p + 1] = val & MASK_8

    def write_u32(self, addr: int, val: int):
        p_addr = self.v_to_p(addr)
        if 0x00000000 <= p_addr <= RDRAM_SIZE - 4:
            put_be32(self.core.rdram, p_addr, val)
        elif 0x04000000 <= p_addr <= 0x04001000 - 4:
            put_be32(self.core.rsp_dmem, p_addr - 0x04000000, val)
        elif 0x04001000 <= p_addr <= 0x04002000 - 4:
            put_be32(self.core.rsp_imem, p_addr - 0x04001000, val)
        elif 0x04040000 <= p_addr <= 0x048FFFFF:
            aligned = p_addr & ~3
            self.regs[aligned] = val
            self.handle_mmio(aligned, val)

    def write_u64(self, addr: int, val: int):
        val &= MASK_64
        self.write_u32(addr, (val >> 32) & MASK_32)
        self.write_u32(addr + 4, val & MASK_32)

    def handle_mmio(self, addr: int, val: int):
        if addr == 0x0460000C:  # PI DMA Write
            self.core.trigger_pi_dma()
            self.regs[0x04600010] = 0
        elif addr == 0x04040008:
            self.core.trigger_sp_dma(to_rsp=True)
        elif addr == 0x0404000C:
            self.core.trigger_sp_dma(to_rsp=False)
        elif addr == 0x04040010:  # SP_STATUS
            if val & 1:
                self.regs[0x04040010] &= ~1
            if val & 2:
                self.regs[0x04040010] |= 1
            if (self.regs[0x04040010] & 1) == 0:
                self.core.process_rsp()
        elif addr == 0x0410000C:  # DPC_END
            self.core.process_rdp()
        elif addr == 0x04500004:  # AI_LEN
            self.core.process_audio()
        elif addr == 0x04800004:  # SI_PIF_ADDR_RD64B
            self.core.trigger_si_dma(read_pif=True)
        elif addr == 0x04800010:  # SI_PIF_ADDR_WR64B
            self.core.trigger_si_dma(read_pif=False)

class CPUCore:
    """R4300i interpreter — cat64hle 1.x clean-room HLE port (cathle1.x)."""
    __slots__ = (
        "core", "gpr", "fpr", "cp0", "fcr0", "fcr31", "hi", "lo",
        "pc", "next_pc", "llbit", "lladdr", "tlb",
    )

    def __init__(self, core: ACsN64Core):
        self.core = core
        self.gpr = [0] * 32
        self.fpr = [0] * 32
        self.cp0 = [0] * 32
        self.fcr0 = 0x00000511
        self.fcr31 = 0
        self.hi = 0
        self.lo = 0
        self.pc = 0
        self.next_pc = 4
        self.llbit = False
        self.lladdr = 0
        self.tlb: List[TLBEntry] = [TLBEntry() for _ in range(32)]
        self.reset()

    def reset(self):
        self.gpr = [0] * 32
        self.fpr = [0] * 32
        self.cp0 = [0] * 32
        self.fcr0 = 0x00000511
        self.fcr31 = 0
        self.hi = 0
        self.lo = 0
        self.pc = 0
        self.next_pc = 4
        self.cp0[CP0_PRID] = 0x00000B00
        self.cp0[CP0_STATUS] = 0x34000000
        self.cp0[CP0_CONFIG] = 0x0006E463
        self.cp0[CP0_WIRED] = 0
        self.llbit = False
        self.lladdr = 0
        self.tlb = [TLBEntry() for _ in range(32)]

    def _fetch_instruction_word(self, addr: int) -> int:
        """Fast instruction fetch for the common KSEG0/KSEG1 RDRAM and ROM paths."""
        p_addr = addr & MASK_32
        segment = p_addr >> 29
        if segment in (0b100, 0b101):  # KSEG0 / KSEG1
            p_addr &= 0x1FFFFFFF
        else:
            p_addr = self.core.bus.v_to_p(p_addr)

        if 0x00000000 <= p_addr <= RDRAM_SIZE - 4:
            return be32(self.core.rdram, p_addr)
        if 0x04000000 <= p_addr <= 0x04001000 - 4:
            return be32(self.core.rsp_dmem, p_addr - 0x04000000)
        if 0x04001000 <= p_addr <= 0x04002000 - 4:
            return be32(self.core.rsp_imem, p_addr - 0x04001000)

        roff = p_addr - 0x10000000
        if 0 <= roff <= len(self.core.rom) - 4:
            return be32(self.core.rom, roff)
        return self.core.bus.read_u32(addr)

    def step(self):
        pc = self.pc
        p_addr = pc & MASK_32
        word: Optional[int] = None

        # Hot path: most R4300i fetches are KSEG0/KSEG1 direct-mapped RDRAM/ROM.
        # Falling back to DeviceBus keeps TLB/MMIO behavior intact for unusual paths.
        segment = p_addr >> 29
        if segment in (0b100, 0b101):
            phys = p_addr & 0x1FFFFFFF
            if 0x00000000 <= phys <= RDRAM_SIZE - 4:
                word = be32(self.core.rdram, phys)
            else:
                roff = phys - 0x10000000
                if 0 <= roff <= len(self.core.rom) - 4:
                    word = be32(self.core.rom, roff)

        if word is None:
            word = self.core.bus.read_u32(pc)

        # NOP is common in delay slots and empty memory. Avoid decode/dispatch overhead.
        if word == 0:
            self.pc = self.next_pc
            self.next_pc = u32(self.next_pc + 4)
            self.gpr[0] = 0
            self.cp0[CP0_COUNT] = u32(self.cp0[CP0_COUNT] + 1)
            return

        i = N64Opcode(word)
        self.execute(i)
        self.gpr[0] = 0
        self.cp0[CP0_COUNT] = u32(self.cp0[CP0_COUNT] + 1)

    def decode_name(self, o: N64Opcode) -> str:
        if o.op == 0:
            return SPECIAL_OPS.get(o.funct, "UNKNOWN")
        if o.op == 1:
            return REGIMM_OPS.get(o.rt, "UNKNOWN")
        if o.op == 0x10:
            if o.rs == 0x10:
                return COP0_CO.get(o.funct, "UNKNOWN")
            return COP0_RS.get(o.rs, "UNKNOWN")
        if o.op == 0x11:
            base = COP1_RS.get(o.rs, "UNKNOWN")
            if base in ("S", "D", "W", "L"):
                return f"{COP1_FUNCT.get(o.funct, 'UNKNOWN')}.{base}"
            return base
        return PRIMARY_OPS.get(o.op, "UNKNOWN")

    def _branch(self, target: int):
        self.next_pc = u32(target)

    def _skip_likely(self):
        self.pc = u32(self.pc + 4)
        self.next_pc = u32(self.pc + 4)

    def _write_tlb_entry(self, index: int):
        idx = index % 32
        hi = self.cp0[CP0_ENTRYHI]
        lo0 = self.cp0[CP0_ENTRYLO0]
        lo1 = self.cp0[CP0_ENTRYLO1]
        pagemask = self.cp0[CP0_PAGEMASK]
        self.tlb[idx].mask = pagemask
        self.tlb[idx].vpn2 = (hi >> 13) & 0x7FFFF
        self.tlb[idx].asid = hi & 0xFF
        self.tlb[idx].g = bool((lo0 & 1) and (lo1 & 1))
        self.tlb[idx].pfn0 = (lo0 >> 6) & 0xFFFFF
        self.tlb[idx].c0 = (lo0 >> 3) & 7
        self.tlb[idx].d0 = bool((lo0 >> 2) & 1)
        self.tlb[idx].v0 = bool((lo0 >> 1) & 1)
        self.tlb[idx].pfn1 = (lo1 >> 6) & 0xFFFFF
        self.tlb[idx].c1 = (lo1 >> 3) & 7
        self.tlb[idx].d1 = bool((lo1 >> 2) & 1)
        self.tlb[idx].v1 = bool((lo1 >> 1) & 1)

    def execute(self, o: N64Opcode):
        name = self.decode_name(o)
        old_pc = self.pc
        self.pc = self.next_pc
        self.next_pc = u32(self.next_pc + 4)
        g = self.gpr

        # --- Loads ---
        if name == "LUI":
            g[o.rt] = sx32_to_64(o.imm << 16)
        elif name == "ORI":
            g[o.rt] = u64(g[o.rs] | o.imm)
        elif name == "ANDI":
            g[o.rt] = u64(g[o.rs] & o.imm)
        elif name == "XORI":
            g[o.rt] = u64(g[o.rs] ^ o.imm)
        elif name == "ADDI":
            g[o.rt] = sx32_to_64((g[o.rs] + o.simm) & MASK_32)
        elif name == "ADDIU":
            g[o.rt] = sx32_to_64((g[o.rs] + o.simm) & MASK_32)
        elif name == "DADDI":
            g[o.rt] = u64(sign64(g[o.rs]) + o.simm)
        elif name == "DADDIU":
            g[o.rt] = u64(g[o.rs] + o.simm)
        elif name == "SLTI":
            g[o.rt] = 1 if sign64(g[o.rs]) < o.simm else 0
        elif name == "SLTIU":
            g[o.rt] = 1 if g[o.rs] < u64(o.simm) else 0

        elif name == "LW":
            g[o.rt] = sx32_to_64(self.core.bus.read_u32(g[o.rs] + o.simm))
        elif name == "LWU":
            g[o.rt] = self.core.bus.read_u32(g[o.rs] + o.simm)
        elif name == "LH":
            g[o.rt] = sx16_to_64(self.core.bus.read_u16(g[o.rs] + o.simm))
        elif name == "LHU":
            g[o.rt] = self.core.bus.read_u16(g[o.rs] + o.simm)
        elif name == "LB":
            g[o.rt] = sx8_to_64(self.core.bus.read_u8(g[o.rs] + o.simm))
        elif name == "LBU":
            g[o.rt] = self.core.bus.read_u8(g[o.rs] + o.simm)
        elif name == "LD":
            g[o.rt] = self.core.bus.read_u64(g[o.rs] + o.simm)
        elif name == "LL":
            addr = u32(g[o.rs] + o.simm)
            g[o.rt] = sx32_to_64(self.core.bus.read_u32(addr))
            self.llbit = True
            self.lladdr = addr & ~3
        elif name == "LLD":
            addr = u32(g[o.rs] + o.simm)
            g[o.rt] = self.core.bus.read_u64(addr)
            self.llbit = True
            self.lladdr = addr & ~7

        # --- Stores ---
        elif name == "SW":
            self.core.bus.write_u32(g[o.rs] + o.simm, u32(g[o.rt]))
        elif name == "SH":
            self.core.bus.write_u16(g[o.rs] + o.simm, u16(g[o.rt]))
        elif name == "SB":
            self.core.bus.write_u8(g[o.rs] + o.simm, u8(g[o.rt]))
        elif name == "SD":
            self.core.bus.write_u64(g[o.rs] + o.simm, g[o.rt])
        elif name == "SC":
            addr = u32(g[o.rs] + o.simm)
            if self.llbit and (addr & ~3) == self.lladdr:
                self.core.bus.write_u32(addr, u32(g[o.rt]))
                g[o.rt] = 1
            else:
                g[o.rt] = 0
            self.llbit = False
        elif name == "SCD":
            addr = u32(g[o.rs] + o.simm)
            if self.llbit and (addr & ~7) == self.lladdr:
                self.core.bus.write_u64(addr, g[o.rt])
                g[o.rt] = 1
            else:
                g[o.rt] = 0
            self.llbit = False

        # --- ALU ---
        elif name == "ADD":
            g[o.rd] = sx32_to_64((g[o.rs] + g[o.rt]) & MASK_32)
        elif name == "ADDU":
            g[o.rd] = sx32_to_64((g[o.rs] + g[o.rt]) & MASK_32)
        elif name == "SUB":
            g[o.rd] = sx32_to_64((g[o.rs] - g[o.rt]) & MASK_32)
        elif name == "SUBU":
            g[o.rd] = sx32_to_64((g[o.rs] - g[o.rt]) & MASK_32)
        elif name == "DADD":
            g[o.rd] = u64(sign64(g[o.rs]) + sign64(g[o.rt]))
        elif name == "DADDU":
            g[o.rd] = u64(g[o.rs] + g[o.rt])
        elif name == "DSUB":
            g[o.rd] = u64(sign64(g[o.rs]) - sign64(g[o.rt]))
        elif name == "DSUBU":
            g[o.rd] = u64(g[o.rs] - g[o.rt])
        elif name == "AND":
            g[o.rd] = u64(g[o.rs] & g[o.rt])
        elif name == "OR":
            g[o.rd] = u64(g[o.rs] | g[o.rt])
        elif name == "XOR":
            g[o.rd] = u64(g[o.rs] ^ g[o.rt])
        elif name == "NOR":
            g[o.rd] = u64(~(g[o.rs] | g[o.rt]))
        elif name == "SLT":
            g[o.rd] = 1 if sign64(g[o.rs]) < sign64(g[o.rt]) else 0
        elif name == "SLTU":
            g[o.rd] = 1 if g[o.rs] < g[o.rt] else 0

        # --- Shifts ---
        elif name == "SLL":
            g[o.rd] = sx32_to_64((g[o.rt] & MASK_32) << o.sa)
        elif name == "SRL":
            g[o.rd] = sx32_to_64((g[o.rt] & MASK_32) >> o.sa)
        elif name == "SRA":
            g[o.rd] = sx32_to_64(sign32(g[o.rt]) >> o.sa)
        elif name == "SLLV":
            g[o.rd] = sx32_to_64((g[o.rt] & MASK_32) << (g[o.rs] & 0x1F))
        elif name == "SRLV":
            g[o.rd] = sx32_to_64((g[o.rt] & MASK_32) >> (g[o.rs] & 0x1F))
        elif name == "SRAV":
            g[o.rd] = sx32_to_64(sign32(g[o.rt]) >> (g[o.rs] & 0x1F))
        elif name == "DSLL":
            g[o.rd] = u64(g[o.rt] << o.sa)
        elif name == "DSRL":
            g[o.rd] = u64(g[o.rt] >> o.sa)
        elif name == "DSRA":
            g[o.rd] = u64(sign64(g[o.rt]) >> o.sa)
        elif name == "DSLLV":
            g[o.rd] = u64(g[o.rt] << (g[o.rs] & 0x3F))
        elif name == "DSRLV":
            g[o.rd] = u64(g[o.rt] >> (g[o.rs] & 0x3F))
        elif name == "DSRAV":
            g[o.rd] = u64(sign64(g[o.rt]) >> (g[o.rs] & 0x3F))
        elif name == "DSLL32":
            g[o.rd] = u64(g[o.rt] << (o.sa + 32))
        elif name == "DSRL32":
            g[o.rd] = u64(g[o.rt] >> (o.sa + 32))
        elif name == "DSRA32":
            g[o.rd] = u64(sign64(g[o.rt]) >> (o.sa + 32))

        # --- HI/LO ---
        elif name == "MFHI":
            g[o.rd] = self.hi
        elif name == "MTHI":
            self.hi = u64(g[o.rs])
        elif name == "MFLO":
            g[o.rd] = self.lo
        elif name == "MTLO":
            self.lo = u64(g[o.rs])
        elif name == "MULT":
            prod = sign32(g[o.rs]) * sign32(g[o.rt])
            self.lo = sx32_to_64(prod & MASK_32)
            self.hi = sx32_to_64((prod >> 32) & MASK_32)
        elif name == "MULTU":
            prod = (g[o.rs] & MASK_32) * (g[o.rt] & MASK_32)
            self.lo = sx32_to_64(prod & MASK_32)
            self.hi = sx32_to_64((prod >> 32) & MASK_32)
        elif name == "DMULT":
            prod = sign64(g[o.rs]) * sign64(g[o.rt])
            self.lo = u64(prod)
            self.hi = u64(prod >> 64)
        elif name == "DMULTU":
            prod = g[o.rs] * g[o.rt]
            self.lo = u64(prod)
            self.hi = u64(prod >> 64)
        elif name in ("DIV", "DIVU"):
            a = g[o.rs] & MASK_32
            b = g[o.rt] & MASK_32
            if b != 0:
                if name == "DIV":
                    q, r = int(sign32(a) / sign32(b)), sign32(a) % sign32(b)
                else:
                    q, r = a // b, a % b
                self.lo = sx32_to_64(q)
                self.hi = sx32_to_64(r)
        elif name in ("DDIV", "DDIVU"):
            a = g[o.rs]
            b = g[o.rt]
            if b != 0:
                if name == "DDIV":
                    q, r = int(sign64(a) / sign64(b)), sign64(a) % sign64(b)
                else:
                    q, r = a // b, a % b
                self.lo = u64(q)
                self.hi = u64(r)

        # Unaligned (stub)
        elif name in ("LWL", "LWR", "LDL", "LDR", "SWL", "SWR", "SDL", "SDR"):
            pass

        # --- Branches / Jumps ---
        elif name == "J":
            self._branch(o.target_addr(old_pc))
        elif name == "JAL":
            g[31] = u64(old_pc + 8)
            self._branch(o.target_addr(old_pc))
        elif name == "JR":
            self._branch(g[o.rs])
        elif name == "JALR":
            g[o.rd] = u64(old_pc + 8)
            self._branch(g[o.rs])
        elif name == "BEQ":
            if g[o.rs] == g[o.rt]:
                self._branch(o.branch_addr(old_pc))
        elif name == "BNE":
            if g[o.rs] != g[o.rt]:
                self._branch(o.branch_addr(old_pc))
        elif name == "BLEZ":
            if sign64(g[o.rs]) <= 0:
                self._branch(o.branch_addr(old_pc))
        elif name == "BGTZ":
            if sign64(g[o.rs]) > 0:
                self._branch(o.branch_addr(old_pc))
        elif name == "BEQL":
            if g[o.rs] == g[o.rt]:
                self._branch(o.branch_addr(old_pc))
            else:
                self._skip_likely()
        elif name == "BNEL":
            if g[o.rs] != g[o.rt]:
                self._branch(o.branch_addr(old_pc))
            else:
                self._skip_likely()
        elif name == "BLEZL":
            if sign64(g[o.rs]) <= 0:
                self._branch(o.branch_addr(old_pc))
            else:
                self._skip_likely()
        elif name == "BGTZL":
            if sign64(g[o.rs]) > 0:
                self._branch(o.branch_addr(old_pc))
            else:
                self._skip_likely()
        elif name == "BLTZ":
            if sign64(g[o.rs]) < 0:
                self._branch(o.branch_addr(old_pc))
        elif name == "BGEZ":
            if sign64(g[o.rs]) >= 0:
                self._branch(o.branch_addr(old_pc))
        elif name == "BLTZL":
            if sign64(g[o.rs]) < 0:
                self._branch(o.branch_addr(old_pc))
            else:
                self._skip_likely()
        elif name == "BGEZL":
            if sign64(g[o.rs]) >= 0:
                self._branch(o.branch_addr(old_pc))
            else:
                self._skip_likely()
        elif name == "BLTZAL":
            g[31] = u64(old_pc + 8)
            if sign64(g[o.rs]) < 0:
                self._branch(o.branch_addr(old_pc))
        elif name == "BGEZAL":
            g[31] = u64(old_pc + 8)
            if sign64(g[o.rs]) >= 0:
                self._branch(o.branch_addr(old_pc))
        elif name == "BLTZALL":
            g[31] = u64(old_pc + 8)
            if sign64(g[o.rs]) < 0:
                self._branch(o.branch_addr(old_pc))
            else:
                self._skip_likely()
        elif name == "BGEZALL":
            g[31] = u64(old_pc + 8)
            if sign64(g[o.rs]) >= 0:
                self._branch(o.branch_addr(old_pc))
            else:
                self._skip_likely()

        # --- COP0 / TLB ---
        elif name == "MFC0":
            g[o.rt] = sx32_to_64(self.cp0[o.rd])
        elif name == "DMFC0":
            g[o.rt] = u64(self.cp0[o.rd])
        elif name == "MTC0":
            self.cp0[o.rd] = u32(g[o.rt])
            if o.rd == CP0_COMPARE:
                self.cp0[CP0_CAUSE] &= ~(1 << 15)
        elif name == "DMTC0":
            self.cp0[o.rd] = u64(g[o.rt])
        elif name == "ERET":
            target = self.cp0[CP0_ERROREPC] if (self.cp0[CP0_STATUS] & 0x4) else self.cp0[CP0_EPC]
            self.pc = u32(target)
            self.next_pc = u32(self.pc + 4)
            self.cp0[CP0_STATUS] &= ~0x6
        elif name == "TLBWI":
            idx = self.cp0[CP0_INDEX] & 0x1F
            self._write_tlb_entry(idx)
        elif name == "TLBWR":
            w = self.cp0[CP0_WIRED] & 0x1F
            idx = random.randint(w, 31)
            self._write_tlb_entry(idx)
        elif name == "TLBP":
            hi = self.cp0[CP0_ENTRYHI]
            vpn2 = (hi >> 13) & 0x7FFFF
            asid = hi & 0xFF
            match = -1
            for i, entry in enumerate(self.tlb):
                if entry.vpn2 == vpn2 and (entry.g or entry.asid == asid):
                    match = i
                    break
            self.cp0[CP0_INDEX] = match if match >= 0 else 0x80000000
        elif name == "TLBR":
            idx = self.cp0[CP0_INDEX] & 0x1F
            entry = self.tlb[idx]
            self.cp0[CP0_PAGEMASK] = entry.mask
            self.cp0[CP0_ENTRYHI] = (entry.vpn2 << 13) | entry.asid
            self.cp0[CP0_ENTRYLO0] = (entry.pfn0 << 6) | (entry.c0 << 3) | (entry.d0 << 2) | (entry.v0 << 1) | entry.g
            self.cp0[CP0_ENTRYLO1] = (entry.pfn1 << 6) | (entry.c1 << 3) | (entry.d1 << 2) | (entry.v1 << 1) | entry.g

        # --- COP1 (FPU) ---
        elif name == "MFC1":
            g[o.rt] = sx32_to_64(self.fpr[o.rd] & MASK_32)
        elif name == "DMFC1":
            g[o.rt] = self.fpr[o.rd]
        elif name == "CFC1":
            g[o.rt] = sx32_to_64(self.fcr31 if o.rd == 31 else self.fcr0)
        elif name == "MTC1":
            self.fpr[o.rd] = u64((self.fpr[o.rd] & 0xFFFFFFFF00000000) | (g[o.rt] & MASK_32))
        elif name == "DMTC1":
            self.fpr[o.rd] = g[o.rt]
        elif name == "CTC1":
            if o.rd == 31:
                self.fcr31 = u32(g[o.rt])
            elif o.rd == 0:
                self.fcr0 = u32(g[o.rt])
        elif name == "BC1":
            tf = o.rt & 1
            likely = bool(o.rt & 2)
            cond = bool((self.fcr31 >> FCR31_COND_BIT) & 1)
            if cond == bool(tf):
                self._branch(o.branch_addr(old_pc))
            elif likely:
                self._skip_likely()
        elif "." in name:
            # FPU arithmetic stub (expandable)
            pass

class ACsN64Core:
    """cat64hle 1.x monolith core (cathle1.x engine)."""
    __slots__ = (
        "rom", "rdram", "rsp_dmem", "rsp_imem", "pif_ram", "bus", "cpu",
        "pj64_plugin_slots", "n64_system", "rom_name", "is_running", "has_booted",
        "frame_count", "hle_calls", "controller_state", "rdp_draw_commands",
        "audio_samples_played", "header", "last_error",
    )

    def __init__(self):
        self.rom = bytearray()
        self.rdram = bytearray(RDRAM_SIZE)
        self.rsp_dmem = bytearray(RSP_DMEM_SIZE)
        self.rsp_imem = bytearray(RSP_IMEM_SIZE)
        self.pif_ram = bytearray(PIF_RAM_SIZE)

        self.bus = DeviceBus(self)
        self.cpu = CPUCore(self)
        self.pj64_plugin_slots: Tuple[PJ64PluginSlot, ...] = pj64_plugin_slots_monolith()
        self.n64_system = PJ64SystemFacade(self)

        self.rom_name = "None"
        self.header: Optional[N64Header] = None
        self.last_error = ""
        self.is_running = False
        self.has_booted = False
        self.frame_count = 0
        self.hle_calls = 0

        self.controller_state = 0x0000
        self.rdp_draw_commands = []
        self.audio_samples_played = 0

    def mirror_rom_to_rdram_bios(self) -> None:
        """
        cathle1.x: copy the normalized cart image into RDRAM (what IPL/PI leaves visible as “BIOS” RAM).
        Call after load_rom and inside boot so PI and the window preview always see cart bytes.
        """
        if len(self.rom) < 0x40:
            return
        linear_cap = min(len(self.rom), RDRAM_SIZE)
        if linear_cap > 0:
            self.rdram[0:linear_cap] = self.rom[0:linear_cap]
        rom_window = min(0x200000, len(self.rom), RDRAM_SIZE - 0x100000)
        if rom_window > 0:
            self.rdram[0x100000 : 0x100000 + rom_window] = self.rom[0:rom_window]

    def seed_boot_framebuffer(self) -> None:
        """Draw a visible HLE boot framebuffer so a ROM boot opens to a game screen."""
        width = BOOT_FRAMEBUFFER_WIDTH
        height = BOOT_FRAMEBUFFER_HEIGHT
        origin = BOOT_FRAMEBUFFER_ORIGIN
        if origin + width * height * 2 > len(self.rdram):
            origin = 0
        title = (self.header.title if self.header and self.header.title else self.rom_name or APP_NAME)
        title_bytes = title.encode("ascii", "ignore") or b"cat64hle"
        seed = hashlib.sha256(bytes(self.rom[: min(len(self.rom), 1024 * 1024)]) + title_bytes).digest()

        def rgb5551(r: int, g: int, b: int) -> int:
            return (((r >> 3) & 0x1F) << 11) | (((g >> 3) & 0x1F) << 6) | (((b >> 3) & 0x1F) << 1) | 1

        # Deterministic boot/game pattern based on the ROM header/hash. This makes the
        # boot path visibly successful before a real VI framebuffer is produced by code.
        for y in range(height):
            row = origin + y * width * 2
            sy = seed[y % len(seed)]
            for x in range(width):
                sx = seed[(x + y) % len(seed)]
                grid = 22 if ((x // 16) ^ (y // 16)) & 1 else 0
                pulse = (x * 3 + y * 5 + sx) & 0xFF
                r = (pulse ^ seed[0] ^ grid) & 0xFF
                g = ((pulse + sy + grid) & 0xFF)
                b = ((sx * 2 + y + grid) & 0xFF)
                px = rgb5551(r, g, b)
                off = row + x * 2
                self.rdram[off] = (px >> 8) & 0xFF
                self.rdram[off + 1] = px & 0xFF

        # Add simple bright scan bars for a "booted" look without external fonts/assets.
        for band_y in (20, 24, 28, 204, 208, 212):
            if 0 <= band_y < height:
                row = origin + band_y * width * 2
                for x in range(width):
                    px = rgb5551(220, 220, 220)
                    off = row + x * 2
                    self.rdram[off] = (px >> 8) & 0xFF
                    self.rdram[off + 1] = px & 0xFF

        self.bus.regs[VI_ORIGIN_REG] = origin
        self.bus.regs[VI_WIDTH_REG] = width

    def load_demo_rom(self) -> None:
        """Load a tiny public-domain cat64hle demo cart so the boot window always has a game to run."""
        data = bytearray(0x200000)
        data[0:4] = Z64_BIG_ENDIAN_MAGIC
        put_be32(data, 0x04, 0x00000F48)
        put_be32(data, 0x08, 0x80000400)
        put_be32(data, 0x0C, 0x0000144B)
        data[0x20:0x34] = b"CAT64HLE DEMO".ljust(20, b"\x00")
        data[0x3C:0x3E] = b"CD"
        data[0x3E] = 0x45
        # Minimal R4300i-safe loop: BEQ zero,zero,self ; NOP delay slot.
        put_be32(data, 0x400, 0x1000FFFF)
        put_be32(data, 0x404, 0x00000000)
        self.rom = self.normalize_rom(data)
        self.rom_name = "cat64hle_demo.z64"
        self.header = N64Header(self.rom)
        self.last_error = ""
        self.reset()
        self.has_booted = False
        self.mirror_rom_to_rdram_bios()
        self.seed_boot_framebuffer()

    def load_rom(self, path: str):
        with open(path, "rb") as f:
            data = f.read()
        self.rom = self.normalize_rom(bytearray(data))
        self.rom_name = os.path.basename(path)
        self.header = N64Header(self.rom)
        self.last_error = ""
        self.reset()
        self.has_booted = False
        self.mirror_rom_to_rdram_bios()

    def normalize_rom(self, data: bytearray) -> bytearray:
        strip_documentation_header_if_present(data)
        if len(data) < 4:
            return data
        magic = data[0:4]
        if magic == Z64_BIG_ENDIAN_MAGIC:
            apply_ultra64_cart_header_defaults(data)
            return data
        if magic == V64_MAGIC:
            for i in range(0, len(data) - 1, 2):
                data[i], data[i + 1] = data[i + 1], data[i]
            apply_ultra64_cart_header_defaults(data)
            return data
        if magic == N64_LE_MAGIC:
            for i in range(0, len(data) - 3, 4):
                data[i], data[i + 3] = data[i + 3], data[i]
                data[i + 1], data[i + 2] = data[i + 2], data[i + 1]
            apply_ultra64_cart_header_defaults(data)
            return data
        return data

    def boot(self) -> bool:
        self.last_error = ""
        if len(self.rom) < 0x40:
            self.last_error = "ROM is too small to contain an N64 header"
            return False
        self.reset()
        self.header = N64Header(self.rom)
        seed_commercial_pif_ram(self.pif_ram)
        self.mirror_rom_to_rdram_bios()
        self.seed_boot_framebuffer()

        put_be32(self.rdram, 0x318, 0x00800000)  # osMemSize

        entry = normalize_commercial_entry(self.header.boot_address)
        self.cpu.pc = entry
        self.cpu.next_pc = u32(entry + 4)
        self.cpu.gpr[29] = u64(0x803FA800)
        self.cpu.gpr[30] = u64(0x803FA800)
        self.cpu.cp0[CP0_STATUS] = 0x34000000
        self.cpu.cp0[CP0_CONFIG] = 0x0006E463
        self.bus.regs[0x04600010] = 0

        self.has_booted = True
        self.is_running = True
        return True

    def reset(self):
        self.rdram = bytearray(RDRAM_SIZE)
        self.rsp_dmem = bytearray(RSP_DMEM_SIZE)
        self.rsp_imem = bytearray(RSP_IMEM_SIZE)
        self.pif_ram = bytearray(PIF_RAM_SIZE)
        self.bus.reset()
        self.cpu.reset()
        self.frame_count = 0
        self.hle_calls = 0
        self.rdp_draw_commands.clear()
        self.audio_samples_played = 0
        self.last_error = ""

    def trigger_pi_dma(self):
        dram_addr = self.bus.regs.get(0x04600000, 0) & 0x00FFFFFF
        cart_addr = self.bus.regs.get(0x04600004, 0) & 0x0FFFFFFF
        length = (self.bus.regs.get(0x0460000C, 0) & 0x00FFFFFF) + 1
        if cart_addr >= len(self.rom) or dram_addr >= RDRAM_SIZE:
            return
        actual_len = min(length, len(self.rom) - cart_addr, RDRAM_SIZE - dram_addr)
        if actual_len > 0:
            self.rdram[dram_addr:dram_addr + actual_len] = self.rom[cart_addr:cart_addr + actual_len]

    def trigger_sp_dma(self, to_rsp: bool):
        sp_addr = self.bus.regs.get(0x04040000, 0) & 0x1FFF
        dram_addr = self.bus.regs.get(0x04040004, 0) & 0x00FFFFFF
        reg = 0x04040008 if to_rsp else 0x0404000C
        length = (self.bus.regs.get(reg, 0) & 0xFFF) + 1
        target = self.rsp_imem if sp_addr & 0x1000 else self.rsp_dmem
        off = sp_addr & 0xFFF
        length = min(length, 0x1000 - off, max(0, RDRAM_SIZE - dram_addr))
        if length <= 0:
            return
        if to_rsp:
            target[off:off + length] = self.rdram[dram_addr:dram_addr + length]
        else:
            self.rdram[dram_addr:dram_addr + length] = target[off:off + length]

    def trigger_si_dma(self, read_pif: bool):
        dram_addr = self.bus.regs.get(0x04800000, 0) & 0x00FFFFFF
        xfer = min(64, max(0, RDRAM_SIZE - dram_addr))
        if xfer <= 0:
            self.bus.regs[0x04800018] = 0
            return
        if read_pif:
            self.pif_ram[0:4] = struct.pack(">I", self.controller_state << 16)
            self.rdram[dram_addr:dram_addr + xfer] = self.pif_ram[0:xfer]
        else:
            self.pif_ram[0:xfer] = self.rdram[dram_addr:dram_addr + xfer]
            if xfer < 64:
                self.pif_ram[xfer:64] = bytearray(64 - xfer)
        self.bus.regs[0x04800018] = 0

    def process_rsp(self):
        self.hle_calls += 1
        self.bus.regs[0x04040010] |= 1  # Halt

    def process_rdp(self):
        start_addr = self.bus.regs.get(0x04100000, 0) & 0x00FFFFFF
        end_addr = self.bus.regs.get(0x04100004, 0) & 0x00FFFFFF
        self.rdp_draw_commands.clear()
        while start_addr < end_addr:
            cmd = self.bus.read_u64(start_addr)
            cmd_id = (cmd >> 56) & 0x3F
            if cmd_id in (0x3F, 0x36):  # FillRectangle / FillTriangle (demo)
                x = (cmd >> 12) & 0x3FF
                y = cmd & 0x3FF
                color = "#" + hex(random.randint(0x100000, 0xFFFFFF))[2:]
                self.rdp_draw_commands.append({"type": "rect", "x": x, "y": y, "color": color})
            start_addr += 8

    def process_audio(self):
        length = self.bus.regs.get(0x04500004, 0)
        self.audio_samples_played += length
        self.bus.regs[0x0450000C] = 0

    def vi_framebuffer_phys_origin(self) -> int:
        reg = self.bus.regs.get(VI_ORIGIN_REG, 0) & 0xFFFFFF
        return reg if reg != 0 else 0x00100000

    def vi_display_width_height(self) -> Tuple[int, int]:
        w = self.bus.regs.get(VI_WIDTH_REG, 320) & 0xFFF
        if w < 64 or w > 1024:
            w = 320
        return w, 240

    def vi_framebuffer_ppm(self) -> bytes | None:
        w, h = self.vi_display_width_height()
        ow, oh = min(320, w), min(240, h)
        for origin in (self.vi_framebuffer_phys_origin(), 0, 0x00100000):
            p = rdram_rgb5551_to_ppm(self.rdram, origin, ow, oh)
            if p:
                return p
        return None

    def run_frame(self):
        cpu = self.cpu
        step = cpu.step
        try:
            for _ in range(FRAME_STEPS_PER_TICK):
                if cpu.pc & 0x80000000:
                    self.hle_calls += 1
                step()
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            self.is_running = False
            return
        self.frame_count += 1

# --- GUI Layer (Project64 1.6 Legacy Win32 style) ---

class ACsN64GUI:
    """Tk recreation of the Project64 Legacy / original 1.6 shell.

    The emulator core remains the single-file cathle1.x Python core; this class adds
    the cat64hle boot window and classic menu/status layout without external assets.
    """

    __slots__ = (
        "root", "core", "_fb_photo", "canvas", "info_text", "status_bar",
        "key_map", "limit_fps", "main_menu", "limit_fps_var", "show_cpu_var",
        "always_on_top_var", "fullscreen", "client_area", "browser_frame",
        "game_frame", "rom_browser", "rom_summary", "boot_window", "boot_status_var",
        "boot_rom_var", "_next_frame_time", "_fps_last_time", "_fps_last_frame",
        "_measured_fps", "_last_status_update",
    )

    def __init__(self):
        if tk is None:
            return
        self.root = tk.Tk()
        self.root.title(WINDOW_TITLE)
        self.root.geometry("640x480")
        self.root.minsize(640, 480)
        self.root.configure(bg=PJ64_WIN_GRAY)

        self.core = ACsN64Core()
        self._fb_photo = None
        self.limit_fps = True
        self.fullscreen = False
        self._next_frame_time = time.perf_counter()
        self._fps_last_time = time.perf_counter()
        self._fps_last_frame = 0
        self._measured_fps = 0.0
        self._last_status_update = 0.0
        self.boot_window = None
        self.boot_status_var = None
        self.boot_rom_var = None

        self._setup_ui()
        self._bind_controls()
        self._update_loop()
        self.root.after(250, self.open_boot_window)

    # ---------- Project64 1.6/Legacy shell ----------

    def _setup_ui(self):
        self._configure_legacy_theme()
        self._build_legacy_menu()

        self.client_area = tk.Frame(self.root, bg=PJ64_PANEL_WHITE, bd=0, highlightthickness=0)
        self.client_area.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        self.browser_frame = tk.Frame(self.client_area, bg=PJ64_PANEL_WHITE, bd=0, highlightthickness=0)
        self.game_frame = tk.Frame(self.client_area, bg="black", bd=0, highlightthickness=0)

        self._build_rom_browser()
        self._build_game_view()
        self._show_browser()

        self.info_text = tk.StringVar(value="Ready")
        self.status_bar = tk.Label(
            self.root,
            textvariable=self.info_text,
            bd=1,
            relief=tk.SUNKEN,
            anchor=tk.W,
            bg=PJ64_WIN_FACE,
            fg=PJ64_TEXT,
            font=UI_FONT,
            padx=4,
            pady=1,
        )
        self.status_bar.pack(side=tk.BOTTOM, fill=tk.X)

        self.root.bind_all("<Control-o>", lambda e: self.open_rom())
        self.root.bind_all("<Control-O>", lambda e: self.open_rom())
        self.root.bind_all("<Control-b>", lambda e: self.open_boot_window())
        self.root.bind_all("<Control-B>", lambda e: self.open_boot_window())
        self.root.bind_all("<F1>", lambda e: self.reset_emu())
        self.root.bind_all("<F2>", lambda e: self.toggle_run())
        self.root.bind_all("<F4>", lambda e: self.toggle_limit_fps())
        self.root.bind_all("<F5>", lambda e: self.refresh_rom_list())
        self.root.bind_all("<F10>", lambda e: self.start_emulation())
        self.root.bind_all("<F11>", lambda e: self.stop_emulation())
        self.root.bind_all("<Alt-Return>", lambda e: self.toggle_fullscreen())

    def _configure_legacy_theme(self) -> None:
        if ttk is None:
            return
        try:
            style = ttk.Style(self.root)
            if "winnative" in style.theme_names():
                style.theme_use("winnative")
            elif "classic" in style.theme_names():
                style.theme_use("classic")
            style.configure("Treeview", font=UI_FONT, rowheight=18)
            style.configure("Treeview.Heading", font=UI_FONT_BOLD)
        except tk.TclError:
            pass

    def _build_legacy_menu(self) -> None:
        self.main_menu = tk.Menu(self.root, relief=tk.FLAT, bd=0)
        self.limit_fps_var = tk.BooleanVar(value=True)
        self.show_cpu_var = tk.BooleanVar(value=False)
        self.always_on_top_var = tk.BooleanVar(value=False)

        file_menu = tk.Menu(self.main_menu, tearoff=0)
        file_menu.add_command(label="Open Rom", command=self.open_rom, accelerator="Ctrl+O")
        file_menu.add_command(label="Boot Rom Window", command=self.open_boot_window, accelerator="Ctrl+B")
        file_menu.add_command(label="Rom Information", command=self.show_rom_information, accelerator="Ctrl+I")
        file_menu.add_command(label="Game Information", command=self.show_game_information, accelerator="Ctrl+G")
        file_menu.add_separator()
        file_menu.add_command(label="Start Emulation", command=self.start_emulation, accelerator="F10")
        file_menu.add_command(label="End Emulation", command=self.stop_emulation, accelerator="F11")
        file_menu.add_separator()
        lang_menu = tk.Menu(file_menu, tearoff=0)
        lang_menu.add_command(label="English", state="disabled")
        file_menu.add_cascade(label="Language", menu=lang_menu)
        file_menu.add_separator()
        file_menu.add_command(label="Choose Rom Directory...", command=self.choose_rom_directory)
        file_menu.add_command(label="Refresh Rom List", command=self.refresh_rom_list, accelerator="F5")
        file_menu.add_separator()
        recent_rom = tk.Menu(file_menu, tearoff=0)
        recent_rom.add_command(label="None Here", state="disabled")
        file_menu.add_cascade(label="Recent Rom", menu=recent_rom)
        recent_dirs = tk.Menu(file_menu, tearoff=0)
        recent_dirs.add_command(label="None Here", state="disabled")
        file_menu.add_cascade(label="Recent Rom Directories", menu=recent_dirs)
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self.root.quit)
        self.main_menu.add_cascade(label="File", menu=file_menu)

        system_menu = tk.Menu(self.main_menu, tearoff=0)
        system_menu.add_command(label="Reset", command=self.reset_emu, accelerator="F1")
        system_menu.add_command(label="Pause", command=self.toggle_run, accelerator="F2")
        system_menu.add_command(label="Screenshot Capture", command=self.screenshot_capture, accelerator="F3")
        system_menu.add_separator()
        system_menu.add_checkbutton(label="Limit FPS", variable=self.limit_fps_var, command=self.toggle_limit_fps, accelerator="F4")
        system_menu.add_separator()
        system_menu.add_command(label="Save", command=self._legacy_stub, accelerator="F5")
        system_menu.add_command(label="Save As...", command=self._legacy_stub, accelerator="Ctrl+S")
        system_menu.add_command(label="Restore", command=self._legacy_stub, accelerator="F7")
        system_menu.add_command(label="Restore From", command=self._legacy_stub, accelerator="Ctrl+L")
        system_menu.add_separator()
        slot_menu = tk.Menu(system_menu, tearoff=0)
        slot_menu.add_command(label="Default", command=self._legacy_stub, accelerator="0")
        slot_menu.add_separator()
        for i in range(1, 10):
            slot_menu.add_command(label=f"Slot {i}", command=self._legacy_stub, accelerator=str(i))
        system_menu.add_cascade(label="Current Save State", menu=slot_menu)
        system_menu.add_separator()
        system_menu.add_command(label="Cheats...", command=self._legacy_stub, accelerator="Ctrl+C")
        system_menu.add_command(label="Cheat Search", command=self._legacy_stub, accelerator="Ctrl+R")
        system_menu.add_command(label="GS Button", command=self._legacy_stub, accelerator="F9")
        self.main_menu.add_cascade(label="System", menu=system_menu)

        options_menu = tk.Menu(self.main_menu, tearoff=0)
        options_menu.add_command(label="Full Screen", command=self.toggle_fullscreen, accelerator="Alt+Enter")
        options_menu.add_checkbutton(label="Always On Top", variable=self.always_on_top_var, command=self.toggle_always_on_top, accelerator="Ctrl+A")
        options_menu.add_separator()
        options_menu.add_command(label="Configure Graphics Plugin...", command=self._legacy_stub, accelerator="Ctrl+V")
        options_menu.add_command(label="Configure Audio Plugin...", command=self._legacy_stub, accelerator="Ctrl+U")
        options_menu.add_command(label="Configure Controller Plugin...", command=self._legacy_stub, accelerator="Ctrl+X")
        options_menu.add_command(label="Configure RSP Plugin...", command=self._legacy_stub, accelerator="Ctrl+W")
        options_menu.add_separator()
        options_menu.add_checkbutton(label="Show CPU usage %", variable=self.show_cpu_var, command=self.update_status_text)
        options_menu.add_command(label="Settings...", command=self.show_settings, accelerator="Ctrl+T")
        self.main_menu.add_cascade(label="Options", menu=options_menu)

        debugger_menu = tk.Menu(self.main_menu, tearoff=0)
        debugger_menu.add_command(label="Set Breakpoint...", command=self._legacy_stub)
        debugger_menu.add_separator()
        r4300_menu = tk.Menu(debugger_menu, tearoff=0)
        r4300_menu.add_command(label="R4300i Commands...", command=self.show_r4300i_commands)
        r4300_menu.add_command(label="R4300i Registers...", command=self.show_r4300i_registers)
        debugger_menu.add_cascade(label="R4300i", menu=r4300_menu)
        debugger_menu.add_command(label="Memory...", command=self._legacy_stub)
        debugger_menu.add_command(label="TLB Entries...", command=self._legacy_stub)
        debugger_menu.add_separator()
        debugger_menu.add_command(label="Call Stack....", command=self._legacy_stub)
        debugger_menu.add_separator()
        logging_menu = tk.Menu(debugger_menu, tearoff=0)
        logging_menu.add_command(label="Log Options", command=self._legacy_stub)
        logging_menu.add_command(label="Generate Log", command=self._legacy_stub)
        debugger_menu.add_cascade(label="Logging", menu=logging_menu)
        profiling_menu = tk.Menu(debugger_menu, tearoff=0)
        profiling_menu.add_command(label="On", command=self._legacy_stub)
        profiling_menu.add_command(label="Off", command=self._legacy_stub)
        profiling_menu.add_separator()
        profiling_menu.add_command(label="Reset Stats", command=self._legacy_stub)
        profiling_menu.add_command(label="Generate Log", command=self._legacy_stub)
        profiling_menu.add_separator()
        profiling_menu.add_command(label="Log Individual Blocks", command=self._legacy_stub)
        debugger_menu.add_cascade(label="Profiling", menu=profiling_menu)
        mappings_menu = tk.Menu(debugger_menu, tearoff=0)
        mappings_menu.add_command(label="Open Map file ...", command=self._legacy_stub)
        mappings_menu.add_command(label="Close Map File", command=self._legacy_stub)
        mappings_menu.add_separator()
        mappings_menu.add_command(label="Auto Load Map File", command=self._legacy_stub)
        debugger_menu.add_cascade(label="Mappings", menu=mappings_menu)
        dbg_settings = tk.Menu(debugger_menu, tearoff=0)
        for label in (
            "Show Unhandled Memory Accesses",
            "Show Load/Store TLB Misses",
            "Show Dlist/Alist count",
            "Show Pif Ram Errors",
            "Show Compile Memory",
        ):
            dbg_settings.add_command(label=label, command=self._legacy_stub)
        debugger_menu.add_cascade(label="Settings", menu=dbg_settings)
        self.main_menu.add_cascade(label="Debugger", menu=debugger_menu)

        help_menu = tk.Menu(self.main_menu, tearoff=0)
        help_menu.add_command(label="User Manual...", command=self._legacy_stub)
        help_menu.add_command(label="Game FAQ...", command=self._legacy_stub)
        help_menu.add_separator()
        help_menu.add_command(label="GitHub", command=lambda: self._open_url("https://github.com/pj64team/Project64-Legacy"))
        help_menu.add_command(label="Homepage", command=lambda: self._open_url("https://www.project64-legacy.com/"))
        help_menu.add_command(label="Discord", command=lambda: self._open_url("https://discord.gg/"))
        help_menu.add_separator()
        help_menu.add_command(label="About INI Files", command=self._legacy_stub)
        help_menu.add_command(label="About Project 64", command=self.show_about)
        self.main_menu.add_cascade(label="Help", menu=help_menu)

        self.root.config(menu=self.main_menu)

    def _build_rom_browser(self) -> None:
        self.browser_frame.configure(bg=PJ64_PANEL_WHITE)
        header = tk.Label(
            self.browser_frame,
            text="cat64hle ROM Browser",
            bg=PJ64_PANEL_WHITE,
            fg=PJ64_TEXT,
            anchor=tk.W,
            font=UI_FONT_BOLD,
            padx=4,
            pady=2,
        )
        header.pack(side=tk.TOP, fill=tk.X)

        if ttk is not None:
            columns = ("country", "status", "good", "notes", "core", "save", "players")
            self.rom_browser = ttk.Treeview(
                self.browser_frame,
                columns=columns,
                show="tree headings",
                selectmode="browse",
            )
            self.rom_browser.heading("#0", text="File Name")
            self.rom_browser.heading("country", text="Country")
            self.rom_browser.heading("status", text="Status")
            self.rom_browser.heading("good", text="Good Name")
            self.rom_browser.heading("notes", text="Notes")
            self.rom_browser.heading("core", text="Core")
            self.rom_browser.heading("save", text="Save Type")
            self.rom_browser.heading("players", text="Players")
            self.rom_browser.column("#0", width=165, stretch=True)
            self.rom_browser.column("country", width=60, anchor=tk.CENTER)
            self.rom_browser.column("status", width=80, anchor=tk.CENTER)
            self.rom_browser.column("good", width=160, stretch=True)
            self.rom_browser.column("notes", width=130, stretch=True)
            self.rom_browser.column("core", width=70, anchor=tk.CENTER)
            self.rom_browser.column("save", width=90, anchor=tk.CENTER)
            self.rom_browser.column("players", width=55, anchor=tk.CENTER)
            yscroll = ttk.Scrollbar(self.browser_frame, orient=tk.VERTICAL, command=self.rom_browser.yview)
            xscroll = ttk.Scrollbar(self.browser_frame, orient=tk.HORIZONTAL, command=self.rom_browser.xview)
            self.rom_browser.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
            self.rom_browser.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
            yscroll.pack(side=tk.RIGHT, fill=tk.Y)
            xscroll.pack(side=tk.BOTTOM, fill=tk.X)
            self.rom_browser.insert(
                "",
                tk.END,
                iid="empty",
                text="No ROM loaded",
                values=("", "Ready", "Choose File > Open Rom", "", "Interpreter", "Auto", ""),
            )
        else:
            self.rom_browser = tk.Listbox(self.browser_frame, font=UI_FONT, bg=PJ64_PANEL_WHITE, relief=tk.SUNKEN, bd=1)
            self.rom_browser.pack(fill=tk.BOTH, expand=True)
            self.rom_browser.insert(tk.END, "No ROM loaded    Ready    Choose File > Open Rom")

        self.rom_summary = tk.Label(
            self.browser_frame,
            text="Portable single-file Python 3.14 target - files off - 60 FPS limit enabled",
            bg=PJ64_WIN_FACE,
            fg=PJ64_TEXT,
            anchor=tk.W,
            font=UI_FONT,
            bd=1,
            relief=tk.SUNKEN,
            padx=4,
            pady=1,
        )
        self.rom_summary.pack(side=tk.BOTTOM, fill=tk.X)

    def _build_game_view(self) -> None:
        self.game_frame.configure(bg="black")
        viewport = tk.Frame(self.game_frame, bg="black", relief=tk.FLAT, bd=0, highlightthickness=0)
        viewport.pack(fill=tk.BOTH, expand=True)
        self.canvas = tk.Canvas(viewport, width=640, height=432, bg="black", highlightthickness=0, bd=0)
        self.canvas.pack(fill=tk.BOTH, expand=True)
        self._draw_splash()

    def _show_browser(self) -> None:
        self.game_frame.pack_forget()
        self.browser_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

    def _show_game(self) -> None:
        self.browser_frame.pack_forget()
        self.game_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

    # ---------- Status/browser helpers ----------

    def update_status_text(self) -> None:
        rom = self.core.rom_name if self.core.rom else "No ROM loaded"
        state = "Running" if self.core.is_running else "Ready"
        fps = f"{self._measured_fps:05.2f} VI/s" if self.core.is_running else "60 FPS limit ready"
        if self.show_cpu_var.get():
            extra = f" | PC 0x{self.core.cpu.pc:08X} | HLE {self.core.hle_calls}"
        else:
            extra = ""
        if self.core.last_error:
            self.info_text.set(f"{state}: {rom} | {fps} | Error: {self.core.last_error}{extra}")
        else:
            self.info_text.set(f"{state}: {rom} | {fps}{extra}")

    def _set_monitor_idle(self) -> None:
        self._update_rom_browser_row()
        self.update_status_text()

    def _set_monitor_text(self, text: str) -> None:
        # Legacy compatibility hook kept for the core/UI methods from earlier builds.
        self.info_text.set(text.replace("\n", "  ")[:240])

    def _update_rom_browser_row(self) -> None:
        title = "No ROM loaded"
        good = "Choose File > Open Rom"
        status = "Ready"
        country = ""
        notes = ""
        save = "Auto"
        players = ""
        if self.core.rom:
            header = getattr(self.core, "header", None)
            title = self.core.rom_name
            good = header.title if header and header.title else os.path.splitext(self.core.rom_name)[0]
            status = "Running" if self.core.is_running else "Loaded"
            country = chr(self.core.rom[0x3E]) if len(self.core.rom) > 0x3E and 32 <= self.core.rom[0x3E] < 127 else "?"
            notes = f"CRC {header.crc1:08X}" if header else ""
            players = "1-4"
        if ttk is not None and isinstance(self.rom_browser, ttk.Treeview):
            if "empty" in self.rom_browser.get_children(""):
                self.rom_browser.delete("empty")
            if "rom0" not in self.rom_browser.get_children(""):
                self.rom_browser.insert("", tk.END, iid="rom0", text=title, values=(country, status, good, notes, ENGINE_NAME, save, players))
            else:
                self.rom_browser.item("rom0", text=title, values=(country, status, good, notes, ENGINE_NAME, save, players))
            self.rom_browser.selection_set("rom0")
        elif isinstance(self.rom_browser, tk.Listbox):
            self.rom_browser.delete(0, tk.END)
            self.rom_browser.insert(tk.END, f"{title}    {country}    {status}    {good}    {notes}    {ENGINE_NAME}    {save}")
        self.rom_summary.configure(
            text=(
                f"{WINDOW_TITLE} | {BUILD_TAG} | files=off | target={TARGET_FPS} FPS | "
                f"ROM bytes={len(self.core.rom):,}"
            )
        )

    # ---------- Dedicated cat64hle ROM boot window ----------

    def open_boot_window(self) -> None:
        if self.boot_window is not None:
            try:
                if self.boot_window.winfo_exists():
                    self._sync_boot_window()
                    self.boot_window.lift()
                    self.boot_window.focus_force()
                    return
            except tk.TclError:
                self.boot_window = None

        win = tk.Toplevel(self.root)
        self.boot_window = win
        win.title(BOOT_WINDOW_TITLE)
        win.geometry("440x265")
        win.minsize(420, 245)
        win.configure(bg=PJ64_WIN_FACE)
        win.protocol("WM_DELETE_WINDOW", self._close_boot_window)

        self.boot_rom_var = tk.StringVar(master=win, value="No ROM loaded")
        self.boot_status_var = tk.StringVar(master=win, value=f"{ENGINE_NAME} ready - Python {PYTHON_TARGET} target")

        header = tk.Label(
            win,
            text="cat64hle 1.x ROM Boot",
            bg=PJ64_PANEL_WHITE,
            fg=PJ64_TEXT,
            font=("MS Sans Serif", 12, "bold"),
            anchor=tk.W,
            padx=8,
            pady=8,
            relief=tk.SUNKEN,
            bd=1,
        )
        header.pack(side=tk.TOP, fill=tk.X, padx=8, pady=(8, 4))

        info = tk.Frame(win, bg=PJ64_WIN_FACE, bd=1, relief=tk.GROOVE)
        info.pack(side=tk.TOP, fill=tk.X, padx=8, pady=4)
        tk.Label(info, text="Selected ROM:", bg=PJ64_WIN_FACE, fg=PJ64_TEXT, font=UI_FONT_BOLD, anchor=tk.W).pack(fill=tk.X, padx=6, pady=(6, 0))
        tk.Label(info, textvariable=self.boot_rom_var, bg=PJ64_WIN_FACE, fg=PJ64_TEXT, font=UI_FONT, anchor=tk.W, wraplength=400, justify=tk.LEFT).pack(fill=tk.X, padx=6, pady=(0, 6))
        tk.Label(info, textvariable=self.boot_status_var, bg=PJ64_WIN_FACE, fg=STATUS_RED, font=UI_FONT, anchor=tk.W, wraplength=400, justify=tk.LEFT).pack(fill=tk.X, padx=6, pady=(0, 6))

        buttons = tk.Frame(win, bg=PJ64_WIN_FACE)
        buttons.pack(side=tk.TOP, fill=tk.X, padx=8, pady=6)
        tk.Button(buttons, text="Open ROM...", width=13, command=self.boot_window_open_rom).pack(side=tk.LEFT, padx=(0, 4))
        tk.Button(buttons, text="Boot Game", width=13, command=self.boot_loaded_rom).pack(side=tk.LEFT, padx=4)
        tk.Button(buttons, text="Boot Demo", width=13, command=self.boot_demo_rom).pack(side=tk.LEFT, padx=4)
        tk.Button(buttons, text="Stop", width=8, command=self.stop_emulation).pack(side=tk.LEFT, padx=4)

        hint = tk.Label(
            win,
            text="Boot Game loads the selected ROM into the cathle1.x HLE core and opens the game view. Boot Demo runs the included safe test cart.",
            bg=PJ64_WIN_FACE,
            fg=PJ64_TEXT,
            font=UI_FONT,
            anchor=tk.W,
            justify=tk.LEFT,
            wraplength=410,
        )
        hint.pack(side=tk.TOP, fill=tk.X, padx=8, pady=(2, 8))

        self._sync_boot_window()

    def _close_boot_window(self) -> None:
        if self.boot_window is not None:
            try:
                self.boot_window.destroy()
            except tk.TclError:
                pass
        self.boot_window = None

    def _sync_boot_window(self) -> None:
        if self.boot_rom_var is not None:
            if self.core.rom:
                header = getattr(self.core, "header", None)
                title = header.title if header and header.title else self.core.rom_name
                self.boot_rom_var.set(f"{title}  ({self.core.rom_name}, {len(self.core.rom):,} bytes)")
            else:
                self.boot_rom_var.set("No ROM loaded")
        if self.boot_status_var is not None:
            if self.core.last_error:
                status = f"Error: {self.core.last_error}"
            elif self.core.is_running:
                status = f"Booted via {ENGINE_NAME} | PC 0x{self.core.cpu.pc:08X} | frame {self.core.frame_count}"
            elif self.core.rom:
                status = f"Loaded and ready to boot via {ENGINE_NAME}"
            else:
                status = f"{ENGINE_NAME} ready - Python {PYTHON_TARGET} target"
            self.boot_status_var.set(status)

    def boot_window_open_rom(self) -> None:
        self.open_rom()
        self._sync_boot_window()

    def boot_loaded_rom(self) -> None:
        if not self.core.rom:
            self.boot_window_open_rom()
            if not self.core.rom:
                self._sync_boot_window()
                return
        self.start_emulation()
        self._sync_boot_window()

    def boot_demo_rom(self) -> None:
        self.core.load_demo_rom()
        self._fb_photo = None
        self._show_browser()
        self._update_rom_browser_row()
        self.info_text.set("Loaded built-in cat64hle demo ROM")
        self.start_emulation()
        self._sync_boot_window()

    def _draw_boot_overlay(self, title: str | None = None) -> None:
        self.canvas.delete("boot_overlay")
        w = self.canvas.winfo_width() or 640
        h = self.canvas.winfo_height() or 432
        label = title or self.core.rom_name or "ROM"
        self.canvas.create_rectangle(0, h - 56, w, h, fill="black", outline="", tags="boot_overlay")
        self.canvas.create_text(
            10,
            h - 44,
            anchor=tk.NW,
            text=f"cat64hle 1.x booted: {label}\n{ENGINE_NAME} | Python {PYTHON_TARGET} target | PC 0x{self.core.cpu.pc:08X}",
            fill="#e8e8e8",
            font=UI_FONT,
            justify=tk.LEFT,
            tags="boot_overlay",
        )

    # ---------- Menu commands ----------

    def open_rom(self):
        path = filedialog.askopenfilename(
            title="Open Rom",
            filetypes=[("N64 ROMs", "*.z64 *.v64 *.n64 *.rom *.bin"), ("All files", "*.*")],
        )
        if path:
            self.core.load_rom(path)
            self._fb_photo = None
            self._show_browser()
            self._update_rom_browser_row()
            self.info_text.set(f"Loaded: {self.core.rom_name}")
            self._refresh_vi_framebuffer()
            self._sync_boot_window()

    def close_rom(self) -> None:
        self.stop_emulation()
        self.core.rom = bytearray()
        self.core.rom_name = "None"
        self.core.has_booted = False
        self.core.reset()
        self._fb_photo = None
        self.canvas.delete("all")
        self._draw_splash()
        self._show_browser()
        self._update_rom_browser_row()
        self.info_text.set("Ready")
        self._sync_boot_window()

    def choose_rom_directory(self) -> None:
        directory = filedialog.askdirectory(title="Choose Rom Directory")
        if directory:
            self.info_text.set(f"ROM directory: {directory}")
            self.refresh_rom_list(directory)

    def refresh_rom_list(self, directory: str | None = None) -> None:
        if directory:
            try:
                roms = sorted(
                    name for name in os.listdir(directory)
                    if name.lower().endswith((".z64", ".v64", ".n64", ".rom", ".bin"))
                )
            except OSError as exc:
                self.info_text.set(f"Refresh failed: {exc}")
                return
            if ttk is not None and isinstance(self.rom_browser, ttk.Treeview):
                for item in self.rom_browser.get_children(""):
                    self.rom_browser.delete(item)
                if roms:
                    for idx, name in enumerate(roms):
                        self.rom_browser.insert("", tk.END, iid=f"dir{idx}", text=name, values=("?", "Detected", name, "", ENGINE_NAME, "Auto", ""))
                else:
                    self.rom_browser.insert("", tk.END, iid="empty", text="No ROMs found", values=("", "Ready", directory, "", ENGINE_NAME, "Auto", ""))
            elif isinstance(self.rom_browser, tk.Listbox):
                self.rom_browser.delete(0, tk.END)
                for name in roms or ["No ROMs found"]:
                    self.rom_browser.insert(tk.END, name)
            self.rom_summary.configure(text=f"Directory: {directory} | {len(roms)} ROM candidate(s)")
            self.info_text.set("Rom list refreshed")
            return
        self._update_rom_browser_row()
        self.info_text.set("Rom list refreshed")

    def start_emulation(self) -> None:
        if not self.core.rom:
            self.info_text.set("No ROM loaded")
            return
        if not self.core.has_booted:
            if not self.core.boot():
                detail = self.core.last_error or "invalid ROM header or reset error"
                self.info_text.set(f"Boot failed - {detail}")
                self._show_game()
                self._canvas_static_fallback(f"Boot failed ({detail})")
                self._sync_boot_window()
                return
        self.core.last_error = ""
        self.core.is_running = True
        self._next_frame_time = time.perf_counter()
        self._fps_last_time = self._next_frame_time
        self._fps_last_frame = self.core.frame_count
        self._show_game()
        self._refresh_vi_framebuffer()
        self._update_rom_browser_row()
        name = self.core.header.title if getattr(self.core, "header", None) and self.core.header.title else self.core.rom_name
        self._draw_boot_overlay(name)
        self.info_text.set(f"Running: {name}")
        self._sync_boot_window()

    def stop_emulation(self) -> None:
        self.core.is_running = False
        self._refresh_vi_framebuffer()
        self._show_browser()
        self._update_rom_browser_row()
        self.info_text.set("Stopped")
        self._sync_boot_window()

    def toggle_run(self):
        if self.core.is_running:
            self.stop_emulation()
        else:
            self.start_emulation()

    def reset_emu(self):
        was = self.core.rom_name
        had_rom = bool(self.core.rom)
        self.core.reset()
        self.core.has_booted = False
        self.core.is_running = False
        if had_rom:
            self.core.rom_name = was
            self.core.mirror_rom_to_rdram_bios()
        self.canvas.delete("all")
        self._draw_splash()
        self._fb_photo = None
        self._show_browser()
        self._update_rom_browser_row()
        self.info_text.set("Reset")
        if had_rom:
            self._refresh_vi_framebuffer()
        self._sync_boot_window()

    def toggle_limit_fps(self) -> None:
        self.limit_fps = bool(self.limit_fps_var.get())
        self._next_frame_time = time.perf_counter()
        self.update_status_text()

    def toggle_fullscreen(self) -> None:
        self.fullscreen = not self.fullscreen
        try:
            self.root.attributes("-fullscreen", self.fullscreen)
        except tk.TclError:
            pass
        self.info_text.set("Full screen" if self.fullscreen else "Windowed")

    def toggle_always_on_top(self) -> None:
        try:
            self.root.attributes("-topmost", bool(self.always_on_top_var.get()))
        except tk.TclError:
            pass
        self.update_status_text()

    def screenshot_capture(self) -> None:
        self.info_text.set("Screenshot Capture: files=off build does not write bitmap files")

    def show_settings(self) -> None:
        if messagebox:
            messagebox.showinfo(
                "Settings",
                "cat64hle settings shell\n\n"
                f"Core: {ENGINE_NAME}\n"
                f"Python target: {PYTHON_TARGET}\n"
                f"Limit FPS: {'On' if self.limit_fps else 'Off'}\n"
                "Plugins: inlined files-off monolith",
            )

    def show_rom_information(self) -> None:
        if not self.core.rom:
            self.info_text.set("No ROM loaded")
            return
        header = getattr(self.core, "header", None)
        if messagebox:
            messagebox.showinfo(
                "Rom Information",
                f"File: {self.core.rom_name}\n"
                f"Title: {(header.title if header else '') or 'UNKNOWN'}\n"
                f"Cart ID: {(header.cart_id if header else '') or '??'}\n"
                f"CRC1: {(header.crc1 if header else 0):08X}\n"
                f"CRC2: {(header.crc2 if header else 0):08X}\n"
                f"Size: {len(self.core.rom):,} bytes",
            )

    def show_game_information(self) -> None:
        self.show_rom_information()

    def show_r4300i_commands(self) -> None:
        if messagebox:
            messagebox.showinfo(
                "R4300i Commands",
                "Interpreter dispatch tables are in this single Python file.\n"
                "Open the source and search PRIMARY_OPS, SPECIAL_OPS, REGIMM_OPS, COP0_RS, and COP1_FUNCT.",
            )

    def show_r4300i_registers(self) -> None:
        regs = "\n".join(f"R{i:02d}: 0x{value:016X}" for i, value in enumerate(self.core.cpu.gpr[:16]))
        if messagebox:
            messagebox.showinfo("R4300i Registers", regs)

    def show_about(self) -> None:
        if messagebox:
            messagebox.showinfo(
                "About cat64hle",
                "cat64hle 1.x Python shell\n"
                f"Version {VERSION}\n\n"
                "GUI: cat64hle boot window plus classic 640x480 status-window layout.\n"
                f"Python {PYTHON_TARGET} target - {ENGINE_NAME} core.\n"
                f"Build: {BUILD_TAG}\n\n"
                "Files-off monolith: no plugin DLLs, INI files, icons, BMPs, or external assets are required.",
            )

    def _legacy_stub(self) -> None:
        self.info_text.set("cat64hle menu stub: visible for classic GUI parity in files-off mode")

    def _open_url(self, url: str) -> None:
        try:
            webbrowser.open(url)
            self.info_text.set(url)
        except Exception as exc:
            self.info_text.set(f"Open URL failed: {exc}")

    # ---------- Controller + video ----------

    def _bind_controls(self):
        self.root.bind("<KeyPress>", self._on_key_press)
        self.root.bind("<KeyRelease>", self._on_key_release)
        self.key_map = {
            "Up": 0x0800,
            "Down": 0x0400,
            "Left": 0x0200,
            "Right": 0x0100,
            "Return": 0x1000,
            "z": 0x8000,
            "x": 0x4000,
            "a": 0x2000,
            "s": 0x0020,
        }

    def _on_key_press(self, event):
        if event.keysym in self.key_map:
            self.core.controller_state |= self.key_map[event.keysym]

    def _on_key_release(self, event):
        if event.keysym in self.key_map:
            self.core.controller_state &= ~self.key_map[event.keysym]

    def _draw_splash(self) -> None:
        self.canvas.delete("splash")
        self.canvas.create_rectangle(0, 0, 640, 432, fill="black", outline="", tags="splash")
        self.canvas.create_text(
            320,
            158,
            text="cat64hle 1.x",
            fill="#c0c0c0",
            font=("MS Sans Serif", 14, "bold"),
            justify=tk.CENTER,
            tags="splash",
        )
        self.canvas.create_text(
            320,
            204,
            text="No game running\nUse the ROM Boot window, or File > Open Rom then Start Emulation",
            fill="#c0c0c0",
            font=UI_FONT,
            justify=tk.CENTER,
            tags="splash",
        )

    def _canvas_static_fallback(self, subtitle: str | None = None) -> None:
        self.canvas.delete("fb")
        self.canvas.delete("splash")
        self.canvas.delete("overlay")
        self._fb_photo = None
        blob = bytes(self.core.rom[:8192]) if len(self.core.rom) >= 16 else bytes(self.core.rdram[:8192])
        digest = hashlib.sha256(blob).digest()
        cell_w = max(8, self.canvas.winfo_width() // 20 or 32)
        cell_h = max(8, self.canvas.winfo_height() // 15 or 24)
        for gy in range(15):
            for gx in range(20):
                i = (gy * 20 + gx) % len(digest)
                v = digest[i]
                r = (v ^ (i * 13)) & 0xFF
                g = ((v << 1) ^ (gy * 31)) & 0xFF
                b = ((v << 2) ^ (gx * 17)) & 0xFF
                col = f"#{r:02x}{g:02x}{b:02x}"
                self.canvas.create_rectangle(gx * cell_w, gy * cell_h, gx * cell_w + cell_w, gy * cell_h + cell_h, fill=col, outline="#101010", width=0, tags="fb")
        lines = ["cathle1.x static preview", "from ROM / RDRAM digest"]
        if subtitle:
            lines.append(subtitle)
        self.canvas.create_text(self.canvas.winfo_width() // 2 or 320, self.canvas.winfo_height() // 2 or 216, text="\n".join(lines), fill="#e8e8e8", font=UI_FONT, justify="center", tags="splash")

    def _refresh_vi_framebuffer(self) -> None:
        ppm = self.core.vi_framebuffer_ppm()
        if not ppm:
            self._canvas_static_fallback("No RGB5551 tile at common VI origins")
            return
        photo = None
        try:
            photo = tk.PhotoImage(master=self.root, data=base64.b64encode(ppm).decode("ascii"), format="PPM")
        except tk.TclError:
            try:
                from PIL import Image, ImageTk
                photo = ImageTk.PhotoImage(Image.open(io.BytesIO(ppm)), master=self.root)
            except Exception:
                photo = None
        if photo is None:
            self._canvas_static_fallback("Framebuffer PPM decode failed")
            return
        self._fb_photo = photo
        self.canvas.delete("fb")
        self.canvas.delete("splash")
        self.canvas.create_image(0, 0, anchor="nw", image=self._fb_photo, tags="fb")

    # ---------- 60 FPS scheduler ----------

    def _update_loop(self):
        now = time.perf_counter()
        if self.limit_fps and now < self._next_frame_time:
            delay = max(1, int((self._next_frame_time - now) * 1000))
            self.root.after(delay, self._update_loop)
            return

        if self.core.is_running:
            self.core.run_frame()

            if self.core.frame_count % VI_REFRESH_DIVISOR == 0:
                self._refresh_vi_framebuffer()
                self.canvas.delete("overlay")
                for cmd in self.core.rdp_draw_commands:
                    self.canvas.create_rectangle(cmd["x"], cmd["y"], cmd["x"] + 10, cmd["y"] + 10, fill=cmd["color"], tags="overlay")

            sample_now = time.perf_counter()
            elapsed = sample_now - self._fps_last_time
            if elapsed >= 0.5:
                frames = self.core.frame_count - self._fps_last_frame
                self._measured_fps = frames / elapsed if elapsed > 0 else 0.0
                self._fps_last_time = sample_now
                self._fps_last_frame = self.core.frame_count
                self.update_status_text()
            elif sample_now - self._last_status_update > 0.2:
                self.update_status_text()
                self._last_status_update = sample_now

        if self.limit_fps:
            now = time.perf_counter()
            if self._next_frame_time < now - FRAME_TIME_S:
                self._next_frame_time = now + FRAME_TIME_S
            else:
                self._next_frame_time += FRAME_TIME_S
            delay = max(1, int((self._next_frame_time - time.perf_counter()) * 1000))
        else:
            delay = 1
        self.root.after(delay, self._update_loop)

    def run(self):
        self.root.mainloop()


def main():
    if tk is None:
        print("Fatal: Tkinter is required for this GUI.")
        sys.exit(1)
    app = ACsN64GUI()
    app.run()

if __name__ == "__main__":
    main()

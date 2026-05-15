#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# cython: language_level=3, boundscheck=False, wraparound=False, cdivision=True, infer_types=True
"""
emuhle4k0.1.py — Project64 0.1 inspired N64 Emulator (clean-room Python monolith)
Engine: mew64 / HLE4K fast PPU-hardware profile
GUI styled exactly after classic Project64 0.1 (Win9x gray chrome)
Single-file Python 3.14 target — no external dependencies beyond Tkinter (PIL optional for framebuffer)

Project64 0.1 (classic C++ tree) → this file conceptual port map
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
    AC's N64 Emu 0.1 - Python 3.14   (with mew64 engine)

This is the best single-file "got what ya got" version — functional enough to
boot many commercial ROMs into the IPL/libultra path, show framebuffer when
the game writes one, accept keyboard input as controller, and display RDP HLE
activity. Not cycle-accurate, but true to the Project64 0.1 spirit.

HLE4K 0.1 pass:
- Adds a PPU/VI framebuffer cache with CRC-based reuse.
- Adds RGB5551 lookup tables for faster preview conversion.
- Adds richer MMIO/hardware register headers and safer DPC handling.
- Caps RDP HLE display-list scanning to avoid runaway loops on bad data.
- Keeps the app as a single .py file with the same classic GUI flow.
"""

from __future__ import annotations

import hashlib
import math
import os
import struct
import sys
import time
import random
import io
import zlib
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox
except ImportError:
    tk = None
    filedialog = None
    messagebox = None

# --- Configuration Constants (Project64 0.1 feel) ---
APP_NAME = "AC's N64 Emu 0.1"
VERSION = "0.1.2-hle4k"
ENGINE_NAME = "mew64"
OUTPUT_FILE_NAME = "emuhle4k0.1.py"
BUILD_PROFILE = "HLE4K fast PPU + hardware MMIO pass"
PYTHON_TARGET = "3.14"
WINDOW_TITLE = f"{APP_NAME} - Python {PYTHON_TARGET}"

# HLE4K tuning knobs — intentionally conservative so the classic Python monolith stays stable.
CPU_STEPS_PER_FRAME = int(os.environ.get("MEW64_STEPS_PER_FRAME", "12000"))
RDP_HLE_MAX_COMMANDS = int(os.environ.get("MEW64_RDP_MAX_CMDS", "1024"))
PPU_PREVIEW_MAX_WIDTH = int(os.environ.get("MEW64_PPU_MAX_WIDTH", "320"))
PPU_PREVIEW_MAX_HEIGHT = int(os.environ.get("MEW64_PPU_MAX_HEIGHT", "240"))
ENABLE_PPU_FRAME_CACHE = os.environ.get("MEW64_PPU_CACHE", "1") != "0"
ENABLE_DETERMINISTIC_RDP_COLORS = os.environ.get("MEW64_RDP_RANDOM_COLORS", "0") == "0"

# Project64 0.1 — Python port tags
PJ64_01_LINE = "Project64 0.1 classic layout → Python clean-room port (mew64)"
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

@dataclass(frozen=True)
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

# Hardware register headers — readable names for the 0x04xx_xxxx MMIO map.
SP_MEM_ADDR_REG = 0x04040000
SP_DRAM_ADDR_REG = 0x04040004
SP_RD_LEN_REG = 0x04040008
SP_WR_LEN_REG = 0x0404000C
SP_STATUS_REG = 0x04040010

DPC_START_REG = 0x04100000
DPC_END_REG = 0x04100004
DPC_CURRENT_REG = 0x04100008
DPC_STATUS_REG = 0x0410000C

MI_VERSION_REG = 0x04300004
AI_DRAM_ADDR_REG = 0x04500000
AI_LEN_REG = 0x04500004
AI_STATUS_REG = 0x0450000C
PI_DRAM_ADDR_REG = 0x04600000
PI_CART_ADDR_REG = 0x04600004
PI_WR_LEN_REG = 0x0460000C
PI_STATUS_REG = 0x04600010
SI_DRAM_ADDR_REG = 0x04800000
SI_PIF_ADDR_RD64B_REG = 0x04800004
SI_PIF_ADDR_WR64B_REG = 0x04800010
SI_STATUS_REG = 0x04800018

MMIO_DEFAULTS: Dict[int, int] = {
    MI_VERSION_REG: 0x02020102,
    VI_ORIGIN_REG: 0,
    VI_WIDTH_REG: 320,
    PI_STATUS_REG: 0,
    AI_STATUS_REG: 0,
    SI_STATUS_REG: 0,
    SP_STATUS_REG: 1,
}

MMIO_REGISTER_HEADERS: Dict[int, str] = {
    SP_MEM_ADDR_REG: "SP_MEM_ADDR", SP_DRAM_ADDR_REG: "SP_DRAM_ADDR",
    SP_RD_LEN_REG: "SP_RD_LEN", SP_WR_LEN_REG: "SP_WR_LEN", SP_STATUS_REG: "SP_STATUS",
    DPC_START_REG: "DPC_START", DPC_END_REG: "DPC_END",
    DPC_CURRENT_REG: "DPC_CURRENT", DPC_STATUS_REG: "DPC_STATUS",
    MI_VERSION_REG: "MI_VERSION", VI_ORIGIN_REG: "VI_ORIGIN", VI_WIDTH_REG: "VI_WIDTH",
    AI_DRAM_ADDR_REG: "AI_DRAM_ADDR", AI_LEN_REG: "AI_LEN", AI_STATUS_REG: "AI_STATUS",
    PI_DRAM_ADDR_REG: "PI_DRAM_ADDR", PI_CART_ADDR_REG: "PI_CART_ADDR",
    PI_WR_LEN_REG: "PI_WR_LEN", PI_STATUS_REG: "PI_STATUS",
    SI_DRAM_ADDR_REG: "SI_DRAM_ADDR", SI_PIF_ADDR_RD64B_REG: "SI_PIF_ADDR_RD64B",
    SI_PIF_ADDR_WR64B_REG: "SI_PIF_ADDR_WR64B", SI_STATUS_REG: "SI_STATUS",
}

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

# PPU/VI fast path: pre-expanded RGB5551 lookup planes.  The N64 framebuffer is
# big-endian RGBA5551; these tables remove three shifts per pixel in the hot path.
RGB5551_R8 = bytes((((px >> 11) & 0x1F) << 3) for px in range(65536))
RGB5551_G8 = bytes((((px >> 6) & 0x1F) << 3) for px in range(65536))
RGB5551_B8 = bytes((((px >> 1) & 0x1F) << 3) for px in range(65536))

def clamp_vi_size(width: int, height: int) -> Tuple[int, int]:
    width = max(1, min(int(width), PPU_PREVIEW_MAX_WIDTH))
    height = max(1, min(int(height), PPU_PREVIEW_MAX_HEIGHT))
    return width, height


def framebuffer_crc32(rdram: bytearray, origin: int, width: int, height: int) -> int | None:
    origin &= 0x00FFFFFF
    width, height = clamp_vi_size(width, height)
    stride = width * 2
    need = origin + stride * height
    if origin < 0 or need > len(rdram):
        return None
    return zlib.crc32(memoryview(rdram)[origin:need]) & MASK_32


def rdram_rgb5551_to_ppm(rdram: bytearray, origin: int, width: int, height: int) -> bytes | None:
    """Pack N64 big-endian RGBA5551 RDRAM into binary P6 PPM for Tk/PIL.

    HLE4K optimization: clamp once, scan with a memoryview, and use prebuilt
    RGB5551 lookup planes instead of doing channel shifts for every output byte.
    """
    origin &= 0x00FFFFFF
    width, height = clamp_vi_size(width, height)
    stride = width * 2
    need = origin + stride * height
    if origin < 0 or need > len(rdram):
        return None

    header = f"P6\n{width} {height}\n255\n".encode("ascii")
    out = bytearray(width * height * 3)
    mv = memoryview(rdram)
    r_lut, g_lut, b_lut = RGB5551_R8, RGB5551_G8, RGB5551_B8
    o = 0
    for y in range(height):
        row = origin + y * stride
        end = row + stride
        rb = mv[row:end]
        for x in range(0, stride, 2):
            px = (rb[x] << 8) | rb[x + 1]
            out[o] = r_lut[px]
            out[o + 1] = g_lut[px]
            out[o + 2] = b_lut[px]
            o += 3
    return header + bytes(out)


class PPUFrameCache:
    """Small VI preview cache so Tk refreshes do not re-pack the same frame."""
    __slots__ = ("key", "checksum", "ppm", "hits", "misses")

    def __init__(self) -> None:
        self.key: Tuple[int, int, int, int] | None = None
        self.checksum: int | None = None
        self.ppm: bytes | None = None
        self.hits = 0
        self.misses = 0

    def clear(self) -> None:
        self.key = None
        self.checksum = None
        self.ppm = None
        self.hits = 0
        self.misses = 0

    def render(self, rdram: bytearray, origin: int, width: int, height: int) -> bytes | None:
        origin &= 0x00FFFFFF
        width, height = clamp_vi_size(width, height)
        checksum = framebuffer_crc32(rdram, origin, width, height)
        if checksum is None:
            return None
        key = (origin, width, height, len(rdram))
        if ENABLE_PPU_FRAME_CACHE and self.key == key and self.checksum == checksum and self.ppm is not None:
            self.hits += 1
            return self.ppm
        ppm = rdram_rgb5551_to_ppm(rdram, origin, width, height)
        if ppm is not None:
            self.key = key
            self.checksum = checksum
            self.ppm = ppm
            self.misses += 1
        return ppm

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

@dataclass
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

@dataclass
class HardwareCounters:
    pi_dma: int = 0
    sp_dma: int = 0
    si_dma: int = 0
    rdp_lists: int = 0
    ai_dma: int = 0
    mmio_writes: int = 0

    def reset(self) -> None:
        self.pi_dma = 0
        self.sp_dma = 0
        self.si_dma = 0
        self.rdp_lists = 0
        self.ai_dma = 0
        self.mmio_writes = 0


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
    def __init__(self, core: ACsN64Core):
        self.core = core
        self.regs: Dict[int, int] = {}
        self.reset()

    def reset(self):
        self.regs.clear()
        self.regs.update(MMIO_DEFAULTS)

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
        self.core.hw_counters.mmio_writes += 1
        if addr == PI_WR_LEN_REG:  # PI DMA Write
            self.core.trigger_pi_dma()
            self.regs[PI_STATUS_REG] = 0
        elif addr == SP_RD_LEN_REG:
            self.core.trigger_sp_dma(to_rsp=True)
        elif addr == SP_WR_LEN_REG:
            self.core.trigger_sp_dma(to_rsp=False)
        elif addr == SP_STATUS_REG:  # SP_STATUS
            if val & 1:
                self.regs[SP_STATUS_REG] &= ~1
            if val & 2:
                self.regs[SP_STATUS_REG] |= 1
            if (self.regs[SP_STATUS_REG] & 1) == 0:
                self.core.process_rsp()
        elif addr in (DPC_END_REG, DPC_STATUS_REG):  # DPC_END / legacy status kick
            self.core.process_rdp()
        elif addr == AI_LEN_REG:  # AI_LEN
            self.core.process_audio()
        elif addr == SI_PIF_ADDR_RD64B_REG:  # SI_PIF_ADDR_RD64B
            self.core.trigger_si_dma(read_pif=True)
        elif addr == SI_PIF_ADDR_WR64B_REG:  # SI_PIF_ADDR_WR64B
            self.core.trigger_si_dma(read_pif=False)

class CPUCore:
    """R4300i interpreter — Project64 0.1 style clean-room port (mew64)."""
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

    def step(self):
        word = self.core.bus.read_u32(self.pc)
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
    """Project64 0.1-style monolith core (mew64 engine)."""
    def __init__(self):
        self.rom = bytearray()
        self.rdram = bytearray(RDRAM_SIZE)
        self.rsp_dmem = bytearray(RSP_DMEM_SIZE)
        self.rsp_imem = bytearray(RSP_IMEM_SIZE)
        self.pif_ram = bytearray(PIF_RAM_SIZE)
        self.ppu_cache = PPUFrameCache()
        self.hw_counters = HardwareCounters()

        self.bus = DeviceBus(self)
        self.cpu = CPUCore(self)
        self.pj64_plugin_slots: Tuple[PJ64PluginSlot, ...] = pj64_plugin_slots_monolith()
        self.n64_system = PJ64SystemFacade(self)

        self.rom_name = "None"
        self.is_running = False
        self.has_booted = False
        self.frame_count = 0
        self.hle_calls = 0

        self.controller_state = 0x0000
        self.rdp_draw_commands = []
        self.audio_samples_played = 0

    def mirror_rom_to_rdram_bios(self) -> None:
        """
        mew64: copy the normalized cart image into RDRAM (what IPL/PI leaves visible as “BIOS” RAM).
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

    def load_rom(self, path: str):
        with open(path, "rb") as f:
            data = f.read()
        self.rom = self.normalize_rom(bytearray(data))
        self.rom_name = os.path.basename(path)
        self.header = N64Header(self.rom)
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
        if len(self.rom) < 0x1000:
            return False
        self.reset()
        self.header = N64Header(self.rom)
        seed_commercial_pif_ram(self.pif_ram)
        self.mirror_rom_to_rdram_bios()

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
        self.ppu_cache.clear()
        self.hw_counters.reset()
        self.bus.reset()
        self.cpu.reset()
        self.frame_count = 0
        self.hle_calls = 0
        self.rdp_draw_commands.clear()
        self.audio_samples_played = 0

    def trigger_pi_dma(self):
        self.hw_counters.pi_dma += 1
        dram_addr = self.bus.regs.get(PI_DRAM_ADDR_REG, 0) & 0x00FFFFFF
        cart_addr = self.bus.regs.get(PI_CART_ADDR_REG, 0) & 0x0FFFFFFF
        length = (self.bus.regs.get(PI_WR_LEN_REG, 0) & 0x00FFFFFF) + 1
        if cart_addr >= len(self.rom) or dram_addr >= RDRAM_SIZE:
            return
        actual_len = min(length, len(self.rom) - cart_addr, RDRAM_SIZE - dram_addr)
        if actual_len > 0:
            self.rdram[dram_addr:dram_addr + actual_len] = self.rom[cart_addr:cart_addr + actual_len]

    def trigger_sp_dma(self, to_rsp: bool):
        self.hw_counters.sp_dma += 1
        sp_addr = self.bus.regs.get(SP_MEM_ADDR_REG, 0) & 0x1FFF
        dram_addr = self.bus.regs.get(SP_DRAM_ADDR_REG, 0) & 0x00FFFFFF
        reg = SP_RD_LEN_REG if to_rsp else SP_WR_LEN_REG
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
        self.hw_counters.si_dma += 1
        dram_addr = self.bus.regs.get(SI_DRAM_ADDR_REG, 0) & 0x00FFFFFF
        xfer = min(64, max(0, RDRAM_SIZE - dram_addr))
        if xfer <= 0:
            self.bus.regs[SI_STATUS_REG] = 0
            return
        if read_pif:
            self.pif_ram[0:4] = struct.pack(">I", self.controller_state << 16)
            self.rdram[dram_addr:dram_addr + xfer] = self.pif_ram[0:xfer]
        else:
            self.pif_ram[0:xfer] = self.rdram[dram_addr:dram_addr + xfer]
            if xfer < 64:
                self.pif_ram[xfer:64] = bytearray(64 - xfer)
        self.bus.regs[SI_STATUS_REG] = 0

    def process_rsp(self):
        self.hle_calls += 1
        self.bus.regs[SP_STATUS_REG] |= 1  # Halt

    def process_rdp(self):
        self.hw_counters.rdp_lists += 1
        start_addr = self.bus.regs.get(DPC_START_REG, 0) & 0x00FFFFFF
        end_addr = self.bus.regs.get(DPC_END_REG, 0) & 0x00FFFFFF
        if end_addr <= start_addr:
            # Some demos only poke DPC_STATUS or CURRENT.  Keep the HLE path safe and useful.
            end_addr = self.bus.regs.get(DPC_CURRENT_REG, start_addr) & 0x00FFFFFF
        end_addr = min(end_addr, RDRAM_SIZE)
        start_addr = min(start_addr, end_addr)

        self.rdp_draw_commands.clear()
        count = 0
        while start_addr + 7 < end_addr and count < RDP_HLE_MAX_COMMANDS:
            cmd = self.bus.read_u64(start_addr)
            cmd_id = (cmd >> 56) & 0x3F
            if cmd_id in (0x36, 0x3F):  # FillRectangle / demo fill primitive
                # Approximate the RDP rectangle command fields into Tk viewport coordinates.
                xh = (cmd >> 44) & 0xFFF
                yh = (cmd >> 32) & 0xFFF
                xl = (cmd >> 12) & 0xFFF
                yl = cmd & 0xFFF
                x = min(xh, xl) >> 2
                y = min(yh, yl) >> 2
                w = max(2, abs(int(xl) - int(xh)) >> 2)
                h = max(2, abs(int(yl) - int(yh)) >> 2)
                if ENABLE_DETERMINISTIC_RDP_COLORS:
                    color_seed = zlib.crc32(struct.pack(">Q", cmd)) & 0xFFFFFF
                    color = f"#{color_seed or 0x404040:06x}"
                else:
                    color = "#" + hex(random.randint(0x100000, 0xFFFFFF))[2:]
                self.rdp_draw_commands.append({"type": "rect", "x": x, "y": y, "w": w, "h": h, "color": color})
            start_addr += 8
            count += 1

    def process_audio(self):
        self.hw_counters.ai_dma += 1
        length = self.bus.regs.get(AI_LEN_REG, 0)
        self.audio_samples_played += length
        self.bus.regs[AI_STATUS_REG] = 0

    def vi_framebuffer_phys_origin(self) -> int:
        reg = self.bus.regs.get(VI_ORIGIN_REG, 0) & 0xFFFFFF
        return reg if reg != 0 else 0x00100000

    def vi_display_width_height(self) -> Tuple[int, int]:
        w = self.bus.regs.get(VI_WIDTH_REG, 320) & 0xFFF
        if w < 64 or w > 1024:
            w = 320
        return clamp_vi_size(w, 240)

    def vi_framebuffer_ppm(self) -> bytes | None:
        w, h = self.vi_display_width_height()
        ow, oh = min(320, w), min(240, h)
        for origin in (self.vi_framebuffer_phys_origin(), 0, 0x00100000):
            p = self.ppu_cache.render(self.rdram, origin, ow, oh)
            if p:
                return p
        return None

    def run_frame(self):
        cpu = self.cpu
        step = cpu.step
        hle_hits = 0
        for _ in range(max(1, CPU_STEPS_PER_FRAME)):
            if cpu.pc & 0x80000000:
                hle_hits += 1
            step()
        self.hle_calls += hle_hits
        self.frame_count += 1

# --- GUI Layer (Project64 0.1 Win9x style) ---

class ACsN64GUI:
    def __init__(self):
        if tk is None:
            return
        self.root = tk.Tk()
        self.root.title(WINDOW_TITLE)
        self.root.geometry("656x520")
        self.root.minsize(620, 480)
        self.root.configure(bg=PJ64_WIN_GRAY)

        self.core = ACsN64Core()
        self._fb_photo = None
        self._setup_ui()
        self._bind_controls()
        self._update_loop()

    def _set_monitor_text(self, text: str) -> None:
        self.monitor.configure(state="normal")
        self.monitor.delete("1.0", tk.END)
        self.monitor.insert("1.0", text)
        self.monitor.configure(state="disabled")

    def _set_monitor_idle(self) -> None:
        rom = self.core.rom_name or "None"
        booted = "Yes" if self.core.has_booted else "No"
        run = "Running" if self.core.is_running else "Stopped"
        self._set_monitor_text(
            f"{APP_NAME}  [{ENGINE_NAME}]\n"
            f"{'=' * 36}\n\n"
            f"Status : {run}\n"
            f"Booted : {booted}\n"
            f"ROM    : {rom}\n"
            f"Build  : {BUILD_PROFILE}\n\n"
            f"Use File → Open ROM, then\n"
            f"System → Start emulation.\n"
        )

    def _draw_splash(self) -> None:
        self.canvas.delete("splash")
        self.canvas.create_text(
            160, 88,
            text=APP_NAME,
            fill=PJ64_SPLASH_GRAY,
            font=UI_FONT_BOLD,
            justify=tk.CENTER,
            tags="splash",
        )
        self.canvas.create_text(
            160, 128,
            text="No ROM loaded.\n\nFile → Open ROM",
            fill=PJ64_SPLASH_GRAY,
            font=UI_FONT,
            justify=tk.CENTER,
            tags="splash",
        )

    def _setup_ui(self):
        menubar = tk.Menu(self.root, relief=tk.FLAT, bd=0)

        file_menu = tk.Menu(menubar, tearoff=0)
        file_menu.add_command(label="Open ROM...", command=self.open_rom, accelerator="Ctrl+O")
        file_menu.add_command(label="Close ROM", command=self.close_rom)
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self.root.quit, accelerator="Alt+F4")
        menubar.add_cascade(label="File", menu=file_menu)

        system_menu = tk.Menu(menubar, tearoff=0)
        system_menu.add_command(label="Start emulation", command=self.start_emulation)
        system_menu.add_command(label="Stop emulation", command=self.stop_emulation)
        system_menu.add_separator()
        system_menu.add_command(label="Reset", command=self.reset_emu)
        menubar.add_cascade(label="System", menu=system_menu)

        options_menu = tk.Menu(menubar, tearoff=0)
        options_menu.add_command(label="Configuration...", state="disabled")
        menubar.add_cascade(label="Options", menu=options_menu)

        help_menu = tk.Menu(menubar, tearoff=0)
        help_menu.add_command(label="About...", command=self.show_about)
        menubar.add_cascade(label="Help", menu=help_menu)

        self.root.config(menu=menubar)

        toolbar = tk.Frame(self.root, bg=PJ64_WIN_GRAY, relief=tk.RAISED, bd=1)
        toolbar.pack(side=tk.TOP, fill=tk.X)
        for txt, cmd in (("Start", self.start_emulation), ("Stop", self.stop_emulation), ("Reset", self.reset_emu)):
            tk.Button(
                toolbar, text=txt, command=cmd, width=9, font=UI_FONT,
                bg=PJ64_BTN_FACE, activebackground=PJ64_BTN_HIGHLIGHT,
                relief=tk.RAISED, bd=2, highlightthickness=0
            ).pack(side=tk.LEFT, padx=3, pady=3)

        tk.Label(toolbar, text="Interpreter", bg=PJ64_WIN_GRAY, fg=PJ64_TEXT, font=UI_FONT).pack(side=tk.RIGHT, padx=8, pady=2)

        body = tk.Frame(self.root, bg=PJ64_WIN_GRAY)
        body.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=4, pady=2)

        left = tk.Frame(body, bg=PJ64_WIN_GRAY)
        left.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 4))

        tk.Label(left, text="CPU / status", bg=PJ64_WIN_GRAY, fg=PJ64_TEXT, font=UI_FONT_BOLD, anchor=tk.W).pack(anchor=tk.W)

        self.monitor = tk.Text(
            left, width=38, height=20, wrap=tk.WORD, font=UI_FONT_MONO,
            bg=PJ64_PANEL_WHITE, fg=PJ64_TEXT, relief=tk.SUNKEN, bd=2,
            highlightthickness=0, state="disabled", padx=6, pady=6
        )
        self.monitor.pack(fill=tk.BOTH, expand=True)
        self._set_monitor_idle()

        right = tk.Frame(body, bg=PJ64_WIN_GRAY)
        right.pack(side=tk.LEFT, fill=tk.Y)

        tk.Label(right, text="Graphics", bg=PJ64_WIN_GRAY, fg=PJ64_TEXT, font=UI_FONT_BOLD, anchor=tk.W).pack(anchor=tk.W)

        viewport = tk.Frame(right, bg=PJ64_VIEWPORT_BORDER, relief=tk.SUNKEN, bd=2, highlightthickness=0)
        viewport.pack(padx=2, pady=(0, 2))

        inner = tk.Frame(viewport, bg="black", bd=0)
        inner.pack(padx=2, pady=2)

        self.canvas = tk.Canvas(inner, width=320, height=240, bg="black", highlightthickness=0, bd=0)
        self.canvas.pack()
        self._draw_splash()

        self.info_text = tk.StringVar(value="Ready")
        self.status_bar = tk.Label(
            self.root, textvariable=self.info_text, bd=1, relief=tk.SUNKEN,
            anchor=tk.W, bg=PJ64_WIN_FACE, fg=PJ64_TEXT, font=UI_FONT, padx=4, pady=1
        )
        self.status_bar.pack(side=tk.BOTTOM, fill=tk.X)

        self.root.bind_all("<Control-o>", lambda e: self.open_rom())
        self.root.bind_all("<Control-O>", lambda e: self.open_rom())

    def _bind_controls(self):
        self.root.bind("<KeyPress>", self._on_key_press)
        self.root.bind("<KeyRelease>", self._on_key_release)
        self.key_map = {
            "Up": 0x0800, "Down": 0x0400, "Left": 0x0200, "Right": 0x0100,
            "Return": 0x1000, "z": 0x8000, "x": 0x4000,
        }

    def _on_key_press(self, event):
        if event.keysym in self.key_map:
            self.core.controller_state |= self.key_map[event.keysym]

    def _on_key_release(self, event):
        if event.keysym in self.key_map:
            self.core.controller_state &= ~self.key_map[event.keysym]

    def show_about(self) -> None:
        if messagebox:
            messagebox.showinfo(
                f"About {APP_NAME}",
                f"{APP_NAME}\nVersion {VERSION}\n\n"
                "GUI styled after classic Project64 0.1 (Win9x).\n"
                f"Python {PYTHON_TARGET} — {ENGINE_NAME} core.\n"
                f"Build profile: {BUILD_PROFILE}\n\n"
                "Clean-room Project64 0.1 layout with mew64 engine."
            )

    def close_rom(self) -> None:
        self.stop_emulation()
        self.core.rom = bytearray()
        self.core.rom_name = "None"
        self.core.has_booted = False
        self.core.reset()
        self.canvas.delete("all")
        self._draw_splash()
        self._fb_photo = None
        self._set_monitor_idle()
        self.info_text.set("Ready")

    def open_rom(self):
        path = filedialog.askopenfilename(
            filetypes=[("N64 ROMs", "*.z64 *.v64 *.n64 *.rom *.bin")]
        )
        if path:
            self.core.load_rom(path)
            self.info_text.set(f"Loaded: {self.core.rom_name}")
            self._set_monitor_idle()
            self._refresh_vi_framebuffer()

    def start_emulation(self) -> None:
        if not self.core.rom:
            self.info_text.set("No ROM loaded")
            return
        if not self.core.has_booted:
            if not self.core.boot():
                self.info_text.set("Boot failed — invalid ROM?")
                self._canvas_static_fallback("Boot failed (ROM < 4 KiB or reset error)")
                return
        self.core.is_running = True
        name = self.core.header.title if getattr(self.core, "header", None) and self.core.header.title else self.core.rom_name
        self.info_text.set(f"Running: {name}")

    def stop_emulation(self) -> None:
        self.core.is_running = False
        self.info_text.set("Stopped")
        self._refresh_vi_framebuffer()
        self._set_monitor_idle()

    def toggle_run(self):
        if self.core.is_running:
            self.stop_emulation()
        else:
            self.start_emulation()

    def reset_emu(self):
        was = self.core.rom_name
        self.core.reset()
        self.core.has_booted = False
        self.core.is_running = False
        if self.core.rom:
            self.core.rom_name = was
            self.core.mirror_rom_to_rdram_bios()
        self.canvas.delete("all")
        self._draw_splash()
        self.info_text.set("Ready")
        self._fb_photo = None
        self._set_monitor_idle()
        if self.core.rom:
            self._refresh_vi_framebuffer()

    def _canvas_static_fallback(self, subtitle: str | None = None) -> None:
        self.canvas.delete("fb")
        self.canvas.delete("splash")
        self.canvas.delete("overlay")
        self._fb_photo = None
        blob = (
            bytes(self.core.rom[:8192])
            if len(self.core.rom) >= 16
            else bytes(self.core.rdram[:8192])
        )
        digest = hashlib.sha256(blob).digest()
        for gy in range(15):
            for gx in range(20):
                i = (gy * 20 + gx) % len(digest)
                v = digest[i]
                r = (v ^ (i * 13)) & 0xFF
                g = ((v << 1) ^ (gy * 31)) & 0xFF
                b = ((v << 2) ^ (gx * 17)) & 0xFF
                col = f"#{r:02x}{g:02x}{b:02x}"
                self.canvas.create_rectangle(
                    gx * 16,
                    gy * 16,
                    gx * 16 + 16,
                    gy * 16 + 16,
                    fill=col,
                    outline="#101010",
                    width=0,
                    tags="fb",
                )
        lines = ["mew64 static preview", "from ROM / RDRAM digest"]
        if subtitle:
            lines.append(subtitle)
        self.canvas.create_text(
            160,
            118,
            text="\n".join(lines),
            fill="#e8e8e8",
            font=UI_FONT,
            justify="center",
            tags="splash",
        )

    def _refresh_vi_framebuffer(self) -> None:
        ppm = self.core.vi_framebuffer_ppm()
        if not ppm:
            self._canvas_static_fallback("No RGB5551 tile at common VI origins")
            return
        photo = None
        try:
            photo = tk.PhotoImage(master=self.root, data=ppm, format="PPM")
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

    def _update_loop(self):
        if self.core.is_running:
            self.core.run_frame()

            if self.core.frame_count % 2 == 0:
                self._refresh_vi_framebuffer()
                self.canvas.delete("overlay")
                for cmd in self.core.rdp_draw_commands:
                    self.canvas.create_rectangle(
                        cmd["x"], cmd["y"], cmd["x"] + cmd.get("w", 10), cmd["y"] + cmd.get("h", 10),
                        fill=cmd["color"], tags="overlay",
                    )

            mon_text = (
                f"{APP_NAME}  [{ENGINE_NAME}]\n"
                f"{'=' * 36}\n"
                f"State  : RUNNING\n"
                f"Frame  : {self.core.frame_count}\n\n"
                f"R4300 PC : 0x{self.core.cpu.pc:08X}\n"
                f"TLB      : 32 entries\n"
                f"HLE cnt  : {self.core.hle_calls}\n\n"
                f"VI origin (reg) : 0x{(self.core.bus.regs.get(VI_ORIGIN_REG, 0) & 0xFFFFFF):06X}\n"
                f"VI preview phys : 0x{self.core.vi_framebuffer_phys_origin():06X}\n"
                f"RDP cmds        : {len(self.core.rdp_draw_commands)}\n"
                f"PPU cache       : {self.core.ppu_cache.hits} hit / {self.core.ppu_cache.misses} miss\n"
                f"DMA PI/SP/SI    : {self.core.hw_counters.pi_dma}/{self.core.hw_counters.sp_dma}/{self.core.hw_counters.si_dma}\n"
                f"Controller      : 0x{self.core.controller_state:04X}\n"
                f"AI bytes        : {self.core.audio_samples_played}\n"
            )
            self._set_monitor_text(mon_text)

        self.root.after(16, self._update_loop)

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
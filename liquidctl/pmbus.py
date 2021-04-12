"""Constants and methods for interfacing with PMBus compliant devices.

Specifications:

- Power Systems Management Protocol Specification.  Revision 1.3.1, 2015.
  Available uppon request, check the PMBus website.

- Power Systems Management Protocol Specification.  Revision 1.2, 2010.
  Available on the PMBus website.
  http://pmbus.org/Assets/PDFS/Public/PMBus_Specification_Part_I_Rev_1-2_20100906.pdf
  http://pmbus.org/Assets/PDFS/Public/PMBus_Specification_Part_II_Rev_1-2_20100906.pdf

- System Management Bus (SMBus) Specification.  Version 3.1, 2018.
  Available on the SMBus website.
  http://smbus.org/specs/SMBus_3_1_20180319.pdf

Additional references:

- Milios, John.  CRC-8 firmware implementations for SMBus.  1999.
 http://sbs-forum.org/marcom/dc2/20_crc-8_firmware_implementations.pdf

- Pircher, Thomas.  pycrc -- parameterisable CRC calculation utility and C
  source code generator: CRC algorithms implemented in Python.
  https://github.com/tpircher/pycrc/blob/master/pycrc/algorithms.py

- White, Robert V.  Using the PMBus Protocol.  2005.
  http://pmbus.org/Assets/Present/Using_The_PMBus_20051012.pdf

Copyright (C) 2019–2019  Jonas Malaco and contributors

Includes a CRC-8 implementation adapted from pycrc by Thomas Pircher.
Copyright (c) 2006-2017  Thomas Pircher  <tehpeh-web@tty1.net>

SPDX-License-Identifier: GPL-3.0-or-later
"""

import math
from enum import IntEnum, IntFlag, unique

from typing import Final


@unique
class WriteBit(IntFlag):
    WRITE: Final[int] = 0x00
    READ: Final[int] = 0x01


@unique
class CommandCode(IntEnum):
    """Incomplete enumeration of the PMBus command codes."""

    PAGE: Final[int] = 0x00

    CLEAR_FAULTS: Final[int] = 0x03

    PAGE_PLUS_WRITE: Final[int] = 0x05
    PAGE_PLUS_READ: Final[int] = 0x06

    VOUT_MODE: Final[int] = 0x20

    FAN_CONFIG_1_2: Final[int] = 0x3a
    FAN_COMMAND_1: Final[int] = 0x3b
    FAN_COMMAND_2: Final[int] = 0x3c
    FAN_CONFIG_3_4: Final[int] = 0x3d
    FAN_COMMAND_3: Final[int] = 0x3e
    FAN_COMMAND_4: Final[int] = 0x3f

    READ_EIN: Final[int] = 0x86
    READ_EOUT: Final[int] = 0x87
    READ_VIN: Final[int] = 0x88
    READ_IIN: Final[int] = 0x89
    READ_VCAP: Final[int] = 0x8a
    READ_VOUT: Final[int] = 0x8b
    READ_IOUT: Final[int] = 0x8c
    READ_TEMPERATURE_1: Final[int] = 0x8d
    READ_TEMPERATURE_2: Final[int] = 0x8e
    READ_TEMPERATURE_3: Final[int] = 0x8f
    READ_FAN_SPEED_1: Final[int] = 0x90
    READ_FAN_SPEED_2: Final[int] = 0x91
    READ_FAN_SPEED_3: Final[int] = 0x92
    READ_FAN_SPEED_4: Final[int] = 0x93
    READ_DUTY_CYCLE: Final[int] = 0x94
    READ_FREQUENCY: Final[int] = 0x95
    READ_POUT: Final[int] = 0x96
    READ_PIN: Final[int] = 0x97
    READ_PMBUS_REVISON: Final[int] = 0x98
    MFR_ID: Final[int] = 0x99
    MFR_MODEL: Final[int] = 0x9a
    MFR_REVISION: Final[int] = 0x9b
    MFR_LOCATION: Final[int] = 0x9c
    MFR_DATE: Final[int] = 0x9d
    MFR_SERIAL: Final[int] = 0x9e

    MFR_SPECIFIC_D1: Final[int] = 0xd1
    MFR_SPECIFIC_D2: Final[int] = 0xd2
    MFR_SPECIFIC_D8: Final[int] = 0xd8
    MFR_SPECIFIC_DC: Final[int] = 0xdc
    MFR_SPECIFIC_EE: Final[int] = 0xee
    MFR_SPECIFIC_F0: Final[int] = 0xf0
    MFR_SPECIFIC_FC: Final[int] = 0xfc


def linear_to_float(bytes, vout_exp=None) -> float:
    """Read PMBus LINEAR11 and ULINEAR16 numeric values.

    If `vout_exp` is None the value is interpreted as a 2 byte LINEAR11 value.
    The mantissa is stored in the lower 11 bits, in two's-complement, and the
    exponent is is stored in the upper 5 bits, also in two's-complement.

    Otherwise the value is assumed to be encoded in ULINEAR16, where the
    exponent is read from the lower 5 bits of `vout_exp` (which is assumed to
    be the output from VOUT_MOE) and the mantissa is the unsigned 2 byte
    integer in `bytes`.

    Per the SMBus specification, the lowest order byte is sent first (endianess
    is little).

    >>> linear_to_float(bytes.fromhex('67e3'))
    54.4375
    >>> linear_to_float(bytes.fromhex('6703'), vout_exp=0x1c)
    54.4375
    """
    tmp = int.from_bytes(bytes[:2], byteorder='little')
    if vout_exp is None:
        exp = tmp >> 11
        fra = tmp & 0x7ff
        if fra > 1023:
            fra = fra - 2048
    else:
        exp = vout_exp & 0x1f
        fra = tmp
    if exp > 15:
        exp = exp - 32
    return fra * 2**exp


def float_to_linear11(float) -> bytes:
    """Encode float in PMBus LINEAR11 format.

    A LINEAR11 number is a 2 byte value with an 11 bit two's complement
    mantissa and a 5 bit two's complement exponent.

    Per the SMBus specification, the lowest order byte is sent first (endianess
    is little).

    >>> float_to_linear11(3.3).hex()
    '4dc3'
    >>> float_to_linear11(0.0).hex()
    '0000'
    >>> linear_to_float(float_to_linear11(2812))
    2812
    >>> linear_to_float(float_to_linear11(-2812))
    -2812
    """
    if float == 0:
        return b'\x00\x00'
    max_y = 1023
    n = math.ceil(math.log(math.fabs(float)/max_y, 2))
    y = round(float * 2**(-n))
    if n < 0:
        n = n + 32
    if y < 0:
        y = y + 2048
    return int.to_bytes((n << 11) | y, length=2, byteorder='little')


def compute_pec(bytes):
    """
    Compute a 8-bit Packet Error Code (PEC) for `bytes`.

    According to the SMBus specification, the PEC is computed using a 8-bit
    cyclic rendundancy check (CRC-8) with the polynominal x⁸ + x² + x¹ + x⁰.

    The computation uses a 256-byte lookup table.

    Based on https://github.com/tpircher/pycrc/blob/master/pycrc/algorithms.py.

    >>> hex(compute_pec(bytes('123456789', 'ascii')))
    '0xf4'
    >>> hex(compute_pec(bytes.fromhex('5c')))
    '0x93'
    >>> hex(compute_pec(bytes.fromhex('5c93')))
    '0x0'
    """
    tbl = _gen_pec_table()
    reg = 0
    for octet in bytes:
        idx = reg ^ octet
        reg = tbl[idx]
    return reg


def _gen_pec_table():
    """Generate the lookup table for compute_pec.

    Once a table is generated it is reused for all subsequent calls.
    """
    global _PEC_TBL
    if _PEC_TBL:
        return _PEC_TBL
    tbl = [0 for i in range(_PEC_TBL_LEN)]
    for i in range(_PEC_TBL_LEN):
        reg = i
        for _ in range(8):
            if reg & _PEC_MSB_MASK != 0:
                reg = (reg << 1) ^ _PEC_POLY
            else:
                reg = (reg << 1)
        tbl[i] = reg & _PEC_MASK
    _PEC_TBL = tbl
    return tbl


_PEC_WIDTH: Final[int] = 8
_PEC_MSB_MASK: Final[int] = 1 << (_PEC_WIDTH - 1) #7
_PEC_MASK: Final[int] = (_PEC_MSB_MASK << 1) - 1
_PEC_POLY: Final[int] = (0b100000111 & _PEC_MASK)
_PEC_TBL_LEN: Final[int] = 256
_PEC_TBL = None

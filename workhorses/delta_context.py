"""Encode BSDIFF literals against target bytes already recoverable from the diff."""

from dataclasses import replace
import lzma

from workhorses import delta_xz as xz
from workhorses.delta import (
    MAX_CONTROLS, Patch, _control_bytes, _header, _pack, _unpack_controls, apply, unpack,
)


MAGIC = b"CTXD0001"


def _known(source, instructions, difference):
    runs = tuple(replace(r, literal=0) for r in instructions)
    return apply(source, Patch(len(difference), runs, difference, b""))


def recode(source, patch, encode_literals):
    parsed = unpack(patch)
    known = _known(source, parsed.instructions, parsed.difference)
    packed = encode_literals(source + known, parsed.literal)
    control = lzma.compress(_control_bytes(parsed.instructions), filters=xz.FILTERS)
    difference = lzma.compress(parsed.difference, filters=xz.FILTERS)
    return _pack(parsed.size, control, difference, packed, magic=MAGIC)


def target_size(patch):
    return _header(patch, magic=MAGIC)[0]


def apply_patch(source, patch):
    size, encoded_control, encoded_diff, encoded_literals = _header(patch, magic=MAGIC)
    control = xz._inflate(encoded_control, 24 * min(size + 1024, MAX_CONTROLS))
    instructions, n_diff, n_lit = _unpack_controls(size, control)
    if xz.target_size(encoded_literals) != n_lit:
        raise ValueError("context delta literal size differs from its control stream")
    difference = xz._inflate(encoded_diff, n_diff)
    if len(difference) != n_diff:
        raise ValueError("context delta difference size differs from its control stream")
    known = _known(source, instructions, difference)
    literals = apply(source + known, xz.decode(encoded_literals))
    output = bytearray(size)
    cursor = 0
    diff_cursor = literal_cursor = 0
    copied, extra = memoryview(known), memoryview(literals)
    for row in instructions:
        d, l = diff_cursor + row.difference, literal_cursor + row.literal
        out = cursor + row.difference
        output[cursor:out] = copied[diff_cursor:d]
        cursor = out + row.literal
        output[out:cursor] = extra[literal_cursor:l]
        diff_cursor, literal_cursor = d, l
    return bytes(output)

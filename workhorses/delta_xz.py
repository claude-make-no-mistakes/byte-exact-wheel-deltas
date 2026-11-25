"""Store a BSDIFF parse in independently bounded XZ streams."""

import lzma

from workhorses.delta import (
    MAX_CONTROLS, Instruction, _control_bytes, _header, _pack, _unpack_data, _unpack_streams, unpack,
)


MAGIC = b"BSDXZ001"
COMPACT = b"BSDXZ002"
FILTERS = [{"id": lzma.FILTER_LZMA2, "preset": 9 | lzma.PRESET_EXTREME, "dict_size": 8 << 20}]


def _write_uint(value, out):
    while value >= 128:
        out.append(128 | (value & 127))
        value >>= 7
    out.append(value)


def _compact_controls(rows):
    body = bytearray()
    _write_uint(len(rows), body)
    for row in rows:
        _write_uint(row.difference, body)
    for row in rows:
        _write_uint(row.literal, body)
    for row in rows:
        _write_uint(2 * row.seek if row.seek >= 0 else -2 * row.seek - 1, body)
    return body


def _restore_controls(raw, size):
    values = iter(raw)

    def take():
        integer = 0
        for shift in range(0, 64, 7):
            byte = next(values, -1)
            if byte == -1 or (shift == 63 and byte > 1):
                raise ValueError("invalid variable-width control field")
            integer |= (byte & 127) << shift
            if byte < 128:
                return integer
        raise ValueError("control field exceeds 64 bits")

    count = take()
    if count > min(size + 1024, MAX_CONTROLS) or count > (len(raw) - 1) // 3:
        raise ValueError("too many compact control instructions")
    difference = [take() for _ in range(count)]
    extra = [take() for _ in range(count)]
    seeks = [take() for _ in range(count)]
    if sum(difference) + sum(extra) != size or next(values, -1) != -1 or any(v == (1 << 64) - 1 for v in seeks):
        raise ValueError("compact instructions exceed the output or seek bound")
    signed = ((v >> 1) ^ -(v & 1) for v in seeks)
    return _control_bytes(Instruction(d, e, s) for d, e, s in zip(difference, extra, signed))


def recode(patch: bytes, *, compact=False) -> bytes:
    """Recompress a validated BSDIFF40 parse in the experimental format."""
    item = unpack(patch)
    magic = MAGIC
    control = lzma.compress(_control_bytes(item.instructions), filters=FILTERS)
    if compact:
        variable = lzma.compress(_compact_controls(item.instructions), filters=FILTERS)
        if len(variable) < len(control):
            magic, control = COMPACT, variable
    streams = (lzma.compress(data, filters=FILTERS) for data in (item.difference, item.literal))
    return _pack(item.size, control, *streams, magic=magic)


def _inflate(stream, limit):
    try:
        reader = lzma.LZMADecompressor(format=lzma.FORMAT_XZ, memlimit=24 << 20)
        data = reader.decompress(stream, max_length=limit + 1)
    except lzma.LZMAError as error:
        raise ValueError("invalid XZ diff stream") from error
    if len(data) > limit or not reader.eof or reader.unused_data:
        raise ValueError("XZ diff stream is truncated, concatenated, or too large")
    return data


def target_size(patch: bytes) -> int:
    return _header(patch, magic=COMPACT if patch.startswith(COMPACT) else MAGIC)[0]


def decode(patch: bytes):
    if patch.startswith(COMPACT):
        size, encoded, difference, literal = _header(patch, magic=COMPACT)
        limit = 5 + 20 * min(size + 1024, MAX_CONTROLS)
        raw = _restore_controls(_inflate(encoded, limit), size)
        return _unpack_data(size, raw, difference, literal, _inflate)
    return _unpack_streams(*_header(patch, magic=MAGIC), _inflate)

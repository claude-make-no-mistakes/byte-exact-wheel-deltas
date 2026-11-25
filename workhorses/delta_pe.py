"""Reversible normalization of AMD64 PE function references and unwind tables."""

from dataclasses import dataclass
from hashlib import blake2s
import re
from struct import Struct, unpack_from
import numpy as np


FORMAT = Struct("<III")
DWORD = Struct("<I")
MASK32 = (1 << 32) - 1
LIMIT = 64 << 20
MAX_FUNCTIONS = 200_000
OPERAND = re.compile(
    rb"([\xe8\xe9])|\x0f[\x80-\x8f]|([\x8b\x8d\x89\x03\x3b\x2b][\x05\x0d\x15\x1d\x25\x2d\x35\x3d])"
)
MODES = ("table", "labels-96", "labels-full", "labels-data")


@dataclass(frozen=True)
class _Section:
    rva: int
    offset: int
    length: int
    code: bool
    name: bytes


def _file_at(sections, rva, length, *, code):
    for section in sections:
        if section.code == code and section.rva <= rva and rva + length <= section.rva + section.length:
            return section.offset + rva - section.rva
    raise ValueError("PE address lies outside its declared section")


def _table(data):
    if not 64 <= len(data) <= LIMIT or data[:2] != b"MZ":
        raise ValueError("expected a bounded AMD64 PE image")
    offset = DWORD.unpack_from(data, 60)[0]
    if offset < 64 or offset + 24 > len(data) or data[offset:offset + 4] != b"PE\0\0":
        raise ValueError("missing PE header")
    machine, count = unpack_from("<HH", data, offset + 4)
    header_size = unpack_from("<H", data, offset + 20)[0]
    optional = offset + 24
    headers = optional + header_size
    end = headers + count * 40
    if machine != 0x8664 or not 1 <= count <= 96 or header_size < 144 or end > len(data):
        raise ValueError("invalid AMD64 PE section table")
    if unpack_from("<H", data, optional)[0] != 0x20B or DWORD.unpack_from(data, optional + 108)[0] < 4:
        raise ValueError("PE image has no exception directory")
    rva, length = unpack_from("<II", data, optional + 112 + 8 * 3)
    if length % FORMAT.size or not 1 <= length // FORMAT.size <= MAX_FUNCTIONS:
        raise ValueError("invalid PE exception table size")
    sections = []
    for p in range(headers, end, 40):
        section_rva, size, start = unpack_from("<III", data, p + 12)
        if size:
            if start < end or start + size > len(data):
                raise ValueError("invalid PE section extent")
            code = bool(DWORD.unpack_from(data, p + 36)[0] & 0x20)
            sections.append(_Section(section_rva, start, size, code, bytes(data[p:p + 8]).rstrip(b"\0")))
    ordered = sorted(sections, key=lambda section: section.offset)
    for a, b in zip(ordered, ordered[1:]):
        if a.offset + a.length > b.offset:
            raise ValueError("overlapping PE sections")
    start = _file_at(sections, rva, length, code=False)
    return sections, start, start + length


def _addresses(buffer, start, end, *, inverse):
    begin = unwind = 0
    for offset in range(start, end, FORMAT.size):
        a, b, c = FORMAT.unpack_from(buffer, offset)
        if inverse:
            begin = (a + begin) & MASK32
            unwind = (c + unwind) & MASK32
            FORMAT.pack_into(buffer, offset, begin, (b + begin) & MASK32, unwind)
        else:
            FORMAT.pack_into(buffer, offset, (a - begin) & MASK32, (b - a) & MASK32, (c - unwind) & MASK32)
            begin, unwind = a, c


def _references(data, sections, first, last, mode):
    labels, calls = {}, []
    previous = 0
    for begin, end, _ in FORMAT.iter_unpack(memoryview(data)[first:last]):
        if end <= begin or begin < previous:
            raise ValueError("PE exception function extents overlap or are empty")
        start = _file_at(sections, begin, end - begin, code=True)
        stop = start + end - begin
        previous = end
        canonical = bytearray(data[start:stop if mode == "labels-full" else min(stop, start + 96)])
        cursor = start
        while (match := OPERAND.search(data, cursor, stop)) is not None:
            operand = match.end()
            if operand + 4 > stop:
                break
            local = operand - start
            if local < len(canonical):
                count = min(4, len(canonical) - local)
                canonical[local:local + count] = bytes(count)
            if match.group(1) or (mode == "labels-data" and match.group(2)):
                calls.append((operand, begin + local + 4, bool(match.group(2))))
            cursor = operand + 4
        key = int.from_bytes(blake2s(canonical, digest_size=4).digest(), "little")
        labels[key] = None if key in labels else begin
    return {key: rva for key, rva in labels.items() if rva is not None}, calls


def _readonly_windows(data, sections):
    for section in sections:
        if section.code or section.name != b".rdata" or section.length < 12:
            continue
        words = [np.ndarray((section.length - 11,), dtype="<u4", buffer=data,
                            offset=section.offset + i, strides=(1,)) for i in (0, 4, 8)]
        a, b, c = words
        yield section.rva, ((a * np.uint32(0x9E3779B1)) ^ ((b << 9) | (b >> 23)) ^
                            (c * np.uint32(0x85EBCA77)))


def _data_labels(data, sections, references, *, inverse):
    windows = list(_readonly_windows(data, sections))
    if not windows or not references:
        return {}
    wanted = set(references) if inverse else {
        int(keys[rva - start]) for start, keys in windows for rva in references
        if start <= rva < start + len(keys)
    }
    if not wanted:
        return {}
    requested = np.array(sorted(wanted), dtype=np.uint32)
    labels = {}
    for start, keys in windows:
        for i in np.flatnonzero(np.isin(keys, requested, kind="sort")):
            key = int(keys[i])
            labels[key] = None if key in labels else start + int(i)
    return {key: rva for key, rva in labels.items() if rva is not None}


def normalize(data: bytes, mode: str):
    if mode not in MODES:
        raise ValueError("unknown PE transform")
    sections, first, last = _table(data)
    output = bytearray(data)
    bitmap = bytearray()
    if mode != "table":
        names, calls = _references(data, sections, first, last, mode)
        by_address = {rva: key for key, rva in names.items()}
        references = [(next_rva + DWORD.unpack_from(data, offset)[0]) & MASK32 for offset, next_rva, _ in calls]
        data_targets = {rva for rva, (_, _, is_data) in zip(references, calls) if is_data}
        data_by_address = {rva: key for key, rva in
                           _data_labels(data, sections, data_targets, inverse=False).items()} if data_targets else {}
        bitmap = bytearray((len(calls) + 7) // 8)
        for i, (target, (offset, _, is_data)) in enumerate(zip(references, calls)):
            label = (data_by_address if is_data else by_address).get(target)
            if label is not None:
                DWORD.pack_into(output, offset, label)
                bitmap[i // 8] |= 1 << (i % 8)
    _addresses(output, first, last, inverse=False)
    return bytes(output), bytes(bitmap)


def recover(data: bytes, mode: str, bitmap: bytes):
    if mode not in MODES:
        raise ValueError("unknown PE transform")
    sections, first, last = _table(data)
    output = bytearray(data)
    _addresses(output, first, last, inverse=True)
    if mode == "table":
        if bitmap:
            raise ValueError("unexpected PE reference bitmap")
    else:
        names, calls = _references(output, sections, first, last, mode)
        if len(bitmap) != (len(calls) + 7) // 8 or (len(calls) % 8 and bitmap[-1] >> (len(calls) % 8)):
            raise ValueError("incorrect PE reference bitmap length or padding")
        data_targets = {DWORD.unpack_from(output, offset)[0]
                        for i, (offset, _, is_data) in enumerate(calls)
                        if is_data and bitmap[i // 8] & (1 << (i % 8))}
        data_names = _data_labels(output, sections, data_targets, inverse=True) if data_targets else {}
        for i, (offset, next_rva, is_data) in enumerate(calls):
            if bitmap[i // 8] & (1 << (i % 8)):
                target = (data_names if is_data else names).get(DWORD.unpack_from(output, offset)[0])
                if target is None:
                    raise ValueError("PE reference label is missing or ambiguous")
                DWORD.pack_into(output, offset, (target - next_rva) & MASK32)
    return bytes(output)

"""Bounded BSDIFF40 encoder postprocessing with exact byte-cost checks."""

import bz2
from dataclasses import dataclass, replace
import struct

import numpy as np


MAGIC = b"BSDIFF40"
MAX_TARGET = 64 << 20
MAX_CONTROLS = 1_000_000


@dataclass(frozen=True)
class Instruction:
    difference: int
    literal: int
    seek: int


@dataclass(frozen=True)
class Patch:
    size: int
    instructions: tuple[Instruction, ...]
    difference: bytes
    literal: bytes


@dataclass(frozen=True)
class Audit:
    controls: int
    control_bytes: int
    initial_bytes: int
    examined_copies: int
    interchangeable_copies: int
    remaps: int
    boundary_changes: int
    exact_encodings: int


def _decode_integer(n: int) -> int:
    return -(n & ((1 << 63) - 1)) if n >> 63 else n


def _integer(n: int) -> bytes:
    if abs(n) >= 1 << 63:
        raise OverflowError("BSDIFF40 integers use 63 magnitude bits")
    return (abs(n) | (int(n < 0) << 63)).to_bytes(8, "little")


def _header(patch: bytes, *, magic: bytes = MAGIC) -> tuple[int, bytes, bytes, bytes]:
    if len(patch) < 32 or patch[:8] != magic:
        raise ValueError(f"expected a {magic.decode('ascii')} patch with a 32-byte header")
    c, d, n = map(_decode_integer, struct.unpack_from("<QQQ", patch, 8))
    if min(c, d, n) < 0 or n > MAX_TARGET or 32 + c + d > len(patch):
        raise ValueError("invalid BSDIFF40 stream lengths or target size")
    return n, patch[32:32 + c], patch[32 + c:32 + c + d], patch[32 + c + d:]


def _inflate(encoded: bytes, limit: int) -> bytes:
    stream = bz2.BZ2Decompressor()
    data = stream.decompress(encoded, max_length=limit + 1)
    if not stream.eof or stream.unused_data or len(data) > limit:
        raise ValueError("BSDIFF40 stream is truncated, concatenated, or too large")
    return data


def unpack(patch: bytes) -> Patch:
    return _unpack_streams(*_header(patch), _inflate)


def _unpack_streams(size, control, difference, literal, inflate):
    raw = inflate(control, 24 * min(size + 1024, MAX_CONTROLS))
    return _unpack_data(size, raw, difference, literal, inflate)


def _unpack_data(size, raw, difference, literal, inflate):
    instructions, diff_len, literal_len = _unpack_controls(size, raw)
    diff, extra = inflate(difference, diff_len), inflate(literal, literal_len)
    if len(diff) != diff_len or len(extra) != literal_len:
        raise ValueError("BSDIFF40 data stream length disagrees with the control stream")
    return Patch(size, instructions, diff, extra)


def _unpack_controls(size, raw):
    if len(raw) % 24:
        raise ValueError("BSDIFF40 control stream ends within an instruction")
    instructions = tuple(
        Instruction(*map(_decode_integer, triple)) for triple in struct.iter_unpack("<QQQ", raw)
    )
    diff_len = sum(i.difference for i in instructions)
    literal_len = sum(i.literal for i in instructions)
    if any(min(i.difference, i.literal) < 0 for i in instructions) or diff_len + literal_len != size:
        raise ValueError("BSDIFF40 instructions do not cover the target")
    # The stock decoder stops once it reaches the advertised target size.
    if instructions and (not size or not (instructions[-1].difference or instructions[-1].literal)):
        raise ValueError("BSDIFF40 contains instructions after the end of the target")
    return instructions, diff_len, literal_len


def _control_bytes(instructions) -> bytes:
    return b"".join(_integer(item) for i in instructions for item in (i.difference, i.literal, i.seek))


def _compress_control(instructions) -> bytes:
    return bz2.compress(_control_bytes(instructions), compresslevel=9)


def _pack(size: int, control: bytes, difference: bytes, literal: bytes, *, magic: bytes = MAGIC) -> bytes:
    return b"".join((magic, _integer(len(control)), _integer(len(difference)), _integer(size),
                     control, difference, literal))


def pack(patch: Patch) -> bytes:
    return _pack(patch.size, _compress_control(patch.instructions),
                 bz2.compress(patch.difference, compresslevel=9), bz2.compress(patch.literal, compresslevel=9))


def _positions(instructions):
    src = dst = 0
    for instruction in instructions:
        yield src, dst
        src += instruction.difference + instruction.seek
        dst += instruction.difference + instruction.literal


def apply(old: bytes, patch: Patch) -> bytes:
    """Independent in-memory BSDIFF40 application for finite experimental inputs."""
    src = np.frombuffer(old, dtype=np.uint8)
    dst = np.empty(patch.size, dtype=np.uint8)
    difference = np.frombuffer(patch.difference, dtype=np.uint8)
    literal = np.frombuffer(patch.literal, dtype=np.uint8)
    diff_cursor = literal_cursor = 0
    for (start, out), instruction in zip(_positions(patch.instructions), patch.instructions):
        diff, extra = instruction.difference, instruction.literal
        dst[out:out + diff] = difference[diff_cursor:diff_cursor + diff]
        # BSDIFF40 adds zero when a difference position is outside the source.
        left = max(0, -start)
        right = min(diff, len(old) - start)
        if right > left:
            dst[out + left:out + right] += src[start + left:start + right]
        dst[out + diff:out + diff + extra] = literal[literal_cursor:literal_cursor + extra]
        diff_cursor += diff
        literal_cursor += extra
    return dst.tobytes()


def _replace_pair(controls, i, delta):
    before, after = controls[i - 1], controls[i]
    return (*controls[:i - 1], replace(before, seek=before.seek + delta),
            replace(after, seek=after.seek - delta), *controls[i + 1:])


def _duplicate_copies(old, controls, *, search_bytes, max_copies):
    if not old:
        return 0, []
    order = sorted(range(1, len(controls)), key=lambda i: (-abs(controls[i - 1].seek), i))
    sources = [src for src, _ in _positions(controls)]
    scanned = 0
    found = []
    for i in order:
        src = sources[i]
        length = controls[i].difference
        if length < 8 or src < 0 or src + length > len(old):
            continue
        if scanned * len(old) + len(old) > search_bytes or scanned >= max_copies:
            break
        scanned += 1
        block = old[src:src + length]
        # Check an alternative on either side of the native match.
        earlier = old.find(block, 0, src + length - 1)
        later = old.find(block, src + 1)
        alternatives = [item for item in (earlier, later) if item >= 0]
        if alternatives:
            found.append((i, alternatives))
    return scanned, found


def _boundary_proposals(old, new, instructions, *, shortlist):
    if not shortlist:
        return []
    src_bytes = np.frombuffer(old, dtype=np.uint8)
    dst_bytes = np.frombuffer(new, dtype=np.uint8)
    candidates = []
    for i, ((src, dst), control) in enumerate(zip(_positions(instructions), instructions)):
        for delta in (8, 32, 128, 512, -8, -32, -128, -512):
            updated = control.difference + delta
            if not 0 <= updated <= control.difference + control.literal:
                continue
            left, right = sorted((updated, control.difference))
            if src + left < 0 or src + right > len(src_bytes):
                continue
            fraction = np.mean(src_bytes[src + left:src + right] == dst_bytes[dst + left:dst + right])
            merit = delta * (float(fraction) - 0.5)
            if merit > 0:
                candidates.append((-merit, i, delta))
    return sorted(candidates)[:shortlist]


def _rewrite_boundary(old, new, seed_patch, controls, i, shift, source, diff_end, literal_start):
    current = controls[i]
    updated = replace(current, difference=current.difference + shift, literal=current.literal - shift,
                      seek=current.seek - shift)
    instructions = (*controls[:i], updated, *controls[i + 1:])
    diff, literal = seed_patch.difference, seed_patch.literal
    # Moving the cut by s and the seek by -s preserves the next source
    # position. Every other output byte keeps its original stream and offset.
    if shift > 0:
        start = source + current.difference
        added = np.frombuffer(literal, count=shift, offset=literal_start, dtype=np.uint8) - np.frombuffer(
            old, count=shift, offset=start, dtype=np.uint8
        )
        diff = b"".join((memoryview(diff)[:diff_end], added, memoryview(diff)[diff_end:]))
        literal = b"".join((memoryview(literal)[:literal_start], memoryview(literal)[literal_start + shift:]))
    else:
        target = diff_end + literal_start
        literal = b"".join((memoryview(literal)[:literal_start], memoryview(new)[target + shift:target],
                            memoryview(literal)[literal_start:]))
        diff = b"".join((memoryview(diff)[:diff_end + shift], memoryview(diff)[diff_end:]))
    return Patch(seed_patch.size, instructions, diff, literal)


def optimize(old: bytes, new: bytes, seed: bytes, *, shortlist: int = 0,
             max_copies: int = 64, max_exact_remaps: int = 24,
             search_bytes: int = 64 << 20) -> tuple[bytes, Audit]:
    """Return the original patch or a shorter BSDIFF40 encoding of the same target."""
    if min(shortlist, max_copies, max_exact_remaps, search_bytes) < 0:
        raise ValueError("optimization budgets must be nonnegative")
    seed_patch = unpack(seed)
    if apply(old, seed_patch) != new:
        raise ValueError("the seed patch does not reconstruct the requested target")
    initial_size, compressed_control, compressed_diff, compressed_literal = _header(seed)
    control_bytes = len(compressed_control)
    controls = seed_patch.instructions
    scanned, copies = _duplicate_copies(old, controls, search_bytes=search_bytes, max_copies=max_copies)
    initial_sources = [src for src, _ in _positions(controls)]
    remaps = encodings = 0
    for i, positions in copies:
        original_source = initial_sources[i]
        best = None
        for src in positions:
            if encodings >= max_exact_remaps:
                break
            candidate = _replace_pair(controls, i, src - original_source)
            if any(abs(candidate[k].seek) >= 1 << 63 for k in (i - 1, i)):
                continue
            compressed = _compress_control(candidate)
            encodings += 1
            if len(compressed) < (len(best[0]) if best else len(compressed_control)):
                best = (compressed, candidate)
        if best:
            compressed_control, controls = best
            remaps += 1
    winner = _pack(initial_size, compressed_control, compressed_diff, compressed_literal) if remaps else seed
    boundary_change = 0
    proposals = _boundary_proposals(old, new, controls, shortlist=shortlist)
    positions = {}
    if proposals:
        wanted = {i for _, i, _ in proposals}
        diff_end = 0
        for i, ((source, target), item) in enumerate(zip(_positions(controls), controls)):
            if i in wanted:
                positions[i] = (source, diff_end + item.difference, target - diff_end)
            diff_end += item.difference
    for _, i, shift in proposals:
        item = controls[i]
        if abs(item.seek - shift) >= 1 << 63:
            continue
        packed = pack(_rewrite_boundary(old, new, seed_patch, controls, i, shift, *positions[i]))
        encodings += 1
        if len(packed) < len(winner):
            winner, boundary_change = packed, 1
    if winner is not seed and apply(old, unpack(winner)) != new:
        raise RuntimeError("an accepted edit failed independent reconstruction")
    return winner, Audit(len(controls), control_bytes, len(seed), scanned, len(copies), remaps,
                         boundary_change, encodings)

"""Byte-exact deltas with independent wheel expansion and PE normalization."""

from dataclasses import dataclass
from hashlib import sha256
from struct import Struct
from zipfile import BadZipFile
import zlib

from workhorses import delta_archive as archive, delta_context as context, delta_pe as pe, delta_xz as xz
from workhorses import delta_record
from workhorses.delta import MAX_TARGET, _header, apply, unpack


MAGIC = b"WDP3"
PREFIX_MAGIC = b"PFXD0001"
HEADER = Struct("<4sI")
RECIPE = Struct("<32s32sHHBB")
SOURCE = Struct("<IIIB")
TARGET = Struct("<IIIBBI")
PREFIX = Struct("<8sIIIB")
TRANSFORMS = (None, *pe.MODES)
HINT_LIMIT = 4 << 20
RECIPE_LIMIT = RECIPE.size + 255 + archive.MAX_MEMBERS * (SOURCE.size + TARGET.size) + HINT_LIMIT


def _same_payload(old, new, before, after, name):
    if name not in before or name not in after:
        return False
    a, m, _ = before[name]
    b, n, _ = after[name]
    return m == n and memoryview(old)[a:a + m] == memoryview(new)[b:b + n]


def _transform(contents, mode):
    if mode and len(contents) >= 64 << 10 and contents.startswith(b"MZ"):
        try:
            data, bitmap = pe.normalize(contents, mode)
            return data, 1, bitmap
        except ValueError:
            pass
    return contents, 0, b""


def _level(contents, encoded, miniz):
    level = next((i for i in archive.LEVELS if archive._deflate(contents, i) == encoded), None)
    if level is None and miniz:
        level = next((archive.MINIZ_LEVEL + i for i in archive.LEVELS
                      if archive._miniz_deflate(contents, i, len(encoded)) == encoded), None)
    return level


@dataclass(frozen=True)
class WheelInput:
    source: bytes
    target: bytes
    metadata: bytes
    source_members: int
    target_members: int
    native_members: int
    split_candidates: tuple[int, ...]

    def wrap(self, patch):
        return HEADER.pack(MAGIC, len(self.metadata)) + self.metadata + patch


def prepare(old: bytes, new: bytes, *, mode=None, independent=True, miniz=True,
            minimum_size=1024, expand_unchanged=False, record_hashes=False) -> WheelInput | None:
    if mode not in TRANSFORMS:
        raise ValueError("invalid wheel PE transform")
    if not isinstance(minimum_size, int) or not 1 <= minimum_size <= MAX_TARGET:
        raise ValueError("invalid minimum size for expanded members")
    if max(len(old), len(new)) > MAX_TARGET:
        return None
    try:
        before = archive._members(old, minimum_size=minimum_size)
        after = archive._members(new, minimum_size=minimum_size)
    except (BadZipFile, ValueError):
        return None
    shared = before.keys() & after.keys()
    same = (set() if expand_unchanged else
            {name for name in shared if _same_payload(old, new, before, after, name)})
    candidates = after.keys() if independent else shared
    chosen = set()
    source_items, target_items, hints, ends = [], [], bytearray(), []
    source_spans, target_spans = [], []
    shift = native = 0
    uses_miniz = False
    for name in sorted(candidates - same, key=lambda k: after[k][0]):
        at, size, raw = after[name]
        if len(target_spans) == archive.MAX_MEMBERS:
            break
        if len(new) + shift + raw - size > MAX_TARGET:
            continue
        try:
            member = archive._member(new, at, size, raw)
        except ValueError:
            continue
        level = _level(member, memoryview(new)[at:at + size], miniz)
        if level is None:
            continue
        transformed, native_flag, bitmap = _transform(member, mode)
        if record_hashes and delta_record.is_record(name):
            masked = delta_record.substitute(member, new, at)
            if masked is not None:
                transformed, native_flag = masked, 2
        if len(hints) + len(bitmap) > HINT_LIMIT:
            transformed, native_flag, bitmap = member, 0, b""
        target_spans.append(TARGET.pack(at + shift, raw, size, level, native_flag, len(bitmap)))
        target_items.append((at, size, transformed))
        hints.extend(bitmap)
        if raw >= 64 << 10 and member.startswith(b"MZ"):
            ends.append(at + shift + raw)
        shift += raw - size
        uses_miniz |= level >= archive.MINIZ_LEVEL
        native += native_flag == 1
        chosen.add(name)
    if not target_items or (mode is not None and not native):
        return None
    shift = 0
    for name in sorted((before.keys() if independent else chosen) - same, key=lambda k: before[k][0]):
        at, size, raw = before[name]
        if len(source_spans) == archive.MAX_MEMBERS:
            break
        if len(old) + shift + raw - size > MAX_TARGET:
            continue
        try:
            member = archive._member(old, at, size, raw)
        except ValueError:
            continue
        transformed, native_flag, _ = _transform(member, mode)
        if record_hashes and delta_record.is_record(name):
            masked = delta_record.substitute(member, old, at)
            if masked is not None:
                transformed, native_flag = masked, 2
        source_spans.append(SOURCE.pack(at, size, raw, native_flag))
        source_items.append((at, size, transformed))
        shift += raw - size
    try:
        left = archive._splice(old, source_items)
        right = archive._splice(new, target_items)
    except ValueError:
        return None
    version = archive.VERSION_MINIZ if uses_miniz else archive.VERSION
    header = RECIPE.pack(sha256(old).digest(), sha256(new).digest(), len(source_items), len(target_items),
                         len(version), TRANSFORMS.index(mode))
    metadata = zlib.compress(b"".join((header, version, *source_spans, *target_spans, hints)), 9)
    return WheelInput(left, right, metadata, len(source_items), len(target_items), native, tuple(ends[:-1]))


def prefix_patch(size, cut, reverse, first, second):
    return PREFIX.pack(PREFIX_MAGIC, size, cut, len(first), reverse) + first + second


def _simple_size(patch):
    return xz.target_size(patch) if patch.startswith((xz.MAGIC, xz.COMPACT)) else _header(patch)[0]


def _simple_apply(source, patch):
    return apply(source, xz.decode(patch) if patch.startswith((xz.MAGIC, xz.COMPACT)) else unpack(patch))


def _prefix_parts(patch):
    if len(patch) < PREFIX.size:
        raise ValueError("truncated prefix delta")
    magic, size, cut, first, reverse = PREFIX.unpack_from(patch)
    split = PREFIX.size + first
    if magic != PREFIX_MAGIC or not 0 < cut < size <= MAX_TARGET or not PREFIX.size < split < len(patch) or reverse not in (0, 1):
        raise ValueError("invalid prefix delta lengths")
    parts = patch[PREFIX.size:split], patch[split:]
    expected = (size - cut, cut) if reverse else (cut, size - cut)
    if tuple(_simple_size(item) for item in parts) != expected:
        raise ValueError("prefix chunks differ from their recorded sizes")
    return size, parts, reverse


def _payload_size(patch):
    if patch.startswith(PREFIX_MAGIC):
        return _prefix_parts(patch)[0]
    if patch.startswith(context.MAGIC):
        return context.target_size(patch)
    return _simple_size(patch)


def _apply_payload(source, patch):
    if patch.startswith(PREFIX_MAGIC):
        _, (first, second), reverse = _prefix_parts(patch)
        prefix = _simple_apply(source, first)
        suffix = _simple_apply(source + prefix, second)
        return suffix + prefix if reverse else prefix + suffix
    if patch.startswith(context.MAGIC):
        return context.apply_patch(source, patch)
    return _simple_apply(source, patch)


def apply_patch(old: bytes, patch: bytes) -> bytes:
    if not patch.startswith(MAGIC):
        return archive.apply_archive_patch(old, patch)
    if len(patch) < HEADER.size or max(len(old), len(patch)) > 2 * MAX_TARGET or len(old) > MAX_TARGET:
        raise ValueError("invalid wheel patch size")
    _, metadata_size = HEADER.unpack_from(patch)
    start = HEADER.size + metadata_size
    if start >= len(patch) or metadata_size > 2 * RECIPE_LIMIT:
        raise ValueError("invalid wheel metadata size")
    metadata = archive._inflate(patch[HEADER.size:start], RECIPE_LIMIT, 15)
    if len(metadata) < RECIPE.size:
        raise ValueError("truncated wheel metadata")
    src_hash, dst_hash, nsrc, ndst, version_size, mode_index = RECIPE.unpack_from(metadata)
    if sha256(old).digest() != src_hash:
        raise ValueError("wheel source hash differs")
    first = RECIPE.size + version_size
    boundary = first + nsrc * SOURCE.size
    end = boundary + ndst * TARGET.size
    if (not 0 <= nsrc <= archive.MAX_MEMBERS or not 1 <= ndst <= archive.MAX_MEMBERS
            or mode_index >= len(TRANSFORMS) or end > len(metadata)):
        raise ValueError("invalid wheel recipe layout")
    mode = TRANSFORMS[mode_index]
    version = metadata[RECIPE.size:first]
    if version not in (archive.VERSION, archive.VERSION_MINIZ):
        raise ValueError("wheel patch requires the recorded DEFLATE runtime")
    allowed = archive.LEVELS
    if version == archive.VERSION_MINIZ:
        allowed = (*allowed, *(archive.MINIZ_LEVEL + k for k in allowed))
    source_records = list(SOURCE.iter_unpack(metadata[first:boundary]))
    target_records = list(TARGET.iter_unpack(metadata[boundary:end]))
    if any(flag not in (0, 1, 2) or (flag == 1 and mode is None) for _, _, _, flag in source_records):
        raise ValueError("invalid source member transform")
    if sum(r[3] == 2 for r in source_records) > 1 or sum(r[4] == 2 for r in target_records) > 1:
        raise ValueError("multiple wheel RECORD transforms")
    if any(level not in allowed or flag not in (0, 1, 2) or (flag == 1 and mode is None) or (length and flag != 1)
           for _, _, _, level, flag, length in target_records):
        raise ValueError("invalid target compression or PE mode")
    hint_size = sum(r[-1] for r in target_records)
    if hint_size != len(metadata) - end or hint_size > HINT_LIMIT:
        raise ValueError("PE hints differ from their recorded lengths")
    payload = patch[start:]
    archive._check_layout(len(old), (entry[:3] for entry in source_records))
    archive._check_layout(_payload_size(payload), (entry[:3] for entry in target_records))

    def expand():
        for offset, encoded, length, flag in source_records:
            blob = archive._member(old, offset, encoded, length)
            if flag == 1:
                blob, _ = pe.normalize(blob, mode)
            elif flag == 2:
                blob = delta_record.substitute(blob, old, offset)
                if blob is None:
                    raise ValueError("source RECORD cannot be normalized")
            yield offset, encoded, blob

    normalized = _apply_payload(archive._splice(old, expand()), payload)

    def compress(member, level, encoded):
        result = (archive._miniz_deflate(member, level - archive.MINIZ_LEVEL, encoded)
                  if level >= archive.MINIZ_LEVEL else archive._deflate(member, level))
        if result is None or len(result) != encoded:
            raise ValueError("wheel compressor produced a different size")
        return result

    deferred = []

    def encode():
        cursor = end
        shift = 0
        for offset, length, encoded, level, flag, count in target_records:
            member = normalized[offset:offset + length]
            if flag == 1:
                member = pe.recover(member, mode, metadata[cursor:cursor + count])
            cursor += count
            if flag == 2:
                deferred.append((offset + shift, encoded, level, member))
                yield offset, length, bytes(encoded)
            else:
                yield offset, length, compress(member, level, encoded)
            shift += encoded - length

    target = archive._splice(normalized, encode())
    if deferred:
        at, size, level, masked = deferred[0]
        record = delta_record.substitute(masked, target, at, restore=True)
        if record is None:
            raise ValueError("target RECORD cannot be reconstructed")
        target = archive._splice(target, [(at, size, compress(record, level, size))])
    if sha256(target).digest() != dst_hash:
        raise ValueError("reconstructed wheel hash differs")
    return target

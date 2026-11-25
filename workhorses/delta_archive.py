"""Normalize reproducible ZIP member payloads before generating a binary delta."""

from collections import Counter
import ctypes as ct
from dataclasses import dataclass
from functools import lru_cache
from hashlib import sha256
from io import BytesIO
from pathlib import Path
import struct
from zipfile import BadZipFile, ZIP_DEFLATED, ZipFile
import zlib

from workhorses import delta_xz
from workhorses.delta import MAGIC as BSDIFF_MAGIC, MAX_TARGET, _header, apply, unpack


MAGIC = b"ZIPD"
MAGIC_EXTENDED = b"ZIP2"
LOCAL = struct.Struct("<4s22xHH")
HEADER = struct.Struct("<4sI")
RECIPE = struct.Struct("<32s32sHB")
SOURCE = struct.Struct("<III")
TARGET = struct.Struct("<IIIB")
MAX_MEMBERS = 4096
VERSION = zlib.ZLIB_RUNTIME_VERSION.encode("ascii")
VERSION_MINIZ = VERSION + b"|miniz_oxide-0.8.2"
LEVELS = (6, 9, 1, 2, 3, 4, 5, 7, 8)
MINIZ_LEVEL = 16
MINIZ_DLL = Path(__file__).resolve().parents[1] / ".research" / "bin" / "wheel_deflate.dll"


def _deflate(payload, level):
    stream = zlib.compressobj(level, zlib.DEFLATED, -15)
    return stream.compress(payload) + stream.flush()


@lru_cache(maxsize=1)
def _miniz_routine():
    library = ct.CDLL(str(MINIZ_DLL))
    routine = library.wheel_miniz_deflate
    routine.argtypes = (ct.c_char_p, ct.c_size_t, ct.c_uint8, ct.c_void_p, ct.c_size_t)
    routine.restype = ct.c_size_t
    return routine


def _miniz_deflate(payload, level, size):
    payload = bytes(payload)
    output = ct.create_string_buffer(size)
    if _miniz_routine()(payload, len(payload), level, output, size) != size:
        return None
    return output.raw


def _inflate(blob, limit, window):
    stream = zlib.decompressobj(window)
    try:
        decoded = stream.decompress(blob, limit + 1)
    except zlib.error as error:
        raise ValueError("invalid compressed archive payload") from error
    if len(decoded) > limit or not stream.eof or stream.unused_data:
        raise ValueError("archive payload has an invalid length or trailing data")
    return decoded


def _members(data, *, minimum_size=1024):
    with ZipFile(BytesIO(data)) as archive:
        entries = archive.infolist()
    counts = Counter(e.filename for e in entries)
    result = {}
    for entry in entries:
        if (entry.compress_type != ZIP_DEFLATED or entry.flag_bits & 1 or counts[entry.filename] != 1
                or not minimum_size <= entry.file_size <= MAX_TARGET):
            continue
        offset = entry.header_offset
        if offset < 0 or offset + LOCAL.size > len(data):
            raise ValueError("invalid local ZIP member header")
        signature, name_bytes, extra_bytes = LOCAL.unpack_from(data, offset)
        start = offset + LOCAL.size + name_bytes + extra_bytes
        if signature != b"PK\x03\x04" or start + entry.compress_size > len(data):
            raise ValueError("invalid ZIP member location")
        result[entry.filename] = (start, entry.compress_size, entry.file_size)
    return result


def _member(data, offset, size, unpacked):
    if offset + size > len(data) or unpacked > MAX_TARGET:
        raise ValueError("member exceeds the archive size bound")
    contents = _inflate(memoryview(data)[offset:offset + size], unpacked, -15)
    if len(contents) != unpacked:
        raise ValueError("member size differs from its recipe")
    return contents


def _splice(data, entries):
    fragments, prev, total = [], 0, len(data)
    for start, size, replacement in entries:
        if start < prev or start + size > len(data):
            raise ValueError("invalid or overlapping archive spans")
        fragments.extend((memoryview(data)[prev:start], replacement))
        total += len(replacement) - size
        if total > MAX_TARGET:
            raise ValueError("archive transform exceeds the size budget")
        prev = start + size
    return b"".join((*fragments, memoryview(data)[prev:]))


def _check_layout(size, spans):
    end, total = 0, size
    for start, length, replacement in spans:
        if start < end or length == 0 or replacement == 0 or start + length > size:
            raise ValueError("invalid, overlapping, or empty member in archive recipe")
        end = start + length
        total += replacement - length
        if total > MAX_TARGET:
            raise ValueError("archive recipe exceeds the transform budget")


@dataclass(frozen=True)
class ArchiveDeltaInput:
    source: bytes
    target: bytes
    metadata: bytes
    members: int
    magic: bytes = MAGIC

    def wrap(self, patch):
        magic = MAGIC_EXTENDED if patch.startswith((delta_xz.MAGIC, delta_xz.COMPACT)) else self.magic
        return HEADER.pack(magic, len(self.metadata)) + self.metadata + patch


def normalize(old: bytes, new: bytes, *, miniz=False) -> ArchiveDeltaInput | None:
    """Expose changed DEFLATE members that the selected codecs reproduce exactly."""
    if len(old) > MAX_TARGET or len(new) > MAX_TARGET:
        return None
    try:
        a, b = _members(old), _members(new)
    except (BadZipFile, ValueError):
        return None
    old_entries, new_entries = [], []
    source_size, target_size = len(old), len(new)
    for name in sorted(a.keys() & b.keys(), key=lambda n: b[n][0]):
        source, target = a[name], b[name]
        lo, size, raw = target
        src, nsrc, old_raw = source
        encoded = memoryview(new)[lo:lo + size]
        if memoryview(old)[src:src + nsrc] == encoded:
            continue
        if source_size + old_raw - nsrc > MAX_TARGET or target_size + raw - size > MAX_TARGET:
            continue
        try:
            content = _member(new, *target)
            level = next((k for k in LEVELS if _deflate(content, k) == encoded), None)
            if level is None and miniz:
                level = next((MINIZ_LEVEL + k for k in LEVELS
                              if _miniz_deflate(content, k, size) == encoded), None)
            if level is None:
                continue
            prior = _member(old, *source)
        except ValueError:
            continue
        if len(old_entries) == MAX_MEMBERS:
            break
        old_entries.append((src, nsrc, prior))
        new_entries.append((lo, size, content, level))
        source_size += old_raw - nsrc
        target_size += raw - size
    if not old_entries:
        return None
    old_entries.sort(key=lambda item: item[0])
    try:
        before = _splice(old, old_entries)
        after = _splice(new, ((pos, size, data) for pos, size, data, _ in new_entries))
    except ValueError:
        return None
    uses_miniz = any(level >= MINIZ_LEVEL for _, _, _, level in new_entries)
    version = VERSION_MINIZ if uses_miniz else VERSION
    metadata = bytearray(RECIPE.pack(sha256(old).digest(), sha256(new).digest(), len(old_entries), len(version)))
    metadata.extend(version)
    for pos, size, data in old_entries:
        metadata.extend(SOURCE.pack(pos, size, len(data)))
    shift = 0
    for pos, size, data, level in new_entries:
        metadata.extend(TARGET.pack(pos + shift, len(data), size, level))
        shift += len(data) - size
    return ArchiveDeltaInput(before, after, zlib.compress(metadata, 9), len(old_entries),
                             MAGIC_EXTENDED if uses_miniz else MAGIC)


def apply_archive_patch(old: bytes, patch: bytes) -> bytes:
    if patch.startswith(BSDIFF_MAGIC):
        return apply(old, unpack(patch))
    if patch.startswith((delta_xz.MAGIC, delta_xz.COMPACT)):
        return apply(old, delta_xz.decode(patch))
    if len(patch) < HEADER.size:
        raise ValueError("truncated archive patch header")
    if len(old) > MAX_TARGET:
        raise ValueError("archive patch source exceeds the size budget")
    magic, metadata_size = HEADER.unpack_from(patch)
    payload_start = HEADER.size + metadata_size
    limit = RECIPE.size + 255 + MAX_MEMBERS * (SOURCE.size + TARGET.size)
    if magic not in (MAGIC, MAGIC_EXTENDED) or payload_start > len(patch) or metadata_size > 2 * limit:
        raise ValueError("invalid archive patch header")
    instructions = _inflate(memoryview(patch)[HEADER.size:payload_start], limit, 15)
    if len(instructions) < RECIPE.size:
        raise ValueError("truncated archive patch recipe")
    source_hash, target_hash, members, version_size = RECIPE.unpack_from(instructions)
    first = RECIPE.size + version_size
    if not 1 <= members <= MAX_MEMBERS or len(instructions) != first + members * (SOURCE.size + TARGET.size):
        raise ValueError("archive recipe record count does not match its length")
    if sha256(old).digest() != source_hash:
        raise ValueError("archive patch source hash differs")
    version = instructions[RECIPE.size:first]
    has_miniz = magic == MAGIC_EXTENDED and version == VERSION_MINIZ
    if version != VERSION and not has_miniz:
        raise ValueError("archive patch requires the recorded DEFLATE runtime version")
    last_source = first + members * SOURCE.size
    source_spans = list(SOURCE.iter_unpack(instructions[first:last_source]))
    target_spans = list(TARGET.iter_unpack(instructions[last_source:]))
    payload = patch[payload_start:]
    allowed = (*LEVELS, *(MINIZ_LEVEL + k for k in LEVELS)) if has_miniz else LEVELS
    if any(level not in allowed for _, _, _, level in target_spans):
        raise ValueError("unsupported target compressor level")
    _check_layout(len(old), source_spans)
    use_xz = magic == MAGIC_EXTENDED and payload.startswith((delta_xz.MAGIC, delta_xz.COMPACT))
    size = delta_xz.target_size(payload) if use_xz else _header(payload)[0]
    _check_layout(size, (span[:3] for span in target_spans))
    transformed = _splice(old, ((pos, size, _member(old, pos, size, length)) for pos, size, length in source_spans))
    normalized = apply(transformed, delta_xz.decode(payload) if use_xz else unpack(payload))

    def recompressed():
        for start, length, size, level in target_spans:
            data = memoryview(normalized)[start:start + length]
            encoded = (_miniz_deflate(data, level - MINIZ_LEVEL, size)
                       if level >= MINIZ_LEVEL else _deflate(data, level))
            if encoded is None or len(encoded) != size:
                raise ValueError("target DEFLATE output does not match the recorded size")
            yield start, length, encoded

    result = _splice(normalized, recompressed())
    if sha256(result).digest() != target_hash:
        raise ValueError("reconstructed ZIP hash differs from target")
    return result

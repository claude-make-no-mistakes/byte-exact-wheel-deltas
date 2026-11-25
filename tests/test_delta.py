import bz2
from base64 import urlsafe_b64encode
from hashlib import sha256
import ctypes as ct
from dataclasses import replace
from io import BytesIO
import lzma
from pathlib import Path
import random
import struct
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch as mock_patch
from zipfile import ZIP_DEFLATED, ZipFile
import zlib

from benchmarks.common import BIN
from benchmarks.followup_delta import reference_boundary
from workhorses import delta_archive, delta_context, delta_pe, delta_record, delta_wheel, delta_xz
from workhorses.delta import Instruction, Patch, _pack, _rewrite_boundary, apply, optimize, pack, unpack
from workhorses.delta_archive import apply_archive_patch, normalize
from workhorses.delta_hybrid import combine


class DeltaTests(unittest.TestCase):
    def test_record_can_precede_members_and_preserves_unrecognized_fields(self):
        def wheel(value, version, reserved=False):
            size = str(len(value)).encode()
            info = b"example-" + version + b".dist-info/RECORD"
            digest = urlsafe_b64encode(sha256(value).digest()).rstrip(b"=")
            correct = b"lib.py,sha256=" + digest + b"," + size + b"\r\n"
            incorrect = b"lib.py,sha256=" + b"A" * 43 + b"," + size + b"\n"
            special = b'"file,1.py",sha256=' + digest + b"," + size + b"\n"
            out = BytesIO()
            with ZipFile(out, "w", ZIP_DEFLATED) as z:
                z.writestr(info.decode(), correct + incorrect + special + info + b",,\n" +
                           (delta_record.MARK if reserved else b""))
                z.writestr("lib.py", value)
                z.writestr("file,1.py", value)
            return out.getvalue()

        old, new = wheel(b"hello" * 200, b"1"), wheel(b"goodbye" * 200, b"2")
        job = delta_wheel.prepare(old, new, record_hashes=True, miniz=False, minimum_size=1)
        self.assertEqual(job.source.count(delta_record.MARK), 1)
        self.assertEqual(job.target.count(delta_record.MARK), 1)
        def literal(data):
            return job.wrap(pack(Patch(len(data), (Instruction(0, len(data), 0),), b"", data)))
        # The independently created ZIP file includes the literal original RECORD and its CRC.
        self.assertEqual(delta_wheel.apply_patch(old, literal(job.target)), new)
        with self.assertRaisesRegex(ValueError, "RECORD|hash"):
            delta_wheel.apply_patch(old, literal(job.target.replace(b"lib.py,sha256=!", b"bad.py,sha256=!")))
        reserved = wheel(b"new" * 200, b"3", reserved=True)
        opaque = delta_wheel.prepare(old, reserved, record_hashes=True, miniz=False, minimum_size=1)
        self.assertNotIn(b"sha256=" + delta_record.MARK, opaque.target)

    def test_wheel_expands_a_new_member_and_checks_chained_chunks(self):
        def archive(include_new):
            out = BytesIO()
            with ZipFile(out, "w", compression=ZIP_DEFLATED) as wheel:
                wheel.writestr("stable.py", b"old\n" * 400)
                if include_new:
                    wheel.writestr("added.py", b"new and similar\n" * 300)
            return out.getvalue()

        old, target = archive(False), archive(True)
        job = delta_wheel.prepare(old, target, miniz=False)
        self.assertEqual((job.source_members, job.target_members), (0, 1))

        def literal(data):
            return delta_xz.recode(pack(Patch(len(data), (Instruction(0, len(data), 0),), b"", data)))

        cut = len(job.target) // 2
        payload = delta_wheel.prefix_patch(len(job.target), cut, True, literal(job.target[cut:]), literal(job.target[:cut]))
        patch = job.wrap(payload)
        self.assertEqual(delta_wheel.apply_patch(old, patch), target)
        with self.assertRaisesRegex(ValueError, "source hash"):
            delta_wheel.apply_patch(old + b"x", patch)
        malformed = delta_wheel.prefix_patch(len(job.target), cut + 1, True, literal(job.target[cut:]),
                                             literal(job.target[:cut]))
        with self.assertRaisesRegex(ValueError, "chunks differ"):
            delta_wheel.apply_patch(old, job.wrap(malformed))

    def test_context_delta_can_reference_recovered_bytes_before_literals(self):
        original = b"ABCD"
        seed = pack(Patch(8, (Instruction(4, 4, 0),), bytes([1, 1, 1, 1]), b"BCDE"))

        def encode(reference, literal):
            self.assertEqual(reference, b"ABCDBCDE")
            patch = Patch(len(literal), (Instruction(0, 0, 4), Instruction(4, 0, 0)), bytes(4), b"")
            return delta_xz.recode(pack(patch))

        context = delta_context.recode(original, seed, encode)
        self.assertEqual(delta_context.apply_patch(original, context), b"BCDEBCDE")

    def test_variable_width_control_stream_preserves_negative_seeks(self):
        rows = (Instruction(2, 1, -3), Instruction(1, 1, (1 << 63) - 1))
        control = delta_xz._compact_controls(rows)
        streams = [lzma.compress(data, filters=delta_xz.FILTERS) for data in (control, bytes(3), b"!x")]
        wire = _pack(5, *streams, magic=delta_xz.COMPACT)
        parsed = delta_xz.decode(wire)
        self.assertEqual(parsed.instructions, rows)
        self.assertEqual(apply(b"ok", parsed), b"ok!\0x")
        with self.assertRaises(ValueError):
            delta_xz._restore_controls(bytes([0xFF] * 10), 1)

    def test_pe_function_references_and_tables_round_trip(self):
        pe = bytearray(1536)
        pe[:2] = b"MZ"
        struct.pack_into("<I", pe, 60, 64)
        pe[64:68] = b"PE\0\0"
        struct.pack_into("<HH", pe, 68, 0x8664, 2)
        struct.pack_into("<H", pe, 84, 240)
        struct.pack_into("<H", pe, 88, 0x20B)
        struct.pack_into("<I", pe, 88 + 108, 16)
        struct.pack_into("<II", pe, 88 + 112 + 8 * 3, 8192, 24)
        struct.pack_into("<III", pe, 328 + 12, 4096, 512, 512)
        struct.pack_into("<I", pe, 328 + 36, 0x20)
        struct.pack_into("<III", pe, 368 + 12, 8192, 512, 1024)
        pe[368:376] = b".rdata\0\0"
        struct.pack_into("<III", pe, 1024, 4096, 4102, 8256)
        struct.pack_into("<III", pe, 1036, 4352, 4360, 8272)
        struct.pack_into("<BiB", pe, 512, 0xE8, 4352 - 4101, 0xC3)
        pe[768:771] = b"\x48\x8d\x05"
        struct.pack_into("<iB", pe, 771, 8300 - 4359, 0xC3)
        pe[1132:1144] = b"opaque bytes"
        original = bytes(pe)
        for mode in delta_pe.MODES:
            cooked, hints = delta_pe.normalize(original, mode)
            self.assertNotEqual(cooked[1024:1048], original[1024:1048])
            if mode.startswith("labels"):
                self.assertEqual(hints, b"\x03" if mode == "labels-data" else b"\x01")
                self.assertNotEqual(cooked[513:517], original[513:517])
            self.assertEqual(delta_pe.recover(cooked, mode, hints), original)

    def test_duplicate_source_offsets_reuse_the_data_streams(self):
        block = bytes(range(64))
        old = block * 40
        offsets = [0, 960, 192, 1792, 128, 640, 1920, 1728, 512, 64, 0]
        instructions = tuple(Instruction(64, 0, nxt - pos - 64)
                             for pos, nxt in zip(offsets, offsets[1:]))
        new = block * len(instructions)
        seed = pack(Patch(len(new), instructions, bytes(len(new)), b""))
        patch, stats = optimize(old, new, seed)
        result = unpack(patch)
        # All old-file blocks contain exactly the same bytes at every offset.
        self.assertEqual(apply(old, result), new)
        self.assertEqual(result.difference, bytes(len(new)))
        self.assertEqual(result.literal, b"")
        self.assertLess(len(patch), len(seed))
        self.assertGreater(stats.remaps, 0)

    def test_boundary_reparse_gain_and_reference_decoder(self):
        old = bytes((n * 71) % 256 for n in range(3072))
        new = old[:2048]
        seed = pack(Patch(len(new), (Instruction(1024, 1024, 0),), bytes(1024), new[1024:]))
        patch, stats = optimize(old, new, seed, shortlist=4)
        # All "literal" bytes were an exact match in the old file. Turning a
        # prefix into residual zeros makes a shorter compressed encoding.
        self.assertEqual(apply(old, unpack(patch)), new)
        self.assertLess(len(patch), len(seed))
        self.assertEqual(stats.boundary_changes, 1)
        if (BIN / "bspatch.exe").is_file():
            with TemporaryDirectory() as tmp:
                source, encoded, rebuilt = [Path(tmp) / name for name in ("source", "patch", "rebuilt")]
                source.write_bytes(old)
                encoded.write_bytes(patch)
                subprocess.run([str(BIN / "bspatch.exe"), str(source), str(rebuilt), str(encoded)],
                               check=True, timeout=5)
                self.assertEqual(rebuilt.read_bytes(), new)

    def test_negative_source_offsets_and_empty_output(self):
        seed = Patch(3, (Instruction(0, 0, -2), Instruction(3, 0, 0)), b"\x07\x08\x02", b"")
        # BSDIFF40 treats old positions -2 and -1 as zero and adds old[0] to byte 2.
        self.assertEqual(apply(b"\x05", unpack(pack(seed))), b"\x07\x08\x07")
        empty = pack(Patch(0, (), b"", b""))
        self.assertEqual(optimize(b"abc", b"", empty)[0], empty)

    def test_boundary_splicing_keeps_raw_bytes_before_and_after_an_edited_record(self):
        rng = random.Random(181)
        old = rng.randbytes(120)
        controls = (Instruction(12, 16, 11), Instruction(15, 17, -5), Instruction(20, 10, 0))
        seed = Patch(90, controls, rng.randbytes(47), rng.randbytes(43))
        new = apply(old, seed)
        for shift in (-8, 8):
            # Instruction 1 starts at source byte 23. It ends at raw diff
            # cursor 27 and starts at raw literal cursor 16 by prefix sums.
            arguments = (old, new, seed, controls, 1, shift, 23, 27, 16)
            self.assertEqual(_rewrite_boundary(*arguments), reference_boundary(*arguments))
            self.assertEqual(apply(old, _rewrite_boundary(*arguments)), new)

    def test_hybrid_uses_the_better_parse_on_both_sides_of_a_window_boundary(self):
        width = 16 << 10
        old = random.Random(720).randbytes(2 * width)
        left = pack(Patch(len(old), (Instruction(width, width, 0),), bytes(width), old[width:]))
        right = pack(Patch(len(old), (Instruction(0, width, width), Instruction(width, 0, 0)),
                           bytes(width), old[:width]))
        product, audit = combine(old, old, left, right, window=width)
        # The best hybrid copies both halves from the unchanged file, so the
        # assembled difference stream contains only zeros and no literals.
        result = unpack(product)
        self.assertEqual(result.difference, bytes(len(old)))
        self.assertEqual(result.literal, b"")
        self.assertEqual(apply(old, result), old)
        self.assertLess(len(product), min(len(left), len(right)))
        self.assertEqual(audit.winning_switches, 1)

    def test_archive_delta_reconstructs_exact_zip_bytes_and_checks_source(self):
        def archive(entries):
            out = BytesIO()
            with ZipFile(out, "w", compression=ZIP_DEFLATED, compresslevel=6) as stream:
                for name, body in entries:
                    stream.writestr(name, body)
                stream.writestr("package/unchanged", b"stable")
            return out.getvalue()

        first = b"repeated content\n" * 2048
        second = random.Random(351).randbytes(4096)
        old = archive([("package/first", first), ("package/second", second)])
        target = archive([("package/second", second[:-15] + b"new payload"), ("package/first", first + b"more")])
        normalized = normalize(old, target)
        self.assertIsNotNone(normalized)
        self.assertEqual(normalized.members, 2)
        residual = bytes((b - a) % 256 for a, b in zip(normalized.source, normalized.target))
        tail = normalized.target[len(residual):]
        inner = pack(Patch(len(normalized.target), (Instruction(len(residual), len(tail), 0),), residual, tail))
        # The outer recipe must return the actual ZIP stream, including header,
        # offsets, compressor bitstream, and untouched member bytes.
        patch = normalized.wrap(inner)
        self.assertEqual(apply_archive_patch(old, patch), target)
        self.assertEqual(apply_archive_patch(old, normalized.wrap(delta_xz.recode(inner))), target)
        with self.assertRaises(ValueError):
            apply_archive_patch(old + b"x", patch)

        metadata = zlib.decompress(normalized.metadata)
        first = delta_archive.RECIPE.size + len(delta_archive.VERSION)
        target_first = first + 2 * delta_archive.SOURCE.size
        source_start, source_size, _ = delta_archive.SOURCE.unpack_from(metadata, first)
        target_start, _, _, _ = delta_archive.TARGET.unpack_from(metadata, target_first)
        modifications = ((delta_archive.RECIPE.size, b"!", "runtime version"),
                         (first + delta_archive.SOURCE.size, source_start.to_bytes(4, "little"), "overlapping"),
                         (target_first + delta_archive.TARGET.size, target_start.to_bytes(4, "little"), "overlapping"),
                         (first + 8, delta_archive.MAX_TARGET.to_bytes(4, "little"), "budget"),
                         (first + 4, (source_size + len(old)).to_bytes(4, "little"), "invalid"))
        for offset, inserted, message in modifications:
            damaged = metadata[:offset] + inserted + metadata[offset + len(inserted):]
            modified = replace(normalized, metadata=zlib.compress(damaged)).wrap(inner)
            with mock_patch("workhorses.delta_archive._member", side_effect=AssertionError("should preflight first")):
                with self.assertRaisesRegex(ValueError, message):
                    apply_archive_patch(old, modified)
        damaged = metadata[:32] + bytes([metadata[32] ^ 1]) + metadata[33:]
        with self.assertRaisesRegex(ValueError, "hash differs from target"):
            apply_archive_patch(old, replace(normalized, metadata=zlib.compress(damaged)).wrap(inner))

    @unittest.skipUnless(delta_archive.MINIZ_DLL.is_file(), "build the native archive codec")
    def test_archive_round_trips_raw_miniz_streams(self):
        body = bytes(range(251)) * 21 + random.Random(252).randbytes(8096)

        class Compressor:
            def __init__(self):
                self.payload = bytearray()

            def compress(self, data):
                self.payload.extend(data)
                return b""

            def flush(self):
                data = bytes(self.payload)
                space = ct.create_string_buffer(2 * len(data))
                count = delta_archive._miniz_routine()(data, len(data), 6, space, len(space))
                return space.raw[:count]

        def archive(payload):
            stream = BytesIO()
            with ZipFile(stream, "w", compression=ZIP_DEFLATED) as wheel:
                wheel.writestr("module.pyd", payload)
            return stream.getvalue()

        old = archive(body)
        with mock_patch("zipfile._get_compressor", side_effect=lambda *args: Compressor()):
            new = archive(body[::-1])
        self.assertIsNone(normalize(old, new))
        job = normalize(old, new, miniz=True)
        self.assertEqual(job.magic, delta_archive.MAGIC_EXTENDED)
        only_literal = pack(Patch(len(job.target), (Instruction(0, len(job.target), 0),), b"", job.target))
        self.assertEqual(apply_archive_patch(old, job.wrap(delta_xz.recode(only_literal))), new)

    def test_xz_delta_rejects_truncated_and_concatenated_streams(self):
        inner = delta_xz.recode(pack(Patch(3, (Instruction(0, 3, 0),), b"", b"abc")))
        self.assertEqual(apply_archive_patch(b"", inner), b"abc")
        for invalid in (inner[:-1], inner + inner[32:]):
            with self.subTest(length=len(invalid)), self.assertRaises(ValueError):
                delta_xz.decode(invalid)

    def test_rejects_malformed_or_mismatched_streams(self):
        seed = pack(Patch(3, (Instruction(0, 3, 0),), b"", b"abc"))
        for bad in (seed[:24], seed[:-1], seed + bz2.compress(b""), b"BSDIFF30" + seed[8:]):
            with self.subTest(length=len(bad)), self.assertRaises((ValueError, OSError)):
                unpack(bad)
        with self.assertRaises(ValueError):
            optimize(b"", b"wrong", seed)


if __name__ == "__main__":
    unittest.main()

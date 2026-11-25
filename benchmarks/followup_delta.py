"""Measure local BSDIFF40 stream edits and hybrid encoder portfolios."""

import argparse
from contextlib import nullcontext
from dataclasses import asdict, replace
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter, process_time
import tracemalloc
from unittest.mock import patch

import numpy as np

from benchmarks.common import BIN, ROOT, WORK, schedule
from benchmarks.delta import _encode_command, _timed_call, _verify
from benchmarks.fetch import sha256
from benchmarks.paper_common import Journal, portable_file
from benchmarks.paper_data import CACHE, _delta, check_frozen, rank
from benchmarks.paper_delta import _common_member, prepare
from workhorses.delta import Patch, _positions, optimize
from workhorses.delta_hybrid import combine


FOLLOWUP = ROOT / "followup"
MANIFEST = FOLLOWUP / "inputs-delta.json"
PAIRS = (
    ("setuptools", "75.8.0", "76.0.0"), ("wheel", "0.45.0", "0.45.1"),
    ("certifi", "2025.1.31", "2025.6.15"), ("charset-normalizer", "3.4.1", "3.4.2"),
    ("sniffio", "1.3.0", "1.3.1"), ("pycparser", "2.22", "2.23"),
    ("colorama", "0.4.5", "0.4.6"), ("markdown-it-py", "3.0.0", "4.0.0"),
    ("lxml", "5.3.1", "5.4.0"), ("greenlet", "3.1.1", "3.2.2"),
    ("contourpy", "1.3.1", "1.3.2"), ("rpds-py", "0.24.0", "0.25.0"),
    ("zstandard", "0.23.0", "0.24.0"), ("xxhash", "3.5.0", "3.6.0"),
    ("multidict", "6.4.3", "6.4.4"), ("yarl", "1.18.3", "1.20.0"),
)
BASELINES = ("hdiff-bsd", "classic")
POST = ("boundary-reference", "boundary-splice", "hybrid")
SEED = 1_524_088
REPEATS = 3
TIMEOUT = 15


def fetch():
    if MANIFEST.exists():
        check_frozen(MANIFEST, restore=True, inputs_only=True)
        return
    frozen = _delta(PAIRS)
    with MANIFEST.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(frozen, indent=2, ensure_ascii=False) + "\n")
    print("Frozen PyPI pairs", len(frozen["cases"]), [r for r in frozen["cases"] if "error" in r], flush=True)


def reference_boundary(old, new, seed_patch, controls, i, shift, source, diff_end, literal_start):
    """Archived boundary rewrite that reconstructs both data streams in full."""
    current = controls[i]
    edited = replace(current, difference=current.difference + shift, literal=current.literal - shift,
                     seek=current.seek - shift)
    instructions = (*controls[:i], edited, *controls[i + 1:])
    old = np.frombuffer(old, dtype=np.uint8)
    new = np.frombuffer(new, dtype=np.uint8)
    diff, extra = bytearray(), bytearray()
    for (src, dst), instruction in zip(_positions(instructions), instructions):
        length = instruction.difference
        block = new[dst:dst + length].copy()
        lo, hi = max(0, -src), min(length, len(old) - src)
        if hi > lo:
            block[lo:hi] -= old[src + lo:src + hi]
        diff.extend(block.tobytes())
        extra.extend(new[dst + length:dst + length + instruction.literal].tobytes())
    return Patch(seed_patch.size, instructions, bytes(diff), bytes(extra))


def _post(method, a, b, inputs):
    if method == "hybrid":
        return combine(a, b, inputs["hdiff-bsd"], inputs["classic"])
    return optimize(a, b, inputs["hdiff-bsd"], shortlist=4, max_copies=64)


def _decoder(method, output, old, new, rebuilt):
    rebuilt.unlink(missing_ok=True)
    return _verify(method, old, new, output, rebuilt, TIMEOUT)


def _pair(old, new, scratch, *, seed):
    a, b = old.read_bytes(), new.read_bytes()
    output, rebuilt = scratch / "study.patch", scratch / "rebuilt.data"
    rows = []
    for repeat in range(REPEATS + 1):
        inputs, source_stats, post_outputs = {}, {}, {}
        for _, method in schedule(BASELINES, seed=seed + repeat, repeats=1):
            output.unlink(missing_ok=True)
            metrics = _timed_call(_encode_command(method, old, new, output), TIMEOUT)
            decoded = _decoder(method, output, old, new, rebuilt) if metrics["status"] == "ran" else None
            verified = bool(decoded and decoded["status"] == "verified")
            if verified:
                inputs[method] = output.read_bytes()
                source_stats[method] = metrics
            rows.append({"method": method, "repeat": repeat - 1, "verified": verified, "encoded": metrics,
                         "decoded": decoded, "bytes": len(inputs[method]) if verified else None})
        for _, method in schedule(POST, seed=seed - repeat, repeats=1):
            required = BASELINES if method == "hybrid" else ("hdiff-bsd",)
            if any(name not in inputs for name in required):
                rows.append({"method": method, "repeat": repeat - 1, "verified": False, "status": "missing_seed"})
                continue
            context = patch("workhorses.delta._rewrite_boundary", reference_boundary) if method == "boundary-reference" else nullcontext()
            try:
                with context:
                    wall, cpu = perf_counter(), process_time()
                    product, audit = _post(method, a, b, inputs)
                    wall, cpu = perf_counter() - wall, process_time() - cpu
                    peak = None
                    if repeat == 0:
                        # Separate, untimed allocation sample. This excludes
                        # native encoders and libbz2's internal memory.
                        tracemalloc.start()
                        try:
                            _post(method, a, b, inputs)
                            _, peak = tracemalloc.get_traced_memory()
                        finally:
                            tracemalloc.stop()
            except (ValueError, OverflowError, OSError, RuntimeError) as failure:
                rows.append({"method": method, "repeat": repeat - 1, "verified": False, "status": "postprocessor_failed",
                             "error": str(failure)})
                continue
            post_outputs[method] = product
            output.write_bytes(product)
            decoded = _decoder(method, output, old, new, rebuilt)
            row = {"method": method, "repeat": repeat - 1, "verified": decoded["status"] == "verified",
                   "decoded": decoded, "bytes": len(product), "post_seconds": wall, "post_cpu_seconds": cpu,
                   "peak_tracked_bytes": peak, "audit": asdict(audit),
                   "total_seconds": wall + sum(source_stats[m]["seconds"] for m in required)}
            rows.append(row)
        if {"boundary-reference", "boundary-splice"} <= post_outputs.keys():
            record = next(r for r in rows[-len(POST):] if r["method"] == "boundary-splice")
            record["boundary_byte_parity"] = post_outputs["boundary-reference"] == post_outputs["boundary-splice"]
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fetch", action="store_true")
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--output", type=Path, default=FOLLOWUP / "runs" / "delta.jsonl")
    args = parser.parse_args()
    if args.fetch:
        fetch()
        return
    cases = json.loads(MANIFEST.read_text(encoding="utf-8"))["cases"]
    programs = ("bsdiff.exe", "bspatch.exe", "hdiffz.exe")
    members = {}
    for row in cases:
        if "error" in row:
            continue
        a, b = (CACHE / f["file"] for f in row["files"])
        members[row["project"]] = _common_member(a, b)
    protocol = {"study": "delta-streams-v1", "probe": args.probe, "seed": SEED, "repeats": REPEATS,
                "manifest_sha256": sha256(MANIFEST), "binaries": {f: sha256(BIN / f) for f in programs},
                "baseline": list(BASELINES), "postprocess": list(POST), "timeout": TIMEOUT,
                "hybrid_window": 32 << 10, "switch_penalties": [0, 16, 64], "members": members}
    journal = Journal(args.output, protocol)
    with TemporaryDirectory(dir=WORK, prefix="followup-delta-") as scratch:
        scratch = Path(scratch)
        for row in cases[:1] if args.probe else cases:
            project = row["project"]
            if "error" in row:
                if project not in journal.completed:
                    journal.add(project, attributes=row, attempts=[])
                continue
            old, new = (portable_file(CACHE, f) for f in row["files"])
            for kind, a, b, member in prepare(old, new, scratch):
                unit = f"{project}/{kind}"
                if unit in journal.completed:
                    continue
                seed = SEED + int(rank("delta-streams", unit)[:10], 16)
                attempts = _pair(a, b, scratch, seed=seed)
                journal.add(unit, attributes={"project": project, "kind": kind, "seed": seed, "member": member,
                                               "old_sha256": sha256(a), "new_sha256": sha256(b),
                                               "old_bytes": a.stat().st_size, "new_bytes": b.stat().st_size}, attempts=attempts)
                failures = [r["method"] for r in attempts if not r["verified"] or not r.get("boundary_byte_parity", True)]
                print(f"{unit} finished. Failing methods: {failures}", flush=True)


if __name__ == "__main__":
    main()

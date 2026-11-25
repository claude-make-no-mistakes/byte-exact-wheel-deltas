"""Compare new exact wheel formats with the frozen archive-aware controls."""

import argparse
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter
import zlib

from benchmarks.common import BIN, ROOT, WORK, schedule
from benchmarks.delta import _verify
from benchmarks.doubling_archive import _archive_trial, _encode
from benchmarks.fetch import sha256
from benchmarks.followup_delta import MANIFEST, TIMEOUT
from benchmarks.paper_common import Journal, portable_file
from benchmarks.paper_data import CACHE, rank
from workhorses import delta_archive, delta_context, delta_wheel, delta_xz
from workhorses.delta import unpack


METHODS = ("archive", "wheel-v3")
PREPARATIONS = (("expand", None, True), ("pe-table", "table", False), ("pe-labels", "labels-96", False))
REPEATS = 3


class _Encoder:
    def __init__(self, scratch):
        self.before, self.after, self.patch, self.rebuilt = (
            scratch / name for name in ("left.bin", "right.bin", "new.patch", "decoded.bin"))
        self.verified = []
        self.verify_seconds = 0.

    def bsd(self, before, after, method):
        self.before.write_bytes(before)
        self.after.write_bytes(after)
        blob, _ = _encode(method, self.before, self.after, self.patch)
        start = perf_counter()
        self.rebuilt.unlink(missing_ok=True)
        verdict = _verify("hdiff-bsd", self.before, self.after, self.patch, self.rebuilt, TIMEOUT)
        self.verify_seconds += perf_counter() - start
        self.verified.append(verdict["status"] == "verified")
        if not self.verified[-1]:
            raise ValueError(f"new inner patch failed stock verification: {verdict}")
        return blob

    def xz(self, before, after, method="hdiff-bsd-unblocked", *, compact=False):
        return delta_xz.recode(self.bsd(before, after, method), compact=compact)


def _new_products(old, target, encoder):
    for label, mode, independent in PREPARATIONS:
        job = delta_wheel.prepare(old, target, mode=mode, independent=independent)
        if job is None:
            continue
        seed = encoder.bsd(job.source, job.target, "hdiff-bsd-unblocked")
        yield label, job.wrap(delta_xz.recode(seed))
        if mode is None and len(unpack(seed).literal) >= 4096:
            ctx = delta_context.recode(job.source, seed, encoder.xz)
            yield "decoded-context", job.wrap(ctx)
        if len(job.split_candidates) == 1:
            cut = job.split_candidates[0]
            first, second = job.target[:cut], job.target[cut:]
            a = encoder.xz(job.source, first)
            b = encoder.xz(job.source + first, second)
            yield label + "-prefix", job.wrap(delta_wheel.prefix_patch(len(job.target), cut, False, a, b))


def _tuned_trial(old, target, scratch):
    previous = _archive_trial("archive-xz", old, target, scratch)
    start = perf_counter()
    old_bytes, target_bytes = old.read_bytes(), target.read_bytes()
    output = scratch / "selected.patch"
    encoder = _Encoder(scratch)
    products = {"previous": output.read_bytes(), **dict(_new_products(old_bytes, target_bytes, encoder))}
    route = min(products, key=lambda route: len(products[route]))
    output.write_bytes(products[route])
    encode_time = previous["encoder_seconds"] + perf_counter() - start - encoder.verify_seconds
    candidates = {name: {"bytes": len(blob), "verified": delta_wheel.apply_patch(old_bytes, blob) == target_bytes}
                  for name, blob in products.items()}
    start = perf_counter()
    rebuilt = delta_wheel.apply_patch(old_bytes, output.read_bytes())
    decode_time = perf_counter() - start
    return {"chosen_bytes": len(products[route]), "chosen_route": route, "previous_bytes": previous["chosen_bytes"],
            "verified": previous["verified"] and all(v["verified"] for v in candidates.values()) and rebuilt == target_bytes,
            "encoder_seconds": encode_time, "decoder_seconds": decode_time, "variants": candidates,
            "new_stock_checks": len(encoder.verified)}


def run(path, probe):
    cases = json.loads(MANIFEST.read_text(encoding="utf-8"))["cases"]
    reference = ROOT / "doubling/summary.json"
    first = json.loads(reference.read_text(encoding="utf-8"))["archive"]["per_project"]
    expected = {r["project"]: r for r in first}
    journal = Journal(path, {"study": "wheel-v3-v1", "manifest_sha256": sha256(MANIFEST),
                             "reference_sha256": sha256(reference), "methods": list(METHODS), "probe": probe,
                             "repeats": REPEATS if not probe else 1, "zlib": zlib.ZLIB_RUNTIME_VERSION,
                             "preparations": [list(v) for v in PREPARATIONS],
                             "code": {f: sha256(ROOT / "workhorses" / f) for f in (
                                 "delta.py", "delta_archive.py", "delta_context.py", "delta_pe.py", "delta_wheel.py",
                                 "delta_xz.py",
                             )},
                             "programs": {f.name: sha256(f) for f in (
                                 BIN / "hdiffz.exe", BIN / "bsdiff.exe", BIN / "bspatch.exe", delta_archive.MINIZ_DLL
                             )}})
    with TemporaryDirectory(dir=WORK, prefix="wheel-v3-") as directory:
        scratch = Path(directory)
        for item in cases:
            name = item["project"]
            if name in journal.completed or (probe and name not in ("lxml", "setuptools", "rpds-py", "zstandard", "contourpy")):
                continue
            old, target = (portable_file(CACHE, record) for record in item["files"])
            seed = int(rank("wheel-v3-order", name)[:12], 16)
            attempts = []
            for repeat, method in schedule(METHODS, seed=seed, repeats=2 if probe else REPEATS + 1):
                row = (_archive_trial("archive", old, target, scratch) if method == "archive"
                       else _tuned_trial(old, target, scratch))
                amounts = expected[name]
                if (not row["verified"] or (method == "archive" and row["chosen_bytes"] != amounts["control_bytes"])
                        or (method == "wheel-v3" and row["previous_bytes"] != amounts["candidate_bytes"])):
                    raise ValueError(f"{name} failed reconstruction or control size reproduction")
                attempts.append({"method": method, "repeat": repeat - 1, **row})
            journal.add(name, attributes={"old_sha256": sha256(old), "new_sha256": sha256(target)}, attempts=attempts)
            chosen = min((r for r in attempts if r["method"] == "wheel-v3"), key=lambda r: r["chosen_bytes"])
            print(f"{name} verified {len(attempts)} deliveries, {chosen['chosen_bytes']} bytes via {chosen['chosen_route']}",
                  flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--probe", action="store_true")
    args = parser.parse_args()
    default = "wheels-probe.jsonl" if args.probe else "wheels.jsonl"
    run(args.output or ROOT / "twice/runs" / default, args.probe)

"""Measure RECORD digest reconstruction against each archived winning wheel recipe."""

import argparse
from hashlib import sha256 as digest
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from benchmarks.common import BIN, ROOT, WORK
from benchmarks.fetch import sha256
from benchmarks.followup_delta import MANIFEST
from benchmarks.paper_common import Journal, load_journal, portable_file
from benchmarks.paper_data import CACHE
from benchmarks.twice_wheel_more import TRIALS, _product
from benchmarks.twice_wheels import _Encoder
from workhorses import delta_wheel


PREVIOUS = ROOT / "twice/runs/wheel-confirm.jsonl"


def _encode(old, new, method, transform, encoder):
    trials = {r[0]: r for r in TRIALS}
    label, style = (method, "direct") if method in trials else method.rsplit("-", 1)
    _, minimum, unchanged, mode, independent = trials[label]
    job = delta_wheel.prepare(old, new, mode=mode, minimum_size=minimum, expand_unchanged=unchanged,
                              independent=independent, record_hashes=transform)
    patch = _product(job, encoder, style)
    rebuilt = delta_wheel.apply_patch(old, patch)
    if rebuilt != new:
        raise ValueError("wheel patch differs from the published archive")
    return {"bytes": len(patch), "patch_sha256": digest(patch).hexdigest(),
            "rebuilt_sha256": digest(rebuilt).hexdigest(), "verified": True}


def run(path):
    _, previous = load_journal(PREVIOUS)
    previous = {r["unit"]: r for r in previous}
    cases = json.loads(MANIFEST.read_text(encoding="utf-8"))["cases"]
    if previous.keys() != {r["project"] for r in cases}:
        raise ValueError("wheel cohort differs from the confirmation")
    journal = Journal(path, {"study": "derived-record-v1", "manifest_sha256": sha256(MANIFEST),
                             "reference_sha256": sha256(PREVIOUS),
                             "code": {p: sha256(ROOT / p) for p in (
                                 "workhorses/delta_wheel.py", "workhorses/delta_record.py", "workhorses/delta_pe.py",
                                 "workhorses/delta_xz.py", "workhorses/delta_archive.py", "workhorses/delta.py",
                                 "benchmarks/record_wheels.py", "benchmarks/twice_wheel_more.py")},
                             "programs": {n: sha256(BIN / n) for n in (
                                 "hdiffz.exe", "bsdiff.exe", "bspatch.exe", "wheel_deflate.dll")}})
    with TemporaryDirectory(dir=WORK, prefix="wheel-record-") as scratch:
        encoder = _Encoder(Path(scratch))
        for item in cases:
            name = item["project"]
            if name in journal.completed:
                continue
            old, new = (portable_file(CACHE, file).read_bytes() for file in item["files"])
            winner, = previous[name]["attempts"]
            recipe = winner["method"]
            base = _encode(old, new, recipe, False, encoder)
            if any(base[k] != winner[k] for k in ("bytes", "verified", "patch_sha256")):
                raise ValueError(f"{name} failed to reproduce the previous patch")
            candidate = _encode(old, new, recipe, True, encoder)
            use_hashes = candidate["bytes"] < base["bytes"]
            chosen = candidate if use_hashes else base
            confirmation = _encode(old, new, recipe, use_hashes, encoder)
            if confirmation != chosen:
                raise ValueError(f"{name} winning patch failed regeneration")
            journal.add(name, attributes={**previous[name]["attributes"], "recipe": recipe,
                                          "use_record": use_hashes},
                        attempts=[{"method": method, **a} for method, a in (
                            ("previous", base), ("record", candidate), ("confirmed", confirmation))])
            print(f"{name} {base['bytes']} -> {chosen['bytes']}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "manuscripts/runs/wheels.jsonl")
    run(parser.parse_args().output)

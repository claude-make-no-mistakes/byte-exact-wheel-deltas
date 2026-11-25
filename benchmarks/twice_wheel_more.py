"""Screen the byte cost of expanding small and unchanged ZIP members."""

import argparse
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from benchmarks.common import ROOT, WORK
from benchmarks.fetch import sha256
from benchmarks.followup_delta import MANIFEST
from benchmarks.paper_common import Journal, load_journal, portable_file
from benchmarks.paper_data import CACHE
from benchmarks.twice_wheels import _Encoder
from workhorses import delta_wheel


TRIALS = (
    ("small", 128, False, None, True),
    ("all-small", 128, True, None, True),
    ("tiny", 32, False, None, True),
    ("small-pe", 128, False, "labels-96", True),
    ("data-pe", 128, False, "labels-data", True),
    ("pe-match", 1024, False, "labels-96", False),
    ("data-match", 1024, False, "labels-data", False),
)


def _prepare(source, target, trial):
    _, minimum, all_members, native, independent = trial
    return delta_wheel.prepare(source, target, mode=native, minimum_size=minimum,
                               expand_unchanged=all_members, independent=independent)


def _product(job, encoder, style):
    if style in ("direct", "classic"):
        engine = "classic" if style == "classic" else "hdiff-bsd-unblocked"
        return job.wrap(encoder.xz(job.source, job.target, engine, compact=True))
    cut, = job.split_candidates
    reverse = style == "reverse"
    chunks = job.target[:cut], job.target[cut:]
    a, b = reversed(chunks) if reverse else chunks
    first = encoder.xz(job.source, a, compact=True)
    second = encoder.xz(job.source + a, b, compact=True)
    return job.wrap(delta_wheel.prefix_patch(len(job.target), cut, reverse, first, second))


def run(path):
    receipts = json.loads(MANIFEST.read_text(encoding="utf-8"))["cases"]
    journal = Journal(path, {"study": "refined-wheel-size-v1", "manifest_sha256": sha256(MANIFEST),
                             "modes": [list(row) for row in TRIALS], "compact": True,
                             "code": {f: sha256(ROOT / "workhorses" / f)
                                      for f in ("delta.py", "delta_archive.py", "delta_pe.py", "delta_wheel.py",
                                                "delta_xz.py")}})
    with TemporaryDirectory(dir=WORK, prefix="small-wheel-") as scratch:
        for row in receipts:
            name = row["project"]
            if name in journal.completed:
                continue
            source, target = (portable_file(CACHE, file).read_bytes() for file in row["files"])
            encoder = _Encoder(Path(scratch))
            results = []
            for trial in TRIALS:
                label, _, _, native, _ = trial
                job = _prepare(source, target, trial)
                if job is None:
                    continue
                styles = ["direct"]
                if len(job.target) >= 4 << 20 and label in ("small", "data-pe"):
                    styles.append("classic")
                if native is not None and len(job.split_candidates) == 1:
                    styles.extend(("prefix", "reverse"))
                for style in styles:
                    method = label if style == "direct" else f"{label}-{style}"
                    blob = _product(job, encoder, style)
                    verified = delta_wheel.apply_patch(source, blob) == target
                    if not verified:
                        raise ValueError(f"{name}/{method} failed byte verification")
                    results.append({"method": method, "bytes": len(blob), "verified": verified,
                                    "normalized_members": job.target_members})
            journal.add(name, attributes={"source_sha256": row["files"][0]["sha256"],
                                          "target_sha256": row["files"][1]["sha256"]}, attempts=results)
            print(name, [(r["method"], r["bytes"]) for r in results], flush=True)


def confirm(path, screen):
    from hashlib import sha256 as digest

    input_header, candidates = load_journal(screen)
    if input_header["protocol"]["manifest_sha256"] != sha256(MANIFEST):
        raise ValueError("wheel confirmation input manifest differs")
    choices = {u["unit"]: min(u["attempts"], key=lambda row: row["bytes"]) for u in candidates}
    entries = json.loads(MANIFEST.read_text(encoding="utf-8"))["cases"]
    if choices.keys() != {r["project"] for r in entries} or not all(r["verified"] for r in choices.values()):
        raise ValueError("wheel confirmation requires all verified winners")
    trials = {r[0]: r for r in TRIALS}
    journal = Journal(path, {"study": "refined-wheel-confirmation-v1", "screen_sha256": sha256(screen),
                             "manifest_sha256": sha256(MANIFEST)})
    with TemporaryDirectory(dir=WORK, prefix="verified-wheel-") as tmp:
        encoder = _Encoder(Path(tmp))
        for r in entries:
            name = r["project"]
            if name in journal.completed:
                continue
            old, new = (portable_file(CACHE, item).read_bytes() for item in r["files"])
            winner = choices[name]
            label, style = winner["method"], "direct"
            if label not in trials:
                label, style = label.rsplit("-", 1)
            job = _prepare(old, new, trials[label])
            blob = _product(job, encoder, style)
            if len(blob) != winner["bytes"] or delta_wheel.apply_patch(old, blob) != new:
                raise ValueError(f"{name} winning wheel patch did not reproduce")
            journal.add(name, attributes={"source_sha256": digest(old).hexdigest(),
                                          "target_sha256": digest(new).hexdigest()},
                        attempts=[{"method": winner["method"], "bytes": len(blob), "verified": True,
                                   "patch_sha256": digest(blob).hexdigest()}])
            print(f"{name} reproduced {len(blob)} bytes", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--confirm", type=Path, metavar="SIZE_JOURNAL")
    args = parser.parse_args()
    if args.confirm:
        confirm(args.output or ROOT / "twice/runs/wheel-confirm.jsonl", args.confirm)
    else:
        run(args.output or ROOT / "twice/runs/wheel-refined.jsonl")

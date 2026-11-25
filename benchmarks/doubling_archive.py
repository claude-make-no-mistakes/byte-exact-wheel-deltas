"""Compare complete byte-verified wheel patches with the previous selector."""

import argparse
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter
import zlib

from benchmarks.common import BIN, ROOT, WORK, schedule
from benchmarks.delta import _encode_command, _timed_call, _verify
from benchmarks.fetch import sha256
from benchmarks.followup_delta import MANIFEST, TIMEOUT, prepare
from benchmarks.paper_common import Journal, load_journal, portable_file
from benchmarks.paper_data import CACHE, rank
from workhorses import delta_xz
from workhorses.delta_archive import MAGIC_EXTENDED, MINIZ_DLL, apply_archive_patch, normalize


REPEATS = 3
METHODS = ("archive", "archive-xz")
ENCODERS = ("hdiff-bsd", "hdiff-bsd-unblocked", "classic")
COMPATIBLE = {"hdiff-bsd-unblocked": "-block-0", "hdiff-bsd-match0": "-m-0"}


def _encode(encoder, source, target, patch):
    patch.unlink(missing_ok=True)
    command = (_encode_command(encoder, source, target, patch) if encoder not in COMPATIBLE
               else [BIN / "hdiffz.exe", "-d", "-BSD", "-p-1", COMPATIBLE[encoder], source, target, patch])
    record = _timed_call(command, TIMEOUT)
    if record["status"] != "ran":
        raise RuntimeError(f"{encoder} failed: {record}")
    return patch.read_bytes(), record


def _archive_trial(method, old, new, scratch):
    tuned = method == "archive-xz"
    patch = scratch / "internal.patch"
    restored = scratch / "restored.bin"
    start = perf_counter()
    old_bytes, new_bytes = old.read_bytes(), new.read_bytes()
    raw, stats = _encode("hdiff-bsd", old, new, patch)
    products = {"raw": raw}
    encoder_records = [dict(stats, route="raw")]
    independent = [(old, new, raw)]
    job = normalize(old_bytes, new_bytes, miniz=tuned)
    jobs = [("expanded", job, ENCODERS if tuned else ENCODERS[:1])]
    if tuned and job is not None and job.magic == MAGIC_EXTENDED:
        jobs.append(("legacy", normalize(old_bytes, new_bytes), ENCODERS[:1]))
    for prefix, candidate, encoders in jobs:
        if candidate is None:
            continue
        norm_old, norm_new = scratch / f"{prefix}.old", scratch / f"{prefix}.new"
        norm_old.write_bytes(candidate.source)
        norm_new.write_bytes(candidate.target)
        for encoder in encoders:
            inner, stats = _encode(encoder, norm_old, norm_new, patch)
            route = encoder if prefix == "expanded" else f"{prefix}-{encoder}"
            encoder_records.append(dict(stats, route=route))
            independent.append((norm_old, norm_new, inner))
            products[route] = candidate.wrap(inner)
            if tuned:
                products[route + "-xz"] = candidate.wrap(delta_xz.recode(inner))
    chosen_route = min(products, key=lambda name: len(products[name]))
    output = scratch / "selected.patch"
    output.write_bytes(products[chosen_route])
    encoder_seconds = perf_counter() - start
    external = []
    for source, target, inner in independent:
        patch.write_bytes(inner)
        restored.unlink(missing_ok=True)
        external.append(_verify("hdiff-bsd", source, target, patch, restored, TIMEOUT))
    variants = {}
    for name, blob in products.items():
        variants[name] = {"bytes": len(blob), "verified": apply_archive_patch(old_bytes, blob) == new_bytes}
    start = perf_counter()
    rebuilt = apply_archive_patch(old_bytes, output.read_bytes())
    decoder_seconds = perf_counter() - start
    return {
        "verified": (all(row["status"] == "verified" for row in external)
                     and all(row["verified"] for row in variants.values()) and rebuilt == new_bytes),
        "chosen_route": chosen_route, "chosen_bytes": len(products[chosen_route]),
        "raw_bytes": len(raw), "metadata_bytes": len(job.metadata) if job else 0,
        "normalized_members": job.members if job else 0, "variants": variants,
        "encoder_seconds": encoder_seconds, "decoder_seconds": decoder_seconds,
        "encoder_records": encoder_records, "bspatch_verification": external,
    }


def _compatible(path, cases):
    archived = ROOT / "followup/runs/delta.jsonl"
    _, blocks = load_journal(archived)
    reference = {b["unit"]: b for b in blocks}
    programs = (BIN / "hdiffz.exe", BIN / "bspatch.exe")
    journal = Journal(path, {"study": "compatible-delta-size-v1", "manifest_sha256": sha256(MANIFEST),
                             "archived_sha256": sha256(archived), "options": COMPATIBLE, "timeout": TIMEOUT,
                             "programs": {file.name: sha256(file) for file in programs}})
    with TemporaryDirectory(dir=WORK, prefix="compatible-delta-") as temporary:
        scratch = Path(temporary)
        patch, rebuilt = scratch / "candidate.patch", scratch / "reconstructed.data"
        for item in cases:
            old, new = (portable_file(CACHE, receipt) for receipt in item["files"])
            for kind, a, b, member in prepare(old, new, scratch):
                unit = f"{item['project']}/{kind}"
                if unit in journal.completed:
                    continue
                original = {k: sha256(file) for k, file in (("old_sha256", a), ("new_sha256", b))}
                if any(digest != reference[unit]["attributes"][k] for k, digest in original.items()):
                    raise ValueError(f"delta input differs from the archived bytes for {unit}")
                hybrid = [r for r in reference[unit]["attempts"] if r["method"] == "hybrid" and r["repeat"] >= 0]
                sizes = {r["bytes"] for r in hybrid}
                if len(hybrid) != REPEATS or len(sizes) != 1 or not all(r["verified"] for r in hybrid):
                    raise ValueError(f"archived hybrid verification failed for {unit}")
                attempts = []
                for mode in COMPATIBLE:
                    product, encode = _encode(mode, a, b, patch)
                    rebuilt.unlink(missing_ok=True)
                    decode = _verify("hdiff-bsd", a, b, patch, rebuilt, TIMEOUT)
                    attempts.append({"method": mode, "new_bytes": len(product), "encode": encode, "decode": decode,
                                     "verified": decode["status"] == "verified"})
                journal.add(unit, attributes={"hybrid_bytes": sizes.pop(), "member": member, **original},
                            attempts=attempts)
                print(f"{unit} verified {sum(r['verified'] for r in attempts)}/{len(attempts)} patches", flush=True)


def _wheels(path, cases, probe):
    protocol = {"study": "archive-xz-v1", "manifest_sha256": sha256(MANIFEST), "probe": probe,
                "methods": list(METHODS), "repeats": REPEATS, "encoders": list(ENCODERS),
                "zlib": zlib.ZLIB_RUNTIME_VERSION, "xz_filters": delta_xz.FILTERS,
                "code_sha256": {f: sha256(ROOT / "workhorses" / f) for f in
                                ("delta.py", "delta_archive.py", "delta_xz.py")},
                "rust_receipts": {f: sha256(ROOT / "native/archive-codec" / f)
                                  for f in ("Cargo.lock", "Cargo.toml", "src/lib.rs")},
                "program_sha256": {path.name: sha256(path) for path in
                                   (MINIZ_DLL, BIN / "bsdiff.exe", BIN / "bspatch.exe", BIN / "hdiffz.exe")}}
    journal = Journal(path, protocol)
    with TemporaryDirectory(dir=WORK, prefix="archive-xz-") as scratch:
        for item in cases:
            name = item["project"]
            if name in journal.completed or (probe and name not in ("rpds-py", "colorama")):
                continue
            old, new = (portable_file(CACHE, receipt) for receipt in item["files"])
            pairs = []
            seed = int(rank("incremental-wheel-order", name)[:12], 16)
            for repeat, method in schedule(METHODS, seed=seed, repeats=REPEATS + 1):
                row = _archive_trial(method, old, new, Path(scratch))
                pairs.append({"repeat": repeat - 1, "method": method, **row})
            journal.add(name, attributes={"source_sha256": sha256(old), "target_sha256": sha256(new)}, attempts=pairs)
            successes = sum(bool(r["verified"]) for r in pairs)
            smallest = min(r["chosen_bytes"] for r in pairs)
            print(f"{name} verified {successes}/{len(pairs)} patches, smallest {smallest} bytes", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    settings = parser.add_mutually_exclusive_group()
    settings.add_argument("--probe", action="store_true")
    settings.add_argument("--compatible", action="store_true")
    args = parser.parse_args()
    cases = json.loads(MANIFEST.read_text(encoding="utf-8"))["cases"]
    if args.compatible:
        _compatible(args.output or ROOT / "doubling/runs/compatible.jsonl", cases)
    else:
        default = WORK / "archive-xz-probe.jsonl" if args.probe else ROOT / "doubling/runs/archive.jsonl"
        _wheels(args.output or default, cases, args.probe)


if __name__ == "__main__":
    main()

"""Paired release-corpus study of BSDIFF40 size and end-to-end cost."""

import argparse
import json
from pathlib import Path
import random
import sys
from tempfile import TemporaryDirectory
import zipfile

from benchmarks.common import BIN, WORK, schedule
from benchmarks.delta import _encode_command, _timed_call, _verify
from benchmarks.fetch import sha256
from benchmarks.paper_common import Journal, PAPER, SEED, portable_file
from benchmarks.paper_data import CACHE, rank
from workhorses.delta import _header


COMPATIBLE = ("classic", "hdiff-bsd", "bsdiff6-orderfix")
METHODS = (*COMPATIBLE, "hdiff-native", "xdelta3", "candidate-4")
TIMEOUT = 15
REPEATS = 3


def _common_member(left, right):
    def choices(path):
        with zipfile.ZipFile(path) as archive:
            return {r.filename: r.file_size for r in archive.infolist()
                    if r.filename.endswith((".py", ".pyd", ".dll")) and ".dist-info/" not in r.filename
                    and 2048 <= r.file_size <= 6 << 20}

    a, b = choices(left), choices(right)
    names = sorted(a.keys() & b.keys())
    if not names:
        return None
    return max(names, key=lambda item: (not item.endswith(".py"), min(a[item], b[item])))


def prepare(old, new, scratch):
    yield "wheel", old, new, None
    member = _common_member(old, new)
    if member is None:
        return
    outputs = []
    for mark, source in (("old", old), ("new", new)):
        target = scratch / f"{mark}.data"
        with zipfile.ZipFile(source) as archive:
            target.write_bytes(archive.read(member))
        outputs.append(target)
    yield ("native-member" if not member.endswith(".py") else "python-member"), *outputs, member


def sample(method, old, new, patch, rebuilt):
    patch.unlink(missing_ok=True)
    rebuilt.unlink(missing_ok=True)
    if method == "bsdiff6-orderfix":
        command = [BIN / "bsdiff6-orderfix.exe", "diff", old, new, "-m", "bsdiff6", "-o", patch]
    elif method == "candidate-4":
        command = [sys.executable, "-B", "-m", "benchmarks.delta", "candidate", old, new, patch,
                   "hdiff-bsd", "4", "64", str(TIMEOUT * 0.8)]
    else:
        command = _encode_command(method, old, new, patch)
    encoded = _timed_call(command, TIMEOUT)
    audit = None
    if method == "candidate-4" and encoded["status"] == "ran":
        info = json.loads(encoded["output_tail"])
        audit = info["audit"]
        for metric in ("peak_process_bytes", "cpu_seconds"):
            extra = info["seed_metrics"][metric]
            if extra is not None and encoded[metric] is not None:
                encoded[metric] += extra
        encoded["output_tail"] = ""
    decoded = _verify(method, old, new, patch, rebuilt, TIMEOUT) if encoded["status"] == "ran" else None
    verified = decoded is not None and decoded["status"] == "verified"
    patch_size = patch.stat().st_size if patch.is_file() else None
    streams = None
    if verified and method not in ("hdiff-native", "xdelta3"):
        _, *sections = _header(patch.read_bytes())
        streams = [len(stream) for stream in sections]
    return {"method": method, "verified": verified, "patch_bytes": patch_size,
            "encoded": encoded, "decoded": decoded, "audit": audit, "stream_bytes": streams}


def diagnostic(journal, path, probe):
    fixtures = (4096,) if probe else (256, 4096, 16_384, 32_768)
    for size in fixtures:
        label = f"upstream-probe/{size}"
        if label in journal.completed:
            continue
        old, new, patch, rebuilt = (path / n for n in ("old", "new", "output", "rebuilt"))
        payload = random.Random(SEED + size).randbytes(size)
        old.write_bytes(payload)
        half = size // 2
        new.write_bytes(payload[:half] + b"three test bytes" + payload[half:])
        attempts = [sample(method, old, new, patch, rebuilt) for method in ("bsdiff6", "bsdiff6-orderfix")]
        journal.add(label, attributes={"kind": "upstream-diagnostic", "source_bytes": size,
                                       "source_sha256": sha256(old), "target_sha256": sha256(new)},
                    attempts=attempts)
        print(f"delta {label} {[(r['method'], r['verified'], r['encoded']['status']) for r in attempts]}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=PAPER / "runs" / "delta.jsonl")
    parser.add_argument("--probe", action="store_true")
    args = parser.parse_args()
    manifest = PAPER / "inputs-delta.json"
    inputs = json.loads(manifest.read_text(encoding="utf-8"))["cases"]
    programs = ("bsdiff.exe", "bspatch.exe", "bsdiff6.exe", "bsdiff6-orderfix.exe", "hdiffz.exe", "hpatchz.exe", "xdelta3.exe")
    protocol = {"study": "delta-v2", "probe": args.probe, "seed": SEED,
                "input_manifest_sha256": sha256(manifest), "exe_sha256": {p: sha256(BIN / p) for p in programs},
                "timeout": TIMEOUT, "repeats": REPEATS, "methods": list(METHODS),
                "candidate_seed": "hdiff-bsd", "shortlist": 4, "candidate_remap_cap": 64}
    journal = Journal(args.output, protocol)
    with TemporaryDirectory(dir=WORK, prefix="paper-delta-") as temporary:
        scratch = Path(temporary)
        diagnostic(journal, scratch, args.probe)
        patch, rebuilt = scratch / "study.patch", scratch / "study.rebuilt"
        # Run each executable and decoder once on a synthetic pair before timing.
        old, new = scratch / "warm.old", scratch / "warm.new"
        payload = random.Random(SEED + 2000).randbytes(8192)
        old.write_bytes(payload)
        new.write_bytes(payload + b"warm")
        for method in METHODS:
            sample(method, old, new, patch, rebuilt)
        for row in inputs[:1] if args.probe else inputs:
            project = row["project"]
            if "error" in row:
                if project not in journal.completed:
                    journal.add(project, attributes=row, attempts=[])
                continue
            a, b = (portable_file(CACHE, file) for file in row["files"])
            for representation, old, new, member in prepare(a, b, scratch):
                ident = f"{project}/{representation}"
                if ident in journal.completed:
                    continue
                attempts, censored = [], set()
                for repeat, method in schedule(METHODS, seed=SEED + int(rank("file", ident)[:8], 16),
                                               repeats=1 if args.probe else REPEATS):
                    if method in censored:
                        result = {"method": method, "verified": False,
                                  "encoded": {"status": "previous_encode_timeout"}, "decoded": None}
                    else:
                        result = sample(method, old, new, patch, rebuilt)
                        if result["encoded"]["status"] == "timeout":
                            censored.add(method)
                    attempts.append({"repeat": repeat, **result})
                source, target = sha256(old), sha256(new)
                attrs = {"kind": "release", "project": project, "representation": representation,
                         "member": member, "tag": row["tag"], "old_sha256": source, "new_sha256": target,
                         "old_bytes": old.stat().st_size, "new_bytes": new.stat().st_size,
                         "identical": source == target}
                journal.add(ident, attributes=attrs, attempts=attempts)
                print(f"delta {ident} finished", flush=True)


if __name__ == "__main__":
    main()

"""Measure BSDIFF40 size headroom, encoder budgets, and byte-exact reconstruction."""

import argparse
import ctypes as ct
from ctypes import wintypes as wt
from dataclasses import asdict
import json
import os
from pathlib import Path, PurePosixPath
import statistics
import subprocess
import sys
from tempfile import TemporaryDirectory
from time import perf_counter
import zipfile

from benchmarks.common import BIN, SEED, WORK, interval, metadata, schedule, write_result
from benchmarks.fetch import DELTA_FILES, cached_asset, sha256
from workhorses.delta import apply, optimize, unpack


COMPATIBLE = ("classic", "hdiff-bsd", "bsdiff6")
NATIVE = ("hdiff-native", "xdelta3")


if os.name == "nt":
    class _MemoryCounters(ct.Structure):
        _fields_ = [("cb", wt.DWORD), ("faults", wt.DWORD), ("peak", ct.c_size_t),
                    ("working", ct.c_size_t), ("peak_pool_paged", ct.c_size_t),
                    ("pool_paged", ct.c_size_t), ("peak_pool_nonpaged", ct.c_size_t),
                    ("pool_nonpaged", ct.c_size_t), ("pagefile", ct.c_size_t), ("peak_pagefile", ct.c_size_t)]

    _get_memory = ct.WinDLL("psapi").GetProcessMemoryInfo
    _get_memory.argtypes = [wt.HANDLE, ct.POINTER(_MemoryCounters), wt.DWORD]
    _get_memory.restype = wt.BOOL
    _get_times = ct.WinDLL("kernel32").GetProcessTimes
    _get_times.argtypes = [wt.HANDLE, *[ct.POINTER(wt.FILETIME)] * 4]
    _get_times.restype = wt.BOOL


def _process_stats(process):
    if os.name != "nt":
        return None, None
    handle = wt.HANDLE(int(process._handle))
    counters = _MemoryCounters(cb=ct.sizeof(_MemoryCounters))
    peak = int(counters.peak) if _get_memory(handle, ct.byref(counters), counters.cb) else None
    timestamps = [wt.FILETIME() for _ in range(4)]
    available = _get_times(handle, *(ct.byref(t) for t in timestamps))
    cpu = sum((t.dwHighDateTime << 32) + t.dwLowDateTime for t in timestamps[2:]) / 1e7 if available else None
    return peak, cpu


def _timed_call(command, timeout):
    start = perf_counter()
    with subprocess.Popen([str(arg) for arg in command], stdout=subprocess.PIPE, stderr=subprocess.STDOUT) as proc:
        try:
            output, _ = proc.communicate(timeout=timeout)
            status = "ran" if proc.returncode == 0 else f"exit_{proc.returncode}"
        except subprocess.TimeoutExpired:
            proc.kill()
            output, _ = proc.communicate()
            status = "timeout"
        seconds = perf_counter() - start
        peak, cpu = _process_stats(proc)
    return {"seconds": seconds, "peak_process_bytes": peak, "cpu_seconds": cpu, "status": status,
            "output_tail": output.decode(errors="replace")[-1000:]}


def _encode_command(method, old, new, patch):
    match method:
        case "classic":
            return [BIN / "bsdiff.exe", old, new, patch]
        case "hdiff-bsd":
            return [BIN / "hdiffz.exe", "-d", "-BSD", "-p-1", old, new, patch]
        case "hdiff-native":
            return [BIN / "hdiffz.exe", "-d", "-c-zstd-19", "-p-1", old, new, patch]
        case "bsdiff6":
            return [BIN / "bsdiff6.exe", "diff", old, new, "-m", "bsdiff6", "-o", patch]
        case "xdelta3":
            return [BIN / "xdelta3.exe", "-e", "-9", "-S", "lzma", "-s", old, new, patch]
        case _:
            raise ValueError(f"unknown binary delta encoder {method}")


def _decode_command(method, old, patch, rebuilt):
    if method == "hdiff-native":
        return [BIN / "hpatchz.exe", old, patch, rebuilt]
    if method == "xdelta3":
        return [BIN / "xdelta3.exe", "-d", "-s", old, patch, rebuilt]
    return [BIN / "bspatch.exe", old, rebuilt, patch]


def _candidate(old, new, patch, seed_method, shortlist, max_copies, timeout):
    with TemporaryDirectory(prefix="seed-", dir=WORK) as scratch:
        seed_path = Path(scratch) / "seed.patch"
        seed_metrics = _timed_call(_encode_command(seed_method, old, new, seed_path), timeout)
        if seed_metrics["status"] != "ran":
            raise RuntimeError(f"seed encoder {seed_method} failed: {seed_metrics}")
        try:
            product, audit = optimize(old.read_bytes(), new.read_bytes(), seed_path.read_bytes(),
                                      shortlist=shortlist, max_copies=max_copies)
        except (ValueError, OSError) as exc:
            raise RuntimeError(f"invalid seed patch: {exc}") from exc
        patch.write_bytes(product)
        seed_metrics.pop("output_tail")
        print(json.dumps({"audit": asdict(audit), "seed_metrics": seed_metrics}), flush=True)


def _members(path, suffix):
    with zipfile.ZipFile(path) as archive:
        return [item for item in archive.namelist()
                if item.endswith(suffix) and not item.endswith("/") and archive.getinfo(item).file_size <= (16 << 20)]


def _cases(args, path):
    cases = []
    requested = {
        "fmt": ("/include/fmt/format.h",),
        "libpng": ("/pngrtran.c",),
        "lz4": ("lz4.exe",),
        "zstd": ("zstd.exe", "libzstd.dll"),
    }
    for project, first, second in DELTA_FILES[args.cohort]:
        old, new = cached_asset(first)[0], cached_asset(second)[0]
        cases.append((project, project + "-release", old, new))
        for suffix in requested.get(project, ()):
            members = _members(old, suffix), _members(new, suffix)
            if any(len(items) != 1 for items in members):
                raise ValueError(f"expected one release member ending in {suffix} in each archive: {members}")
            name = project + "-" + PurePosixPath(suffix).name
            extracted = []
            for mark, release, items in zip(("old", "new"), (old, new), members):
                output = path / f"{name}-{mark}"
                with zipfile.ZipFile(release) as archive:
                    output.write_bytes(archive.read(items[0]))
                extracted.append(output)
            cases.append((project, name, *extracted))
    return cases


def _verify(method, old, new, patch, rebuilt, timeout):
    if method not in NATIVE:
        try:
            if apply(old.read_bytes(), unpack(patch.read_bytes())) != new.read_bytes():
                return {"status": "python_reconstruction_mismatch"}
        except (OSError, EOFError, ValueError) as exc:
            return {"status": "invalid_bsdiff40", "error": str(exc)}
    result = _timed_call(_decode_command(method, old, patch, rebuilt), timeout)
    if result["status"] == "ran":
        result["status"] = "verified" if rebuilt.read_bytes() == new.read_bytes() else "decoder_reconstruction_mismatch"
    return result


def study(args):
    WORK.mkdir(parents=True, exist_ok=True)
    candidate_names = {f"candidate-{count}": count for count in args.shortlist}
    methods = COMPATIBLE + NATIVE + tuple(candidate_names)
    required = {"bsdiff.exe", "bspatch.exe", "hdiffz.exe", "hpatchz.exe", "bsdiff6.exe", "xdelta3.exe"}
    if args.allow_missing_bsdiff6 and not (BIN / "bsdiff6.exe").is_file():
        methods = tuple(m for m in methods if m != "bsdiff6")
        required.remove("bsdiff6.exe")
    if missing := sorted(name for name in required if not (BIN / name).is_file()):
        raise FileNotFoundError(f"build the native binary delta tools first: {', '.join(missing)}")
    rows, inputs = [], []
    with TemporaryDirectory(prefix="delta-", dir=WORK) as scratch:
        path = Path(scratch)
        for i, (project, case, old, new) in enumerate(_cases(args, path)):
            inputs.append({"case": case, "project": project, "old_sha256": sha256(old), "new_sha256": sha256(new)})
            for repeat, method in schedule(methods, seed=SEED + i, repeats=args.repeats + 1):
                patch, rebuilt = path / "output.patch", path / "reconstructed"
                patch.unlink(missing_ok=True)
                rebuilt.unlink(missing_ok=True)
                if method in candidate_names:
                    command = [sys.executable, "-B", "-m", "benchmarks.delta", "candidate", old, new, patch,
                               args.seed_method, str(candidate_names[method]), str(args.max_copies), str(0.8 * args.timeout)]
                else:
                    command = _encode_command(method, old, new, patch)
                encoded = _timed_call(command, args.timeout)
                audit = None
                if encoded["status"] == "ran" and method in candidate_names:
                    candidate_info = json.loads(encoded["output_tail"])
                    audit = candidate_info["audit"]
                    seed_peak = candidate_info["seed_metrics"]["peak_process_bytes"]
                    seed_cpu = candidate_info["seed_metrics"]["cpu_seconds"]
                    if seed_peak is not None and encoded["peak_process_bytes"] is not None:
                        # Both processes are live during seed encoding. Adding
                        # their individual peaks gives a conservative bound.
                        encoded["peak_process_bytes"] += seed_peak
                    if seed_cpu is not None and encoded["cpu_seconds"] is not None:
                        encoded["cpu_seconds"] += seed_cpu
                    encoded["output_tail"] = ""
                decoded = _verify(method, old, new, patch, rebuilt, args.timeout) if encoded["status"] == "ran" else None
                if repeat == 0:  # Warm file-system and executable caches for the complete workflow.
                    continue
                rows.append({
                    "project": project, "case": case, "target_bytes": new.stat().st_size,
                    "repeat": repeat, "method": method, "patch_bytes": patch.stat().st_size if patch.is_file() else None,
                    "encode": encoded, "decode": decoded, "audit": audit,
                    "verified": decoded is not None and decoded["status"] == "verified",
                })
            print(f"{case} finished", flush=True)
    write_result(args.output, {"metadata": {**metadata(), "seed": SEED},
                              "settings": {"cohort": args.cohort, "seed_method": args.seed_method, "repeats": args.repeats,
                                           "timeout": args.timeout, "shortlist": args.shortlist, "max_copies": args.max_copies},
                              "inputs": inputs, "summary": summarize(rows, methods), "attempts": rows})


def summarize(rows, methods):
    cases = sorted({r["case"] for r in rows})
    projects = {r["case"]: r["project"] for r in rows}
    summary = {}
    case_metrics = {}
    for method in methods:
        attempts = [r for r in rows if r["method"] == method]
        complete = {}
        for case in cases:
            these = [r for r in attempts if r["case"] == case]
            if not these or not all(r["verified"] for r in these):
                continue
            complete[case] = {
                "patch_bytes": statistics.median(r["patch_bytes"] for r in these),
                "encode_seconds": statistics.median(r["encode"]["seconds"] for r in these),
                "decode_seconds": statistics.median(r["decode"]["seconds"] for r in these),
                "audit": these[0]["audit"],
            }
        case_metrics[method] = complete
        summary[method] = {"verified_attempts": sum(r["verified"] for r in attempts), "attempts": len(attempts),
                           "verified_cases": len(complete),
                           "requested_cases": len(cases),
                           "patch_bytes_on_verified_cases": sum(r["patch_bytes"] for r in complete.values()),
                           "sum_of_verified_case_median_encode_seconds": sum(r["encode_seconds"] for r in complete.values()),
                           "peak_process_bytes": max((r["encode"]["peak_process_bytes"] or 0 for r in attempts), default=0) or None}
    oracle = {}
    for case in cases:
        eligible = [(info[case]["patch_bytes"], method) for method, info in case_metrics.items()
                    if method in COMPATIBLE and case in info]
        if eligible:
            size, method = min(eligible)
            oracle[case] = {"encoder": method, "bytes": size}
    summary["compatible_per_case_oracle"] = oracle
    for method in methods:
        if not method.startswith("candidate-"):
            continue
        candidate = case_metrics[method]
        scores = {}
        for control in (item for item in COMPATIBLE if item in case_metrics):
            baseline = case_metrics[control]
            matched = sorted(baseline.keys() & candidate.keys())
            by_project = {}
            for case in matched:
                by_project.setdefault(projects[case], []).append(case)

            def total(info, metric, group):
                return sum(info[case][metric] for case in group)

            if matched:
                base_size = total(baseline, "patch_bytes", matched)
                size = total(candidate, "patch_bytes", matched)
                scores[control] = {
                    "matched_cases": len(matched),
                    "requested_cases": len(cases),
                    "candidate_patch_bytes": size,
                    "reference_patch_bytes": base_size,
                    "byte_savings_fraction": 1 - size / base_size,
                    "encoder_time_multiple": total(candidate, "encode_seconds", matched) / total(baseline, "encode_seconds", matched),
                    "decoder_time_multiple": total(candidate, "decode_seconds", matched) / total(baseline, "decode_seconds", matched),
                    "paired_project_savings_fraction": interval(
                        1 - total(candidate, "patch_bytes", group) / total(baseline, "patch_bytes", group)
                        for group in by_project.values()),
                }
        matched = sorted(candidate.keys() & oracle.keys())
        if matched:
            reference = sum(oracle[case]["bytes"] for case in matched)
            scores["per_case_oracle"] = {
                "matched_cases": len(matched), "requested_cases": len(cases), "reference_patch_bytes": reference,
                "byte_savings_fraction": 1 - sum(candidate[case]["patch_bytes"] for case in matched) / reference}
        summary[method]["paired"] = scores
        audited = [entry["audit"] for entry in candidate.values() if entry["audit"] is not None]
        summary[method]["audit"] = {
            "seed_patch_bytes": sum(row["initial_bytes"] for row in audited),
            "compressed_control_bytes": sum(row["control_bytes"] for row in audited),
            "copies_examined": sum(row["examined_copies"] for row in audited),
            "interchangeable_copies": sum(row["interchangeable_copies"] for row in audited),
            "remaps": sum(row["remaps"] for row in audited),
            "boundary_changes": sum(row["boundary_changes"] for row in audited),
        }
    return summary


def main():
    if sys.argv[1:2] == ["candidate"]:
        old, new, patch, method, shortlist, max_copies, timeout = sys.argv[2:]
        _candidate(Path(old), Path(new), Path(patch), method, int(shortlist), int(max_copies), float(timeout))
        return
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cohort", choices=DELTA_FILES, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seed-method", choices=COMPATIBLE, default="hdiff-bsd")
    parser.add_argument("--shortlist", nargs="*", type=int, default=[0, 4])
    parser.add_argument("--max-copies", type=int, default=64)
    parser.add_argument("--timeout", type=float, default=45)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--allow-missing-bsdiff6", action="store_true", help="diagnostic development runs")
    args = parser.parse_args()
    if args.cohort == "holdout" and (args.allow_missing_bsdiff6 or len(args.shortlist) > 1):
        parser.error("holdout uses all baselines and at most one frozen candidate")
    if min(args.repeats, args.timeout) <= 0 or any(x < 0 for x in args.shortlist):
        parser.error("invalid timeout, repeat count, or shortlist")
    study(args)


if __name__ == "__main__":
    main()

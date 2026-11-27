# Byte-exact wheel deltas

[Paper PDF](output/pdf/latex/wheels.pdf) · [LaTeX source](manuscripts/latex/wheels.tex) · [Markdown draft](manuscripts/wheels.md)

Archive-aware binary delta encoding and deferred PEP 427 `RECORD` digest reconstruction for byte-exact Python wheel updates.

## Summary of verified results

Across 16 frozen PyPI release pairs (7 platform-independent Python wheels and 9 Windows `win_amd64` native wheels; 7,053,346 total target bytes), complete self-contained patches (including wrappers, recipes, codec tags, PE hints, SHA-256 digests, and compressed delta streams) total:

| Stage | Total patch bytes | Reduction vs. archive-aware control |
| --- | ---: | ---: |
| Raw BSDIFF40 (`hdiffz -BSD`, archive-unaware) | 5,148,312 | — |
| Original archive-aware control (`ZIPD`) | 888,127 | 0.00% |
| Round 1: archive + `miniz_oxide` + XZ | 611,442 | −31.15% |
| Round 2: timed multi-recipe selector | 464,911 | −47.65% |
| Round 2: size sweep (`WDP3`, prior best) | 441,126 | −50.33% |
| **With derived `RECORD` digests (final)** | **431,406** | **−51.43%** |

- Deriving canonical `RECORD` SHA-256 fields saved **9,720 bytes (−2.20%)** beyond the preceding 441,126-byte recipes and reduced patch size on **all 16/16 projects** (largest absolute gains: `setuptools` −2,593 bytes / −15.50%, `markdown-it-py` −2,040 bytes / −26.34%).
- A second encoding reproduced all 16 winning patch SHA-256 hashes identically, and every decoded wheel matched its published PyPI bytes exactly.
- Paired timings from the earlier 464,911-byte timed selector (`twice/summary.json`) were 9.55× encode time [95% CI 5.90–11.69] and 1.15× decode time [95% CI 0.98–1.39] relative to the 888,127-byte control; the final size sweep and `RECORD` ablation are size-only evaluations.

## Repository layout

- `workhorses/` — Core delta codecs and transforms:
  - `delta_record.py`: canonical PEP 427 `RECORD` digest masking and in-memory reconstruction
  - `delta_wheel.py`: `WDP3` archive-aware preparation, two-pass span-preserving decoder, and `PFXD0001` prefix chaining
  - `delta_archive.py`: `ZIPD`/`ZIP2` DEFLATE reproducibility checks (`zlib` and `miniz_oxide`)
  - `delta_pe.py`: reversible AMD64 PE `.pdata` and reference normalization
  - `delta_xz.py`, `delta_context.py`, `delta.py`, `delta_hybrid.py`: XZ/compact controls, decoded-context literals, and BSDIFF40 postprocessing
- `native/archive-codec/` — Rust FFI wrapper around `miniz_oxide` 0.8.2 (`wheel_deflate.dll`)
- `tests/test_delta.py` — Regression and invariant tests for `RECORD` reconstruction, span reservation, PE round-trip, XZ/compact controls, prefix chaining, context deltas, and malformed-input rejection
- `benchmarks/` — Data fetch/verification, native build helpers, and experiment harnesses (`record_wheels.py`, `twice_wheel_more.py`, `twice_wheels.py`, `doubling_archive.py`, `followup_delta.py`)
- `followup/inputs-delta.json` — Frozen 16-project PyPI wheel manifest with SHA-256 digests
- `manuscripts/` — Paper source (`latex/wheels.tex`, `wheels.md`), verified summary (`summary.json`), and archived confirmation journals (`runs/wheels.jsonl`, `runs/wheel-original.jsonl`)
- `twice/`, `doubling/`, `paper/` — Archived journals, summaries, and historical combined reports from earlier study rounds
- `output/pdf/latex/wheels.pdf` — Compiled paper PDF

## Verification and reproduction

### Unit tests (Linux or Windows)

```bash
python3 -B -m unittest discover -s tests -v
```

All 13 pure-Python tests run on any platform with `numpy` installed; the single `miniz_oxide` FFI test (`test_archive_round_trips_raw_miniz_streams`) runs automatically when `.research/bin/wheel_deflate.dll` is built and is skipped otherwise.

### Rebuilding the paper PDF

```bash
pdflatex -interaction=nonstopmode -output-directory=output/pdf/latex manuscripts/latex/wheels.tex
pdflatex -interaction=nonstopmode -output-directory=output/pdf/latex manuscripts/latex/wheels.tex
```

### Full Windows benchmark replay

The archived benchmark journals were recorded on Windows x64 with Python 3.12, single-threaded BLAS/OpenMP/MKL/Rayon (`OMP_NUM_THREADS=1`, `OPENBLAS_NUM_THREADS=1`, `MKL_NUM_THREADS=1`), Rust, and MSYS2 UCRT64 GCC (including bzip2 development headers; set `WORKHORSES_UCRT64` if MSYS2 is installed outside the default path):

```powershell
python -m pip install -r requirements.txt
python -B -m benchmarks.build packages delta rust archive-codec
python -B -m benchmarks.paper_data --restore --inputs-only delta
python -B -c "from pathlib import Path; from benchmarks.paper_data import check_frozen; check_frozen(Path('followup/inputs-delta.json'), True, True)"
python -B -m unittest discover -s tests -v
python -B -m benchmarks.twice_wheel_more --output .research/replay/wheel-refined.jsonl
python -B -m benchmarks.twice_wheel_more --confirm .research/replay/wheel-refined.jsonl --output twice/runs/wheel-confirm.jsonl
python -B -m benchmarks.record_wheels --output .research/replay/wheels.jsonl
```

Verified byte totals and journal SHA-256 digests are recorded in [manuscripts/summary.json](manuscripts/summary.json) and [twice/summary.json](twice/summary.json). Use a fresh output path when replaying timed experiments; wall-clock timings depend on the workstation.

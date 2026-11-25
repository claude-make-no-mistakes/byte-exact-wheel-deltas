# Second optimization round

The targets were twice the replay and solve throughput and half the delivered wheel patch bytes. The comparisons use the original optimized controls and the same frozen inputs. The intervals resample independent counties, matrices, or projects. [Protocol](PROTOCOL.md) · [Reproduction](README.md)

## R*-tree forest

The six-level forest reached **2.65× [2.44–2.82]** median replay speed at capacity 32 with 30% reinsertion. With reinsertion off it reached **2.01× [1.94–2.09]**. The comparison with the first-round planar implementation was **2.46×** with reinsertion and **1.91×** without it.

| Capacity-32 profile | Reinsertion off | 30% reinsertion |
| --- | ---: | ---: |
| Moving hotspot | 1.27× | 1.44× |
| Write-heavy | 2.01× | 2.40× |
| Insert-only | 4.17× | 6.07× |

Including the initial build gave 2.21× with reinsertion off and 3.01× with it enabled. The four larger capacity-64 traces reached 2.00× and 2.24× for replay.

The directory derives its grid from the initial insertions. Each rectangle goes into the smallest cell that contains it, with the root handling rectangles outside the grid. A cell starts as a flat leaf and promotes to an R*-tree on overflow. Range queries use conservative subtree bounds, and nearest queries use their distance lower bounds to prune the search. The mechanism changes the layout while preserving the range result IDs and the nearest-distance multiset, including valid ties.

All 152 paired trace comparisons matched the archived query signatures. For each reinsertion setting, the candidate independently checked all 316,717 primary queries and 2,265 sampled scaling queries. The scaling signature covered all 83,690 queries per setting. Checkpoints also verified live membership, shard occupancy, tree levels, and conservative bounds. [Spatial journal](runs/spatial.jsonl)

## Repeated sparse solves

The adaptive solver reached **1.50× [1.06–1.72]** across all 19 matrix patterns for the same twelve chronological requests. The ratio includes factorization and response setup. With the shared AMD ordering charged to both routes it reached 1.45× [1.06–1.67]. The fastest individual adaptive result was 11.75× on FIDAP/ex15.

Setup was the main constraint. In the earlier measurements, setup alone exceeded half the baseline sequence on 14 of 19 patterns even after skipping the eight dense response solves. The new route chooses between simplicial and supernodal factorization using CHOLMOD's symbolic flop-to-fill estimate. The cutoff of 140 came from the preliminary probe. After a failed Woodbury check, it reuses the base factor for sequential diagonal updates and applies one correction using a long-double residual. The candidate switched on the same three patterns that previously required fresh shifted factorizations. [CHOLMOD factorization options](https://github.com/DrTimothyAldenDavis/SuiteSparse/blob/dev/CHOLMOD/Include/cholmod.h)

| Method | Median speed | Verified requests | Direct refactor fallbacks |
| --- | ---: | ---: | ---: |
| Original checked Woodbury | 1.00× | 684/684 | 108 |
| Adaptive factor selection and recovery | 1.50× | 684/684 | 0 |
| Forced simplicial | 1.05× | 684/684 | 108 |
| Wider supernodes with postorder | 1.11× | 684/684 | 108 |
| Incomplete-Cholesky PCG, 24 iterations | 0.88× | 684/684 | 468 |

Every result met the same $10^{-11}$ independent backward-error threshold. The worst manufactured forward error was 0.48% for the adaptive solver and 0.58% for the control. PCG completed 216 requests on its iterative route. It reached 5.84× on Trefethen_2000 and 3.04× on shallow_water1, with budget exhaustion leading to checked direct solves on other patterns. Selecting the fastest measured method separately on each matrix after the experiment would yield 1.70× median throughput, an indication of the remaining gap across this method set. [Sparse journal](runs/sparse.jsonl)

## Whole-wheel patch bytes

The final selector delivered **441,126 bytes** across the 16 wheel pairs. That is **50.3% below** the frozen 888,127-byte control and **27.9% below** the first round's 611,442 bytes. The result includes the archive recipes, codec flags, hashes, and complete patch streams. The measured total was 2,937 bytes below the halfway target.

Independent expansion exposed data from new and renamed ZIP members. The final sweep also admitted smaller members, used variable-width bsdiff controls when their compressed size was lower, and tried reversible PE labels for function and read-only data references. The best zstandard patch used an already reconstructed part of the target as additional reference for the second part. The decoder derives that reference from the source wheel and delivered patch. [Microsoft PE format](https://learn.microsoft.com/en-us/windows/win32/debug/pe-format#the-pdata-section)

| Wheel | Original control | First round | Final |
| --- | ---: | ---: | ---: |
| setuptools | 75,348 | 73,475 | 16,727 |
| lxml | 106,073 | 78,926 | 69,665 |
| contourpy | 75,195 | 62,523 | 52,976 |
| rpds-py | 233,779 | 74,342 | 62,074 |
| zstandard | 235,969 | 178,882 | 157,400 |
| All 16 | 888,127 | 611,442 | 441,126 |

The paired delivery trial reached 464,911 bytes. Its median encode time was 9.55× [5.90–11.69] the control and decode time was 1.15× [0.98–1.39]. The final size sweep verified 90 candidate patches. Re-encoding then reproduced all 16 winning sizes and all 16 published target wheels byte for byte. The final patches use the current wheel decoder and the DEFLATE codec recorded in each recipe. [Timed wheel journal](runs/wheels.jsonl) · [Final size sweep](runs/wheel-refined.jsonl) · [Winning patch confirmation](runs/wheel-confirm.jsonl)

## Self-review

All 37 tests passed. The analysis reproduces the frozen controls and checks complete paired coverage. The archive confirmation records the regenerated patch hashes alongside the old and new wheel hashes. [Machine-readable summary](summary.json)

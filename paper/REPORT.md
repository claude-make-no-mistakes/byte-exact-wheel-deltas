# Four workhorse algorithm studies

**Adaptive cubature, sparse ordering, dynamic R*-trees, and BSDIFF40**

## Abstract

We evaluated four proposed optimizations on inputs held out from development. An affine-hinge fit increased verified cubature completion by 66.7 pp [53.1–79.2] on quadratic-background kinks. Completion rates matched in the oscillatory family. Bounded AMD and adaptive COLAMD reached 0.85× [0.82–0.94] and 0.98× [0.95–1.01] of stock first-factorization pipeline speed. A prefix/suffix split pass reached 1.09× [1.08–1.11] of stock R*-tree throughput with reinsertion off and preserved topology signatures and query answers. A compatible BSDIFF40 postprocessor saved 0.0306% of delivered bytes and cost 10.05× the total encode time of its seed encoder. These experiments use 288 integrands, 43 sparse matrices, 24 counties, and 24 software projects. Each study uses input-level pairing and checks correctness independently of the optimization objective.

| Study | What the expanded cohort supported | Independent unit |
| --- | --- | --- |
| Cubature | A higher completion rate for a defined integrand family and a reusable pilot cache. | Integrand |
| Sparse ordering | The extra ordering work exceeded the median factorization savings. | Matrix collection group within factorization class |
| Spatial indexing | A faster score evaluation for the same split. Replay throughput depended on the write mix. | County |
| Binary delta | Small stream-remap headroom and limited payoff from four boundary trials. | Release project |

## Study design

The pilot set the method settings and practical targets. This study froze new inputs or random seeds before comparative measurements. We randomized method order within timing blocks and took the median of three repeats for each method and condition. The reported speed ratios use reference time divided by candidate time. Within each independent unit, we aggregate paired ratios by the median on the log scale over conditions such as tolerances, relabelings, or trace profiles. We exponentiate the median across independent units. Bracketed intervals are 95% percentile intervals from 20,000 bootstrap resamples of independent units. They describe variation across these input cohorts on one workstation. Secondary contrasts and the four larger spatial replays are descriptive.

The timing stages ran serially on one Windows x64 workstation. BLAS and Rayon were limited to one thread. The ParAMD parallel control requested four OpenMP threads. Journals record settings, failures, and executable hashes. [Frozen protocol](PROTOCOL.md) · [Reproduction guide](README.md) · [Machine-readable summary](summary.json)

## 1. Adaptive cubature

### Method and estimand

We integrate deterministic scalar functions on the unit cube in dimensions 2, 4, 6, and 8. An independent seed determines each parameter draw. There are 48 draws per single-kink family and 32 per other family. Each draw is evaluated at absolute tolerances 3 × 10⁻⁵ and 3 × 10⁻⁶, with relative tolerance zero and a 65,536-point cap. References use analytic separable or trigonometric integrals and a 100-digit hinge reference. For peak tests, the integral is near one even when the samples miss the peak.

The baseline uses directional subdivision and a Genz–Malik cubature rule. The blind variant spends 256 fit and 64 validation points, estimates a single affine hinge against a quadratic background, and accepts it only if both sides are sampled and the held-out residual norm falls to at most 55% of the background-only norm. It integrates the fitted hinge analytically and applies the unchanged rule to the residual. The known-plane comparison supplies the manufactured hinge to bound the cost of estimating its parameters. External controls use SciPy's Genz–Malik rule, Cuba Cuhre, and eight independently scrambled Sobol nets with up to six nested looks. The Sobol stopping threshold uses a Student-width rule with Bonferroni adjustment for the scheduled looks. Its calibration is empirical. [01–05]

### Single kinks

Across both tolerances, blind fitting returned 86 verified answers out of 96 on quadratic kinks. Directional Genz–Malik returned 22, Cuhre 24, and the known-plane method 96/96. Averaged equally over the two tolerances within each draw, the paired increase in actual tolerance attainment versus directional was **58.3 pp [45.8–70.8]**. The corresponding draw-level sign test had 33 net improvements, 0 declines, and two-sided p = 2.3e-10. The paired increase in verified convergence was **66.7 pp [53.1–79.2]**. At the tighter tolerance alone, the verified gain was 66.7 pp [52.1–79.2], with 32 blind-only verified draws.

![Verified and independently accurate estimates for two single-kink families](figures/cubature.png)

**Figure 1.** Both panels contain 48 independent integrands at each tolerance. Dark bars require reported convergence and true error within tolerance. Light extensions are accurate returned estimates without reported convergence. The SciPy and Cuhre adapters return no estimate when the evaluation cap interrupts the method. The known-plane control received the kink parameters directly.

## Cubature robustness and cost

| Integrand family | n | Directional | Blind | SciPy | Cuhre | Sobol | Known plane |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Quadratic kink | 96 | 22 | 86 | 22 | 24 | 22 | 96 |
| Mixed kink | 96 | 24 | 24 | 24 | 24 | 17 | 34 |
| Two kinks | 64 | 16 | 16 | 16 | 16 | 9 | — |
| Curved kink | 64 | 24 | 24 | 16 | 37 | 46 | — |
| Smooth Gaussian | 64 | 34 | 34 | 25 | 45 | 22 | — |
| Oscillatory | 64 | 16 | 16 | 16 | 24 | 7 | — |
| Product peak | 64 | 40 | 39 | 29 | 50 | 43 | — |
| Narrow Gaussian | 64 | 2 | 2 | 0 | 2 | 0 | — |

**Table 1.** Verified case-and-tolerance counts. Denominators include caps and false convergence. The known-plane control applies to the two single-kink families. The cached variant had the same outcomes as the blind fit.

| Family and reference | Draws / tolerances | Point ratio | Wall time ratio |
| --- | --- | --- | --- |
| Quadratic / directional | 12 / 22 | 9.94× [7.80–11.41] | 1.05× [0.80–1.21] |
| Quadratic / Cuhre | 12 / 24 | 28.52× [23.20–31.18] | 0.29× [0.22–0.35] |
| Mixed / directional | 12 / 24 | 0.40× [0.32–0.90] | 0.28× [0.20–0.50] |
| Both kink families / directional | 24 / 46 | 1.58× [0.47–9.12] | 0.54× [0.28–0.89] |

**Table 2.** Reference divided by blind on cases where both methods verified. Points include pilot evaluations. Intervals resample integrands, keeping both tolerances together. These conditional ratios concern a subset of cases and accompany the coverage table.

On quadratic kinks at dimensions 2, 4, 6, and 8, blind versus directional attainment was 24 vs 22, 24 vs 6, 24 vs 2, 14 vs 0 out of 24. The fit accepted 18 of 96 inputs with oscillatory backgrounds and 86 of 96 with quadratic backgrounds. Blind and directional each verified 24 mixed cases. The supplied plane verified 34/96 there. On shared quadratic successes, the wall-time interval relative to directional spanned parity. Cuhre was faster. The two-family result missed the 2× evaluation and lower wall-time target.

Caching the QR factorization and starting features yielded **1.32× [1.29–1.35]** warm speed over 288 integrands. Cold preparation had dimension-specific medians of 6.0–7.7 ms. The cache changed 0 evaluation counts or convergence decisions. The maximum estimate difference was 3.3e-16. Timing includes function evaluation, fitting, analytic correction, and adaptive integration.

Blind and directional falsely converged on 62 of 64 narrow-peak cases. The fit was rejected on every narrow peak. Scrambled Sobol falsely converged on 46 cases. The single-hinge result supports optional use under the tested model. Narrow peaks need independent resolution checks. [Coverage by dimension and family](summary.json)

## 2. Sparse matrix ordering

### Controlled factorization pipelines

We selected 19 real symmetric positive definite (SPD) and 24 structurally full-rank nonsymmetric matrices from a frozen SuiteSparse catalog. Within each class, a collection group contributes at most one matrix, and every pilot group is excluded. Orders are stratified and the catalog filters bound the estimated factor nonzeros and work. We keep the released values. Each matrix runs under its original labeling and one seeded consistent row and column relabeling. [06–10]

The CHOLMOD control uses a supplied permutation with additional postordering disabled. The bounded AMD variant examines a prefix of four equal-degree pivots and spends at most 192 quotient-graph inspections per candidate. A replacement requires a certified smaller immediate fill score using intervals. Exact lookahead uses the same candidate prefix without the practical inspection cap. Stock AMD, its disabled hook, METIS, and ParAMD are matched controls. The LU experiment supplies each COLAMD permutation to the same SuperLU pipeline, with natural column ordering afterward, scaling disabled, and the same partial pivoting threshold. Adaptive COLAMD changes density knobs when unusually dense rows or columns account for a dominant share of a fixed work proxy.

The primary stopwatch sums ordering, analysis or column materialization, and the first numerical factorization. We validate every permutation, solve against a seeded right-hand side, and measure the infinity-norm backward error scaled by ‖A‖∞‖x‖∞ + ‖b‖∞. All 1374/1374 timed attempts verified against the 10⁻¹¹ threshold. The maximum observed backward error was 5.5e-15. Stock and hook-disabled AMD had identical permutation hashes in 38 label conditions with 0 mismatches.

| Ordering method | First factorization pipeline | Order only | Factor nonzeros | Storage |
| --- | --- | --- | --- | --- |
| SPD / Hook disabled | 1.00× [0.99–1.01] | 0.97× | 1.000× | 1.000× |
| SPD / Bounded lookahead | 0.85× [0.82–0.94] | 0.41× | 1.000× | 1.000× |
| SPD / Exact lookahead | 0.75× [0.74–0.79] | 0.22× | 1.010× | 1.006× |
| SPD / METIS | 0.25× [0.22–0.42] | 0.14× | 1.045× | 1.163× |
| SPD / ParAMD (1 thread) | 0.86× [0.77–0.90] | 0.35× | 0.976× | 0.974× |
| SPD / ParAMD (4 threads) | 0.50× [0.26–0.66] | 0.10× | 0.965× | 0.973× |
| LU / Fixed row threshold 4 | 1.01× [1.00–1.04] | 0.99× | 1.000× | 1.000× |
| LU / Fixed column threshold 4 | 0.99× [0.97–1.01] | 1.01× | 1.000× | 1.000× |
| LU / Adaptive thresholds | 0.98× [0.95–1.01] | 0.88× | 1.000× | 1.000× |

**Table 3.** Ratios to stock AMD on SPD matrices or stock COLAMD on LU matrices. Values above one indicate less candidate time or storage. The SPD storage column counts CHOLMOD numeric slots. The LU storage column counts L and U CSC array bytes. Factor nonzeros measure the symbolic Cholesky count on SPD matrices and the stored L + U nonzero count on nonsymmetric matrices.

## Sparse ordering mechanism and limits

![Paired first-factorization pipeline time ratios and bootstrap intervals](figures/sparse.png)

**Figure 2.** Median paired speed with 95% group-bootstrap intervals on a log scale. The dashed line is parity. The two matrix classes use separate stock baselines and separate cohorts.

### The cost of looking ahead

Bounded AMD changed 135,023 of 489,464 scored choices, counting one block per label. Ordering reached 0.41× [0.25–0.63] and analysis plus factorization 1.01× [0.97–1.04] of stock speed. Median factor nonzeros and storage stayed at parity. Exact lookahead traded more ordering time for a small drop in symbolic fill. Subsequent quotient-graph changes and supernode storage limit what a lower immediate fill score can predict.

### Adaptive density thresholds

Adaptive COLAMD triggered on 3 of 24 groups. Ordering reached 0.88× [0.83–0.93] and analysis plus factorization 1.01× [0.99–1.03] of stock speed. Both fixed-threshold controls were near parity. Table 4 gives all three triggered cases without a subgroup interval.

| Triggered matrix | vs stock | vs row 4 | vs col. 4 | Nonzeros vs stock |
| --- | --- | --- | --- | --- |
| Hollinger/jan99jac080sc | 0.20× | 0.19× | 0.19× | 0.392× |
| Shen/shermanACb | 2.87× | 2.41× | 3.11× | 2.958× |
| Meszaros/iprob | 0.92× | 0.92× | 0.95× | 1.000× |

**Table 4.** Reference-to-adaptive speed and factor nonzero ratios for the matrices whose requested density knobs differed from stock.

Both policies fell short of the 1.20× pipeline target. The timings apply to a first factorization at the selected orders and fill ranges. Repeated factorizations, equilibrated LU, and larger matrices are outside the measured comparison.

## 3. Dynamic R*-trees

### Reusing split bounds

Boost scores all legal cuts in each corner sort. Let n be the number of entries in an overfull node and m the minimum occupancy. The left group size ranges from m through n − m. The stock loop initializes two boxes and expands them n − 2 times per cut, or (n − 2m + 1)(n − 2) times per sort. The revised loop seeds a box at either end and accumulates each bounding box with one union as the cut advances. Each side grows from m to n − m entries, for 2(n − m − 1) expansions per sort. The accumulation is linear in n and sorting stays O(n log n). At the primary capacity, n = 33 and m = 10 (434 → 44 expansions). At larger capacity, n = 65 and m = 20 (1,638 → 88 expansions).

On the tested Cartesian rectangles, box union is coordinatewise minimum and maximum. Induction on the traversed entries gives the same two boxes at every legal cut as a fresh scan. The score calculation and cut order are preserved, including ties. With identical input and insertion histories this preserves the same split decision, and induction over updates gives the same tree structure. Prefix/suffix accumulation has precedent in bounding-volume hierarchies and other spatial code. This contribution measures the substitution in the pinned Boost path. [11–13]

### Traces and verification

We use TIGER/Line 2024 road-object bounding boxes from 24 counties stratified by Census region and population band. Each county supplies 12,000 operations in each of three generated profiles. Moving hotspot requests queries with probability 0.75. Write-heavy uses 0.35. Insert-only has no queries. Four preassigned higher-population counties also provide 60,000-operation write-heavy traces at capacity 64. All measurements time a replay after the initial insertion phase. The stand-alone C++ binary switches the split policy at runtime. Reinsertion is either disabled or set to the integer floor of 30% of capacity for the matched split pair. [14]

![Spatial throughput by workload, split mode, and tree capacity](figures/spatial.png)

**Figure 3.** Paired replay throughput. Prefix comparisons hold reinsertion fixed. Query history uses stock with reinsertion off. Each of the three main profiles covers 24 counties. The last row describes four larger traces preassigned from the same cohort.

## Spatial replay results

The primary traces contained 864,000 operations and 317,260 queries. For each control and candidate, the verifier checked every primary query against a scan, comparing range result IDs and nearest-distance multisets with cutoff ties. Larger traces compared signatures of all 84,247 query results and scanned 2,281 queries per method, sampling range and nearest queries separately. Prefix comparisons agreed on periodic topology signatures in 152 method–trace pairs. The verification run recorded 0 failed checks. Tree scores and shape can be established from the bound identity above on these inputs. The hashes additionally check the executable on data with equal-coordinate ties and closed edges.

| Trace profile | Counties | Prefix / off | Prefix / 30% | Query history |
| --- | --- | --- | --- | --- |
| Moving hotspot | 24 | 1.04× [1.03–1.05] | 1.01× [0.98–1.03] | 0.95× [0.94–0.97] |
| Write-heavy | 24 | 1.08× [1.07–1.11] | 1.08× [1.06–1.09] | 0.93× [0.92–0.94] |
| Insert-only | 24 | 1.26× [1.23–1.29] | 1.14× [1.13–1.17] | 1.00× [0.99–1.01] |
| Larger write-heavy | 4 | 1.09× [1.07–1.22] | 1.03× [1.00–1.08] | 0.96× [0.95–1.09] |

**Table 5.** Replay wall-time ratios by profile. The larger traces use capacity 64, up to 60,000 initial entries, and a 60,000-operation replay. The 24-county comparisons use capacity 32 and up to 3,000 initial entries. Each interval is grouped by county.

| Method | Initial build | Query p95 | Query p99 | Update p95 | Update p99 |
| --- | --- | --- | --- | --- | --- |
| Prefix / off | 1.26× [1.24–1.27] | 0.99× [0.97–1.00] | 0.98× [0.97–1.01] | 1.04× [1.02–1.06] | 1.61× [1.58–1.65] |
| Prefix / 30% | 1.15× [1.12–1.16] | 0.98× [0.97–1.00] | 0.99× [0.96–1.01] | 1.02× [1.00–1.03] | 1.24× [1.22–1.27] |
| History / off | 1.00× [0.98–1.00] | 1.01× [0.99–1.02] | 1.01× [0.98–1.02] | 1.00× [0.99–1.01] | 0.72× [0.62–0.77] |

**Table 6.** Paired build and per-operation latency ratios over the primary profiles. Query latency aggregates the two mixed profiles. The timed replay includes per-operation timing overhead and excludes trace parsing and counter-enabled verification.

Pooling the primary profiles within counties, the prefix split reached **1.09× [1.08–1.11]** of stock replay throughput with reinsertion off and **1.08× [1.07–1.09]** with reinsertion enabled. The number of counted split-bound expansions for the no-reinsertion replays fell from 36,681,680 to 3,718,880. These are counts of expansion work implied by the executed split calls. Queries use the same tree layout. Replay gains depend on how much time the trace spends splitting and on interactions with reinsertion.

The query-history policy performed 128 sibling redistributions. It cut range-node visits by 1.18% and changed instrumented non-root leaf writes by +0.78%. Its throughput was 0.95× [0.94–0.97] and the geometry policy achieved 0.83× [0.80–0.83]. The history policy fell short of the 1.25× throughput target. The split pass is a separate improvement with a simpler correctness contract. The study covers in-memory, two-dimensional boxes and generated query traces. Persistence, geographic distance, concurrent access, and other Boost traversal strategies were outside scope.

## 4. Compatible binary delta encoding

### Matching formats and workload units

We froze two PyPI releases for each of 24 projects, with an identical platform and ABI tag within each pair. The workload contains 48 pairs across wheels, 10 shared native members, and 14 shared Python members. A deterministic rule prefers a common eligible native member, then the member with the largest minimum length. It retains 5 byte-identical pairs. Encoders see the same byte sequences and have a 15-second process cap for each encode and decode. The compatible candidate starts from HDiffPatch's BSDIFF40 encoder. It tests equality of duplicate source segments, explores at most four ranked boundary shifts, and keeps the smallest verified encoding seen. [15–19]

We require byte-for-byte reconstruction. For BSDIFF40 the in-memory decoder and stock `bspatch` must both succeed. HDiffPatch native and xdelta3 use their format-specific decoders. Encoder time includes process startup. The postprocessor's encoder time includes its seed process, with a 12-second nested allowance, and all parsing and recompression. Raw results retain timeouts, process exit reasons, and repeats censored after an encode timeout. Paired comparisons use only complete verified pairs and retain full coverage denominators.

| Encoder | Verified pairs | Bytes vs HDiff BSD | Total encode time | Decode time ratio |
| --- | --- | --- | --- | --- |
| Classic bsdiff | 48/48 | +3.186% | 2.52× | 1.01× [0.96–1.06] |
| HDiffPatch BSD | 48/48 | Reference | Reference | Reference |
| bsdiff6 comparator repair | 8/48 | -3.092% | 313.34× | 1.01× [0.91–1.31] |
| HDiffPatch native | 48/48 | -4.140% | 1.14× | 1.22× [1.08–1.46] |
| xdelta3 VCDIFF | 48/48 | +1.082% | 1.20× | 1.33× [1.07–1.66] |
| Compatible candidate | 48/48 | -0.031% | 10.05× | 1.12× [1.00–1.24] |

**Table 7.** Bytes and encode elapsed time are ratios of sums of case medians for the pair set shared with HDiffPatch BSD. The decode column uses median paired reference time divided by treatment time and project-cluster intervals. Native methods change the patch format and serve as context for applications that can choose the decoder.

The pinned bsdiff6 comparator delegates from `cmp` to `partial_cmp` and back, with no base case. The unchanged executable hit the cap in 4/4 prespecified synthetic runs. A separate repair establishes a total order over the existing tuple fields and can affect chosen matches. It reconstructed 4/4 synthetic targets and verified 8/48 release pairs. Release attempts included 114 path-finder index panics and 2 encode timeouts. The panicking branch passes the target length into a routine expecting an index into 64 candidates. Among the 8 pairs with all compatible controls verified, HDiffPatch BSD had the lowest or tied-lowest size on 5, classic on 1, and repaired bsdiff6 on 6.

## Binary delta headroom and cost

### A size bound on control-stream remapping

Write a BSDIFF40 patch length as 32 + C + D + L for its header and compressed control, difference, and literal streams. Remapping a copy to an equal source slice can hold D and L fixed. The reduction is then C − C′ ≤ C, since C′ ≥ 0. Boundary moves change the parse and can affect all three streams.

The seed patches had 180,309 control bytes out of 7,347,524 total bytes, an aggregate headroom of **2.45%**. The median project bound was 13.39% [5.59–16.84], and 24/48 pairs allowed a theoretical gain of 5% or more. Small patches can have large relative bounds and little aggregate weight.

![Control-stream upper bounds by file representation](figures/delta.png)

**Figure 4.** Each dot is one frozen wheel or common member. The dashed line marks the 5% target. File pairs from the same project remain grouped for uncertainty estimates. The logarithmic axis shows headroom of small control streams in the larger wheels.

### Delivered patches

The candidate saved **2,245 of 7,347,524 bytes (0.0306%)**. The aggregate counts wheels and members as separate conditions. Wheels accounted for 593 saved bytes, native members 1,506, and Python members 146. Equal project weights yielded a median saving of 0.050% [0.013–0.096]. The method reduced 30 patches, increased 0, and verified 144 timed outputs across 48/48 pairs. One repeat per pair recorded 55 accepted remaps and 23 chosen boundary changes.

Aggregate encode time was **10.05×** the seed time. Reference-to-candidate encode wall speed was 0.05× [0.04–0.06]. The CPU ratio was 0.82× [0.71–1.00] over 24 projects with positive matched process times. The peak working-set ratio was 0.56× [0.53–0.57], using the sum of the parent and seed peaks for the candidate. The decode time ratio was 1.12× [1.00–1.24].

The candidate missed the pilot's 5% size target and 2× encode-time budget. The study covers postprocessing of completed BSDIFF40 patches on the selected releases.

## References and artifacts

[01] A. Genz and A. Malik. Adaptive numerical integration over an N-dimensional rectangular region. *Journal of Computational and Applied Mathematics*, 1980. [Publisher record](https://www.sciencedirect.com/science/article/pii/0771050X8090039X)

[02] T. Hahn. Cuba, a library for multidimensional numerical integration. *Computer Physics Communications*, 2005. [Paper](https://arxiv.org/abs/hep-ph/0404043) · [Cuba source](https://feynarts.de/cuba/)

[03] SciPy developers. Cubature, Genz–Malik rule and public API. [Documentation](https://docs.scipy.org/doc/scipy/reference/generated/scipy.integrate.cubature.html)

[04] SciPy developers. Scrambled Sobol sequences and balance properties. [Documentation](https://docs.scipy.org/doc/scipy/reference/generated/scipy.stats.qmc.Sobol.html)

[05] D. Meister and T. Harada. Geometric integration for neural control variates. *Computer Graphics Forum*, 2025. [Paper](https://diglib.eg.org/bitstream/handle/10.1111/cgf70275/cgf70275.pdf)

[06] P. Amestoy, T. Davis, and I. Duff. AMD, an approximate minimum-degree ordering algorithm. *ACM TOMS*, 2004. [Article](https://doi.org/10.1145/1024074.1024081)

[07] T. Davis, J. Gilbert, S. Larimore, and E. Ng. COLAMD, a column approximate minimum-degree ordering algorithm. *ACM TOMS*, 2004. [Article](https://doi.org/10.1145/1024074.1024080)

[08] T. Davis and Y. Hu. The University of Florida Sparse Matrix Collection. *ACM TOMS*, 2011. [Article](https://doi.org/10.1145/2049662.2049663) · [Collection](https://sparse.tamu.edu/)

[09] SuiteSparse and SuperLU authors. [CHOLMOD and COLAMD source](https://github.com/DrTimothyAldenDavis/SuiteSparse) · [SuperLU source](https://github.com/xiaoyeli/superlu)

[10] Y.-H. Chang, A. Buluç, and J. Demmel. Parallelizing the approximate minimum degree ordering algorithm, 2026. [Paper](https://arxiv.org/html/2504.17097v2) · [Source](https://github.com/PASSIONLab/ParAMD)

[11] N. Beckmann, H. Kriegel, R. Schneider, and B. Seeger. The R*-tree. *SIGMOD*, 1990. [Paper](https://people.csail.mit.edu/tdanford/6830papers/beckmann-r-star-tree.pdf)

[12] Boost developers. R*-tree redistribution in Boost.Geometry 1.92. [Pinned source](https://github.com/boostorg/geometry/blob/boost-1.92.0/include/boost/geometry/index/detail/rtree/rstar/redistribute_elements.hpp)

[13] M. Pharr, W. Jakob, and G. Humphreys. Forward and backward bound accumulation for the surface-area heuristic. [PBRT 4th edition](https://pbr-book.org/4ed/Primitives_and_Intersection_Acceleration/Bounding_Volume_Hierarchies)

[14] U.S. Census Bureau. TIGER/Line 2024 roads and Vintage 2024 county estimates. [Road archives](https://www2.census.gov/geo/tiger/TIGER2024/ROADS/) · [Population table](https://www2.census.gov/programs-surveys/popest/datasets/2020-2024/counties/totals/co-est2024-alldata.csv)

[15] C. Percival. Naïve differences of executable code. [Paper](https://www.daemonology.net/papers/bsdiff.pdf) · [BSDIFF](https://www.daemonology.net/bsdiff/)

[16] HDiffPatch and xdelta authors. [HDiffPatch source and options](https://github.com/sisong/HDiffPatch) · [xdelta3 source](https://github.com/jmacd/xdelta)

[17] J. Rederlechner, U. Planta, and A. Abbasi. One small patch for a file, one giant leap for OTA updates. *SpaceSec*, 2026. [Paper](https://www.ndss-symposium.org/wp-content/uploads/spacesec26-70.pdf) · [Pinned reconstruction source](https://github.com/Julian-Rederle/bsdiff6/tree/25b63ab56330b42ac6cc6718a9a29d938dc79a7a)

[18] Python Package Index. Release metadata and distribution artifact hashes. [JSON API](https://docs.pypi.org/api/json/)

[19] D. Korn, J. MacDonald, J. Mogul, and K. Vo. The VCDIFF generic differencing and compression data format, 2002. [RFC 3284](https://www.rfc-editor.org/rfc/rfc3284)

Frozen manifests, scripts, complete raw workloads, and instructions for regenerating the summaries and figures accompany this report. [Artifact guide](README.md)

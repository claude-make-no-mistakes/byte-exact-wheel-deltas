"""Mix compatible parses while comparing the fully compressed BSDIFF40 size."""

from dataclasses import dataclass

from workhorses.delta import MAX_CONTROLS, Instruction, Patch, _positions, apply, pack, unpack


@dataclass(frozen=True)
class HybridAudit:
    windows: int
    exact_encodings: int
    winning_switches: int
    input_minimum: int


def _spans(patch):
    diff, literal = memoryview(patch.difference), memoryview(patch.literal)
    d = l = 0
    for (source, target), item in zip(_positions(patch.instructions), patch.instructions):
        if item.difference:
            stop = d + item.difference
            yield target, source, diff[d:stop]
            d = stop
        if item.literal:
            stop = l + item.literal
            yield target + item.difference, None, literal[l:stop]
            l = stop


def _windows(patch, size):
    spans = iter(_spans(patch))
    current = next(spans, None)
    for left in range(0, patch.size, size):
        right = min(left + size, patch.size)
        parts = []
        while current is not None and current[0] < right:
            target, source, stream = current
            start, stop = max(left - target, 0), min(len(stream), right - target)
            parts.append((None if source is None else source + start, stream[start:stop]))
            if target + len(stream) > right:
                break
            current = next(spans, None)
        yield parts


def _assemble(parts):
    # The output stream order is implicit. A difference span still addresses
    # its original source bytes, even when its neighbor comes from another parse.
    records = []
    diff, literal = [], []
    diff_size = literal_size = 0
    for source, data in parts:
        if source is None:
            if not records:
                records.append([0, 0, 0])
            records[-1][2] += len(data)
            literal.append(data)
            literal_size += len(data)
        else:
            if records and not records[-1][2] and source == records[-1][0] + records[-1][1]:
                records[-1][1] += len(data)
            else:
                records.append([source, len(data), 0])
            diff.append(data)
            diff_size += len(data)
    controls = []
    if records and records[0][0] != 0:
        controls.append(Instruction(0, 0, records[0][0]))
    for i, (source, count, extra) in enumerate(records):
        seek = records[i + 1][0] - source - count if i + 1 < len(records) else 0
        controls.append(Instruction(count, extra, seek))
    return Patch(diff_size + literal_size, tuple(controls), b"".join(diff), b"".join(literal))


def _choose(costs, penalty):
    previous = costs[0]
    back = []
    for a, b in costs[1:]:
        left = int(previous[1] + penalty < previous[0])
        right = int(not (previous[0] + penalty < previous[1]))
        back.append((left, right))
        previous = (a + previous[left] + penalty * left,
                    b + previous[right] + penalty * (1 - right))
    path = [int(previous[1] < previous[0])]
    for stage in reversed(back):
        path.append(stage[path[-1]])
    return tuple(reversed(path))


def combine(old: bytes, new: bytes, first: bytes, second: bytes, *, window: int = 32 << 10):
    """Return the best input or a smaller compatible encoding of the same target.

    Per-window compressed costs propose paths. Only the exact full patch size
    selects the winner, and the winning bytes must reconstruct the target.
    """
    if window < 1:
        raise ValueError("window size must be positive")
    a, b = unpack(first), unpack(second)
    if apply(old, a) != new or apply(old, b) != new:
        raise ValueError("both seed patches must reconstruct the requested target")
    best = min((first, second), key=len)
    minimum = len(best)
    blocks = list(zip(_windows(a, window), _windows(b, window)))
    if not blocks:
        return best, HybridAudit(0, 0, 0, minimum)
    try:
        costs = [tuple(len(pack(_assemble(parts))) for parts in pair) for pair in blocks]
    except OverflowError:
        # A window can start at an offset that needs several original seeks.
        return best, HybridAudit(len(blocks), 0, 0, minimum)
    paths = dict.fromkeys(_choose(costs, penalty) for penalty in (0, 16, 64))
    attempts = switches = 0
    for decisions in paths:
        assembled = _assemble(part for pair, use in zip(blocks, decisions) for part in pair[use])
        if len(assembled.instructions) > MAX_CONTROLS:
            continue
        try:
            patch = pack(assembled)
        except OverflowError:
            continue
        attempts += 1
        if len(patch) < len(best):
            best = patch
            switches = sum(a != b for a, b in zip(decisions, decisions[1:]))
    if best is not first and best is not second and apply(old, unpack(best)) != new:
        raise RuntimeError("the mixed patch failed independent reconstruction")
    return best, HybridAudit(len(blocks), attempts, switches, minimum)

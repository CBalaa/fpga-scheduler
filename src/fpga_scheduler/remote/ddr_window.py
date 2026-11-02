"""Plan bounded physical-DDR backdoor reads for application-declared outputs.

The NH profile maps `nanhu_xilinx.i_ddr[0]` to the deployment's physical DDR base
(default 0x80000000) with one 64-byte RTL element per address. Two read modes exist:

* `prefix` (default): read the verified prefix `i_ddr[N-1:0]` from the base through
  the highest declared output, exactly like the established image readback path.
  Correct on already-validated hardware but the snapshot grows with the address.
* `window`: read only the 64-byte-aligned span that contains every declared output,
  using `i_ddr[high:low]`. This keeps far outputs cheap (622 MiB offset -> KiB read)
  but relies on nonzero RTL slices, which were not exercised by the original image
  readback. Validate it once per hardware profile with an A/B artifact comparison.

The module is pure (no hardware, no I/O) so it can be unit tested and reused by
`uvhs_session.capture_outputs`.

`plan()` returns the single bounded read that covers every declaration:

* `mode`: the effective read mode;
* `window_start`: first physical byte stored in the snapshot file;
* `window_size`: number of snapshot bytes (multiple of ELEMENT);
* `memory`: RTL selection expression for `readmem -rtl`;
* `element_low` / `element_high`: included RTL element indices relative to `base`.

The caller extracts each output with `source.seek(addr - window_start)`, then reads
exactly `nbytes` bytes.
"""
import re

ELEMENT = 64
ADDRESS_SPACE = 2 ** 32
ADDRESS = re.compile(r'0x[0-9a-fA-F]{1,8}')


class WindowError(ValueError):
    """A declared output cannot be read inside the configured DDR window."""


def address(value):
    """Parse one physical address; only 32-bit `0x`-hex strings are accepted."""
    if not isinstance(value, str) or ADDRESS.fullmatch(value) is None:
        raise WindowError('Invalid output physical address')
    result = int(value, 16)
    if result >= ADDRESS_SPACE:
        raise WindowError('Output address is outside the 4 GiB physical address space')
    return result


def plan(outputs, base, span, mode='prefix'):
    """Validate declarations and return the single read that covers them.

    `outputs` is a non-empty list of declaration dicts with `address` (0x hex
    string) and `nbytes`. `base` must be 64-byte aligned; `span` is the
    configured permitted window size. Raises `WindowError` for an invalid mode,
    an empty declaration list, a misaligned base/span, or an output that does not
    fit inside `[base, base + span)` of the 4 GiB physical address space.
    """
    if mode not in ('prefix', 'window'):
        raise WindowError("DDR read mode must be 'prefix' or 'window'")
    if type(base) is not int or base < 0 or base % ELEMENT:
        raise WindowError('DDR base must be a nonnegative multiple of 64')
    if type(span) is not int or span < ELEMENT or span % ELEMENT:
        raise WindowError('DDR window span must be a positive multiple of 64')
    if base + span > ADDRESS_SPACE:
        raise WindowError('DDR base and span must fit the 4 GiB physical address space')
    if not isinstance(outputs, (list, tuple)) or not outputs:
        raise WindowError('No outputs to read')
    first = last = None
    for item in outputs:
        if not isinstance(item, dict):
            raise WindowError('Invalid output declaration')
        try:
            start, size = address(item['address']), item['nbytes']
        except KeyError:
            raise WindowError('Invalid output declaration') from None
        if type(size) is not int or size < 1:
            raise WindowError('Invalid output byte count')
        end = start + size
        if start < base or end > base + span:
            raise WindowError('Output lies outside the configured physical DDR window')
        if first is None or start < first:
            first = start
        if last is None or end > last:
            last = end
    window_end = (last + ELEMENT - 1) // ELEMENT * ELEMENT
    if mode == 'prefix':
        window_start = base
    else:
        window_start = first - first % ELEMENT
    size = window_end - window_start
    low = (window_start - base) // ELEMENT
    high = (window_end - base) // ELEMENT - 1
    if mode == 'prefix':
        memory = f'nanhu_xilinx.i_ddr[{size // ELEMENT - 1}:0]'
    else:
        memory = f'nanhu_xilinx.i_ddr[{high}:{low}]'
    return dict(mode=mode, window_start=window_start, window_size=size,
                element_low=low, element_high=high, memory=memory)


def describe_reads(outputs, base, span):
    """Diagnostic one-liners for logs and tests (both read modes)."""
    return {mode: plan(outputs, base, span, mode) for mode in ('prefix', 'window')}

"""Validate deployment-owned NH initialization Tcl without changing its bytes.

This deliberately supports the small, auditable dialect in verified hardware
initializers. It is a profile check, not a Tcl interpreter or general sandbox.
"""
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re


NH_CLOCK_HZ = 5529600


@dataclass(frozen=True)
class InitScript:
    path: Path
    content: bytes
    sha256: str
    configures_clock: bool


_ENV_NAME = 'UV_RUN_SYS_CLK_HZ'
_ENV_CONDITION = r'if\s+\{\s*\[info\s+exists\s+::env\(UV_RUN_SYS_CLK_HZ\)\]\s*\}\s+\{'
_ENV_SET = r'set\s+sys_clk_hz\s+\$::env\(UV_RUN_SYS_CLK_HZ\)'
_QUERIES = {('-user',), ('-fpgas',), ('-fpgas', '-all'), ('-version',),
            ('-connector',), ('-connector', '-type', 'fmc'), ('-voltage',),
            ('-clock',), ('-reset',), ('-ipinfo',)}


def load_init_script(path, number):
    """Return an immutable verified initializer, or fail before touching hardware."""
    if type(number) is not int or number < 0:
        raise ValueError('Initialization target must be a nonnegative FPGA port number')
    path = Path(path)
    if not path.is_absolute():
        raise ValueError('Initialization Tcl path must be absolute; resolve it relative to the config file')
    try:
        path = path.resolve()
        if not path.is_file():
            raise ValueError('not a regular file')
        content = path.read_bytes()
        source = content.decode('utf-8')
    except (OSError, RuntimeError, UnicodeError, ValueError) as exc:
        raise ValueError(f'FPGA{number}: initialization Tcl must be readable UTF-8: {path} ({exc})') from exc

    def fail(line, reason):
        where = f'{path}:{line}' if line else str(path)
        raise ValueError(f'FPGA{number} initialization Tcl {where}: {reason}')

    if '\x00' in source:
        fail(0, 'NUL bytes are not supported')
    lines = []
    for line, text in enumerate(source.splitlines(), 1):
        # Tcl only uses CR/LF as command separators; Python splitlines also
        # recognizes other characters, so reject those rather than guessing.
        if any(char in text for char in ('\v', '\f')):
            fail(line, 'unsupported control character')
        text = text.strip()
        if text.endswith(chr(92)):
            fail(line, 'Tcl line continuations are unsupported; use one audited command per line')
        if text and not text.startswith('#'):
            lines.append((line, text))
    if not lines:
        fail(0, 'initialization Tcl is empty')
    # Never accept characters that splitlines would hide from the line checker.
    if any(char in source for char in ('\v', '\f', '\x1c', '\x1d', '\x1e', '\x85', '\u2028', '\u2029')):
        fail(0, 'unsupported control or Unicode line-separator character')

    events = {name: [] for name in ('release', 'load', 'allocate', 'download',
                                   'initialize', 'reset', 'frequency', 'commit', 'clock_set', 'clock_env')}
    target = 'b%d.f%d' % divmod(number, 4)
    allocation = load_target = None
    clock_value = None
    index = 0
    while index < len(lines):
        line, text = lines[index]
        index += 1
        one_line_env = re.fullmatch(_ENV_CONDITION + r'\s*' + _ENV_SET + r'\s*\}', text)
        if one_line_env or re.fullmatch(_ENV_CONDITION + r'\s*', text):
            if not one_line_env:
                if (index + 1 >= len(lines)
                        or not re.fullmatch(_ENV_SET, lines[index][1])
                        or lines[index + 1][1] != '}'):
                    fail(line, 'unsupported Tcl conditional; only the exact sys_clk_hz environment override is allowed')
                index += 2
            if clock_value is None or events['clock_env']:
                fail(line, 'sys_clk_hz environment override requires one preceding literal clock assignment')
            events['clock_env'].append(line)
            if _ENV_NAME in os.environ:
                value = os.environ[_ENV_NAME]
                if not re.fullmatch(r'[0-9]+', value) or int(value) != NH_CLOCK_HZ:
                    fail(line, f'{_ENV_NAME}={value!r} is incompatible with NH clock {NH_CLOCK_HZ} Hz')
                clock_value = int(value)
            continue

        # Braces may quote a single literal word. No Tcl evaluation, escaped
        # words, embedded commands, quoted strings, or command chaining here.
        if any(char in text for char in (';', '[', ']', chr(92), '"')):
            fail(line, 'unsupported Tcl syntax; command chaining, substitutions and escapes are not allowed')
        matches = list(re.finditer(r'\{[^{}\s]+\}|[^\s{}]+', text))
        end = 0
        words, braced = [], []
        for match in matches:
            gap = text[end:match.start()]
            if gap.strip() or (end and not gap):
                fail(line, 'unsupported Tcl word syntax')
            token = match.group()
            braced.append(token.startswith('{'))
            words.append(token[1:-1] if braced[-1] else token)
            end = match.end()
        if text[end:].strip() or not words:
            fail(line, 'unsupported Tcl word syntax')
        if any('$' in word and (word != '$sys_clk_hz' or braced[i]) for i, word in enumerate(words)):
            fail(line, 'dynamic Tcl values are unsupported except the verified $sys_clk_hz frequency')
        command, *args = words
        if command == 'query' and tuple(args) in _QUERIES:
            continue
        if command == 'allocate' and len(args) == 2 and args[0] == '-board':
            if args[1].lower() != target:
                fail(line, f'allocate target {args[1]} does not match FPGA{number} ({target})')
            allocation = args[1]
            events['allocate'].append(line)
        elif words == ['load_db', '-release']:
            events['release'].append(line)
        elif command == 'load_db' and len(args) in (2, 4) and args[:2] == ['-db', './hw.dat']:
            if len(args) == 4:
                if args[2] != '-to' or args[3].lower() != target:
                    fail(line, f'load_db target must match FPGA{number} ({target})')
                load_target = args[3]
            events['load'].append(line)
        elif words == ['config', '-connector']:
            pass
        elif command == 'set' and len(args) == 2 and args[0] == 'sys_clk_hz':
            if not re.fullmatch(r'[0-9]+', args[1]) or int(args[1]) != NH_CLOCK_HZ:
                fail(line, f'sys_clk_hz must be {NH_CLOCK_HZ} Hz for the NH profile')
            if events['clock_set'] or events['clock_env']:
                fail(line, 'only one literal sys_clk_hz assignment is supported')
            clock_value = int(args[1])
            events['clock_set'].append(line)
        elif (command == 'config' and len(args) == 4 and args[:2] == ['-clock', '-frequency']
              and args[3] == 'sys_clk'):
            if args[2] == '$sys_clk_hz':
                if clock_value is None:
                    fail(line, '$sys_clk_hz requires a preceding verified literal assignment')
                value = clock_value
            elif re.fullmatch(r'[0-9]+', args[2]):
                value = int(args[2])
            else:
                fail(line, 'unsupported clock frequency value')
            if value != NH_CLOCK_HZ:
                fail(line, f'clock frequency {value} Hz is incompatible with NH {NH_CLOCK_HZ} Hz')
            events['frequency'].append(line)
        elif words == ['config', '-clock', '-commit']:
            events['commit'].append(line)
        elif words == ['reset', '-name', 'cpu_reset', '-value', '1']:
            events['reset'].append(line)
        elif words == ['download']:
            events['download'].append(line)
        elif words == ['initialize']:
            events['initialize'].append(line)
        else:
            fail(line, f'unsupported initialization command: {text}; application commands, reset release, '
                       'source, exit and arbitrary Tcl are not allowed')

    for name in ('release', 'load', 'download', 'initialize'):
        if len(events[name]) != 1:
            fail(0, f'expected exactly one {name} command, found {len(events[name])}')
    if len(events['allocate']) > 1:
        fail(0, 'expected at most one allocate command')
    if allocation and load_target:
        fail(events['load'][0], 'allocate plus load_db -to is incompatible; preserve one verified allocation style')
    if not allocation and not load_target:
        fail(events['load'][0], f'target {target} must be explicit in allocate -board or load_db -to')
    release, load, download, initialize = (events[name][0] for name in ('release', 'load', 'download', 'initialize'))
    if not release < load < download < initialize:
        fail(0, 'expected load_db -release before database load, then download before initialize')
    if allocation and events['allocate'][0] >= load:
        fail(events['allocate'][0], 'allocate must precede database load; its order relative to release is preserved')
    if not any(load < line < download for line in events['reset']):
        fail(0, 'CPU reset must be asserted after database load and before download')
    configures_clock = bool(events['frequency'] or events['commit'])
    if configures_clock:
        if len(events['frequency']) != 1 or len(events['commit']) != 1:
            fail(0, 'clock configuration requires exactly one frequency command and one commit')
        if not load < events['frequency'][0] < events['commit'][0] < download:
            fail(0, 'clock frequency/commit must follow database load and precede download')
    return InitScript(path, content, hashlib.sha256(content).hexdigest(), configures_clock)

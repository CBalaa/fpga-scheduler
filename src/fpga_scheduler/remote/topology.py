"""Read physical UVHS topology without treating an absent mapped row as free.

``query -fpgas -all`` only sees the boards attached to the runtime host on
some multi-host installations. The ``-tb`` table supplies physical status;
deployment-owned B-to-TB mappings keep those identities independent of row
order and are checked against every mapping the live topology does expose.
"""
import re
import socket


_BOARD_COLUMNS = (
    ('system', 'System Name'), ('topology_board', 'Topology Board ID'),
    ('device_type', 'Device Type'), ('hardware_id', 'HW ID CODE'),
    ('hardware_location', 'HW Location'), ('mapped_board', 'Mapped Board ID'),
    ('used_host', 'Used by Host'), ('used_user', 'Used by User'),
)
_FPGA_COLUMNS = (
    ('system', 'System Name'), ('topology_board', 'Topology Board ID'),
    ('fpga', 'Topology Fpga Id'), ('mapped_board', 'Mapped Board ID'),
    ('device_type', 'Device Type'), ('used_host', 'Used by Host'),
    ('used_user', 'Used by User'),
)


def _columns(line, definition):
    """Use the vendor's fixed columns so blank fields retain their meaning."""
    columns = []
    for name, title in definition:
        # The vendor manual misspells Mapped as Maped in some releases.
        title_pattern = 'Map+ed Board ID' if name == 'mapped_board' else re.escape(title)
        match = re.search(title_pattern, line, re.I)
        if match is None:
            raise RuntimeError('UVHS returned an incomplete topology table header: ' + line.strip())
        columns.append((name, match.start()))
    duration = re.search(r'\bDuration\b', line)
    if duration:
        columns.append(('duration', duration.start()))
    if any(first[1] >= second[1] for first, second in zip(columns, columns[1:])):
        raise RuntimeError('UVHS returned an invalid topology column order')
    return columns


def _cells(line, columns):
    # P3 prints the Mapped Board ID header one character wider than its data
    # column. Subsequent fields consequently start one character before their
    # header. Recover that single character only at a whitespace boundary;
    # wider/ambiguous overlap remains an invalid table rather than truncated
    # owner or host data. Correctly aligned versions need no adjustment.
    starts = []
    for _, start in columns:
        if 0 < start <= len(line) and not line[start - 1].isspace():
            start -= 1
            if start and not line[start - 1].isspace():
                raise RuntimeError('UVHS returned ambiguous topology column boundaries')
        starts.append(start)
    return {name: line[starts[i]:starts[i + 1] if i + 1 < len(starts) else None].strip()
            for i, (name, _) in enumerate(columns)}


def parse_topology(output):
    """Parse board and FPGA topology tables, including wrapped host/user cells.

    Accept a combined query transcript. Only rows under the relevant table
    header count as topology; physical-status rows also start with TB IDs.
    """
    boards, fpgas = {}, {}
    columns = kind = current = None
    seen = set()

    def finish():
        nonlocal current
        if current is None:
            return
        if current['system'] != 'uvhs-0':
            raise RuntimeError('UVHS topology returned an unsupported system: ' + current['system'])
        tb = current['topology_board']
        if not re.fullmatch(r'TB\d+', tb) or not current['device_type']:
            raise RuntimeError('UVHS returned an invalid topology device row')
        mapped = current['mapped_board']
        if mapped and not re.fullmatch(r'B\d+(?:\.F[0-3])?', mapped):
            raise RuntimeError('UVHS returned an invalid topology board mapping: ' + mapped)
        if kind == 'boards':
            if mapped and '.' in mapped:
                raise RuntimeError('UVHS returned an FPGA mapping in a board topology row')
            key, destination = tb, boards
        else:
            if not re.fullmatch(r'F[0-3]', current['fpga']):
                raise RuntimeError('UVHS returned an invalid topology FPGA ID')
            key, destination = (tb, int(current['fpga'][1:])), fpgas
        if key in destination:
            raise RuntimeError('UVHS returned a duplicate topology device: ' + str(key))
        destination[key] = current
        current = None

    for line in output.splitlines():
        if 'System Name' in line and 'Topology Board ID' in line:
            finish()
            kind = 'fpgas' if re.search('Topology Fpga Id', line, re.I) else 'boards'
            if kind in seen:
                raise RuntimeError('UVHS returned duplicate topology tables')
            seen.add(kind)
            columns = _columns(line, _FPGA_COLUMNS if kind == 'fpgas' else _BOARD_COLUMNS)
            continue
        if columns is None or not line.strip() or re.fullmatch(r'\s*-+\s*', line):
            continue
        if (not re.match(r'uv(?:hs|aps)-\d+\s', line)
                and line[:columns[1][1]].strip()):
            finish()
            columns = kind = None
            continue
        cells = _cells(line, columns)
        if cells['system'].startswith(('uvhs-', 'uvaps-')):
            finish()
            current = cells
        elif not cells['system'] and current is not None:
            # UVHS wraps long usernames/PIDs and localhost.localdomain across
            # physical lines, splitting tokens without an inserted separator.
            for name, value in cells.items():
                if value and name not in ('used_host', 'used_user', 'duration'):
                    raise RuntimeError('UVHS returned an invalid topology continuation row')
                current[name] += value
        else:
            finish()
            columns = kind = None
    finish()
    if seen != {'boards', 'fpgas'}:
        raise RuntimeError('UVHS did not return both board and FPGA topology tables')
    if not boards or not fpgas:
        raise RuntimeError('UVHS did not return physical topology devices')
    return dict(boards=boards, fpgas=fpgas)


def parse_physical_fpgas(output):
    """Parse the power/owner/reservation table produced by ``-fpgas -all -tb``."""
    ports = {}
    in_table = False
    for line in output.splitlines():
        if (re.search(r'System Name\s+Board\s+FPGA\s+Power\s+Status\s+Owner\s+Booked', line)):
            in_table = True
            continue
        if not in_table or not line.strip() or re.fullmatch(r'\s*-+\s*', line):
            continue
        fields = line.split()
        if not fields or not fields[0].startswith(('uvhs-', 'uvaps-')):
            in_table = False
            continue
        if len(fields) > 1 and re.fullmatch(r'B\d+', fields[1]):
            # A combined transcript also contains the ordinary runtime table.
            continue
        if (len(fields) not in (7, 8) or fields[0] != 'uvhs-0'
                or not re.fullmatch(r'TB\d+', fields[1])
                or not re.fullmatch(r'F[0-3]', fields[2])
                or fields[3] not in ('on', 'off') or fields[4] != 'link'
                or fields[5] not in ('up', 'down') or fields[-1] not in ('true', 'false')):
            raise RuntimeError('UVHS returned an invalid physical FPGA row: ' + line.strip())
        key = fields[1], int(fields[2][1:])
        if key in ports:
            raise RuntimeError('UVHS returned a duplicate physical FPGA: ' + str(key))
        ports[key] = dict(topology_board=fields[1], fpga=fields[2],
                          power=fields[3], owner=fields[6] if len(fields) == 8 else '',
                          booked=fields[-1] == 'true')
    if not ports:
        raise RuntimeError('UVHS did not return physical FPGA status')
    return ports


def merge_inventory(runtime_ports, physical_ports, topology, physical_boards, *, local_host=None):
    """Combine every source conservatively using an explicit B-to-TB mapping.

    A discovered device needs its own complete physical-status and topology
    rows. Missing rows never create a free execution slot. Runtime rows may be
    absent for an unattached board, but any reported owner/reservation/off
    status still blocks that slot. Never infer B IDs from TB IDs or row order.
    """
    if (not isinstance(physical_boards, dict) or not physical_boards
            or any(type(board) is not int or board < 0
                   or not isinstance(tb, str) or not re.fullmatch(r'TB\d+', tb)
                   for board, tb in physical_boards.items())
            or len(set(physical_boards.values())) != len(physical_boards)):
        raise RuntimeError('UVHS physical board mapping must be a nonempty one-to-one B-to-TB mapping')
    inverse = {tb: board for board, tb in physical_boards.items()}
    local_host = socket.gethostname() if local_host is None else local_host
    observed = {}
    for tb, record in topology['boards'].items():
        mapped = record['mapped_board']
        if not mapped:
            continue
        board = int(mapped[1:])
        if board in observed and observed[board] != tb:
            raise RuntimeError('UVHS topology maps multiple physical boards to ' + mapped)
        observed[board] = tb
        if ((tb in inverse and inverse[tb] != board)
                or (board in physical_boards and physical_boards[board] != tb)):
            raise RuntimeError(f'UVHS topology mapping conflict: {tb} maps to {mapped}; '
                               'deployment topology_boards disagrees')

    ports = {}
    for board, tb in physical_boards.items():
        topology_board = topology['boards'].get(tb)
        if topology_board is None:
            continue
        for fpga in range(4):
            key = tb, fpga
            physical, discovered = physical_ports.get(key), topology['fpgas'].get(key)
            if physical is None or discovered is None:
                continue
            mapped = discovered['mapped_board']
            if mapped and mapped not in (f'B{board}', f'B{board}.F{fpga}'):
                raise RuntimeError(f'UVHS topology FPGA mapping conflict: {tb}.F{fpga} maps to {mapped}')
            number = board * 4 + fpga
            runtime = runtime_ports.get(number)
            owners = []
            for owner in (physical['owner'], discovered['used_user'],
                          runtime['owner'] if runtime else ''):
                if owner and owner not in owners:
                    owners.append(owner)
            for host in (topology_board['used_host'], discovered['used_host']):
                if host and host != local_host:
                    owner = 'host:' + host
                    if owner not in owners:
                        owners.append(owner)
            sources = ['physical', 'topology']
            if runtime is not None:
                sources.append('runtime')
            ports[number] = dict(number=number, board=f'B{board}', fpga=f'F{fpga}',
                                 owner=', '.join(owners), owners=owners,
                                 board_owners=([topology_board['used_user']]
                                               if topology_board['used_user'] else []),
                                 booked=physical['booked'] or bool(runtime and runtime['booked']),
                                 power='off' if physical['power'] == 'off' or (
                                     runtime is not None and runtime['power'] != 'on') else 'on',
                                 topology_board=tb, discovered=True,
                                 attached=bool(topology_board['mapped_board']),
                                 used_host=discovered['used_host'], sources=sources)
    return ports

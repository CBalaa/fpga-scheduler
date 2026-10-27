"""Configuration paths resolve relative to their TOML file, never the cwd."""
from dataclasses import dataclass, field
import math
from pathlib import Path, PurePosixPath
import re
import tomllib


def positive(value, name, minimum=0, inclusive=False):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or (value < minimum if inclusive else value <= minimum)):
        raise ValueError(f'Invalid {name}')
    return value


@dataclass(frozen=True)
class Node:
    id: str
    ports: tuple[int, ...]
    ssh_host: str = ''
    root: str = ''
    python: str = 'python3'
    uvhs_shell: str = ''
    uart_prefix: str = '/dev/FPGA'
    ssh_config: str = ''
    relay_command: tuple[str, ...] = ()


@dataclass(frozen=True)
class Config:
    backend: str
    state_dir: Path
    token_file: Path
    nodes: tuple[Node, ...]
    bind: str = '127.0.0.1'
    port: int = 8765
    poll_interval: float = 3
    queue_capacity: int = 256
    cooldown_seconds: float = 60
    retention_days: float = 7
    transfer_timeout: float = 120
    client_heartbeat_seconds: float = 2
    client_lease_seconds: float = 15
    simulator: dict = field(default_factory=dict)
    resident: dict = field(default_factory=dict)


def load(path):
    path = Path(path).resolve()
    with path.open('rb') as handle:
        data = tomllib.load(handle)
    allowed = set(Config.__dataclass_fields__)
    if set(data) - allowed:
        raise ValueError(f'Unknown config fields: {sorted(set(data) - allowed)}')
    backend = data.get('backend')
    if backend not in ('simulator', 'bosc', 'bosc-resident'):
        raise ValueError('backend must be simulator, bosc or bosc-resident')
    for name in ('state_dir', 'token_file'):
        value = data.get(name)
        if not isinstance(value, str) or not value:
            raise ValueError(f'{name} is required')
        data[name] = (path.parent / value).resolve()
    nodes = []
    raw_nodes = data.get('nodes')
    if not isinstance(raw_nodes, list) or not raw_nodes:
        raise ValueError('At least one node is required')
    for raw in raw_nodes:
        if not isinstance(raw, dict) or set(raw) - set(Node.__dataclass_fields__):
            raise ValueError('Invalid node fields')
        raw = dict(raw)
        ident, ports = raw.get('id'), raw.get('ports')
        if not isinstance(ident, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', ident):
            raise ValueError('Invalid node ID')
        if (not isinstance(ports, list) or not ports or any(type(p) is not int or p < 0 for p in ports)
                or len(set(ports)) != len(ports)):
            raise ValueError('ports must be distinct nonnegative integers, in priority order')
        raw['ports'] = tuple(ports)
        relay = raw.get('relay_command', [])
        if not isinstance(relay, list) or any(not isinstance(s, str) or not s for s in relay):
            raise ValueError('relay_command must be an argv array')
        raw['relay_command'] = tuple(relay)
        for name in ('ssh_host', 'root', 'python', 'uvhs_shell', 'uart_prefix', 'ssh_config'):
            if name in raw and (not isinstance(raw[name], str) or '\x00' in raw[name] or '\n' in raw[name]):
                raise ValueError(f'Invalid node {name}')
        if backend == 'bosc':
            if any(p > 7 for p in ports):
                raise ValueError('BOSC UVHS ports must be within 0..7')
            host = raw.get('ssh_host', '')
            if not host or host.startswith('-') or any(c.isspace() for c in host):
                raise ValueError('An OpenSSH host alias is required')
            for name in ('root', 'uvhs_shell', 'uart_prefix'):
                value = raw.get(name, '/dev/FPGA' if name == 'uart_prefix' else '')
                if not PurePosixPath(value).is_absolute() or '..' in PurePosixPath(value).parts:
                    raise ValueError(f'{name} must be an absolute remote path')
            if not raw.get('python', 'python3'):
                raise ValueError('Remote python executable is required')
        if raw.get('ssh_config'):
            raw['ssh_config'] = str((path.parent / raw['ssh_config']).resolve())
        nodes.append(Node(**raw))
    if len({n.id for n in nodes}) != len(nodes):
        raise ValueError('Node IDs must be unique')
    data['nodes'] = tuple(nodes)
    for name in ('poll_interval', 'retention_days', 'transfer_timeout',
                 'client_heartbeat_seconds', 'client_lease_seconds'):
        if name in data:
            positive(data[name], name)
    if data.get('client_heartbeat_seconds', 2) >= data.get('client_lease_seconds', 15):
        raise ValueError('client_heartbeat_seconds must be less than client_lease_seconds')
    for name, default in (('client_heartbeat_seconds', 2), ('client_lease_seconds', 15)):
        if not .01 <= data.get(name, default) <= 3600:
            raise ValueError(f'{name} must be within 0.01..3600')
    if 'cooldown_seconds' in data:
        positive(data['cooldown_seconds'], 'cooldown_seconds', inclusive=True)
    for name, maximum in (('port', 65535), ('queue_capacity', 100000)):
        if name in data and (type(data[name]) is not int or not 1 <= data[name] <= maximum):
            raise ValueError(f'Invalid {name}')
    if 'bind' in data and (not isinstance(data['bind'], str) or not data['bind']):
        raise ValueError('Invalid bind address')
    sim = data.get('simulator', {})
    sim_fields = {'duration', 'offline_nodes', 'busy_nodes', 'failure_nodes', 'disconnect_nodes', 'transfer_failure_nodes'}
    if not isinstance(sim, dict) or set(sim) - sim_fields:
        raise ValueError('Invalid simulator options')
    positive(sim.get('duration', 1), 'simulator duration', inclusive=True)
    for name in sim_fields - {'duration'}:
        values = sim.get(name, [])
        if not isinstance(values, list) or any(v not in {n.id for n in nodes} for v in values):
            raise ValueError(f'Invalid simulator {name}')
    if backend == 'bosc-resident':
        if len(nodes) != 1 or any(p > 7 for p in nodes[0].ports):
            raise ValueError('bosc-resident requires one local node with ports in 0..7')
        if nodes[0].ssh_host or nodes[0].relay_command:
            raise ValueError('bosc-resident does not use SSH or a relay')
        if not Path(nodes[0].uvhs_shell).is_absolute():
            raise ValueError('Resident uvhs_shell must be an absolute executable path')
    if not isinstance(data.get('resident', {}), dict):
        raise ValueError('resident must be a table')
    resident = dict(data.get('resident', {}))
    fields = {'hardware_project', 'lock_dir', 'startup_timeout', 'command_timeout',
              'uart_quiet_seconds', 'recovery_attempts', 'recovery_backoff',
              'max_job_log_bytes', 'max_session_log_bytes', 'max_storage_bytes', 'profile', 'shared_clock_index',
              'output_ddr_base', 'output_ddr_bytes', 'max_output_bytes', 'explicit_allocation',
              'keep_load_files', 'storage_retry_seconds'}
    if set(resident) - fields:
        raise ValueError('Unknown resident options')
    if backend == 'bosc-resident':
        if resident.get('profile', 'nh-only') != 'nh-only':
            raise ValueError('Only the nh-only resident profile is supported')
        if not resident.get('hardware_project') or not resident.get('lock_dir'):
            raise ValueError('Resident hardware_project and shared lock_dir are required')
    for name in ('hardware_project', 'lock_dir'):
        if name in resident:
            if not isinstance(resident[name], str) or not resident[name] or any(c in resident[name] for c in ('\x00', '\n', '\r')):
                raise ValueError(f'Invalid resident {name}')
            resident[name] = str((path.parent / resident[name]).resolve())
    if ('shared_clock_index' in resident and
            (type(resident['shared_clock_index']) is not int or not 0 <= resident['shared_clock_index'] <= 23)):
        raise ValueError('shared_clock_index must be an integer in 0..23')
    if type(resident.get('explicit_allocation', False)) is not bool:
        raise ValueError('explicit_allocation must be a boolean')
    if type(resident.get('keep_load_files', False)) is not bool:
        raise ValueError('keep_load_files must be a boolean')
    # i_ddr[0] corresponds to this profile's physical DDR base, not a user-selected base.
    if resident.get('output_ddr_base', 0x80000000) != 0x80000000 or type(resident.get('output_ddr_base', 0x80000000)) is not int:
        raise ValueError('output_ddr_base must be 0x80000000 for this hardware profile')
    for name, default, maximum in (('output_ddr_bytes', 4194304, 268435456),
                                    ('max_output_bytes', 16777216, 16777216)):
        value = resident.get(name, default)
        if type(value) is not int or not 1 <= value <= maximum:
            raise ValueError(f'{name} must be an integer within 1..{maximum}')
    if resident.get('output_ddr_bytes', 4194304) % 64:
        raise ValueError('output_ddr_bytes must be a multiple of 64')
    for name in fields - {'hardware_project', 'lock_dir', 'profile', 'shared_clock_index', 'output_ddr_base',
                          'explicit_allocation', 'keep_load_files'}:
        if name in resident:
            positive(resident[name], name)
    for name in ('recovery_attempts', 'max_job_log_bytes', 'max_session_log_bytes', 'max_storage_bytes'):
        if name in resident and type(resident[name]) is not int:
            raise ValueError(f'{name} must be an integer')
    data['resident'] = resident
    return Config(**data)

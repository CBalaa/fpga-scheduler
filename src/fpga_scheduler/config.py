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
    simulator: dict = field(default_factory=dict)


def load(path):
    path = Path(path).resolve()
    with path.open('rb') as handle:
        data = tomllib.load(handle)
    allowed = set(Config.__dataclass_fields__)
    if set(data) - allowed:
        raise ValueError(f'Unknown config fields: {sorted(set(data) - allowed)}')
    backend = data.get('backend')
    if backend not in ('simulator', 'bosc'):
        raise ValueError('backend must be simulator or bosc')
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
    for name in ('poll_interval', 'retention_days', 'transfer_timeout'):
        if name in data:
            positive(data[name], name)
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
    return Config(**data)

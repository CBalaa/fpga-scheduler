"""Offline scenario images, deliberately distinct from executable RISC-V bins."""
import json
import math
import os
from pathlib import Path
import secrets
import shutil

MAGIC = b'FPGA_SIM_V1\n'
MODES = ('hello', 'hang', 'trap', 'duplicate', 'missing-start', 'malformed',
         'readback-error', 'command-error', 'command-timeout', 'cleanup-error',
         'disconnect', 'uvhs-exit')
MAX_SCENARIO = 65536
STATE_IDENTITY = dict(backend='resident-sim', version=1)


def check_state_directory(state_dir):
    """Validate before either the service or supervisor can retire old executors."""
    root = Path(state_dir).resolve()
    marker = root / 'sim/board.json'
    if marker.exists():
        if json.loads(marker.read_text()) != STATE_IDENTITY:
            raise ValueError('Invalid simulator state identity; use a separate state_dir')
    elif (root / 'resident').exists() or (root / 'jobs.sqlite3').exists():
        raise ValueError('resident-sim refuses an existing non-simulation state_dir; use a fresh directory')


def validate_scenario(data):
    fields = {'mode', 'label', 'duration', 'exit_code', 'output', 'fragment_bytes', 'outputs'}
    if not isinstance(data, dict) or set(data) - fields:
        raise ValueError('Invalid FPGA sim scenario fields')
    data = {**dict(mode='hello', label='hello', duration=.5, exit_code=0,
                   output='SIMULATED FPGA hello: PASS\n', fragment_bytes=11, outputs=[]), **data}
    if data['mode'] not in MODES:
        raise ValueError('Unknown FPGA sim mode')
    for key in ('label', 'output'):
        if not isinstance(data[key], str) or len(data[key].encode()) > 32768:
            raise ValueError(f'Invalid FPGA sim {key}')
    duration = data['duration']
    if (isinstance(duration, bool) or not isinstance(duration, (int, float)) or
            not math.isfinite(duration) or not 0 <= duration <= 600):
        raise ValueError('FPGA sim duration must be within 0..600 seconds')
    if type(data['exit_code']) is not int or not -(2 ** 31) <= data['exit_code'] < 2 ** 31:
        raise ValueError('FPGA sim exit_code must be a signed 32-bit integer')
    if type(data['fragment_bytes']) is not int or not 1 <= data['fragment_bytes'] <= 4096:
        raise ValueError('FPGA sim fragment_bytes must be within 1..4096')
    from fpga_scheduler.remote.runner import Completion, output_declaration
    outputs = data['outputs']
    if not isinstance(outputs, list) or len(outputs) > 16:
        raise ValueError('Invalid simulated outputs')
    # Reuse real declarations; scenarios additionally contain small hex data.
    for item in outputs:
        if not isinstance(item, dict) or 'data' not in item or not isinstance(item['data'], str):
            raise ValueError('Simulated output requires hexadecimal data')
        declaration = output_declaration({k: v for k, v in item.items() if k != 'data'})
        if len(bytes.fromhex(item['data'])) != declaration['nbytes']:
            raise ValueError('Simulated output data length mismatch')
        address = int(declaration['address'], 16)
        if not 0x80000000 <= address < address + declaration['nbytes'] <= 0x80400000:
            raise ValueError('Simulated output outside private 4 MiB DDR')
    protocol = Completion()
    protocol.feed(b'@@FPGA_START {"version":1}\n')
    for item in outputs:
        protocol.feed(('@@FPGA_OUTPUT ' + json.dumps({k: v for k, v in item.items() if k != 'data'}) + '\n').encode())
    return data


def write_image(path, **scenario):
    data = validate_scenario(scenario)
    content = MAGIC + json.dumps(data, ensure_ascii=False, allow_nan=False).encode() + b'\n'
    if len(content) > MAX_SCENARIO:
        raise ValueError('FPGA sim scenario is too large')
    path = Path(path)
    with path.open('xb') as handle:
        handle.write(content)
    return path


def read_image(path):
    with Path(path).open('rb') as handle:
        content = handle.read(MAX_SCENARIO + 1)
    if not content.startswith(MAGIC) or len(content) > MAX_SCENARIO:
        raise ValueError('Local FPGA sim accepts only FPGA_SIM_V1 scenario images; use tools/fpga_sim.py image')
    return validate_scenario(json.loads(content[len(MAGIC):]))


def prepare_initialization_scripts(directory, ports):
    """Prepare software-only explicit initializers; preserve existing deployment files."""
    root = Path(directory).resolve()
    root.mkdir(parents=True, exist_ok=True)
    scripts = {}
    for number in ports:
        path = root / f'init{number}.tcl'
        if not path.exists():
            board, fpga = divmod(number, 4)
            path.write_text('# Source: repository software-only FPGA simulator, version 1\n'
                            'load_db -release\n'
                            f'load_db -db ./hw.dat -to {{b{board}.f{fpga}}}\n'
                            'reset -name cpu_reset -value 1\nquery -reset\n'
                            'download\ninitialize\nquery -reset\n')
        scripts[number] = str(path)
    return scripts


def create_environment(directory, *, port=18766, ports=(4, 5)):
    """Create a new deployment; never overwrite an existing config or token."""
    if not shutil.which('tclsh'):
        raise ValueError('resident-sim requires tclsh (Linux Tcl 8.6+) on PATH')
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError('Invalid TCP port')
    if not ports or len(set(ports)) != len(ports) or any(type(p) is not int or p not in range(8) for p in ports):
        raise ValueError('Virtual ports must be distinct integers in 0..7')
    root = Path(directory).resolve()
    root.mkdir(parents=True, exist_ok=False)
    token = root / 'service.token'
    fd = os.open(token, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as handle:
        handle.write(secrets.token_urlsafe(32) + '\n')
    prepare_initialization_scripts(root / 'init', ports)
    scripts = '\n'.join(f'"{number}" = "./init/init{number}.tcl"' for number in ports)
    config = root / 'config.local.toml'
    config.write_text('backend = "bosc-resident"\nstate_dir = "./state"\ntoken_file = "./service.token"\n'
                      f'bind = "127.0.0.1"\nport = {port}\npoll_interval = 0.05\ncooldown_seconds = 0\n'
                      '\n[resident]\nstartup_timeout = 5\ncommand_timeout = 2\nuart_quiet_seconds = 0.03\n'
                      'recovery_backoff = 0.1\nhardware_project = "./state/sim/project"\n'
                      'lock_dir = "./state/sim/locks"\n\n[resident.init_scripts]\n' + scripts
                      + '\n\n[[nodes]]\nid = "sim-local"\nports = '
                      + str(list(ports)) + '\nuvhs_shell = '
                      + json.dumps(str(root / 'state/sim/uvhs-sim')) + '\nuart_prefix = '
                      + json.dumps(str(root / 'state/sim/uart')) + '\n')
    images = root / 'images'
    images.mkdir()
    for mode in MODES:
        write_image(images / f'{mode}.bin', mode=mode, duration=10 if mode == 'command-timeout' else .5)
    return dict(config=str(config), token_file=str(token), endpoint=f'http://127.0.0.1:{port}',
                images=str(images), simulated=True)

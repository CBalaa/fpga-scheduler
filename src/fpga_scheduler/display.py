"""Readable CLI views with optional ANSI colors; API payloads stay unchanged."""
import os
import sys
import time


def color_enabled(mode='auto', stream=None):
    if mode != 'auto':
        return mode == 'always'
    stream = sys.stdout if stream is None else stream
    return ('NO_COLOR' not in os.environ and os.environ.get('TERM') != 'dumb'
            and stream.isatty())


def paint(text, tone, *, color=False):
    codes = dict(green='32', cyan='36', yellow='33', red='31', dim='2', heading='1;36')
    return f'\033[{codes[tone]}m{text}\033[0m' if color and tone else text


def _state_tone(state):
    if state in ('READY', 'RUNNING', 'OK', 'completed'):
        return 'green'
    if state in ('BUSY', 'running'):
        return 'cyan'
    if state in ('RECOVERING', 'STOPPING', 'queued', 'cancelling', 'cancelled', 'timeout'):
        return 'yellow'
    if state in ('QUARANTINED', 'UNAVAILABLE', 'UNREACHABLE', 'DEGRADED', 'failed',
                 'trap', 'protocol_error', 'platform_error'):
        return 'red'
    return 'dim'


def _exit_tone(value):
    return 'dim' if value is None else 'green' if value == 0 else 'red'


def _text(value):
    if value is None or value == '':
        return '-'
    return ' '.join(str(value).split())


def _table(headers, rows, *, color=False, tones=None):
    rows = [[_text(value) for value in row] for row in rows]
    widths = [max([len(header), *(len(row[i]) for row in rows)]) for i, header in enumerate(headers)]

    def line(row, row_tones):
        cells = [paint(value, tone, color=color) + ' ' * (width - len(value))
                 for value, width, tone in zip(row, widths, row_tones)]
        return '  '.join(cells).rstrip()

    lines = [line(headers, ['heading'] * len(headers)),
             line(['-' * width for width in widths], ['dim'] * len(headers))]
    lines.extend(line(row, tones[i] if tones else [None] * len(headers)) for i, row in enumerate(rows))
    return '\n'.join(lines)


def _duration(task):
    result = task.get('result') or {}
    seconds = result.get('seconds')
    if seconds is None and task.get('started') is not None:
        end = task['finished'] if task.get('finished') is not None else time.time()
        seconds = max(0, end - task['started'])
    return '-' if seconds is None else f'{seconds:.2f}s'


def _target(task):
    node, port = task.get('node'), task.get('fpga')
    return f'{node or "-"}/FPGA{port}' if port is not None else _text(node)


def _timestamp(value):
    return '-' if value is None else time.strftime('%Y-%m-%d %H:%M:%S %Z', time.localtime(value))


def jobs(data, *, color=False):
    tasks = data.get('jobs', [])
    lines = [f'Jobs: {data.get("total", len(tasks))} matching | showing {len(tasks)} | offset {data.get("offset", 0)}']
    if not tasks:
        return '\n'.join([*lines, 'No matching jobs.'])
    rows, tones = [], []
    for task in tasks:
        result = task.get('result') or {}
        rows.append([task['id'], task['state'], _target(task), task.get('position'),
                     _duration(task), result.get('execution_status'), result.get('app_exit_code')])
        tones.append([None, _state_tone(task['state']), None, None, None,
                      _state_tone(task['state']), _exit_tone(result.get('app_exit_code'))])
    lines.append(_table(['ID', 'STATE', 'TARGET', 'QUEUE', 'TIME', 'RESULT', 'APP EXIT'],
                        rows, color=color, tones=tones))
    for task in tasks:
        reason = task.get('error') or task.get('waiting_reason')
        if reason:
            lines.append(paint(f'  {task["id"]}: {_text(reason)}',
                               'red' if task.get('error') else 'yellow', color=color))
    return '\n'.join(lines)


def devices(data, *, color=False):
    ports = data.get('devices', [])
    lines = []
    if ports:
        rows = [[p.get('node'), p['number'], p.get('state'),
                 'yes' if p['available'] else 'no', p.get('downloads'), p.get('session')] for p in ports]
        tones = [[None, None, _state_tone(p.get('state')), 'green' if p['available'] else 'yellow',
                  None, 'dim'] for p in ports]
        lines.append(_table(['NODE', 'FPGA', 'STATE', 'AVAILABLE', 'DOWNLOADS', 'SESSION'],
                            rows, color=color, tones=tones))
        for port in ports:
            if port.get('reasons'):
                reason = (f'  {_text(port.get("node"))}/FPGA{port["number"]}: ' +
                          '; '.join(_text(reason) for reason in port['reasons']))
                tone = 'red' if port.get('state') in ('QUARANTINED', 'UNAVAILABLE') else 'yellow'
                lines.append(paint(reason, tone, color=color))
    else:
        lines.append('No ports reported.')
    for node, error in data.get('errors', {}).items():
        lines.append(paint(f'  Node {_text(node)}: {_text(error)}', 'red', color=color))
    return '\n'.join(lines)


def health(data, *, color=False):
    stopping = data.get('stopping', False)
    alive = data.get('dispatcher_alive', False)
    state = 'STOPPING' if stopping else 'OK' if alive else 'DEGRADED'
    lines = ['API: ' + paint(state, _state_tone(state), color=color),
             f'Service: PID {_text(data.get("pid"))} | backend {_text(data.get("backend"))}',
             'Dispatcher: ' + paint('running' if alive else 'stopped', 'green' if alive else 'red', color=color)]
    if not data.get('hardware_checked'):
        lines.append(paint('Hardware: not checked by health; use devices for port status.', 'dim', color=color))
    if data.get('scheduler_error'):
        lines.append(paint(f'Scheduler warning: {_text(data["scheduler_error"])}', 'yellow', color=color))
    return '\n'.join(lines)


def daemon(action, data, *, color=False):
    if action == 'stop':
        return 'Daemon: ' + paint('STOPPED', 'dim', color=color) + (' (already stopped)' if data.get('already_stopped') else '')
    record = data.get('supervisor') or {}
    api = data.get('api')
    alive = data.get('supervisor_alive', action == 'start' and bool(record))
    if not alive:
        state = 'STOPPED'
    elif record.get('stopping') or (api or {}).get('stopping'):
        state = 'STOPPING'
    elif api is None or not api.get('dispatcher_alive'):
        state = 'DEGRADED'
    else:
        state = 'RUNNING'
    lines = ['Daemon: ' + paint(state, _state_tone(state), color=color)]
    if record:
        lines.append(f'Supervisor: PID {_text(record.get("pid"))} | ' +
                     ('running' if alive else 'stopped') + f' | restarts {record.get("restarts", 0)}')
        lines.append(f'Config: {_text(record.get("config"))}')
    if api is not None:
        lines.append(health(api, color=color))
    elif data.get('api_error'):
        lines.append(paint(f'API: UNREACHABLE | {_text(data["api_error"])}', 'red', color=color))
    else:
        lines.append('API: not queried (supervisor stopped)')
    if 'queue' in data:
        lines.append(f'Queue: {data["queue"]["total"]} active jobs (queued / running / cancelling)')
    if data.get('queue_error'):
        lines.append(paint(f'Queue: unavailable | {_text(data["queue_error"])}', 'yellow', color=color))
    if 'ports' in data:
        lines.extend(['', paint('Ports:', 'heading', color=color), devices(data['ports'], color=color)])
    if data.get('ports_error'):
        lines.append(paint(f'Ports: unavailable | {_text(data["ports_error"])}', 'yellow', color=color))
    if data.get('ports_note'):
        lines.append(paint(_text(data['ports_note']), 'dim', color=color))
    return '\n'.join(lines)


def task(data, *, include_output=True, color=False):
    lines = [f'Task: {data["id"]}', 'State: ' + paint(data['state'], _state_tone(data['state']), color=color),
             f'Target: {_target(data)}']
    for key in ('created', 'started', 'finished'):
        if data.get(key) is not None:
            lines.append(f'{key.title()}: {_timestamp(data[key])}')
    if data.get('position') is not None:
        lines.append(f'Queue position: {data["position"]}')
    if data.get('waiting_reason'):
        lines.append(paint(f'Waiting: {_text(data["waiting_reason"])}', 'yellow', color=color))
    if data.get('error'):
        lines.append(paint(f'Error: {_text(data["error"])}', 'red', color=color))
    result = data.get('result')
    if result:
        status = result.get('execution_status')
        tone = _exit_tone(result.get('exit_code')) if status == 'exited' else _state_tone(status)
        lines.extend(['Execution: ' + paint(_text(status), tone, color=color),
                      'App exit code: ' + paint(_text(result.get('app_exit_code')), _exit_tone(result.get('app_exit_code')), color=color),
                      'Platform exit code: ' + paint(_text(result.get('exit_code')), _exit_tone(result.get('exit_code')), color=color),
                      f'Duration: {_duration(data)}'])
        details = result.get('details') or {}
        for key, label in (('error', 'Execution error'), ('cancel_reason', 'Cancel reason'),
                           ('interrupted_status', 'Interrupted status'), ('cleanup_error', 'Cleanup error'),
                           ('artifact_error', 'Output collection error'), ('artifacts_skipped', 'Outputs skipped')):
            if details.get(key):
                lines.append(paint(f'{label}: {_text(details[key])}',
                                   'yellow' if key in ('cancel_reason', 'interrupted_status') else 'red', color=color))
        if 'reset_confirmed' in details:
            lines.append('Reset confirmed: ' + paint('yes' if details['reset_confirmed'] else 'no',
                                                    'green' if details['reset_confirmed'] else 'red', color=color))
        application = details.get('application') or {}
        for key in ('mcause', 'mepc', 'mtval'):
            if key in application:
                lines.append(f'{key}: {_text(application[key])}')
        output = result.get('uart_output')
        if output is None:
            output = result.get('output', '')
        if include_output and output:
            lines.extend(['', paint('UART:', 'heading', color=color), output.rstrip('\n')])
        for item in result.get('artifacts', []):
            lines.append(f'Output: {item["name"]} | {item["nbytes"]} bytes | {item["dtype"]} {item["shape"]}')
    return '\n'.join(lines)


def render(command, data, *, action=None, color=False):
    if command == 'download':
        return f'Saved {data["name"]}: {data["path"]} (SHA256 verified)'
    if command == 'artifacts':
        items = data['artifacts']
        if not items:
            return 'No saved DDR outputs.'
        return _table(['NAME', 'BYTES', 'DTYPE', 'SHAPE', 'ADDRESS', 'SHA256'],
                      [[p['name'], p['nbytes'], p['dtype'], p['shape'], p['address'], p['sha256']] for p in items],
                      color=color)
    if command == 'daemon':
        return daemon(action, data, color=color)
    if command in ('jobs', 'devices', 'health'):
        return {'jobs': jobs, 'devices': devices, 'health': health}[command](data, color=color)
    return task(data, include_output=command not in ('run', 'submit'), color=color)

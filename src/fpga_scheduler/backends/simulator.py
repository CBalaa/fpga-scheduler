"""Deterministic configurable hardware substitute, including failure modes."""
import hashlib
import threading
import time

from ..models import BUSY_EXIT, BUSY_TOKEN, Cancellation, cancelled_result


class Simulator:
    supports_cancellation = True
    def __init__(self, nodes, duration=1, offline_nodes=(), busy_nodes=(), failure_nodes=(),
                 disconnect_nodes=(), transfer_failure_nodes=()):
        self.nodes = tuple(nodes)
        self.by_id = {n.id: n for n in self.nodes}
        self.duration = duration
        self.offline = set(offline_nodes)
        self.busy = set(busy_nodes)
        self.failures = set(failure_nodes)
        self.disconnects = set(disconnect_nodes)
        self.transfer_failures = set(transfer_failure_nodes)
        self.lock = threading.RLock()
        self.active = set()
        self.events = []

    def probe(self, node):
        if node in self.offline:
            raise OSError('Simulated offline node')
        with self.lock:
            return [dict(number=p, available=node not in self.busy and (node, p) not in self.active,
                         reasons=['simulated busy'] if node in self.busy or (node, p) in self.active else [])
                    for p in self.by_id[node].ports]

    def prepare(self, payload, node, image, *, cancellation=None):
        if cancellation:
            cancellation.check()
        if node in self.transfer_failures:
            raise OSError('Simulated input transfer failure')
        if hashlib.sha256(image.read_bytes()).hexdigest() != payload['sha256']:
            raise ValueError('Input checksum mismatch')

    def run(self, payload, node, port, *, cancellation=None):
        cancellation = cancellation or Cancellation()
        key = node, port
        with self.lock:
            if key in self.active or node in self.busy:
                return dict(exit_code=BUSY_EXIT, output=BUSY_TOKEN, node=node, seconds=0)
            self.active.add(key)
            self.events.append(('start', payload['id'], node, port, time.time()))
        start = time.monotonic()
        try:
            cancellation.event.wait(min(self.duration, payload['timeout']))
            reason = cancellation.finish()
            if reason:
                result = cancelled_result(reason, node=node, seconds=time.monotonic() - start)
                result['details'].update(simulated=True, reset_confirmed=True, hardware_started=True)
                return result
            if node in self.disconnects:
                raise OSError('Simulated disconnect after execution started')
            status = 124 if self.duration > payload['timeout'] else (1 if node in self.failures else 0)
            return dict(output='SIMULATED FPGA execution ' + ('PASS' if status == 0 else 'FAILED'),
                        exit_code=status, node=node, seconds=time.monotonic() - start,
                        execution_status='timeout' if status == 124 else 'exited',
                        app_exit_code=None if status == 124 else status,
                        details={'simulated': True, 'reset_confirmed': True})
        finally:
            with self.lock:
                self.active.remove(key)
                self.events.append(('finish', payload['id'], node, port, time.time()))

"""Chunked resumable image upload: staging storage, HTTP protocol and SDK."""
import hashlib
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from urllib.request import ProxyHandler, Request, build_opener

from fpga_scheduler.client import Client, SchedulerError
from fpga_scheduler.config import Config, Node
from fpga_scheduler.models import Conflict, MAX_IMAGE, MAX_IMAGE_CHUNK
from fpga_scheduler.service import Application
from fpga_scheduler.storage import Images, StorageQuotaExceeded

TOKEN = 'u' * 48
CHUNK = 4 * 1024 * 1024
BIG = 20 * 1024 * 1024 + 7


def sha_file(path, block=1024 * 1024):
    digest = hashlib.sha256()
    with Path(path).open('rb') as handle:
        while data := handle.read(block):
            digest.update(data)
    return digest.hexdigest()


def eventually(predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.01)
    raise AssertionError('Condition did not become true before deadline')


class ImagesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.ident = 'a' * 32

    def tearDown(self):
        self.tmp.cleanup()

    def test_append_status_resume_and_freeze(self):
        images = Images(self.root)
        self.assertEqual(images.max_image_bytes, MAX_IMAGE)
        self.assertEqual(images.append(self.ident, 0, b'abc'), dict(offset=0, next_offset=3))
        self.assertEqual(images.status(self.ident), dict(id=self.ident, uploaded_bytes=3,
                                                         max_image_bytes=MAX_IMAGE, complete=False))
        self.assertEqual(images.append(self.ident, 3, b'defg'), dict(offset=3, next_offset=7))
        # A fully retried chunk is idempotent and stores nothing new.
        self.assertEqual(images.append(self.ident, 3, b'defg'), dict(offset=3, next_offset=7))
        self.assertEqual(images.part_path(self.ident).read_bytes(), b'abcdefg')
        # An overlapping retry with a matching prefix appends only the suffix.
        self.assertEqual(images.append(self.ident, 5, b'fghij'), dict(offset=5, next_offset=10))
        self.assertEqual(images.part_path(self.ident).read_bytes(), b'abcdefghij')
        status = images.status(self.ident)
        self.assertEqual(status['uploaded_bytes'], 10)
        self.assertFalse(status['complete'])
        self.assertFalse(images.path(self.ident).exists())
        # Offsets beyond stored bytes and mismatched prefixes both conflict.
        with self.assertRaises(Conflict):
            images.append(self.ident, 11, b'x')
        with self.assertRaises(Conflict):
            images.append(self.ident, 0, b'XYZ')
        self.assertEqual(images.status(self.ident)['uploaded_bytes'], 10)
        for offset in (-1, True, 1.5, '1', None):
            with self.subTest(offset=offset), self.assertRaises(ValueError):
                images.append(self.ident, offset, b'x')
        for content in (b'', b'x' * (MAX_IMAGE_CHUNK + 1)):
            with self.subTest(size=len(content)), self.assertRaises(ValueError):
                images.append(self.ident, 10, content)
        # Wrong digest leaves the staged bytes in place for a later POST retry.
        with self.assertRaises(ValueError):
            images.freeze(self.ident, '0' * 64)
        self.assertTrue(images.part_path(self.ident).exists())
        digest = hashlib.sha256(b'abcdefghij').hexdigest()
        frozen = images.freeze(self.ident, digest)
        self.assertEqual(frozen, images.path(self.ident))
        self.assertEqual(frozen.read_bytes(), b'abcdefghij')
        self.assertFalse(images.part_path(self.ident).exists())
        self.assertEqual((frozen.parent / 'sha256').read_text(), digest)
        self.assertEqual(images.status(self.ident), dict(id=self.ident, uploaded_bytes=10,
                                                         max_image_bytes=MAX_IMAGE, complete=True))
        # Frozen input is immutable through both upload paths.
        with self.assertRaises(Conflict):
            images.append(self.ident, 10, b'k')
        with self.assertRaises(Conflict):
            images.put(self.ident, b'x')
        self.assertEqual(images.status('b' * 32), dict(id='b' * 32, uploaded_bytes=0,
                                                       max_image_bytes=MAX_IMAGE, complete=False))

    def test_usage_counts_part_files_and_quota_is_structured(self):
        images = Images(self.root, max_bytes=20)
        digest = images.put(self.ident, b'12345678')
        self.assertEqual(digest, hashlib.sha256(b'12345678').hexdigest())
        self.assertEqual(images.storage_status()['used_bytes'], 8)
        other = 'b' * 32
        images.append(other, 0, b'12345')
        self.assertEqual(images.storage_status()['used_bytes'], 13)
        with self.assertRaises(StorageQuotaExceeded) as caught:
            images.append(other, 5, b'6789012345')
        error = caught.exception
        self.assertEqual(error.details['error_code'], 'input_storage_quota_exceeded')
        self.assertEqual(error.details['requested_bytes'], 10)
        self.assertEqual(error.details['limit_bytes'], 20)
        self.assertEqual(images.storage_status()['used_bytes'], 13)
        with self.assertRaises(StorageQuotaExceeded):
            images.put('c' * 32, b'x' * 8)
        images.freeze(other, hashlib.sha256(b'12345').hexdigest())
        self.assertEqual(images.storage_status()['used_bytes'], 13)
        self.assertEqual(images.path(other).read_bytes(), b'12345')

    def test_legacy_put_rejects_a_staged_part_upload(self):
        images = Images(self.root)
        images.append(self.ident, 0, b'staged bytes')
        with self.assertRaises(Conflict) as caught:
            images.put(self.ident, b'direct write')
        self.assertIn('already staged', str(caught.exception))
        # The direct write was refused before touching input.bin.
        self.assertFalse(images.path(self.ident).exists())
        self.assertEqual(images.part_path(self.ident).read_bytes(), b'staged bytes')
        self.assertEqual(images.storage_status()['used_bytes'], 12)
        images.freeze(self.ident, hashlib.sha256(b'staged bytes').hexdigest())
        with self.assertRaises(Conflict):
            images.put(self.ident, b'direct write')

    def test_max_image_bytes_limits_chunks_but_not_default_legacy_put(self):
        images = Images(self.root, max_image_bytes=5)
        self.assertEqual(images.append(self.ident, 0, b'abc'), dict(offset=0, next_offset=3))
        with self.assertRaises(ValueError):
            images.append(self.ident, 3, b'def')
        self.assertEqual(images.status(self.ident)['max_image_bytes'], 5)
        self.assertEqual(images.status(self.ident)['uploaded_bytes'], 3)
        # Legacy put keeps the historical 16 MiB single-shot contract.
        with self.assertRaises(ValueError):
            images.put('b' * 32, b'x' * (MAX_IMAGE + 1))
        for value in (0, -1, True, 5.0, '5'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                Images(self.root / ('i' + str(type(value).__name__) + str(value)), max_image_bytes=value)


class UploadHTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.token = self.root / 'token'
        self.token.write_text(TOKEN)
        self.token.chmod(0o600)
        self.config = Config('simulator', self.root / 'state', self.token,
                             (Node('local', (4, 5)),), port=0, poll_interval=.01,
                             max_image_bytes=64 * 1024 * 1024, simulator={'duration': .05})
        self.app = Application(self.config)
        self.endpoint = 'http://127.0.0.1:' + str(self.app.server.server_address[1])
        self.app.queue.start()
        self.thread = threading.Thread(target=self.app.server.serve_forever, kwargs={'poll_interval': .01})
        self.thread.start()
        self.images_dir = self.config.state_dir / 'images'
        self.opener = build_opener(ProxyHandler({}))
        self.clients = []
        self.big = self.root / 'big.bin'
        self.big.write_bytes(bytes(range(256)) * (BIG // 256) + bytes(range(BIG % 256)))

    def tearDown(self):
        for client in self.clients:
            try:
                client.close()
            except Exception:
                pass
        self.app.server.shutdown()
        self.thread.join()
        self.app.close()
        self.tmp.cleanup()

    def client(self, **kwargs):
        client = Client(self.endpoint, token=TOKEN, request_timeout=10, **kwargs)
        self.clients.append(client)
        return client

    def test_large_image_chunks_execute_and_server_sha256_matches(self):
        self.assertGreater(self.big.stat().st_size, MAX_IMAGE)
        client = self.client(upload_chunk_bytes=CHUNK)
        puts = []
        original = client._request

        def request(path, payload=None, method='GET', **kwargs):
            value = original(path, payload, method, **kwargs)
            if method == 'PUT' and '/image?' in path:
                puts.append((int(parse_qs(urlsplit(path).query)['offset'][0]), len(kwargs['raw'])))
            return value

        with patch.object(client, '_request', side_effect=request):
            task = client.submit(self.big)
        # Chunks are sequential, bounded and exactly cover the file.
        expected = list(range(0, BIG, CHUNK))
        self.assertEqual([offset for offset, _ in puts], expected)
        self.assertEqual(sum(length for _, length in puts), BIG)
        self.assertTrue(all(length <= CHUNK for _, length in puts))
        done = client.wait(task.id, timeout=30, poll_interval=.01)
        self.assertEqual(done.state, 'completed')
        self.assertTrue(done.result.ok)
        server_input = self.images_dir / task.id / 'input.bin'
        self.assertEqual(sha_file(server_input), sha_file(self.big))
        self.assertEqual((server_input.parent / 'sha256').read_text(), sha_file(self.big))
        self.assertFalse((server_input.parent / 'input.bin.part').exists())
        self.assertEqual(self.app.queue.images.status(task.id),
                         dict(id=task.id, uploaded_bytes=BIG,
                              max_image_bytes=64 * 1024 * 1024, complete=True))

    def test_lost_chunk_response_resumes_without_reuploading_stored_bytes(self):
        client = self.client(upload_chunk_bytes=CHUNK)
        original = client._request
        puts, dropped = [], []

        def request(path, payload=None, method='GET', **kwargs):
            value = original(path, payload, method, **kwargs)
            if method == 'PUT' and '/image?' in path:
                offset = int(parse_qs(urlsplit(path).query)['offset'][0])
                puts.append(offset)
                if len(puts) == 3 and not dropped:
                    dropped.append(offset)
                    raise SchedulerError('Simulated lost chunk response')
            return value

        with patch.object(client, '_request', side_effect=request):
            task = client.submit(self.big)
        self.assertEqual(len(dropped), 1)
        # The resumed upload continues after the durably stored bytes: no replay.
        self.assertEqual(puts, list(range(0, BIG, CHUNK)))
        self.assertEqual(len(puts), len(set(puts)))
        self.assertGreater(puts[3], dropped[0])
        done = client.wait(task.id, timeout=30, poll_interval=.01)
        self.assertEqual(done.state, 'completed')
        self.assertTrue(done.result.ok)
        self.assertEqual(sha_file(self.images_dir / task.id / 'input.bin'), sha_file(self.big))

    def test_chunk_requests_refresh_a_short_lease(self):
        self.app.queue.heartbeat_seconds = .1
        self.app.queue.lease_seconds = .8
        client = self.client(upload_chunk_bytes=CHUNK)
        # Disable the background heartbeat: only chunk PUTs can refresh the lease.
        client._heartbeat = lambda: None
        original = client._request

        def request(path, payload=None, method='GET', **kwargs):
            value = original(path, payload, method, **kwargs)
            if method == 'PUT' and '/image?' in path:
                time.sleep(.2)
            return value

        started = time.monotonic()
        with patch.object(client, '_request', side_effect=request):
            task = client.submit(self.big)
        self.assertGreater(time.monotonic() - started, .8)
        # The chunked upload refreshed the lease, so POST /jobs accepted it.
        done = client.wait(task.id, timeout=30, poll_interval=.01)
        self.assertEqual(done.state, 'completed')
        self.assertTrue(done.result.ok)

    def test_oversized_image_is_rejected_by_configured_max_image_bytes(self):
        self.app.queue.images.max_image_bytes = 1024
        client = self.client()
        oversized = self.root / 'oversized.bin'
        oversized.write_bytes(b'x' * 2048)
        with self.assertRaises(SchedulerError) as caught:
            client.submit(oversized)
        error = caught.exception
        self.assertEqual(error.status, 400)
        self.assertIn('max_image_bytes', str(error))
        self.assertIsNotNone(error.task_id)
        self.assertEqual(client.jobs()['total'], 0)
        self.assertFalse(self.app.backend.events)
        self.assertEqual(self.app.queue.images.status(error.task_id)['uploaded_bytes'], 0)
        # The server enforces the limit even for a raw client that skips the probe.
        request = Request(self.endpoint + '/jobs/' + 'b' * 32 + '/image?offset=0',
                          data=b'x' * 2048, method='PUT',
                          headers={'Authorization': 'Bearer ' + TOKEN})
        with self.assertRaises(HTTPError) as caught:
            self.opener.open(request, timeout=5)
        self.assertEqual(caught.exception.code, 400)

    def test_old_daemon_falls_back_to_legacy_put_and_rejects_large_images(self):
        client = self.client()
        original = client._request

        def request(path, payload=None, method='GET', **kwargs):
            if method == 'GET' and path.startswith('/jobs/') and path.endswith('/image'):
                raise SchedulerError('Unknown endpoint', status=404)
            return original(path, payload, method, **kwargs)

        small = self.root / 'small.bin'
        small.write_bytes(b'legacy single-shot image')
        with patch.object(client, '_request', side_effect=request):
            task = client.submit(small)
        done = client.wait(task.id, timeout=30, poll_interval=.01)
        self.assertEqual(done.state, 'completed')
        self.assertEqual(sha_file(self.images_dir / task.id / 'input.bin'), sha_file(small))
        with patch.object(client, '_request', side_effect=request), \
                self.assertRaises(SchedulerError) as caught:
            client.submit(self.big)
        error = caught.exception
        self.assertIn('upgrade', str(error))
        self.assertEqual(error.details['error_code'], 'daemon_upgrade_required')
        self.assertIsNotNone(error.task_id)
        self.assertEqual(self.app.queue.images.status(error.task_id)['uploaded_bytes'], 0)
        self.assertEqual(client.jobs(('completed',))['total'], 1)


class ClientOptionTests(unittest.TestCase):
    def test_upload_options_are_validated_before_any_request(self):
        endpoint = 'http://127.0.0.1:1'
        for chunk in (0, -1, True, 1.5, '1024', MAX_IMAGE_CHUNK + 1):
            with self.subTest(chunk=chunk), self.assertRaises(ValueError):
                Client(endpoint, token=TOKEN, upload_chunk_bytes=chunk)
        for attempts in (0, -1, True, 1.5, '5', 101):
            with self.subTest(attempts=attempts), self.assertRaises(ValueError):
                Client(endpoint, token=TOKEN, upload_attempts=attempts)
        client = Client(endpoint, token=TOKEN, upload_chunk_bytes=1024, upload_attempts=1)
        self.assertEqual(client.upload_chunk_bytes, 1024)
        self.assertEqual(client.upload_attempts, 1)


if __name__ == '__main__':
    unittest.main()

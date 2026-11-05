"""Raw-text caption output over TCP for hardware-free Linux/VM deployments."""
import base64
import copy
import http.client
import os
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest import mock

from spankvanc.caption_tcp import CaptionFanout, CaptionSocket, TcpAddressError, format_line, parse_address, wait_for
from spankvanc.engine import DEFAULT, ConfigError, Engine, command, plan, validate
from spankvanc.server import DualStackServer, host_port, make_handler, serve

# Stock distribution FFmpeg on a VM: no DeckLink, no patched caption filter.
STOCK = dict(ffmpeg='/usr/bin/ffmpeg', libx264=True, aac=True, mpeg2video=True, srt=True,
             whisper=dict(available=True, cuda=False, models=['small.en']))


def tcp_route(address='tcp://192.0.2.10:2001', **extra):
    cfg = copy.deepcopy(DEFAULT)
    cfg['input'].update(kind='udp', address='udp://0.0.0.0:5000')
    cfg['outputs'] = [dict(name='Encoder', kind='tcp', address=address, captions='recaption',
                           scte='strip', enabled=True, **extra)]
    return cfg


def lines(sock, count, timeout=5):
    sock.settimeout(timeout)
    data = b''
    while data.count(b'\n') < count:
        part = sock.recv(4096)
        if not part:
            break
        data += part
    return data


class AddressAndFormatTests(unittest.TestCase):
    def test_parse_address(self):
        self.assertEqual(parse_address('tcp://10.0.0.5:2001'), ('10.0.0.5', 2001, False))
        self.assertEqual(parse_address('tcp://0.0.0.0:2001?listen=1'), ('0.0.0.0', 2001, True))
        for bad in ('udp://10.0.0.5:2001', 'tcp://10.0.0.5', 'tcp://:2001', 'tcp://h:2001/x',
                    'tcp://h:2001?mode=x', 'tcp://u:p@h:2001', 'tcp://h:99999'):
            with self.assertRaises(TcpAddressError, msg=bad):
                parse_address(bad)

    def test_format_line(self):
        self.assertEqual(format_line('  Café “quoted” — ok…  '), b'Cafe "quoted" - ok...\r\n')
        self.assertEqual(format_line('hello', uppercase=True, ending='cr'), b'HELLO\r')
        self.assertEqual(format_line('a\x1b[2Jb\nc', ending='lf'), b'a?[2Jb c\n')
        self.assertEqual(format_line('   '), b'')


class ConfigTests(unittest.TestCase):
    def test_tcp_only_route_needs_no_decklink_engine(self):
        cfg = validate(tcp_route())
        out = cfg['outputs'][0]
        self.assertEqual((out['video_codec'], out['tcp_line_ending'], out['tcp_uppercase']), ('text', 'crlf', False))
        result = plan(cfg, STOCK)
        self.assertTrue(result['ready'], result['blockers'])
        self.assertEqual(result['outputs'][0]['container'], 'Raw caption text / TCP')
        args = command(cfg, STOCK, 'JOURNAL')
        # Only the Whisper audio pipe; no media output and no 608 filter.
        self.assertEqual(args[-1], 'pipe:1')
        self.assertNotIn('tcp://192.0.2.10:2001', args)
        self.assertFalse(any('spankvanc_cc' in a for a in args))

    def test_tcp_alongside_media_output(self):
        cfg = tcp_route()
        cfg['outputs'].append(dict(name='Program', kind='udp', address='udp://192.0.2.20:5000', captions='pass', scte='strip', enabled=True))
        result = plan(validate(cfg), STOCK)
        self.assertTrue(result['ready'], result['blockers'])
        args = command(validate(cfg), STOCK, 'JOURNAL')
        self.assertIn('pipe:1', args)
        self.assertTrue(any(a.startswith('udp://192.0.2.20:5000') for a in args))

    def test_whisper_still_required(self):
        result = plan(validate(tcp_route()), dict(STOCK, whisper={'available': False}))
        self.assertFalse(result['ready'])

    def test_rejections(self):
        for change in (dict(captions='pass'), dict(scte='convert'), dict(spanish_cc2=True),
                       dict(tcp_line_ending='crcr'), dict(tcp_uppercase='yes'),
                       dict(address='tcp://host-without-port'), dict(address='udp://h:1')):
            cfg = tcp_route()
            cfg['outputs'][0].update(change)
            with self.assertRaises(ConfigError, msg=change):
                validate(cfg)

    def test_tcp_fields_removed_from_other_kinds(self):
        cfg = tcp_route()
        cfg['outputs'][0].update(kind='udp', address='udp://192.0.2.20:5000', captions='pass', tcp_uppercase=True)
        self.assertNotIn('tcp_uppercase', validate(cfg)['outputs'][0])


class SocketTests(unittest.TestCase):
    def test_client_mode_sends_lines_and_reconnects(self):
        server = socket.create_server(('127.0.0.1', 0))
        port = server.getsockname()[1]
        sink = CaptionSocket('enc', f'tcp://127.0.0.1:{port}', log=lambda line: None, retry_max=0.2).start()
        try:
            conn, _ = server.accept()
            conn.sendall(b'\xff\xfd\x01Encoder ready>')  # Telnet negotiation/prompt is ignored
            sink.send('first phrase')
            sink.send('second phrase')
            self.assertEqual(lines(conn, 2), b'first phrase\r\nsecond phrase\r\n')
            self.assertTrue(wait_for(lambda: sink.state()['sent'] == 2))
            conn.close()
            conn, _ = server.accept()  # client reconnects by itself
            self.assertTrue(wait_for(lambda: sink.state()['status'] == 'connected'))
            sink.send('after reconnect')
            self.assertEqual(lines(conn, 1), b'after reconnect\r\n')
            self.assertEqual(sink.state()['recent'][-1], 'after reconnect')
            conn.close()
        finally:
            sink.close()
            server.close()

    def test_listen_mode_broadcasts_to_clients(self):
        logs = []
        probe = socket.create_server(('127.0.0.1', 0)); free = probe.getsockname()[1]; probe.close()
        sink = CaptionSocket('feed', f'tcp://127.0.0.1:{free}?listen=1', uppercase=True, ending='lf', log=logs.append).start()
        try:
            a = socket.create_connection(('127.0.0.1', sink.port))
            b = socket.create_connection(('127.0.0.1', sink.port))
            self.assertTrue(wait_for(lambda: sink.state()['clients'] == 2))
            a.sendall(b'\xff\xfb\x18garbage typed by an operator\r\n')
            sink.send('Live caption')
            self.assertEqual(lines(a, 1), b'LIVE CAPTION\n')
            self.assertEqual(lines(b, 1), b'LIVE CAPTION\n')
            a.close()
            self.assertTrue(wait_for(lambda: sink.state()['clients'] == 1))
            sink.send('still going')
            self.assertEqual(lines(b, 1), b'STILL GOING\n')
            b.close()
        finally:
            sink.close()
        self.assertIn('Caption TCP feed: listening', logs)

    def test_busy_listen_port_fails_start(self):
        busy = socket.create_server(('127.0.0.1', 0))
        try:
            cfg = validate(tcp_route(f'tcp://127.0.0.1:{busy.getsockname()[1]}?listen=1'))
            with self.assertRaises(OSError):
                CaptionFanout.start(cfg, lambda line: None)
        finally:
            busy.close()

    @mock.patch('spankvanc.engine.doctor', return_value=STOCK)
    def test_engine_forwards_whisper_phrases(self, doctor):
        server = socket.create_server(('127.0.0.1', 0))
        with tempfile.TemporaryDirectory() as folder:
            engine = Engine(Path(folder))
            fanout = CaptionFanout.start(validate(tcp_route(f'tcp://127.0.0.1:{server.getsockname()[1]}')), engine.logs.append)
            try:
                conn, _ = server.accept()
                engine._whisper_log('Whisper ready: small.en / cpu', None, fanout)
                engine._whisper_log('Whisper CC1: Good evening.', None, fanout)
                engine._whisper_log('Whisper clear', None, fanout)
                self.assertEqual(lines(conn, 1), b'Good evening.\r\n')
                conn.close()
            finally:
                fanout.close()
                server.close()


class ServerAccessTests(unittest.TestCase):
    def request(self, port, host, auth=None):
        conn = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
        headers = {'Host': host}
        if auth:
            headers['Authorization'] = 'Basic ' + base64.b64encode(auth.encode()).decode()
        conn.request('GET', '/', headers=headers)
        response = conn.getresponse()
        response.read()
        conn.close()
        return response.status

    def run_server(self, allowed, password):
        httpd = DualStackServer(('127.0.0.1', 0), None)
        port = httpd.server_address[1]
        httpd.RequestHandlerClass = make_handler(mock.Mock(), 'token', port, allowed, password)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        return port

    def test_host_port(self):
        self.assertEqual(host_port('VM.lan:8877'), ('vm.lan', '8877'))
        self.assertEqual(host_port('[::1]:8877'), ('::1', '8877'))
        self.assertEqual(host_port('10.0.0.5'), ('10.0.0.5', None))

    def test_loopback_default_unchanged(self):
        port = self.run_server(('127.0.0.1', 'localhost'), None)
        self.assertEqual(self.request(port, f'127.0.0.1:{port}'), 200)
        self.assertEqual(self.request(port, f'evil.example:{port}'), 403)

    def test_lan_requires_password_and_allowed_host(self):
        port = self.run_server(('127.0.0.1', 'localhost', '10.0.0.5'), 'correct horse battery')
        self.assertEqual(self.request(port, f'10.0.0.5:{port}'), 401)
        self.assertEqual(self.request(port, f'10.0.0.5:{port}', 'admin:wrong password!'), 401)
        self.assertEqual(self.request(port, f'10.0.0.5:{port}', 'admin:correct horse battery'), 200)
        self.assertEqual(self.request(port, f'rebind.example:{port}', 'admin:correct horse battery'), 403)

    def test_serve_refuses_lan_without_password_or_host(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('SPANKVANC_PASSWORD', None)
            with self.assertRaisesRegex(ConfigError, 'SPANKVANC_PASSWORD'):
                serve(8877, Path(folder), '0.0.0.0')
            os.environ['SPANKVANC_PASSWORD'] = 'long enough secret'
            with self.assertRaisesRegex(ConfigError, '--allow-host'):
                serve(8877, Path(folder), '0.0.0.0')


if __name__ == '__main__':
    unittest.main()

import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from experiment import haversine, parse_ping, parse_trace, read_ips, trace_ip


class MeasurementsTest(unittest.TestCase):
    def test_macos_ping(self):
        data = parse_ping('5 packets transmitted, 4 packets received, 20.0% packet loss\n'
                          'round-trip min/avg/max/stddev = 1.100/2.200/4.000/0.850 ms')
        self.assertEqual(data['avg_ms'], 2.2)
        self.assertEqual(data['received'], 4)
        self.assertEqual(data['loss_pct'], 20)

    def test_linux_ping(self):
        data = parse_ping('5 packets transmitted, 5 received, 0% packet loss\n'
                          'rtt min/avg/max/mdev = 0.011/0.022/0.030/0.004 ms')
        self.assertEqual(data['received'], 5)
        self.assertEqual(data['min_ms'], .011)

    def test_no_reply_is_not_zero(self):
        data = parse_ping('5 packets transmitted, 0 packets received, 100.0% packet loss')
        self.assertIsNone(data['avg_ms'])
        self.assertEqual(data['loss_pct'], 100)

    def test_trace_missing_multipath_and_negative_deltas(self):
        hops = parse_trace('traceroute to 8.8.8.8 (8.8.8.8), 30 hops max\n'
                           ' 1  192.168.1.1  5.0 ms  6.0 ms  4.0 ms\n'
                           ' 2  * * *\n'
                           ' 3  10.0.0.1  4.0 ms 10.0.0.2  2.0 ms *\n'
                           ' 4  8.8.8.8  12.0 ms 11.0 ms 10.0 ms\n', '8.8.8.8')
        self.assertEqual(len(hops), 4)
        self.assertIsNone(hops[1]['avg_ms'])
        self.assertEqual(hops[2]['ips'], ['10.0.0.1', '10.0.0.2'])
        self.assertLess(hops[2]['avg_ms'] - hops[0]['avg_ms'], 0)
        self.assertTrue(hops[-1]['destination'])
        self.assertEqual(hops[-1]['ttl'], 4)

    def test_unreachable_destination_is_not_complete(self):
        hops = parse_trace(' 1  8.8.8.8  1.0 ms !H', '8.8.8.8')
        self.assertFalse(hops[0]['destination'])

    def test_macos_trace_wait_and_command_errors(self):
        args = SimpleNamespace(max_hops=30, probes=3, wait=1.0, trace_timeout=120)
        raw = dict(output='traceroute: bad option', returncode=1, timed_out=False)
        with tempfile.TemporaryDirectory() as folder:
            with patch('experiment.platform.system', return_value='Darwin'):
                with patch('experiment.execute', return_value=raw) as run:
                    result = trace_ip('8.8.8.8', args, Path(folder))
        command = run.call_args.args[0]
        self.assertEqual(command[command.index('-w') + 1], '1')
        self.assertEqual(result['status'], 'error')

    def test_ipv6(self):
        hops = parse_trace(' 8  2001:4860:4860::8888  25.2 ms * 26.0 ms', '2001:4860:4860::8888')
        self.assertTrue(hops[0]['destination'])

    def test_distance_known_city_pair(self):
        self.assertAlmostEqual(haversine(40.4237, -86.9212, 51.5074, -.1278), 6418, delta=10)
        self.assertEqual(haversine(42, -71, 42, -71), 0)

    def test_input_comments_duplicates_and_invalid(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'ips.txt'
            path.write_text('# comment\n8.8.8.8\n8.8.8.8 # duplicate\n::1\n')
            self.assertEqual(read_ips(path), ['8.8.8.8', '::1'])
            path.write_text('-bad-option\n')
            with self.assertRaises(ValueError):
                read_ips(path)


if __name__ == '__main__':
    unittest.main()

"""In-memory route preview tests; no forwarding process or network is used."""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'deploy'))
from sidecar_route_preview import preview_route


OUTBOUNDS = '''listen = "127.0.0.1:10880"
[[outbounds]]
tag = "direct"
kind = "direct"
[[outbounds]]
tag = "hong-kong"
kind = "http_connect"
addr = "127.0.0.1:18892"
username = "private-user"
password = "private-password"
[[outbounds]]
tag = "manual-socks"
kind = "socks5"
addr = "127.0.0.1:18893"
'''


def rule(kind, value, outbound='hong-kong'):
    return '\n[[rules]]\n%s = "%s"\noutbound = "%s"\n' % (kind, value, outbound)


def saved(profile='polymarket', enabled=True, country='Hong Kong', proxy='127.0.0.1:18892', domains=None):
    return {'profile': profile, 'enabled': enabled, 'country': country, 'proxy': proxy,
            'domains': domains if domains is not None else ['polymarket.com']}


class RoutePreviewTests(unittest.TestCase):
    def test_exact_suffix_contains_and_default_match_the_rust_order(self):
        config = OUTBOUNDS + rule('domain_exact', 'special.polymarket.com', 'manual-socks')
        config += rule('domain_contains', 'poly', 'hong-kong')
        config += rule('domain_suffix', 'polymarket.com', 'direct')
        exact = preview_route(config, 'SPECIAL.POLYMARKET.COM.', [])
        self.assertEqual(exact['domain'], 'special.polymarket.com')
        self.assertEqual(exact['rule'], {'type': 'domain_exact', 'value': 'special.polymarket.com',
                                       'index': 0, 'source': 'manual'})
        self.assertEqual(exact['outbound']['tag'], 'manual-socks')
        contains = preview_route(config, 'www.polymarket.com', [])
        self.assertEqual(contains['rule']['type'], 'domain_contains')
        self.assertEqual(contains['rule']['index'], 1)
        self.assertEqual(contains['outcome'], 'proxy')
        fallback = preview_route(config, 'unmatched.example', [])
        self.assertFalse(fallback['matched'])
        self.assertIsNone(fallback['rule'])
        self.assertEqual(fallback['outbound'], {'tag': 'direct', 'kind': 'direct', 'proxy': None})
        self.assertEqual(fallback['outcome'], 'direct')

    def test_suffix_matches_root_and_subdomains_but_not_similar_names(self):
        config = OUTBOUNDS + rule('domain_suffix', '.Polymarket.COM')
        for host in ('polymarket.com', 'gamma-api.polymarket.com', 'nested.ws.polymarket.com'):
            with self.subTest(host=host):
                self.assertTrue(preview_route(config, host, [])['matched'])
        for host in ('evilpolymarket.com', 'polymarket.com.evil.example'):
            with self.subTest(host=host):
                self.assertFalse(preview_route(config, host, [])['matched'])

    def test_first_rule_wins_instead_of_longest_suffix(self):
        config = OUTBOUNDS + rule('domain_suffix', 'example.com', 'manual-socks')
        config += rule('domain_exact', 'api.example.com', 'hong-kong')
        result = preview_route(config, 'api.example.com', [])
        self.assertEqual(result['rule']['index'], 0)
        self.assertEqual(result['outbound']['tag'], 'manual-socks')

    def test_unmatched_uses_first_outbound_even_if_it_is_a_proxy(self):
        config = '''[[outbounds]]
tag = "default-tunnel"
kind = "socks5"
addr = "127.0.0.1:1081"
[[outbounds]]
tag = "later-direct"
'''
        result = preview_route(config, 'example.com', [saved()])
        self.assertEqual(result['outbound'], {'tag': 'default-tunnel', 'kind': 'socks5',
                                            'proxy': '127.0.0.1:1081'})
        self.assertFalse(result['matched'])
        self.assertEqual(result['outcome'], 'proxy')
        self.assertEqual(result['services'], [])
        self.assertIsNone(result['country'])

    def test_ui_source_and_enabled_shared_services_are_identified(self):
        config = OUTBOUNDS + '\n# sidecar-ui-managed-begin\n'
        config += rule('domain_suffix', 'polymarket.com')
        config += '# sidecar-ui-managed-end\n'
        routes = [saved(), saved('other-app'), saved('disabled-app', enabled=False),
                  saved('different-exit', proxy='127.0.0.1:18893')]
        result = preview_route(config, 'clob.polymarket.com', routes)
        self.assertEqual(result['rule']['source'], 'ui')
        self.assertEqual(result['services'], ['polymarket', 'other-app'])
        self.assertEqual(result['country'], 'Hong Kong')
        self.assertNotIn('username', result['outbound'])
        self.assertNotIn('password', result['outbound'])
        routes[0].pop('enabled')
        self.assertEqual(preview_route(config, 'clob.polymarket.com', routes)['services'],
                         ['polymarket', 'other-app'])

    def test_manual_rule_cannot_be_overridden_by_ui_metadata_or_country(self):
        config = OUTBOUNDS + rule('domain_exact', 'clob.polymarket.com', 'manual-socks')
        config += '# sidecar-ui-managed-begin\n' + rule('domain_suffix', 'polymarket.com')
        config += '# sidecar-ui-managed-end\n'
        result = preview_route(config, 'clob.polymarket.com', [saved()])
        self.assertEqual(result['rule']['source'], 'manual')
        self.assertEqual(result['outbound']['tag'], 'manual-socks')
        self.assertEqual(result['services'], [])
        self.assertIsNone(result['country'])
        result = preview_route(OUTBOUNDS + rule('domain_suffix', 'example.com'),
                               'example.com', [saved()])
        self.assertEqual(result['services'], [])
        self.assertIsNone(result['country'], 'same proxy alone cannot establish service or country')

    def test_disabled_route_is_not_attributed_and_conflicting_countries_are_unknown(self):
        config = OUTBOUNDS + rule('domain_suffix', 'polymarket.com')
        result = preview_route(config, 'polymarket.com', [saved(enabled=False)])
        self.assertEqual(result['services'], [])
        self.assertIsNone(result['country'])
        result = preview_route(config, 'polymarket.com', [saved(), saved('other', country='Japan')])
        self.assertIsNone(result['country'])

    def test_legacy_rule_section_and_key_aliases_follow_assignment_order(self):
        config = OUTBOUNDS + '''
[[routing.rules]]
type = "domain_exact"
value = "unused.example"
domain = ".MiXeD.Example"
outbound_tag = "manual-socks"
[[rules]]
domain_exact = "discarded.example"
type = "domain_contains"
value = "middle"
outbound = "hong-kong"
'''
        first = preview_route(config, 'mixed.example', [])
        self.assertEqual(first['outbound']['tag'], 'manual-socks')
        self.assertEqual(first['rule']['type'], 'domain_exact')
        second = preview_route(config, 'a-middle-b.example', [])
        self.assertEqual(second['outbound']['tag'], 'hong-kong')
        self.assertEqual(second['rule']['type'], 'domain_contains')

    def test_double_quote_comments_unknown_escapes_and_bare_values_match_rust(self):
        config = '''[[outbounds]]
tag = tunnel
kind = http_connect
addr = "127.0.0.1:8888#not-a-comment" # comment after string
[[rules]]
domain_contains = "\\e\\x\\a\\m\\p\\l\\e" # unknown escapes lose their backslashes
outbound_tag = tunnel
'''
        result = preview_route(config, 'example.com', [])
        self.assertTrue(result['matched'])
        self.assertEqual(result['rule']['value'], 'example')
        self.assertEqual(result['outbound']['proxy'], '127.0.0.1:8888#not-a-comment')
        # Rust does not implement TOML single-quoted strings.
        config = OUTBOUNDS + "\n[[rules]]\ndomain_exact = 'example.com'\noutbound = hong-kong\n"
        self.assertFalse(preview_route(config, 'example.com', [])['matched'])

    def test_leading_dots_trimmed_for_all_rule_types_and_rule_trailing_dots_preserved(self):
        config = OUTBOUNDS + rule('domain_exact', '..EXAMPLE.COM')
        self.assertTrue(preview_route(config, 'example.com...', [])['matched'])
        config = OUTBOUNDS + rule('domain_contains', '..EXAMPLE')
        self.assertTrue(preview_route(config, 'myexample.net', [])['matched'])
        config = OUTBOUNDS + rule('domain_suffix', 'example.com.')
        self.assertFalse(preview_route(config, 'example.com.', [])['matched'])

    def test_plain_ipv4_ipv6_and_ascii_hostname_normalization(self):
        for host in ('192.0.2.1', '2001:DB8::1', 'localhost', 'xn--bcher-kva.example'):
            with self.subTest(host=host):
                result = preview_route(OUTBOUNDS, host, [])
                self.assertEqual(result['is_ip'], ':' in host or host == '192.0.2.1')
        config = OUTBOUNDS + rule('domain_exact', '2001:db8::1')
        self.assertTrue(preview_route(config, '2001:DB8::1', [])['matched'])

    def test_unsafe_or_ambiguous_inputs_are_rejected_without_dns_or_file_access(self):
        bad = (None, 7, '', 'https://example.com', 'example.com:443', '[::1]:443',
               'user@example.com', '*.example.com', 'example.com/path', 'example.com?x=1',
               'a..example', '-bad.example', 'bad-.example', 'under_score.example',
               'a' * 64 + '.example', 'example.com\n', 'example.com\x00', 'bücher.example')
        with patch('builtins.open', side_effect=AssertionError('unexpected disk I/O')):
            with patch('socket.getaddrinfo', side_effect=AssertionError('unexpected DNS lookup')):
                for host in bad:
                    with self.subTest(host=host), self.assertRaises(ValueError):
                        preview_route(OUTBOUNDS, host, [])
                self.assertEqual(preview_route(OUTBOUNDS, 'example.com', [])['outcome'], 'direct')

    def test_invalid_config_fails_instead_of_reporting_a_guessed_exit(self):
        invalid = ('', '[[unknown]]\nx=1', '[[outbounds]]\nkind = impossible\ntag = x',
                   '[[outbounds]]\ntag = x\nkind = socks5',
                   '[[outbounds]]\ntag = x\n[[outbounds]]\ntag = x',
                   OUTBOUNDS + rule('domain_suffix', 'example.com', 'absent'),
                   OUTBOUNDS + '\n[[rules]]\ntype = invalid\n',
                   OUTBOUNDS + '\n[[rules]]\noutbound = direct\n',
                   'reload_interval_secs = -1\n' + OUTBOUNDS)
        for config in invalid:
            with self.subTest(config=config), self.assertRaises(ValueError):
                preview_route(config, 'example.com', [])


if __name__ == '__main__':
    unittest.main()

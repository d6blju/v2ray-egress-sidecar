"""Pure, on-demand preview of the Rust forwarder's hostname routing decision.

This intentionally parses the small Rust configuration dialect, not general TOML.
It neither resolves the supplied hostname nor probes the selected exit.
"""
import ipaddress
import re


MANAGED_BEGIN = '# sidecar-ui-managed-begin'
MANAGED_END = '# sidecar-ui-managed-end'
RULE_TYPES = ('domain_exact', 'domain_suffix', 'domain_contains')
OUTBOUND_KINDS = ('direct', 'socks5', 'http_connect')


def ascii_lower(value):
    # Rust uses to_ascii_lowercase rather than Unicode case folding.
    return value.translate(str.maketrans('ABCDEFGHIJKLMNOPQRSTUVWXYZ',
                                         'abcdefghijklmnopqrstuvwxyz'))


def normalize_host(value):
    if not isinstance(value, str) or not value:
        raise ValueError('enter a plain hostname or IP address')
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError('hostname must not contain control characters')
    value = value.strip()
    if not value or any(ord(char) > 127 for char in value):
        raise ValueError('enter an ASCII hostname; use punycode for international domains')
    host = ascii_lower(value.rstrip('.'))
    try:
        ipaddress.ip_address(host)
        return host, True
    except ValueError:
        pass
    label = re.compile(r'^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$')
    if len(host) > 253 or not all(label.fullmatch(part) for part in host.split('.')):
        raise ValueError('enter a plain hostname or IP address, without URL, port or wildcard')
    return host, False


def strip_comment(line):
    """Match src/main.rs strip_comment, including double-quote-only strings."""
    quoted, escaped, out = False, False, []
    for char in line:
        if escaped:
            out.append(char)
            escaped = False
        elif char == '\\' and quoted:
            out.append(char)
            escaped = True
        elif char == '"':
            quoted = not quoted
            out.append(char)
        elif char == '#' and not quoted:
            break
        else:
            out.append(char)
    return ''.join(out)


def parse_value(value):
    """Use precisely the escape substitutions understood by the Rust parser."""
    if value.startswith('"') and value.endswith('"') and len(value) >= 2:
        chars = iter(value[1:-1])
        out = []
        replacements = {'"': '"', '\\': '\\', 'n': '\n', 'r': '\r', 't': '\t'}
        for char in chars:
            if char != '\\':
                out.append(char)
                continue
            escaped = next(chars, None)
            out.append('\\' if escaped is None else replacements.get(escaped, escaped))
        return ''.join(out)
    return value.strip()


def parse_config(text):
    if not isinstance(text, str):
        raise ValueError('configuration is missing')
    outbounds, rules = [], []
    section, current, ui = 'root', None, False
    root_numbers = {'reload_interval_secs', 'max_connections', 'thread_stack_bytes',
                    'tcp_idle_timeout_secs', 'tcp_half_close_timeout_secs', 'udp_idle_timeout_secs'}
    for raw in text.split('\n'):
        if raw.strip() == MANAGED_BEGIN:
            ui = True
        elif raw.strip() == MANAGED_END:
            ui = False
        line = strip_comment(raw).strip()
        if not line:
            continue
        if line.startswith('[[') and line.endswith(']]'):
            # trim_*_matches removes repeated tokens in the Rust parser.
            name = line
            while name.startswith('[['):
                name = name[2:]
            while name.endswith(']]'):
                name = name[:-2]
            name = name.strip()
            if name == 'outbounds':
                section = 'outbound'
                current = {'tag': '', 'kind': 'direct', 'proxy': None}
                outbounds.append(current)
            elif name in ('rules', 'routing.rules'):
                section = 'rule'
                current = {'type': 'domain_suffix', 'value': '', 'outbound': '',
                           'index': len(rules), 'source': 'ui' if ui else 'manual'}
                rules.append(current)
            else:
                raise ValueError('unsupported section: ' + name)
            continue
        key, separator, value = line.partition('=')
        key = key.strip()
        if not separator or not key:
            raise ValueError('invalid configuration assignment')
        value = parse_value(value.strip())
        if section == 'root':
            if key in root_numbers:
                if not re.fullmatch(r'\+?[0-9]+', value) or int(value) > 2 ** 64 - 1:
                    raise ValueError(key + ' must be an unsigned integer')
        elif section == 'outbound':
            if key == 'kind':
                if value not in OUTBOUND_KINDS:
                    raise ValueError('unsupported outbound kind: ' + value)
                current['kind'] = value
            elif key in ('tag', 'addr'):
                current['proxy' if key == 'addr' else key] = value
        elif key == 'type':
            if value not in RULE_TYPES:
                raise ValueError('unsupported rule type: ' + value)
            current['type'] = value
        elif key in ('value', 'domain'):
            current['value'] = value
        elif key in RULE_TYPES:
            current['type'], current['value'] = key, value
        elif key in ('outbound', 'outbound_tag'):
            current['outbound'] = value
    if not outbounds:
        raise ValueError('at least one outbound is required')
    tags = set()
    for outbound in outbounds:
        if not outbound['tag'] or outbound['tag'] in tags:
            raise ValueError('missing or duplicate outbound tag')
        if outbound['kind'] != 'direct' and outbound['proxy'] is None:
            raise ValueError('proxy outbound address is required')
        tags.add(outbound['tag'])
    for rule in rules:
        if not rule['value'] or rule['outbound'] not in tags:
            raise ValueError('rule value or outbound is invalid')
    return outbounds, rules


def matches(host, rule_type, value):
    value = ascii_lower(value.lstrip('.'))
    if rule_type == 'domain_exact':
        return host == value
    if rule_type == 'domain_suffix':
        return host == value or host.endswith('.' + value)
    return value in host


def preview_route(config_text, domain, routes):
    """Return the first matching config rule, or the first outbound as default.

    Rule indices are zero-based. ``proxy`` is None for direct outbounds. Service
    attribution requires an enabled saved suffix and the same selected proxy;
    manual/default exits without that evidence have no inferred country.
    """
    host, is_ip = normalize_host(domain)
    outbounds, rules = parse_config(config_text)
    selected_rule = next((rule for rule in rules
                          if matches(host, rule['type'], rule['value'])), None)
    tag = selected_rule['outbound'] if selected_rule else outbounds[0]['tag']
    outbound = dict(next(item for item in outbounds if item['tag'] == tag))
    outcome = 'direct' if outbound['kind'] == 'direct' else 'proxy'
    if outcome == 'direct':
        outbound['proxy'] = None
    services, countries = [], set()
    for route in routes or []:
        if (outcome != 'proxy' or not route.get('enabled', True)
                or route.get('proxy') != outbound['proxy']):
            continue
        if not any(matches(host, 'domain_suffix', suffix) for suffix in route.get('domains', [])):
            continue
        profile = route.get('profile')
        if profile and profile not in services:
            services.append(profile)
        countries.add(route.get('country'))
    rule = ({key: selected_rule[key] for key in ('type', 'value', 'index', 'source')}
            if selected_rule else None)
    return {'domain': host, 'is_ip': is_ip, 'matched': selected_rule is not None,
            'rule': rule, 'outbound': outbound, 'services': services,
            'country': next(iter(countries)) if len(countries) == 1 else None,
            'outcome': outcome}

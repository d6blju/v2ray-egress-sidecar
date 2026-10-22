"""Add a session gate to an existing Basic-protected sidecar location only."""
import re
import os
import stat
import sys

from sidecar_route_transaction import atomic


def block_end(text, start):
    depth, quote, escaped, comment = 0, None, False, False
    for index in range(start, len(text)):
        char = text[index]
        if comment:
            comment = char != '\n'
        elif escaped:
            escaped = False
        elif char == '\\':
            escaped = True
        elif quote:
            if char == quote:
                quote = None
        elif char in ('"', "'"):
            quote = char
        elif char == '#':
            comment = True
        elif char == '{':
            depth += 1
        elif char == '}':
            depth -= 1
            if depth == 0:
                return index + 1
    raise ValueError('unterminated nginx block')


def protect(text):
    if '# sidecar-session-guard-v1' in text:
        return text
    matches = list(re.finditer(r'(?m)^(\s*)location\s+/sidecar-stats/\s*\{', text))
    if len(matches) != 1:
        raise ValueError('expected exactly one sidecar static location')
    match = matches[0]
    end = block_end(text, match.end() - 1)
    body = text[match.end():end - 1]
    directives = []
    for name in ('auth_basic', 'auth_basic_user_file', 'alias'):
        values = re.findall(r'(?m)^\s*' + name + r'\s+([^;\n]+);', body)
        if len(values) != 1 or (name == 'auth_basic' and values[0] == 'off'):
            raise ValueError('missing or ambiguous ' + name)
        directives.append(values[0])
    if re.search(r'\b(auth_request|error_page)\b', body):
        raise ValueError('existing static authentication/error handling requires manual review')
    for uri in ('/_sidecar_session_check', '/sidecar-stats/login.html', '/sidecar-stats/auth.js'):
        if uri in text:
            raise ValueError('conflicting location: ' + uri)
    realm, credential_file, root = directives
    if not root.endswith('/'):
        raise ValueError('static alias must end in slash')
    gate = '''
        # sidecar-session-guard-v1
        location = /_sidecar_session_check {
            internal;
            auth_basic off;
            proxy_pass http://127.0.0.1:18082/api/auth/check;
            proxy_pass_request_body off;
            proxy_set_header Content-Length "";
            proxy_set_header Host $http_host;
            proxy_set_header X-Real-IP $remote_addr;
        }
'''
    for asset in ('login.html', 'auth.js'):
        gate += '''
        location = /sidecar-stats/%s {
            auth_basic %s;
            auth_basic_user_file %s;
            alias %s%s;
            add_header Cache-Control "no-store" always;
            add_header X-Frame-Options DENY always;
            add_header X-Content-Type-Options nosniff always;
        }
''' % (asset, realm, credential_file, root, asset)
    guard = '''
            auth_request /_sidecar_session_check;
            auth_request_set $sidecar_session_cookie $upstream_http_set_cookie;
            add_header Set-Cookie $sidecar_session_cookie always;
            add_header Cache-Control "no-store" always;
            add_header X-Frame-Options DENY always;
            add_header X-Content-Type-Options nosniff always;
            error_page 401 =302 /sidecar-stats/login.html;
'''
    return text[:match.start()] + gate + text[match.start():match.end()] + guard + text[match.end():]


if __name__ == '__main__':
    path = sys.argv[1]
    with open(path, encoding='utf-8') as source:
        original = source.read()
    updated = protect(original)
    if updated != original:
        atomic(path, updated, mode=stat.S_IMODE(os.stat(path).st_mode))

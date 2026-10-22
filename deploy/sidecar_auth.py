"""Password verification, persistent opaque sessions and session-bound CSRF."""
import base64
import contextlib
import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import sqlite3
import subprocess
import threading
import time
from http.cookies import SimpleCookie
from urllib.parse import urlsplit

from sidecar_route_transaction import atomic


class AuthError(Exception):
    def __init__(self, message, status=401):
        super().__init__(message)
        self.status = status


def password_record(password, iterations=600000):
    salt = secrets.token_bytes(24)
    return {'algorithm': 'pbkdf2_sha256', 'iterations': iterations, 'salt': salt.hex(),
            'hash': hashlib.pbkdf2_hmac('sha256', password.encode(), salt, iterations).hex()}


def password_matches(password, record):
    try:
        if record['algorithm'] != 'pbkdf2_sha256':
            return False
        value = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(record['salt']), int(record['iterations'])).hex()
        return hmac.compare_digest(value, record['hash'])
    except (KeyError, ValueError, TypeError):
        return False


class AuthManager:
    def __init__(self, directory, htpasswd=None, secure=True, cookie_path='/', iterations=600000,
                 idle=1800, lifetime=43200):
        self.directory, self.htpasswd = directory, htpasswd
        self.secure, self.cookie_path, self.iterations = secure, cookie_path, iterations
        self.idle, self.lifetime = idle, lifetime
        if not cookie_path.startswith('/') or any(c in cookie_path for c in (';', '\r', '\n')):
            raise ValueError('invalid session cookie path')
        os.makedirs(directory, mode=0o700, exist_ok=True)
        self.users_path = os.path.join(directory, 'auth-users.json')
        self.key_path = os.path.join(directory, 'auth-csrf.key')
        self.lock = threading.RLock()
        with self.credentials_lock():
            if not os.path.exists(self.key_path):
                atomic(self.key_path, secrets.token_hex(32))
        with open(self.key_path) as source:
            self.key = bytes.fromhex(source.read().strip())
        self.database = os.path.join(directory, 'auth-sessions.sqlite3')
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY, username TEXT, revision TEXT,
                    created REAL, seen REAL, expires REAL);
                CREATE INDEX IF NOT EXISTS session_expiry ON sessions(expires);
                CREATE TABLE IF NOT EXISTS attempts (key TEXT PRIMARY KEY, started REAL, count INTEGER);
            ''')
        os.chmod(self.database, 0o600)

    @contextlib.contextmanager
    def credentials_lock(self):
        with open(os.path.join(self.directory, 'auth.lock'), 'a') as lock:
            if os.name != 'nt':
                import fcntl
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if os.name != 'nt':
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    @contextlib.contextmanager
    def db(self):
        with self.lock:
            db = sqlite3.connect(self.database, timeout=3)
            try:
                with db:
                    yield db
            finally:
                db.close()

    def users(self):
        try:
            with open(self.users_path, encoding='utf-8') as source:
                return json.load(source)
        except FileNotFoundError:
            return {}

    def legacy_hash(self, username):
        if not self.htpasswd:
            return None
        try:
            with open(self.htpasswd, encoding='utf-8') as source:
                for line in source:
                    name, separator, value = line.strip().partition(':')
                    if separator and hmac.compare_digest(name.encode(), username.encode()):
                        return value
        except FileNotFoundError:
            pass
        return None

    def revision(self, username):
        record = self.users().get(username)
        if not record:
            return None
        legacy = self.legacy_hash(username)
        if 'legacy_fingerprint' in record:
            if not legacy or hashlib.sha256(legacy.encode()).hexdigest() != record['legacy_fingerprint']:
                return None
        return hashlib.sha256(json.dumps(record, sort_keys=True).encode()).hexdigest()

    def set_password(self, username, password):
        self.check_fields(username, password)
        with self.credentials_lock():
            users = self.users()
            users[username] = password_record(password, self.iterations)
            atomic(self.users_path, json.dumps(users, sort_keys=True, indent=2))
        with self.db() as db:
            db.execute('DELETE FROM sessions WHERE username=?', (username,))

    @staticmethod
    def check_fields(username, password):
        if not isinstance(username, str) or not isinstance(password, str) or not username or len(username) > 128 or ':' in username or any(c in username for c in '\r\n') or not password or any(c in password for c in '\r\n\x00') or len(password.encode()) > 1024:
            raise AuthError('invalid credentials')

    def verify_legacy(self, password, hashed):
        if hashed.startswith('$apr1$'):
            salt = hashed.split('$')[2]
            try:
                result = subprocess.run(['openssl', 'passwd', '-apr1', '-salt', salt, '-stdin'],
                                        input=password + '\n', universal_newlines=True,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5)
                return result.returncode == 0 and hmac.compare_digest(result.stdout.strip(), hashed)
            except (OSError, subprocess.TimeoutExpired):
                return False
        if hashed.startswith('{SHA}'):
            return hmac.compare_digest(base64.b64encode(hashlib.sha1(password.encode()).digest()).decode(), hashed[5:])
        try:
            import crypt
            value = crypt.crypt(password, hashed)
            return bool(value) and hmac.compare_digest(value, hashed)
        except (ImportError, ValueError):
            return False

    def throttle(self, peer, username):
        now = time.time()
        keys = ['all-logins', 'ip:' + peer, 'account:' + hashlib.sha256(username.encode()).hexdigest()]
        with self.db() as db:
            db.execute('DELETE FROM attempts WHERE started<?', (now - 60,))
            for key, limit in zip(keys, (30, 10, 10)):
                row = db.execute('SELECT count FROM attempts WHERE key=?', (key,)).fetchone()
                if row and row[0] >= limit:
                    raise AuthError('too many login attempts; retry in one minute', 429)
            for key in keys:
                row = db.execute('SELECT count FROM attempts WHERE key=?', (key,)).fetchone()
                if row:
                    db.execute('UPDATE attempts SET count=count+1 WHERE key=?', (key,))
                else:
                    db.execute('INSERT INTO attempts VALUES (?,?,1)', (key, now))

    def authenticate(self, username, password, peer):
        self.check_fields(username, password)
        self.throttle(peer, username)
        record = self.users().get(username)
        if record and self.revision(username) and password_matches(password, record):
            return username
        legacy = self.legacy_hash(username)
        # An established modern hash must not silently fall back to its weaker predecessor.
        if record and self.revision(username):
            raise AuthError('invalid credentials')
        if not legacy or not self.verify_legacy(password, legacy):
            raise AuthError('invalid credentials')
        with self.credentials_lock():
            users = self.users()
            users[username] = dict(password_record(password, self.iterations),
                                   legacy_fingerprint=hashlib.sha256(legacy.encode()).hexdigest())
            atomic(self.users_path, json.dumps(users, sort_keys=True, indent=2))
        return username

    @staticmethod
    def token_hash(token):
        return hashlib.sha256(token.encode()).hexdigest()

    def csrf(self, token):
        return hmac.new(self.key, ('csrf:' + token).encode(), hashlib.sha256).hexdigest()

    def create(self, username=None, old=None):
        token = secrets.token_urlsafe(32)
        now = time.time()
        revision = self.revision(username) if username else None
        if username and not revision:
            raise AuthError('account changed; log in again')
        with self.db() as db:
            db.execute('DELETE FROM sessions WHERE expires<? OR seen<?', (now, now - self.idle))
            if db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0] >= 2000:
                raise AuthError('session capacity reached; retry later', 429)
            if old:
                db.execute('DELETE FROM sessions WHERE token_hash=?', (self.token_hash(old),))
            db.execute('INSERT INTO sessions VALUES (?,?,?,?,?,?)',
                       (self.token_hash(token), username, revision,
                        now, now, now + (self.lifetime if username else 300)))
        return token

    def lookup(self, token, touch=True):
        if not token or len(token) > 128:
            return None
        now = time.time()
        with self.db() as db:
            row = db.execute('SELECT username,revision,created,seen,expires FROM sessions WHERE token_hash=?', (self.token_hash(token),)).fetchone()
            if not row:
                return None
            revision = self.revision(row[0]) if row[0] else None
            if now > row[4] or now - row[3] > self.idle or (row[0] and (not revision or revision != row[1])):
                db.execute('DELETE FROM sessions WHERE token_hash=?', (self.token_hash(token),))
                return None
            if touch and now - row[3] >= 30:
                db.execute('UPDATE sessions SET seen=? WHERE token_hash=?', (now, self.token_hash(token)))
            return {'username': row[0], 'expires': row[4], 'created': row[2]}

    def cookie(self, token, name='sidecar_session', max_age=None):
        age = self.lifetime if max_age is None else max_age
        return '%s=%s; Path=%s; Max-Age=%d; HttpOnly; SameSite=Strict%s' % (name, token, self.cookie_path, age, '; Secure' if self.secure else '')

    @staticmethod
    def cookies(handler):
        jar = SimpleCookie()
        try:
            jar.load(handler.headers.get('Cookie', ''))
        except Exception:
            return '', False
        return (jar['sidecar_session'].value if 'sidecar_session' in jar else '',
                'sidecar_logged_out' in jar)

    def origin_check(self, handler):
        if handler.headers.get('Sec-Fetch-Site') == 'cross-site':
            raise AuthError('cross-site request rejected', 403)
        origin = handler.headers.get('Origin')
        if origin:
            parsed = urlsplit(origin)
            expected_scheme = 'https' if self.secure else 'http'
            if parsed.scheme != expected_scheme or parsed.netloc.lower() != handler.headers.get('Host', '').lower() or parsed.path or parsed.query or parsed.fragment:
                raise AuthError('request origin rejected', 403)

    def require(self, handler, write=False, prelogin=False):
        self.origin_check(handler)
        token, _ = self.cookies(handler)
        session = self.lookup(token)
        if not session or (not prelogin and not session['username']):
            raise AuthError('login required')
        if write:
            if handler.headers.get('Content-Type', '').split(';')[0].strip().lower() != 'application/json':
                raise AuthError('application/json required', 415)
            supplied = handler.headers.get('X-CSRF-Token', '')
            if not hmac.compare_digest(supplied.encode(), self.csrf(token).encode()):
                raise AuthError('CSRF token rejected', 403)
        return token, session

    def peer(self, handler):
        peer = handler.client_address[0]
        if ipaddress.ip_address(peer).is_loopback:
            try:
                peer = str(ipaddress.ip_address(handler.headers.get('X-Real-IP', peer)))
            except ValueError:
                pass
        return peer

    def bootstrap(self, handler, internal=False):
        self.origin_check(handler)
        token, logged_out = self.cookies(handler)
        session = self.lookup(token)
        if session and session['username']:
            return token, session, None
        authorization = handler.headers.get('Authorization', '')
        if not logged_out and authorization.startswith('Basic ') and (internal or handler.headers.get('X-Sidecar-Session') == '1'):
            try:
                decoded = base64.b64decode(authorization[6:], validate=True).decode('utf-8')
                username, password = decoded.split(':', 1)
            except (ValueError, UnicodeError):
                raise AuthError('invalid credentials')
            username = self.authenticate(username, password, self.peer(handler))
            token = self.create(username, token)
            return token, self.lookup(token), self.cookie(token)
        if not session:
            token = self.create(old=token)
            session = self.lookup(token)
            return token, session, self.cookie(token, max_age=300)
        return token, session, None

    def logout(self, token):
        with self.db() as db:
            db.execute('DELETE FROM sessions WHERE token_hash=?', (self.token_hash(token),))


def main():
    import argparse
    import getpass
    parser = argparse.ArgumentParser(description='Create/update a local admin password without command-line secrets')
    parser.add_argument('--directory', default=os.environ.get('SIDECAR_ADMIN_STATE_DIR', '/var/lib/v2ray-egress-sidecar-admin'))
    parser.add_argument('username')
    args = parser.parse_args()
    password = getpass.getpass('Password: ')
    if password != getpass.getpass('Confirm password: '):
        raise SystemExit('passwords differ')
    AuthManager(args.directory).set_password(args.username, password)
    print('Account saved; existing sessions revoked.')


if __name__ == '__main__':
    main()

"""Durable route replacement, conditional rollback, and startup recovery."""
import contextlib
import hashlib
import json
import os
import stat
import tempfile
import time
import uuid


def digest(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def read_text(path):
    try:
        with open(path, encoding='utf-8') as source:
            return source.read()
    except FileNotFoundError:
        return None


def atomic(path, text, mode=0o600):
    directory = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(prefix='.route-', dir=directory)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='') as target:
            target.write(text)
            target.flush()
            os.fsync(target.fileno())
        os.chmod(tmp, mode)
        if os.name != 'nt' and os.path.exists(path):
            original = os.stat(path)
            os.chown(tmp, original.st_uid, original.st_gid)
        os.replace(tmp, path)
        if os.name != 'nt':
            directory_fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


class RouteError(RuntimeError):
    def __init__(self, message, result=None):
        super().__init__(message)
        self.transaction_result = result


class RouteTransaction:
    def __init__(self, config, state, directory):
        self.config, self.state, self.directory = config, state, directory
        os.makedirs(directory, mode=0o700, exist_ok=True)
        self.journal = os.path.join(directory, 'route-transaction.json')
        self.receipt = os.path.join(directory, 'route-last-result.json')
        self.failure = os.path.join(directory, 'route-last-failure.json')

    @contextlib.contextmanager
    def locked(self):
        with open(os.path.join(self.directory, 'route.lock'), 'a') as lock:
            if os.name != 'nt':
                import fcntl
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield self
            finally:
                if os.name != 'nt':
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def save(self, path, data):
        atomic(path, json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2) + '\n')

    def result(self):
        pending = read_text(self.journal)
        if pending:
            try:
                transaction = json.loads(pending)
                if not isinstance(transaction, dict):
                    raise ValueError('invalid journal object')
            except ValueError:
                return {'status': 'recovery_blocked', 'phase': 'journal_corrupt', 'updated_at': time.time(),
                        'error': 'transaction journal is unreadable; manual reconciliation required'}
            if transaction.get('phase') == 'committed':
                return dict(transaction['result'], cleanup_pending=True)
        text = read_text(self.receipt)
        return json.loads(text) if text else None

    def fail(self, error, phase='preflight'):
        result = {'status': 'failed', 'phase': phase, 'updated_at': time.time(), 'error': str(error)[:1000]}
        self.save(self.receipt, result)
        self.save(self.failure, result)
        return result

    def rollback(self, transaction, checkpoint, confirm):
        current = read_text(self.config)
        if current is None or digest(current) not in (transaction['old_hash'], transaction['new_hash']):
            raise RouteError('configuration changed externally; rollback blocked to preserve the external edit')
        state = read_text(self.state)
        if state not in (transaction['old_state'], transaction['new_state']):
            raise RouteError('state changed externally; rollback blocked to preserve the external edit')
        needs_confirmation = digest(current) != transaction['old_hash'] or transaction['phase'] == 'rolling_back'
        transaction['phase'] = 'rolling_back'
        self.save(self.journal, transaction)
        cursor, cursor_error = None, None
        if needs_confirmation:
            try:
                cursor = checkpoint()
            except Exception as error:
                cursor_error = error
            atomic(self.config, transaction['old_config'], transaction['mode'])
        if transaction['old_state'] is None:
            if os.path.exists(self.state):
                os.unlink(self.state)
        else:
            atomic(self.state, transaction['old_state'])
        if needs_confirmation:
            if cursor_error:
                raise RouteError('old files restored; reload confirmation unavailable: %s' % cursor_error)
            confirm(cursor, transaction['old_config'])

    def recover(self, checkpoint, confirm):
        text = read_text(self.journal)
        if not text:
            return None
        transaction = json.loads(text)
        if transaction['phase'] == 'committed':
            if digest(read_text(self.config) or '') != transaction['new_hash'] or read_text(self.state) != transaction['new_state']:
                raise RouteError('committed transaction has external edits; manual reconciliation required')
            result = dict(transaction['result'], recovered=True)
        else:
            try:
                self.rollback(transaction, checkpoint, confirm)
            except Exception as error:
                self.fail(error, 'recovery_conflict')
                raise
            result = {'status': 'rolled_back', 'phase': 'startup_recovery', 'id': transaction['id'],
                      'error': 'interrupted transaction restored', 'updated_at': time.time()}
        self.save(self.receipt, result)
        if result['status'] != 'applied':
            self.save(self.failure, result)
        os.unlink(self.journal)
        return result

    def execute(self, expected, candidate, state, validate, checkpoint, confirm, verify=None):
        if read_text(self.journal):
            raise RouteError('an unresolved transaction exists; recover it before another mutation')
        validate(expected)
        validate(candidate)
        if read_text(self.config) != expected:
            raise RouteError('configuration changed during preflight; retry against the new configuration')
        identifier = uuid.uuid4().hex
        backup = self.config + '.bak.' + identifier + '-transaction'
        state = dict(state, backup=backup, config_sha256=digest(candidate))
        new_state = json.dumps(state, ensure_ascii=False, sort_keys=True, indent=2) + '\n'
        transaction = {'id': identifier, 'phase': 'prepared', 'mode': stat.S_IMODE(os.stat(self.config).st_mode),
                       'old_config': expected, 'old_hash': digest(expected), 'new_hash': digest(candidate),
                       'old_state': read_text(self.state), 'new_state': new_state}
        if transaction['old_state'] is not None and not isinstance(json.loads(transaction['old_state']), dict):
            raise RouteError('state file is not a JSON object')
        cursor = checkpoint()
        atomic(backup, expected)
        self.save(self.journal, transaction)
        try:
            if read_text(self.config) != expected or read_text(self.state) != transaction['old_state']:
                raise RouteError('configuration or state changed before publication')
            atomic(self.config, candidate, transaction['mode'])
            transaction['phase'] = 'published'
            self.save(self.journal, transaction)
            evidence = confirm(cursor, candidate)
            if verify:
                verify()
            if read_text(self.config) != candidate or read_text(self.state) != transaction['old_state']:
                raise RouteError('configuration or state changed during reload verification')
            atomic(self.state, new_state)
            result = {'status': 'applied', 'phase': 'committed', 'id': identifier, 'updated_at': time.time(),
                      'config_sha256': digest(candidate), 'reload': evidence, 'backup': backup}
            transaction.update(phase='committed', result=result)
            self.save(self.journal, transaction)
        except Exception as error:
            result = {'status': 'rolled_back', 'phase': transaction['phase'], 'id': identifier,
                      'updated_at': time.time(), 'error': str(error)[:1000], 'backup': backup}
            try:
                self.rollback(transaction, checkpoint, confirm)
                os.unlink(self.journal)
            except Exception as rollback_error:
                result.update(status='rollback_failed', rollback_error=str(rollback_error)[:1000])
            self.save(self.receipt, result)
            self.save(self.failure, result)
            raise RouteError(json.dumps(result, ensure_ascii=False), result)
        # Once committed, a receipt/cleanup failure must not undo a durable commit.
        # Startup recovery can finish these idempotent bookkeeping operations.
        try:
            self.save(self.receipt, result)
            os.unlink(self.journal)
        except OSError:
            result['cleanup_pending'] = True
        return state, result

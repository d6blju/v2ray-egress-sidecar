import copy
import json
import os
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from sidecar_streaming import test_service, test_matrix


class StreamingJobs:
    def __init__(self, pool, protected_ports, state_dir, tester=test_service, matrix_tester=test_matrix, mutation_lock=None):
        self.pool = pool
        self.protected_ports = protected_ports
        self.tester = tester
        self.matrix_tester = matrix_tester
        self.mutation_lock = mutation_lock or threading.Lock()
        self.path = os.path.join(state_dir, "streaming-jobs.json")
        self.lock = threading.Lock()
        self.executor = ThreadPoolExecutor(max_workers=1)
        try:
            with open(self.path, "r", encoding="utf-8") as source:
                self.jobs = json.load(source)
        except (OSError, ValueError):
            self.jobs = {}
        for job in self.jobs.values():
            if job["status"] in ("queued", "running"):
                job.update(status="interrupted", error="API restarted during this test")

    def _save(self):
        os.makedirs(os.path.dirname(self.path), mode=0o700, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".streaming-", dir=os.path.dirname(self.path))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                json.dump(self.jobs, output, ensure_ascii=False)
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def get(self, identifier):
        with self.lock:
            return copy.deepcopy(self.jobs.get(identifier))

    def recent(self):
        with self.lock:
            return copy.deepcopy(sorted(self.jobs.values(), key=lambda job: job["created_at"], reverse=True)[:20])

    def active_countries(self):
        with self.lock:
            return {job["country"].casefold() for job in self.jobs.values() if job["status"] in ("queued", "running")}

    def submit(self, profile, country, protocol, titles, preferred_port):
        key = [profile, country, protocol, titles]
        with self.lock:
            pending = [job for job in self.jobs.values() if job["status"] in ("queued", "running")]
            for job in pending:
                if job["key"] == key:
                    return copy.deepcopy(job)
            if len(pending) >= 4:
                raise ValueError("streaming test queue is full; wait for current tests")
            if len(self.jobs) >= 20:
                finished = [job for job in self.jobs.values() if job["status"] not in ("queued", "running")]
                if finished:
                    oldest = min(finished, key=lambda job: job["created_at"])
                    del self.jobs[oldest["id"]]
            identifier = uuid.uuid4().hex
            job = {"id": identifier, "key": key, "profile": profile, "country": country,
                   "protocol": protocol, "status": "queued", "created_at": int(time.time())}
            self.jobs[identifier] = job
            self._save()
            snapshot = copy.deepcopy(job)
        self.executor.submit(self._perform, identifier, titles, preferred_port)
        return snapshot

    def _perform(self, identifier, titles, preferred_port):
        with self.lock:
            self.jobs[identifier]["status"] = "running"
            snapshot = copy.deepcopy(self.jobs[identifier])
            self._save()
        try:
            with self.mutation_lock:
                info = self.pool.ensure(snapshot["country"], snapshot["protocol"], preferred_port, self.protected_ports())
            result = self.matrix_tester(snapshot["country"], info, titles) if snapshot["profile"] == "matrix" else self.tester(snapshot["profile"], snapshot["country"], info, titles)
            with self.lock:
                self.jobs[identifier].update(status="done", result=result, finished_at=int(time.time()))
                self._save()
        except Exception as error:
            with self.lock:
                self.jobs[identifier].update(status="failed", error=str(error), finished_at=int(time.time()))
                self._save()

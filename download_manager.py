"""Download queue with observable per-track state.

The old flow ran downloads inline in the App, reported progress by appending
lines to a text box, and had no notion of an individual track's state. So a
failure scrolled out of view and was gone: nothing recorded what failed, and
there was no way to retry it or to cancel a long album mid-run.
"""

import threading
import time
from concurrent.futures import ThreadPoolExecutor

QUEUED = "queued"
RUNNING = "running"
DONE = "done"
SKIPPED = "skipped"
FAILED = "failed"
CANCELLED = "cancelled"

TERMINAL = (DONE, SKIPPED, FAILED, CANCELLED)


class Job:
    __slots__ = ("url", "label", "state", "error", "path", "started", "finished")

    def __init__(self, url, label=None):
        self.url = url
        self.label = label or url
        self.state = QUEUED
        self.error = None
        self.path = None
        self.started = None
        self.finished = None

    @property
    def ok(self):
        return self.state in (DONE, SKIPPED)


class DownloadManager:
    """Runs a batch of track URLs through `process_track` on a worker pool.

    `on_change` fires (on worker threads) whenever a job's state changes, so
    a UI can render live per-track status instead of a scrolling log.
    """

    def __init__(self, sp, process_track, on_change=None, on_log=None):
        self._sp = sp
        self._process_track = process_track
        self._on_change = on_change
        self._on_log = on_log
        self._lock = threading.Lock()
        self.jobs = []
        self._cancel = threading.Event()
        self._thread = None

    # ------------------------------------------------------------- state

    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive()

    def summary(self):
        with self._lock:
            jobs = list(self.jobs)
        counts = {s: 0 for s in (QUEUED, RUNNING, DONE, SKIPPED, FAILED, CANCELLED)}
        for job in jobs:
            counts[job.state] = counts.get(job.state, 0) + 1
        counts["total"] = len(jobs)
        return counts

    def failed_jobs(self):
        with self._lock:
            return [j for j in self.jobs if j.state == FAILED]

    def _log(self, message):
        if self._on_log:
            try:
                self._on_log(message)
            except Exception:
                pass

    def _touch(self, job):
        if self._on_change:
            try:
                self._on_change(job)
            except Exception:
                pass

    # ----------------------------------------------------------- control

    def start(self, urls, out_dir, jobs=3, quality="192", labels=None):
        """Queue `urls` and begin. Returns False if a batch is already running."""
        if self.running:
            return False
        labels = labels or {}
        with self._lock:
            self.jobs = [Job(u, labels.get(u)) for u in urls]
        self._cancel.clear()
        self._thread = threading.Thread(
            target=self._run, args=(out_dir, jobs, quality), daemon=True
        )
        self._thread.start()
        return True

    def retry_failed(self, out_dir, jobs=3, quality="192"):
        """Re-run only the jobs that failed, keeping the rest of the batch."""
        if self.running:
            return False
        failed = self.failed_jobs()
        if not failed:
            return False
        for job in failed:
            job.state = QUEUED
            job.error = None
            self._touch(job)
        self._cancel.clear()
        self._thread = threading.Thread(
            target=self._run, args=(out_dir, jobs, quality, failed), daemon=True
        )
        self._thread.start()
        return True

    def cancel(self):
        """Ask the batch to stop. In-flight downloads finish; the rest are dropped."""
        self._cancel.set()

    def wait(self, timeout=None):
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=timeout)

    # -------------------------------------------------------------- work

    def _run(self, out_dir, workers, quality, subset=None):
        with self._lock:
            pending = list(subset if subset is not None else self.jobs)
        pending = [j for j in pending if j.state == QUEUED]
        if not pending:
            return

        workers = max(1, min(int(workers), 8))
        self._log(f"Downloading {len(pending)} track(s), {workers} at a time.")
        started = time.time()

        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(lambda j: self._one(j, out_dir, quality), pending))

        counts = self.summary()
        elapsed = time.time() - started
        parts = [f"{counts[DONE]} downloaded"]
        if counts[SKIPPED]:
            parts.append(f"{counts[SKIPPED]} already present")
        if counts[FAILED]:
            parts.append(f"{counts[FAILED]} failed")
        if counts[CANCELLED]:
            parts.append(f"{counts[CANCELLED]} cancelled")
        self._log(f"Finished in {elapsed:.0f}s: " + ", ".join(parts) + ".")
        if counts[FAILED]:
            self._log("Press Retry failed to try those again.")

    def _one(self, job, out_dir, quality):
        if self._cancel.is_set():
            job.state = CANCELLED
            job.finished = time.time()
            self._touch(job)
            return

        job.state = RUNNING
        job.started = time.time()
        self._touch(job)

        try:
            result = self._process_track(
                self._sp, job.url, out_dir,
                log_callback=self._log, quality=quality,
            )
        except Exception as e:
            result = {"ok": False, "error": f"{type(e).__name__}: {e}"}

        job.finished = time.time()
        if result.get("ok"):
            job.state = SKIPPED if result.get("skipped") else DONE
            job.path = result.get("path")
            meta = result.get("metadata")
            if meta and job.label == job.url:
                job.label = f"{', '.join(meta['artists'])} - {meta['name']}"
        else:
            job.state = FAILED
            job.error = result.get("error") or "unknown error"
        self._touch(job)

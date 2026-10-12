"""Scan the libraries, change files, restore them, and keep everyone informed.

Only one job runs at a time (a scan, a batch of changes or a restore), so a file
is never read and rewritten at once.
"""

import datetime
import os
import threading
import time
from contextlib import contextmanager

import utils.config as config
from core import plan as plans
from core import swap
from core.plan import Rules, decide
from core.probe import PROBE_VERSION, MediaInfo, ProbeError, probe
from core.remux import RemuxError, remux, supported
from core.store import Store
from services.connections import Arr, Jellyfin, ServiceError
from utils.logger import logger
from utils.paths import inside

# Statuses Dubarr adds on top of the plan's
SEEDING = "seeding"   # would change, but it's hardlinked and probably still seeding
ERROR = "error"       # couldn't be read or changed

APPLY_EVERY_SECONDS = 600


class Busy(Exception):
    pass


def in_window(start: int, end: int, now: datetime.datetime = None) -> bool:
    """Is it within the hours files may be changed? The same hour twice means any time."""
    if start == end:
        return True
    hour = (now or datetime.datetime.now()).hour
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def size(n: int) -> str:
    for unit in ("bytes", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1024


class Engine:
    def __init__(self, store: Store):
        self.store = store
        self._job = threading.Lock()
        self._stop = threading.Event()
        self.activity = None          # what's happening now, for the web UI
        self.last_scan = 0.0
        self.last_apply = 0.0
        self.last_purge = 0.0
        self._services = {}

    # Jobs

    @contextmanager
    def job(self, task, wait=False):
        if not self._job.acquire(blocking=wait):
            raise Busy(f"Dubarr is busy: {self.activity['task'] if self.activity else 'working'}")
        self._stop.clear()
        self.activity = {"task": task, "detail": "", "done": 0, "total": 0, "started": time.time()}
        try:
            yield
        finally:
            self.activity = None
            self._job.release()

    def busy(self):
        return self._job.locked()

    def stop(self):
        self._stop.set()

    def background(self, target, *args, wait=False, **kwargs):
        """Run a job in its own thread. Raises Busy straight away, unless wait is set."""
        if not wait and self.busy():
            raise Busy(f"Dubarr is busy: {self.activity['task'] if self.activity else 'working'}")

        def run():
            try:
                target(*args, wait=wait, **kwargs)
            except Busy:
                pass
            except Exception as e:
                logger.exception(f"{target.__name__} failed: {e}")

        threading.Thread(target=run, daemon=True).start()

    # Libraries

    def library_for(self, path):
        matches = [lib for lib in config.library_list if inside(path, lib["path"])]
        return max(matches, key=lambda lib: len(lib["path"])) if matches else None

    def rules(self, library):
        return Rules(library["audio"], library["subtitles"], config.strip_audio, config.strip_subtitles)

    def _walk(self, library):
        recycle = config.recycle_folder
        for folder, dirs, files in os.walk(library["path"]):
            # Hidden folders include the recycle folder; never look inside it
            dirs[:] = sorted(d for d in dirs if not d.startswith(".")
                             and not (recycle and inside(os.path.join(folder, d), recycle)))
            for name in sorted(files):
                path = os.path.join(folder, name)
                if name.endswith(".dubarr.tmp"):
                    # Left behind by a change that was interrupted (none runs during a scan)
                    try:
                        os.remove(path)
                        logger.info(f"Removed an unfinished copy left behind: {path}")
                    except OSError:
                        pass
                    continue
                if name.startswith(".") or not supported(name):
                    continue
                yield path

    # Scanning

    def check_file(self, library, path, reread=False):
        """Read a file's tracks (or reuse what's known) and decide what to do. Returns its status."""
        try:
            st = os.stat(path)
        except OSError as e:
            logger.warning(f"Can't read {path}: {e.strerror or e}")
            return None

        cached = self.store.file(path)
        info = None
        if cached and cached["info"] and not reread and cached["size"] == st.st_size and cached["mtime"] == st.st_mtime:
            info = MediaInfo.from_dict(cached["info"])
            if info.version != PROBE_VERSION:
                info = None
        if info is None:
            try:
                info = probe(path)
            except ProbeError as e:
                self.store.save_file(path, library["path"], st.st_size, st.st_mtime, st.st_nlink, None, ERROR,
                                     f"Couldn't read the file: {e}")
                return ERROR

        plan = decide(info, self.rules(library))
        status, message = plan.status, plan.message
        if plan.changes and st.st_nlink > 1 and not config.process_hardlinked:
            status, message = SEEDING, f"{plan.message}. Waiting: the file is hardlinked, probably still seeding"
        self.store.save_file(path, library["path"], st.st_size, st.st_mtime, st.st_nlink, info.to_dict(),
                             status, message, plan.to_dict(), plan.saved_bytes)
        return status

    def scan(self, trigger="manual", paths=None, wait=False):
        """Check every file in the libraries, or just the paths given."""
        with self.job("Scanning", wait=wait):
            started = time.time()
            counts = {}
            if paths is None:
                libraries = config.library_list
                self.store.forget_libraries([lib["path"] for lib in libraries])
                for library in libraries:
                    if not os.path.isdir(library["path"]):
                        logger.warning(f"Library folder {library['path']} doesn't exist. Is it mapped in the container?")
                        continue
                    self.activity["detail"] = library["path"]
                    present = set()
                    for path in self._walk(library):
                        if self._stop.is_set():
                            break
                        present.add(path)
                        self.activity["done"] += 1
                        self.activity["detail"] = path
                        status = self.check_file(library, path)
                        counts[status] = counts.get(status, 0) + 1
                    if not self._stop.is_set():
                        self.store.forget_missing(library["path"], present)
                self.last_scan = time.time()
            else:
                self.activity["total"] = len(paths)
                for path in paths:
                    library = self.library_for(path)
                    if not library:
                        logger.warning(f"{path} isn't in any library set up in Dubarr, so it's left alone")
                        continue
                    self.activity["done"] += 1
                    self.activity["detail"] = path
                    status = self.check_file(library, path, reread=True)
                    counts[status] = counts.get(status, 0) + 1

            found = ", ".join(f"{n} {k.replace('_', ' ')}" for k, n in sorted(counts.items(), key=str) if k)
            logger.info(f"Scan ({trigger}) checked {sum(counts.values())} file(s) in {time.time() - started:.0f}s"
                        + (f": {found}" if found else ""))

    def imported(self, paths, wait=True):
        """Radarr or Sonarr just imported these. Check them now, and change them if allowed."""
        self.scan("import", paths, wait=wait)
        self.apply("import", paths, wait=wait)

    # Changing files

    def apply(self, trigger="manual", paths=None, force=False, wait=False):
        """Change files waiting to be changed.

        Automatic passes respect report-only mode, the time window and the batch size.
        force is for the Apply buttons, where you've chosen the files yourself.
        """
        if not force and (config.report_only or not in_window(config.active_from, config.active_until)):
            return
        with self.job("Changing files", wait=wait):
            self.last_apply = time.time()
            statuses = [plans.CHANGE] + ([SEEDING] if config.process_hardlinked else [])
            if paths:
                rows = [r for r in (self.store.file(p) for p in paths) if r and r["status"] in statuses and not r["ignored"]]
            else:
                _, rows = self.store.files(statuses=statuses, ignored=False, limit=1_000_000)
                if not force:
                    rows = rows[:config.max_changes_per_pass]
            if not rows:
                return

            self.activity["total"] = len(rows)
            run_id = self.store.start_run(trigger)
            changed = failed = saved = 0
            for row in rows:
                if self._stop.is_set():
                    logger.info("Stopped before finishing the batch")
                    break
                if not force and not in_window(config.active_from, config.active_until):
                    logger.info("Outside the hours set for changing files. The rest will wait")
                    break
                self.activity["detail"] = row["path"]
                result = self.change_file(run_id, row["path"])
                self.activity["done"] += 1
                if result is None:
                    continue
                if result is False:
                    failed += 1
                else:
                    changed += 1
                    saved += result
            self.store.finish_run(run_id, changed, failed, saved)
            if changed or failed:
                logger.log("ACTION", f"Changed {changed} file(s), saving {size(saved)}"
                           + (f". {failed} couldn't be changed, see the Dashboard" if failed else ""))
            self.purge()

    def change_file(self, run_id, path):
        """Strip one file. Returns bytes saved, False on failure, or None if there was nothing to do."""
        library = self.library_for(path)
        if not library:
            return None
        # Read the file again right before changing it, in case it changed since the scan
        status = self.check_file(library, path, reread=True)
        row = self.store.file(path)
        if status != plans.CHANGE and not (status == SEEDING and config.process_hardlinked):
            return None

        info = MediaInfo.from_dict(row["info"])
        plan = decide(info, self.rules(library))
        try:
            new = remux(info, plan)
            history_id = swap.swap(self.store, run_id, path, info.size, new.size, plan.message, plan.remove,
                                   swap.recycle_root(library["path"], config.recycle_folder), config.recycle_days)
        except (RemuxError, swap.SwapError, OSError) as e:
            reason = getattr(e, "strerror", None) or str(e)
            logger.error(f"Couldn't change {path}: {reason}. The original is untouched")
            st = os.stat(path)
            self.store.save_file(path, library["path"], st.st_size, st.st_mtime, st.st_nlink, row["info"], ERROR,
                                 f"Couldn't change it: {reason}", row["plan"], row["saved_bytes"])
            return False

        logger.log("ACTION", f"{plan.message} from {path} (saved {size(info.size - new.size)}, change #{history_id})")
        self.check_file(library, path, reread=True)
        self.notify(path)
        return info.size - new.size

    # Restoring

    def restore(self, history_id, wait=False):
        with self.job("Restoring", wait=wait):
            self._restore_one(history_id)

    def restore_run(self, run_id, wait=False):
        with self.job("Restoring", wait=wait):
            _, entries = self.store.history(run_id=run_id, limit=1_000_000)
            entries = [e for e in entries if e["state"] == "done"]
            self.activity["total"] = len(entries)
            for entry in entries:
                self.activity["detail"] = entry["path"]
                try:
                    self._restore_one(entry["id"])
                except swap.SwapError as e:
                    logger.error(f"Couldn't restore {entry['path']}: {e}")
                self.activity["done"] += 1

    def _restore_one(self, history_id):
        path = swap.restore(self.store, history_id)
        # Leave it alone from now on, or the next pass would strip it again
        self.store.set_ignored(path, True)
        library = self.library_for(path)
        if library:
            self.check_file(library, path, reread=True)
        logger.log("ACTION", f"Restored the original of {path}. Dubarr will leave this file alone until you say otherwise")
        self.notify(path)

    def purge(self):
        count = swap.purge(self.store, config.recycle_days)
        self.last_purge = time.time()
        if count:
            logger.info(f"Emptied {count} original(s) older than {config.recycle_days} days from the recycle folder")

    def recover(self):
        for message in swap.recover(self.store):
            logger.warning(message)

    # Radarr, Sonarr and Jellyfin

    def _service(self, kind):
        if not getattr(config, f"{kind}_enabled") or not getattr(config, f"{kind}_api_key"):
            return None
        key = (kind, getattr(config, f"{kind}_url"), getattr(config, f"{kind}_api_key"),
               tuple(getattr(config, f"{kind}_paths")), config.request_timeout)
        if self._services.get(kind, (None,))[0] != key:
            self._services[kind] = (key, Jellyfin(config) if kind == "jellyfin" else Arr(kind, config))
        return self._services[kind][1]

    def notify(self, path):
        """Ask Radarr or Sonarr, and Jellyfin, to read the changed file again."""
        for kind in ("radarr", "sonarr"):
            arr = self._service(kind)
            if not arr:
                continue
            try:
                title = arr.rescan(path)
                if title:
                    logger.info(f"Asked {arr.name} to rescan {title}")
            except ServiceError as e:
                logger.warning(f"Couldn't ask {arr.name} to rescan: {e}")
        jellyfin = self._service("jellyfin")
        if jellyfin:
            try:
                jellyfin.updated(path)
            except ServiceError as e:
                logger.warning(f"Couldn't tell Jellyfin about the change: {e}")

    # Main loop

    def tick(self):
        """Called every minute: scan when it's due, change files when allowed, empty the recycle folder daily."""
        now = time.time()
        if self.busy():
            return
        try:
            if now - self.last_scan >= config.scan_interval_hours * 3600:
                self.scan("scheduled")
            if now - self.last_apply >= APPLY_EVERY_SECONDS:
                self.apply("scheduled")
            if now - self.last_purge >= 24 * 3600:
                with self.job("Emptying the recycle folder"):
                    self.purge()
        except Busy:
            pass

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
from core import detect, swap, tagging
from core.plan import NOT_FROM_BAZARR, Plan, Rules, _removal, decide, mergeable
from core.probe import PROBE_VERSION, MediaInfo, ProbeError, find_sidecars, probe, sidecar_name
from core.remux import RemuxError, remux, supported, temp_path
from core.store import Store
from services.connections import Arr, Bazarr, Jellyfin, ServiceError
from utils.languages import LANGUAGES, name, normalise
from utils.logger import logger
from utils.paths import inside

# Statuses Dubarr adds on top of the plan's
SEEDING = "seeding"   # would change, but it's hardlinked and probably still seeding
TOO_SMALL = "too_small"  # would change, but would save less than the minimum set
ERROR = "error"       # couldn't be read or changed
NOT_BAZARR = "not_bazarr"  # only subtitle files Bazarr didn't download to remove
AUTO_TAGGED = "auto_tagged"  # tagged from a guess: tracks wait until you've checked it

APPLY_EVERY_SECONDS = 600
DETECT_EVERY_SECONDS = 600
DETECT_PAUSE_SECONDS = 5     # between files in background detection, so Whisper and the disks get a rest
NOTIFY_QUIET_SECONDS = 60    # the other apps are told once changes have stopped for this long
NOTIFY_MOST_SECONDS = 300    # or after this, while changes keep coming


class Busy(Exception):
    pass


def added_tag(f):
    """The language tag a subtitle file gets as it goes into the film. One read from its text is a guess, so the
    track stays untagged, with the guess as a suggestion, unless Tag automatically is at or below how sure it is."""
    if not f.get("guessed"):
        return f["language"]
    if config.auto_tag_confidence and f.get("guess_confidence", 0) * 100 >= config.auto_tag_confidence:
        return f["language"]
    return "und"


def estimated_saving(plan):
    """Bytes a change would save, or None if it can't be told. Subtitle tracks without a recorded size are
    tiny next to the film, but an audio track of unknown size could be gigabytes."""
    if any(r["type"] == "audio" and not r["size"] for r in plan.remove):
        return None
    return plan.saved_bytes


def disk_of(path):
    """Which disk a file is on. Unraid's user shares say so directly; elsewhere the device id stands in.
    Films on different disks can be rewritten side by side without slowing each other down."""
    try:
        return os.getxattr(path, "system.LOCATION").decode(errors="replace").strip("\x00") or None
    except (OSError, AttributeError):
        pass
    try:
        return f"device {os.stat(path).st_dev}"
    except OSError:
        return None


def run_by_disk(rows, work, keep_going, workers):
    """Call work(row) for every row, up to `workers` at a time, never two on the same disk at once.
    Stops handing out new rows as soon as keep_going() says so."""
    if workers <= 1:
        for row in rows:
            if not keep_going():
                return
            work(row)
        return

    pending = [(disk_of(r["path"]), r) for r in rows]
    busy = set()
    condition = threading.Condition()
    stopped = False

    def next_row():
        nonlocal stopped
        with condition:
            while True:
                if stopped or not pending:
                    return None
                for i, (disk, row) in enumerate(pending):
                    if disk not in busy:
                        if not keep_going():
                            stopped = True
                            return None
                        pending.pop(i)
                        busy.add(disk)
                        return disk, row
                condition.wait()

    def worker():
        while True:
            picked = next_row()
            if not picked:
                with condition:
                    condition.notify_all()
                return
            disk, row = picked
            try:
                work(row)
            except Exception as e:
                logger.exception(f"Changing {row['path']} failed: {e}")
            finally:
                with condition:
                    busy.discard(disk)
                    condition.notify_all()

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(min(workers, len(rows)))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


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
        self._stopped_at = 0.0  # when Stop was last pressed: automatic tagging queued before then is dropped
        self._cancelled = 0     # goes up with each Stop all: every job waiting its turn before then is dropped
        self._waiting = []      # the jobs waiting their turn, by name, for the web UI
        self._saving = {}       # {path: how saving your tags is going}, shown in the file's row
        self._writing = {}      # {copy being written: (its expected size, the original's size)}, for progress
        self._carry = {}        # {path: {new track index: guess}} kept across a rewrite, saved once it's read again
        self._not_replaced = {}  # path: why Radarr or Sonarr couldn't replace it, shown on the No audio list
        self._auto_failed = set()  # (path, size, mtime) automatic tagging couldn't do: not tried again until it changes
        self.activity = None          # what's happening now, for the web UI
        self.last_scan = 0.0
        self.last_apply = 0.0
        self.last_purge = 0.0
        self.last_subtitles = 0.0
        self.last_detect = 0.0
        self.last_replace = 0.0
        self._detecting = threading.Lock()  # background detection only reads files, so it runs beside jobs
        self._yield = 0         # jobs you started waiting for automatic tagging to finish its file: it holds off
        self._notices = []      # files to tell Radarr, Sonarr, Bazarr and Jellyfin about, outside the job
        self._notices_ready = threading.Condition()
        self._notifier = None
        self.notify_quiet = NOTIFY_QUIET_SECONDS
        self.detect_pause = DETECT_PAUSE_SECONDS
        self._services = {}
        self._unreachable = {}  # Radarr, Sonarr or Jellyfin: when to try asking them again
        self._alerts = []       # problems with the other apps, newest last, shown as pop-ups in the web UI
        self._alert_id = 0

    # Jobs

    @contextmanager
    def job(self, task, wait=False, automatic=False):
        cancelled = self._cancelled
        if wait:
            self._waiting.append(task)
        try:
            got = self._job.acquire(blocking=wait)
        finally:
            if wait:
                self._waiting.remove(task)
        if not got:
            raise Busy(f"Dubarr is busy: {self.activity['task'] if self.activity else 'working'}")
        if self._cancelled != cancelled:
            self._job.release()
            raise Busy(f"{task} was cancelled")
        self._stop.clear()
        # done/total count files; bytes_done/bytes_total, when set, give a better time estimate for changes
        self.activity = {"task": task, "detail": "", "done": 0, "total": 0, "bytes_done": 0, "bytes_total": 0,
                         "started": time.time(), "automatic": automatic}
        try:
            yield
        finally:
            self.activity = None
            self._job.release()

    def busy(self):
        return self._job.locked()

    def progress(self):
        """What's happening now, with an estimate of the time left. None when idle."""
        a = self.activity
        if not a:
            return None
        a = dict(a)
        a["bytes_done"] = min(a["bytes_total"], a["bytes_done"] + self._written()) if a["bytes_total"] else 0
        elapsed = time.time() - a["started"]
        eta = None
        if a["bytes_total"] and a["bytes_done"]:
            eta = elapsed / a["bytes_done"] * (a["bytes_total"] - a["bytes_done"])
        elif a["total"] and a["done"]:
            eta = elapsed / a["done"] * (a["total"] - a["done"])
        a["waiting"] = self.waiting()
        a["elapsed_seconds"] = round(elapsed)
        a["eta_seconds"] = round(eta) if eta is not None else None
        return a

    def _remux(self, info, plan):
        """remux(), with the copy's growth counted in the job's progress, so a big film doesn't sit at 0."""
        target = temp_path(info.path)
        a = self.activity
        if a is not None and not a["bytes_total"]:
            a["bytes_total"] = info.size  # one film by itself: its own size is the whole job
        self._writing[target] = (max(1, info.size - (plan.saved_bytes or 0)), info.size)
        try:
            new = remux(info, plan)
        finally:
            self._writing.pop(target, None)
        self._carry_guesses(info, plan, new)
        return new

    def _carry_guesses(self, info, plan, new):
        """Suggestions for tracks that stay keep going in the new copy, so a rewrite doesn't lose them.
        Tracks keep their order within each kind (audio, subtitles), whatever their numbers become."""
        row = self.store.file(info.path)
        if not row:
            return
        old = self.guesses(row)
        if not old:
            return
        kept = set(plan.keep)
        carried = {}
        for kind in ("audio", "subtitle"):
            before = [t.index for t in info.of(kind) if t.index in kept]
            after = [t.index for t in new.of(kind)]
            for was, now in zip(before, after):
                if was in old:
                    carried[now] = old[was]
        if carried:
            self._carry[info.path] = carried

    def _written(self):
        """How much of the originals the copies being written have got through, going by their size so far."""
        total = 0
        for target, (expected, original) in list(self._writing.items()):
            try:
                total += original * min(1.0, os.path.getsize(target) / expected)
            except OSError:
                pass
        return int(total)

    def stop(self):
        """Stop the job after the current file, and drop automatic tagging waiting behind it, so it doesn't
        start up as soon as the job ends. Tags you saved yourself are still written."""
        self._stopped_at = time.time()
        self._stop.set()

    def stop_all(self):
        """Stop the job after the current file and cancel every job waiting its turn, tags you saved included."""
        self._cancelled += 1
        self.stop()

    def waiting(self):
        """How many of each job are waiting their turn, e.g. {"Tagging": 3}."""
        counts = {}
        for task in list(self._waiting):
            counts[task] = counts.get(task, 0) + 1
        return counts

    def stopped_since(self, started):
        return self._stopped_at >= started

    def background(self, target, *args, wait=False, **kwargs):
        """Run a job in its own thread. Raises Busy straight away, unless wait is set. Automatic tagging doesn't
        count as busy: it finishes the file it's on, then holds off while your job runs."""
        yielding = False
        if not wait and self.busy():
            if not (self.activity and self.activity.get("automatic")):
                raise Busy(f"Dubarr is busy: {self.activity['task'] if self.activity else 'working'}")
            wait = yielding = True
            self._yield += 1

        def run():
            try:
                target(*args, wait=wait, **kwargs)
            except Busy:
                pass
            except Exception as e:
                logger.exception(f"{target.__name__} failed: {e}")
            finally:
                if yielding:
                    self._yield -= 1

        threading.Thread(target=run, daemon=True).start()

    # Libraries

    def library_for(self, path):
        matches = [lib for lib in config.library_list if inside(path, lib["path"])]
        return max(matches, key=lambda lib: len(lib["path"])) if matches else None

    def rules(self, library, path=None):
        return Rules(library["audio"], library["subtitles"], config.strip_audio, config.strip_subtitles,
                     config.strip_commentary, config.one_audio_per_language, config.one_subtitle_per_language,
                     config.assume_unlabelled_is_full, config.strip_subtitle_files, config.prefer_text_files,
                     config.keep_original_audio, self.original_language(path) if path else None,
                     self.bazarr_files(path) if path else None, config.mux_subtitle_files)

    def bazarr_files(self, path):
        """The subtitle files Bazarr downloaded, when Dubarr is to keep only those and Bazarr looks after this film
        or series. None otherwise, including when Bazarr's history is empty: then there's no telling which are its."""
        if config.bazarr_other_subtitles == config.OTHER_SUBTITLES_KEEP:
            return None
        bazarr = self._service("bazarr")
        if not bazarr or time.time() < self._unreachable.get("bazarr", 0):
            return None
        try:
            if not bazarr.locate(path):
                return None
            return bazarr.downloaded_subtitles() or None
        except ServiceError as e:
            logger.warning(f"Couldn't ask Bazarr which subtitles it downloaded: {e}. Trying again in 10 minutes")
            self._unreachable["bazarr"] = time.time() + 600
            self.alert("Bazarr", f"Couldn't ask Bazarr which subtitles it downloaded: {e}")
            return None

    def original_language(self, path):
        """The original language of a file's film or series, from Radarr or Sonarr. None if neither knows,
        or the setting is off. If one can't be reached it isn't asked again for 10 minutes, so a scan
        doesn't wait on it for every file."""
        if not config.keep_original_audio:
            return None
        for kind in ("radarr", "sonarr"):
            arr = self._service(kind)
            if not arr or time.time() < self._unreachable.get(kind, 0):
                continue
            try:
                code = arr.original_language(path)
            except ServiceError as e:
                logger.warning(f"Couldn't ask {arr.name} for original languages: {e}. Trying again in 10 minutes")
                self._unreachable[kind] = time.time() + 600
                self.alert(arr.name, f"Couldn't ask {arr.name} for original languages: {e}")
                continue
            if code:
                return code
        return None

    def tag_original_only(self, path):
        """Tag a film or series that only has its original-language audio in Radarr or Sonarr and Jellyfin,
        so it can be filtered by there."""
        label = config.original_only_tag
        if not label or not config.keep_original_audio:
            return
        for kind in ("radarr", "sonarr", "jellyfin"):
            service = self._service(kind)
            if not service or time.time() < self._unreachable.get(kind, 0):
                continue
            try:
                title = service.add_tag(path, label)
            except ServiceError as e:
                logger.warning(f"Couldn't tag {os.path.basename(path)} in {service.name}: {e}. Trying again in 10 minutes")
                self._unreachable[kind] = time.time() + 600
                self.alert(service.name, f"Couldn't tag {os.path.basename(path)} in {service.name}: {e}")
                continue
            if title:
                logger.info(f"Tagged {title} as {label} in {service.name}: only its original language audio")

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

    def evaluate(self, library, path, reread=False, allow_small=False):
        """Read a file's tracks (or reuse what's known) and decide what to do.
        Returns (status, message, plan, info, stat), with plan and info None if the file couldn't be read."""
        st = os.stat(path)
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
                return ERROR, f"Couldn't read the file: {e}", None, None, st
        # Subtitle files come and go without the video changing, so they're always looked at afresh
        info.external = find_sidecars(path)
        # A file whose name doesn't say its language is read, so you can see what it is before putting it in
        for f in info.external:
            if f["text"] and f["language"] == "und":
                found = detect.file_language(f["path"])
                if found:
                    f["guess"], f["guess_confidence"] = found

        rules = self.rules(library, path)
        plan = decide(info, rules)
        for f in plan.add_files:
            f["tag"] = added_tag(f)
        if not plan.add_files:
            plan.can_add = [f["path"] for f in mergeable(info, rules)]
        status, message = plan.status, plan.message
        if plan.rewrites:
            saving = estimated_saving(plan)
            minimum = config.min_saving_mb * 1024 * 1024
            waiting = None
            # Putting subtitle files into the film is something you turned on, so it isn't held back for its size
            if minimum and saving is not None and saving < minimum and not allow_small and not plan.add_files:
                status, waiting = TOO_SMALL, f"Saves less than the {size(minimum)} set, so it's left for now"
            elif st.st_nlink > 1 and not config.process_hardlinked:
                status, waiting = SEEDING, "Waiting: the file is hardlinked, probably still seeding"
            if waiting:
                message = f"{plan.message}. {waiting}"
                # The video has to wait, but subtitle files beside it can go now: deleting one costs nothing
                files_only = decide(info, rules, tracks=False)
                if files_only.remove_files:
                    status, plan = plans.CHANGE, files_only
                    message = f"{files_only.message}. The rest waits: {waiting[0].lower()}{waiting[1:]}"
        # A tag set from a guess could be wrong, and a wrong tag could remove the wrong track: wait for a check
        held = self.store.held(path, config.trust_confidence) if plan.rewrites else None
        if held:
            status = AUTO_TAGGED
            message = f"{held['message']}. Press Looks right to let Dubarr change it: {plan.message[0].lower()}{plan.message[1:]}"
        # Only subtitle files Bazarr didn't download: their own list, removed by hand or by their own setting
        if (status == plans.CHANGE and not plan.rewrites and plan.remove_files
                and all(f["reason"] == NOT_FROM_BAZARR for f in plan.remove_files)):
            status = NOT_BAZARR
        return status, message, plan, info, st

    def check_file(self, library, path, reread=False):
        """Decide what to do with a file and record it. Returns its status."""
        try:
            status, message, plan, info, st = self.evaluate(library, path, reread)
        except FileNotFoundError:
            # Deleted or renamed since the last scan: drop it from the lists
            if self.store.forget(path):
                logger.info(f"{path} no longer exists, so it was taken off the lists")
            return None
        except OSError as e:
            logger.warning(f"Can't read {path}: {e.strerror or e}")
            return None
        self.store.save_file(path, library["path"], st.st_size, st.st_mtime, st.st_nlink,
                             info.to_dict() if info else None, status, message,
                             plan.to_dict() if plan else None, plan.saved_bytes if plan else 0)
        carried = self._carry.pop(path, None)
        if carried and info:
            for index, g in carried.items():
                if any(t.index == index and t.language == "und" for t in info.tracks):
                    self.store.save_guess(path, index, st.st_size, st.st_mtime, g["language"], g["confidence"], g["by"])
        if plan and plan.original_only:
            self.tag_original_only(path)
        return status

    def alert(self, service, message):
        """A problem with another app worth a pop-up. The same one isn't repeated within 10 minutes."""
        now = time.time()
        if any(a["message"] == message and now - a["at"] < 600 for a in self._alerts):
            return
        self._alert_id += 1
        self._alerts = (self._alerts + [{"id": self._alert_id, "at": now, "service": service, "message": message}])[-20:]

    def alerts(self, since=0):
        return [a for a in self._alerts if a["id"] > since]

    def check_mappings(self):
        """Pop up the folders Radarr and Sonarr keep films in that Dubarr can't see, the usual sign of a missing
        Folder mapping (paths set up the TRaSH guides way differ between containers)."""
        for kind in ("radarr", "sonarr"):
            arr = self._service(kind)
            if not arr or time.time() < self._unreachable.get(kind, 0):
                continue
            try:
                missing = arr.unmapped_folders()
            except ServiceError as e:
                self._down(kind, e)
                self.alert(arr.name, str(e) if arr.name in str(e) else f"Couldn't reach {arr.name}: {e}")
                continue
            for remote, local in missing:
                hint = ""
                same = [lib["path"] for lib in config.library_list
                        if os.path.basename(lib["path"].rstrip("/")) == os.path.basename(remote.rstrip("/"))]
                if same:
                    hint = f", e.g. {remote}={same[0]}"
                self.alert(arr.name, f"{arr.name} keeps {'films' if kind == 'radarr' else 'series'} in {remote}, which "
                           f"Dubarr can't see{f' (looked in {local})' if local != remote else ''}. If it's one of your "
                           f"libraries, add a Folder mapping in Settings > {arr.name}{hint}")

    def scan(self, trigger="manual", paths=None, wait=False):
        """Check every file in the libraries, or just the paths given."""
        with self.job("Scanning", wait=wait):
            started = time.time()
            counts = {}
            if paths is None:
                self.check_mappings()
                libraries = config.library_list
                self.store.forget_libraries([lib["path"] for lib in libraries])
                found = []
                self.activity["detail"] = "Listing files"
                for library in libraries:
                    if not os.path.isdir(library["path"]):
                        logger.warning(f"Library folder {library['path']} doesn't exist. Is it mapped in the container?")
                        continue
                    found.append((library, list(self._walk(library))))
                self.activity["total"] = sum(len(paths) for _, paths in found)
                for library, library_paths in found:
                    for path in library_paths:
                        if self._stop.is_set():
                            break
                        self.activity["detail"] = path
                        status = self.check_file(library, path)
                        counts[status] = counts.get(status, 0) + 1
                        self.activity["done"] += 1
                    if not self._stop.is_set():
                        self.store.forget_missing(library["path"], set(library_paths))
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

            # Newly found untagged tracks get their suggestions at the next minute's check, not in 10 minutes
            if counts.get(plans.UNKNOWN_AUDIO) or counts.get(plans.UNKNOWN_SUBTITLES):
                self.last_detect = 0.0
            found = ", ".join(f"{n} {k.replace('_', ' ')}" for k, n in sorted(counts.items(), key=str) if k)
            logger.info(f"Scan ({trigger}) checked {sum(counts.values())} file(s) in {time.time() - started:.0f}s"
                        + (f": {found}" if found else ""))

    def imported(self, paths, wait=True):
        """Radarr or Sonarr just imported these. Check them now, and change them if allowed."""
        self.scan("import", paths, wait=wait)
        self.apply("import", paths, wait=wait)

    # Changing files

    def apply(self, trigger="manual", paths=None, force=False, wait=False, library=None, status=None):
        """Change files waiting to be changed.

        Automatic passes respect report-only mode, the time window and the batch size.
        force is for the Apply buttons, where you've chosen the files yourself. library limits it to one library,
        and status NOT_BAZARR to that list instead of the files waiting to be changed.
        """
        if not force and (config.report_only or not in_window(config.active_from, config.active_until)):
            return
        with self.job("Changing files", wait=wait):
            self.last_apply = time.time()
            if status == NOT_BAZARR:
                statuses = [NOT_BAZARR]
            else:
                statuses = [plans.CHANGE] + ([SEEDING] if config.process_hardlinked else [])
                if paths and force:
                    statuses += [TOO_SMALL, NOT_BAZARR]  # chosen by hand, so worth it
            if paths:
                rows = [r for r in (self.store.file(p) for p in paths) if r and r["status"] in statuses and not r["ignored"]]
            else:
                _, rows = self.store.files(statuses=statuses, ignored=False, limit=1_000_000, library=library)
                if not force:
                    rows = rows[:config.max_changes_per_pass]
            if not rows:
                return

            self.activity["total"] = len(rows)
            # Rewriting takes time in proportion to size, so estimate from bytes
            self.activity["bytes_total"] = sum(r["size"] for r in rows)
            run_id = self.store.start_run(trigger)
            totals = {"changed": 0, "failed": 0, "saved": 0}
            lock = threading.Lock()
            working = []

            def work(row):
                with lock:
                    working.append(os.path.basename(row["path"]))
                    self.activity["detail"] = ", ".join(working)
                try:
                    result = self.change_file(run_id, row["path"], allow_small=bool(paths and force), by_hand=force)
                finally:
                    with lock:
                        working.remove(os.path.basename(row["path"]))
                        self.activity["detail"] = ", ".join(working)
                        self.activity["done"] += 1
                        self.activity["bytes_done"] += row["size"]
                with lock:
                    if result is False:
                        totals["failed"] += 1
                    elif result is not None:
                        totals["changed"] += 1
                        totals["saved"] += result

            def keep_going():
                if self._stop.is_set():
                    logger.info("Stopped before finishing the batch")
                    return False
                if not force and not in_window(config.active_from, config.active_until):
                    logger.info("Outside the hours set for changing files. The rest will wait")
                    return False
                return True

            run_by_disk(rows, work, keep_going, max(1, config.workers))
            changed, failed, saved = totals["changed"], totals["failed"], totals["saved"]
            self.store.finish_run(run_id, changed, failed, saved)
            if changed or failed:
                logger.log("ACTION", f"Changed {changed} file(s), saving {size(saved)}"
                           + (f". {failed} couldn't be changed, see the Dashboard" if failed else ""))
            self.purge()

    def change_file(self, run_id, path, allow_small=False, by_hand=False, subtitles_only=False):
        """Strip one file. Returns bytes saved, False on failure, or None if there was nothing to do.

        by_hand is for files you chose to apply. subtitles_only removes just the subtitle files Bazarr didn't
        download, leaving the rest of the change for later."""
        library = self.library_for(path)
        if not library:
            return None
        # Read the file again right before changing it, in case it changed since the scan
        try:
            status, _, plan, info, st = self.evaluate(library, path, reread=True, allow_small=allow_small)
        except OSError:
            return None
        if subtitles_only:
            others = [f for f in (plan.remove_files if plan else []) if f["reason"] == NOT_FROM_BAZARR]
            if not others:
                self.check_file(library, path)
                return None
            plan = Plan(plans.CHANGE, "", remove_files=others)
        else:
            allowed = (status == plans.CHANGE or (status == SEEDING and config.process_hardlinked)
                       or (status == NOT_BAZARR and by_hand))
            if not allowed:
                self.check_file(library, path)
                return None
            # Subtitles Bazarr didn't download only go by themselves when that's chosen in Settings
            if not by_hand and config.bazarr_other_subtitles != config.OTHER_SUBTITLES_AUTO:
                plan.remove_files = [f for f in plan.remove_files if f["reason"] != NOT_FROM_BAZARR]
                if not plan.rewrites and not plan.remove_files:
                    self.check_file(library, path)
                    return None

        return self._carry_out(run_id, library, path, info, plan)

    def _carry_out(self, run_id, library, path, info, plan):
        """change_file()'s work once the plan is settled."""
        root = swap.recycle_root(library["path"], config.recycle_folder)
        saved = 0
        try:
            if plan.rewrites:
                new = self._remux(info, plan)
                history_id = swap.swap(self.store, run_id, path, info.size, new.size, plan.message, plan.remove,
                                       root, config.recycle_days)
                saved += info.size - new.size
                logger.log("ACTION", f"{plan.message} from {path} (saved {size(info.size - new.size)}, change #{history_id})")
            for f in plan.add_files:
                message = f"Put the {name(f['language'])} subtitle file into the film"
                if f.get("guessed"):
                    sure = round(f.get("guess_confidence", 0) * 100)
                    message += (f", tagged automatically (Dubarr {sure}% sure)" if f["tag"] != "und"
                                else f", untagged, as its language is a guess (Dubarr {sure}% sure)")
                history_id = swap.recycle_file(self.store, run_id, f["path"], path, message, root, config.recycle_days)
                logger.log("ACTION", f"Put {f['path']} into the film and moved it to the recycle folder "
                           f"(change #{history_id})")
            for f in plan.remove_files:
                if f["reason"] == NOT_FROM_BAZARR:
                    message, why = "Removed a subtitle file beside the film that Bazarr didn't download", \
                        "Bazarr didn't download it"
                else:
                    message = f"Removed the {name(f['language'])} subtitle file beside the film"
                    why = f"{name(f['language'])} subtitles"
                history_id = swap.recycle_file(self.store, run_id, f["path"], path, message, root, config.recycle_days)
                saved += f["size"]
                logger.log("ACTION", f"Removed {f['path']} ({why}, change #{history_id})")
        except (RemuxError, swap.SwapError, OSError) as e:
            self._carry.pop(path, None)
            reason = getattr(e, "strerror", None) or str(e)
            logger.error(f"Couldn't change {path}: {reason}. The original is untouched")
            st = os.stat(path)
            self.store.save_file(path, library["path"], st.st_size, st.st_mtime, st.st_nlink,
                                 info.to_dict(), ERROR, f"Couldn't change it: {reason}", plan.to_dict(), plan.saved_bytes)
            return False

        self.check_file(library, path, reread=True)
        self._suggest_added(path, plan)
        self.notify(path)
        return saved

    def _suggest_added(self, path, plan):
        """Subtitle files put in untagged come last in the film: their guesses become its suggestions."""
        guessed = [f for f in plan.add_files if f.get("tag") == "und" and f.get("guessed")]
        row = self.store.file(path) if guessed else None
        if not row or not row["info"]:
            return
        subtitles = [t for t in row["info"]["tracks"] if t["type"] == "subtitle"][-len(plan.add_files):]
        for t, f in zip(subtitles, plan.add_files):
            if f.get("tag") == "und" and f.get("guessed") and t["language"] == "und":
                self.store.save_guess(path, t["index"], row["size"], row["mtime"], f["language"],
                                      f.get("guess_confidence", 0), "Dubarr")

    def add_subtitle_files(self, path, files=None, wait=False):
        """Put the subtitle files beside a film into it, whichever list it's on. Returns what was done.
        files, a list of their paths, puts in just those, whatever their language and whatever is inside already."""
        with self.job("Putting subtitle files in", wait=wait):
            self.activity["detail"] = path
            library = self.library_for(path)
            if not library:
                raise tagging.TagError("This file isn't in a library set up in Dubarr")
            status, _, plan, info, st = self.evaluate(library, path, reread=True)
            if info is None:
                raise tagging.TagError("Couldn't read the file")
            if st.st_nlink > 1 and not config.process_hardlinked:
                raise tagging.TagError("The file is hardlinked, probably still seeding. Putting subtitles in would "
                                       "change the torrent's data too. Try again once seeding stops")
            if files:
                files = [{**f, "language": f["language"] if f["language"] != "und" else f.get("guess", "und"),
                          "guessed": f["language"] == "und" and bool(f.get("guess"))}
                         for f in info.external if f["path"] in files and f["text"]]
            else:
                files = mergeable(info, self.rules(library, path))
            if not files:
                self.check_file(library, path)
                return "No subtitle files to put in"
            for f in files:
                f["tag"] = added_tag(f)
            n = len(files)
            add = Plan(plans.CHANGE, f"Put {n} subtitle file{'s' if n > 1 else ''} into the film",
                       keep=[t.index for t in info.tracks], add_files=files)
            run_id = self.store.start_run("manual")
            done = self._carry_out(run_id, library, path, info, add)
            self.store.finish_run(run_id, 0 if done is False else 1, 1 if done is False else 0, 0)
            return "Couldn't put them in, see the file's message" if done is False else add.message

    def replace_no_audio(self, trigger="manual", paths=None, library=None, wait=False):
        """Have Radarr or Sonarr delete files with no audio and search for another copy. Each file is read again
        first, so only one that really has no audio goes."""
        with self.job("Replacing files with no audio", wait=wait):
            self.last_replace = time.time()
            if paths:
                rows = [r for r in (self.store.file(p) for p in paths) if r and r["status"] == plans.NO_AUDIO]
            else:
                _, rows = self.store.files(statuses=[plans.NO_AUDIO], ignored=False, limit=1_000_000, library=library)
            rows = [r for r in rows if not r["ignored"]]
            arrs = [a for a in (self._service(k) for k in ("radarr", "sonarr")) if a]
            if not rows:
                return
            if not arrs:
                logger.warning("Files with no audio are replaced through Radarr or Sonarr, and neither is set up")
                return
            self.activity["total"] = len(rows)
            replaced = 0
            for row in rows:
                if self._stop.is_set():
                    break
                path = row["path"]
                self.activity["detail"] = path
                self.activity["done"] += 1
                if self._replace_one(path, arrs):
                    replaced += 1
            if replaced:
                logger.log("ACTION", f"Replaced {replaced} file(s) with no audio through Radarr or Sonarr")
            if len(rows) > replaced:
                self.alert("Radarr/Sonarr", f"{len(rows) - replaced} file(s) with no audio couldn't be replaced. The No "
                           "audio list says why for each. If Radarr or Sonarr don't know them, check the Folder mapping")
                logger.warning(f"{len(rows) - replaced} file(s) with no audio couldn't be replaced. The No audio list "
                               "says why for each one")

    def _replace_one(self, path, arrs):
        library = self.library_for(path)
        if not library or self.check_file(library, path, reread=True) != plans.NO_AUDIO:
            return False
        file_size = os.path.getsize(path)
        for arr in arrs:
            try:
                result = arr.replace(path)
            except ServiceError as e:
                logger.error(f"Couldn't have {arr.name} replace {path}: {e}")
                self._not_replaced[path] = f"{arr.name} couldn't replace it: {e}"
                return False
            if result:
                self._not_replaced.pop(path, None)
                title, blocklisted = result
                message = (f"Deleted by {arr.name}, as it has no audio. {arr.name} is searching for another copy"
                           + (" and won't pick the same release again" if blocklisted else ""))
                self.store.add_replaced_history(path, file_size, message)
                self.store.forget(path)
                logger.log("ACTION", f"{arr.name} deleted {title} ({path}), which has no audio, and is searching "
                           "for another copy" + (". The release it came from is blocklisted" if blocklisted else ""))
                return True
        # Not theirs, but they have the film or episode as missing: Dubarr deletes it, and they search
        for arr in arrs:
            try:
                missing = arr.missing(path)
            except ServiceError as e:
                logger.warning(f"Couldn't ask {arr.name} about {path}: {e}")
                continue
            if not missing:
                continue
            title, command = missing
            try:
                history_id = swap.recycle_file(
                    self.store, None, path, path, f"Deleted, as it has no audio. {arr.name} has {title} as missing "
                    "and is searching for a copy", swap.recycle_root(library["path"], config.recycle_folder),
                    config.recycle_days)
            except (OSError, swap.SwapError) as e:
                self._not_replaced[path] = f"Couldn't delete it: {getattr(e, 'strerror', None) or e}"
                return False
            self.store.forget(path)
            self._not_replaced.pop(path, None)
            logger.log("ACTION", f"Deleted {path}, which has no audio and isn't {arr.name}'s file for {title} "
                       f"(change #{history_id}). {arr.name} has it as missing, so it's searching for a copy")
            try:
                arr.search(command)
            except ServiceError as e:
                logger.warning(f"Couldn't ask {arr.name} to search for {title}: {e}. Search for it there")
            return True
        reasons = []
        for arr in arrs:
            try:
                reasons.append(arr.why_not(path))
            except ServiceError:
                pass
        reason = next((r for r in reasons if r), None) or (
            f"{' and '.join(a.name for a in arrs)} {'don' if len(arrs) > 1 else 'doesn'}'t have it. If "
            f"{'they' if len(arrs) > 1 else 'it'} should, check the Folder mapping in Settings")
        self._not_replaced[path] = reason
        logger.warning(f"{path} has no audio, but wasn't replaced: {reason}")
        return False

    def not_replaced(self, path):
        return self._not_replaced.get(path)

    def not_replaced_paths(self, library=None):
        return [p for p in list(self._not_replaced) if not library or inside(p, library)]

    def delete_no_audio(self, paths=None, library=None, wait=False):
        """Delete files with no audio that Radarr and Sonarr can't replace, into the recycle folder so they can be
        restored from History. Each is read again first, so only one that really has no audio goes."""
        with self.job("Deleting files with no audio", wait=wait):
            # Without paths: every file Radarr and Sonarr couldn't replace
            paths = paths or self.not_replaced_paths(library)
            rows = [r for r in (self.store.file(p) for p in paths)
                    if r and r["status"] == plans.NO_AUDIO and not r["ignored"]]
            if not rows:
                return
            self.activity["total"] = len(rows)
            run_id = self.store.start_run("manual")
            deleted = saved = 0
            for row in rows:
                if self._stop.is_set():
                    break
                path = row["path"]
                self.activity["detail"] = path
                self.activity["done"] += 1
                library = self.library_for(path)
                if not library or self.check_file(library, path, reread=True) != plans.NO_AUDIO:
                    continue
                try:
                    size_before = os.path.getsize(path)
                    history_id = swap.recycle_file(self.store, run_id, path, path, "Deleted: no audio",
                                                   swap.recycle_root(library["path"], config.recycle_folder),
                                                   config.recycle_days)
                except (OSError, swap.SwapError) as e:
                    logger.error(f"Couldn't delete {path}: {getattr(e, 'strerror', None) or e}")
                    continue
                self.store.forget(path)
                self._not_replaced.pop(path, None)
                deleted += 1
                saved += size_before
                logger.log("ACTION", f"Deleted {path}, which has no audio (change #{history_id})")
                try:
                    self.notify(path)
                except Exception as e:
                    logger.warning(f"Couldn't tell the other apps {path} was deleted: {e}")
            self.store.finish_run(run_id, deleted, 0, saved)

    def remove_other_subtitles(self, trigger="scheduled", wait=False):
        """Remove the subtitle files Bazarr didn't download, from every file that has some, without changing the
        rest of a file that waits to be changed. Report only doesn't stop it: you turned it on separately."""
        with self.job("Removing subtitles Bazarr didn't download", wait=wait):
            self.last_subtitles = time.time()
            _, rows = self.store.files(statuses=[plans.CHANGE, SEEDING, TOO_SMALL, NOT_BAZARR], ignored=False,
                                       limit=1_000_000)
            rows = [r for r in rows if any(f.get("reason") == NOT_FROM_BAZARR
                                           for f in (r["plan"] or {}).get("remove_files", []))]
            if not rows:
                return
            self.activity["total"] = len(rows)
            run_id = self.store.start_run(trigger)
            changed = failed = saved = 0
            for row in rows:
                if self._stop.is_set():
                    break
                self.activity["detail"] = row["path"]
                result = self.change_file(run_id, row["path"], subtitles_only=True)
                if result is False:
                    failed += 1
                elif result is not None:
                    changed += 1
                    saved += result
                self.activity["done"] += 1
            self.store.finish_run(run_id, changed, failed, saved)
            if changed or failed:
                logger.log("ACTION", f"Removed subtitles Bazarr didn't download from {changed} film(s) or episode(s)"
                           + (f". {failed} couldn't be changed, see the Dashboard" if failed else ""))

    # Removing tracks by hand

    def plan_by_hand(self, path, indexes, files):
        """A change made of the tracks and subtitle files you ticked. Returns (library, info, plan).
        Raises ValueError (or TagError) if the choice can't be made."""
        library = self.library_for(path)
        if not library:
            raise ValueError("This file isn't in a library set up in Dubarr")
        info = self.known_file(path)
        by_index = {t.index: t for t in info.tracks}
        tracks = []
        for index in dict.fromkeys(int(i) for i in indexes):
            track = by_index.get(index)
            if not track or track.type not in ("audio", "subtitle"):
                raise ValueError(f"Track {index} isn't an audio or subtitle track")
            tracks.append(track)
        beside = {f["path"]: f for f in find_sidecars(path)}
        chosen = []
        for file in dict.fromkeys(files):
            if file not in beside:
                raise ValueError(f"{os.path.basename(file)} isn't a subtitle file beside this film any more")
            chosen.append({**beside[file], "reason": "Removed by hand"})
        if not tracks and not chosen:
            raise ValueError("Tick at least one track or subtitle file to remove")

        removed = {t.index for t in tracks}
        audio_left = [t for t in info.of("audio") if t.index not in removed]
        if info.of("audio") and not audio_left:
            raise ValueError("At least one audio track has to stay")
        if tracks and os.stat(path).st_nlink > 1 and not config.process_hardlinked:
            raise ValueError("The file is hardlinked, probably still seeding. Removing tracks would store the film "
                             "twice. Try again once seeding stops, or remove only subtitle files")
        default_audio = None
        if tracks and not any(t.default for t in audio_left):
            default_audio = audio_left[0].index

        parts = [f"{name(t.language)} {'audio' if t.type == 'audio' else 'subtitles'}" for t in tracks]
        if chosen:
            parts.append(f"{len(chosen)} subtitle file{'s' if len(chosen) > 1 else ''}")
        message = "Removed by hand: " + (", ".join(parts[:-1]) + " and " + parts[-1] if len(parts) > 1 else parts[0])
        plan = Plan(plans.CHANGE, message, remove=[_removal(t, "Removed by hand") for t in tracks],
                    keep=[t.index for t in info.tracks if t.index not in removed], default_audio=default_audio,
                    saved_bytes=sum(t.size for t in tracks), remove_files=chosen)
        return library, info, plan

    def remove_by_hand(self, path, indexes, files, wait=False):
        """Remove the tracks and subtitle files you ticked from one film, the same safe way as any change:
        a checked copy, with the original in the recycle folder."""
        with self.job("Removing tracks", wait=wait):
            self.activity["detail"] = path
            library = self.library_for(path)
            if library:
                # Read the file again, in case it changed since the Dashboard showed it
                self.check_file(library, path, reread=True)
            library, info, plan = self.plan_by_hand(path, indexes, files)
            root = swap.recycle_root(library["path"], config.recycle_folder)
            run_id = self.store.start_run("manual")
            saved = 0
            try:
                if plan.rewrites:
                    new = self._remux(info, plan)
                    history_id = swap.swap(self.store, run_id, path, info.size, new.size, plan.message, plan.remove,
                                           root, config.recycle_days)
                    saved += info.size - new.size
                    logger.log("ACTION", f"{plan.message} from {path} (saved {size(info.size - new.size)}, "
                                         f"change #{history_id})")
                for f in plan.remove_files:
                    history_id = swap.recycle_file(self.store, run_id, f["path"], path,
                                                   "Removed a subtitle file beside the film by hand", root,
                                                   config.recycle_days)
                    saved += f["size"]
                    logger.log("ACTION", f"Removed {f['path']} by hand (change #{history_id})")
            except (RemuxError, swap.SwapError, OSError) as e:
                self._carry.pop(path, None)
                logger.error(f"Couldn't remove tracks from {path}: {getattr(e, 'strerror', None) or e}. "
                             "The original is untouched")
                self.store.finish_run(run_id, 0, 1, 0)
                return
            self.store.finish_run(run_id, 1, 0, saved)
            self.check_file(library, path, reread=True)
            self.notify(path)

    def subtitle_sources(self):
        """{subtitle file: provider} for the subtitle files Bazarr downloaded, or None when Bazarr isn't connected,
        can't be reached or its history is empty."""
        bazarr = self._service("bazarr")
        if not bazarr or time.time() < self._unreachable.get("bazarr", 0):
            return None
        try:
            return bazarr.subtitle_sources() or None
        except ServiceError as e:
            logger.warning(f"Couldn't ask Bazarr which subtitles it downloaded: {e}. Trying again in 10 minutes")
            self._unreachable["bazarr"] = time.time() + 600
            self.alert("Bazarr", f"Couldn't ask Bazarr which subtitles it downloaded: {e}")
            return None

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
        entry = self.store.history_entry(history_id)
        restored = swap.restore(self.store, history_id)
        # Leave the video alone from now on, or the next pass would strip it (or its subtitle file) again
        path = entry.get("video") or restored
        self.store.set_ignored(path, True)
        library = self.library_for(path)
        if library:
            self.check_file(library, path, reread=True)
        logger.log("ACTION", f"Restored the original of {path}. Dubarr will leave this file alone until you say otherwise")
        self.notify(path)

    # Tagging

    def known_file(self, path):
        """A file Dubarr has scanned, as MediaInfo. Only these can be sampled or tagged."""
        row = self.store.file(path)
        if not row or not row["info"]:
            raise tagging.TagError("Dubarr doesn't know this file. Scan first")
        return MediaInfo.from_dict(row["info"])

    def tags_wanted(self, path, changes):
        """Check a tagging request against what's known of the file. Returns {index: code} of the tags that differ
        from what's there now. Raises TagError if it can't be done."""
        if not self.library_for(path):
            raise tagging.TagError("This file isn't in a library set up in Dubarr")
        info = self.known_file(path)
        current = {t.index: t.language for t in info.tracks}
        wanted = {}
        for index, code in changes.items():
            try:
                index = int(index)
            except (TypeError, ValueError):
                raise tagging.TagError(f"{index} isn't a track number")
            track = next((t for t in info.tracks if t.index == index), None)
            if not track or track.type not in ("audio", "subtitle"):
                raise tagging.TagError(f"Track {index} isn't an audio or subtitle track")
            if not tagging.valid_tag(code):
                raise tagging.TagError(f"{code} isn't a language Dubarr knows")
            if current.get(index) != code:
                wanted[index] = code
        return wanted

    def files_wanted(self, path, files):
        """Check languages chosen for subtitle files beside a film, e.g. {"/movies/Film/Film.srt": "eng"}. Returns
        {file: code} of the ones that change. Raises TagError if one can't be done."""
        if not self.library_for(path):
            raise tagging.TagError("This file isn't in a library set up in Dubarr")
        beside = {f["path"]: f for f in find_sidecars(path)}
        wanted = {}
        for file, code in (files or {}).items():
            f = beside.get(file)
            if not f:
                raise tagging.TagError(f"{os.path.basename(file)} isn't a subtitle file beside this film")
            if code not in LANGUAGES:
                raise tagging.TagError(f"{code} isn't a language Dubarr knows")
            if f["language"] == code:
                continue
            target = sidecar_name(path, f, code)
            if os.path.exists(target) and target != file:
                raise tagging.TagError(f"There's already a {os.path.basename(target)} beside the film")
            wanted[file] = code
        return wanted

    def _name_files(self, path, files):
        """A subtitle file can't hold a language tag, so its name carries it: Film.srt becomes Film.en.srt."""
        beside = {f["path"]: f for f in find_sidecars(path)}
        for file, code in self.files_wanted(path, files).items():
            target = sidecar_name(path, beside[file], code)
            os.rename(file, target)
            logger.log("ACTION", f"Renamed {file} to {os.path.basename(target)}, as it's {name(code)}")
            self.store.add_tag_history(path, f"Named the subtitle file {os.path.basename(target)}, as it's {name(code)}")

    def save_tags(self, path, changes, files=None):
        """Your tags for a file, written in turn so you can go on to the next one. saving() says how it's going.
        files names subtitle files beside it with their language, {file: code}."""
        self._saving[path] = "Waiting to save the tags" if self.busy() else "Saving the tags"
        self.background(self._save_tags, path, changes, files, wait=True)

    def _save_tags(self, path, changes, files=None, wait=True):
        try:
            self.tag(path, changes, wait=wait, files=files)
            self._saving.pop(path, None)
        except Busy:
            self._saving[path] = "The tags weren't saved: stopped"
        except Exception as e:
            self._saving[path] = f"The tags weren't saved: {e}"
            raise

    def saving(self, path):
        return self._saving.get(path)

    def tag(self, path, changes, wait=False, guesses=None, since=None, automatic=False, files=None):
        """Set the language of tracks, e.g. {2: "eng", 4: "fra"}. Returns what was done.

        guesses ({index: guess}) says the tags come from detection rather than from you. Unless every one is at
        least trust_confidence sure, the file then waits under Tagged automatically before tracks are removed.
        Tags you set yourself mean you've looked at the file, so any such wait ends.
        since is when the automatic tagging asking for this began: if Stop was pressed after that, it's dropped."""
        with self.job("Tagging automatically" if automatic else "Tagging", wait=wait, automatic=automatic):
            if guesses and since is not None and self.stopped_since(since):
                return "Stopped"
            if path in self._saving:
                self._saving[path] = "Saving the tags"
            if files:
                self._name_files(path, files)
                if not changes:
                    library = self.library_for(path)
                    self.check_file(library, path, reread=True)
                    self.notify(path)
                    return "Named the subtitle files"
            return self._write_tags(path, changes, guesses)

    def _write_tags(self, path, changes, guesses=None):
        """tag()'s work, inside a job that's already running."""
        self.activity["detail"] = path
        library = self.library_for(path)
        if not library:
            raise tagging.TagError("This file isn't in a library set up in Dubarr")
        self.check_file(library, path, reread=True)
        info = self.known_file(path)
        wanted = self.tags_wanted(path, changes)
        if not wanted:
            return "Nothing to change"

        st = os.stat(path)
        if st.st_nlink > 1 and not config.process_hardlinked:
            raise tagging.TagError("The file is hardlinked, probably still seeding. Changing its tags would change "
                                   "the torrent's data too. Try again once seeding stops")

        message = tagging.describe_changes(info, wanted)
        if guesses:
            used = [guesses[i] for i in wanted]
            message += " automatically (" + ", ".join(
                f"{g['by']} {round(g['confidence'] * 100)}% sure" for g in used) + ")"
            self.store.set_auto_tags(path, message, min(g["confidence"] for g in used))
        else:
            self.store.release(path)
        try:
            if path.lower().endswith((".mkv", ".mk3d")):
                tagging.tag_mkv(info, wanted)
                if guesses:
                    self.store.add_tag_history(path, message)
            elif tagging.tag_mp4(info, wanted):
                if guesses:
                    self.store.add_tag_history(path, message)
            else:
                new = tagging.retag_mp4(info, wanted)
                swap.swap(self.store, None, path, info.size, new.size, message, [],
                          swap.recycle_root(library["path"], config.recycle_folder), config.recycle_days)
        except (tagging.TagError, RemuxError, swap.SwapError, OSError) as e:
            if guesses:
                self.store.release(path)
            reason = getattr(e, "strerror", None) or str(e)
            logger.error(f"Couldn't tag {path}: {reason}. The original is untouched")
            raise e if isinstance(e, tagging.TagError) else tagging.TagError(reason)

        logger.log("ACTION", f"{message} in {path}")
        self.check_file(library, path, reread=True)
        self.notify(path)
        return message

    def looks_right(self, path, wait=False):
        """You've checked a file tagged automatically: its tracks can be changed as usual."""
        self.store.release(path)
        self.scan("manual", [path], wait=wait)

    # Detecting languages

    def guesses(self, row):
        """{track index: guess} for a file as it is now, without the ones that failed."""
        return {i: g for i, g in self.store.guesses(row["path"], row["size"], row["mtime"]).items() if g["language"]}

    def guess_file(self, row):
        """Detect the language of a file's untagged tracks that haven't been tried yet. Returns its guesses.
        Raises ServiceError if Whisper or Ollama can't be reached, so the rest can wait."""
        info = MediaInfo.from_dict(row["info"])
        tried = self.store.guesses(row["path"], row["size"], row["mtime"])
        for t in info.tracks:
            if t.language != "und" or t.index in tried or not detect.can_detect(t):
                continue
            try:
                g = detect.detect(info, t.index)
                self.store.save_guess(row["path"], t.index, row["size"], row["mtime"], g["language"],
                                      g["confidence"], g["by"])
            except (detect.DetectError, tagging.TagError) as e:
                self.store.save_guess(row["path"], t.index, row["size"], row["mtime"], error=str(e))
        return self.guesses(row)

    def detect_pass(self):
        """Work through the untagged lists in the background, keeping a suggestion for each track, and tag the
        ones sure enough if that's turned on. Only while Dubarr is idle and within the hours set for background
        work, with a pause between files: it stops as soon as a job starts and carries on at the next pass."""
        if not self._detecting.acquire(blocking=False):
            return
        try:
            started = self.last_detect = time.time()
            if not config.detect_in_background or not in_window(config.auto_tag_from, config.auto_tag_until):
                return
            _, rows = self.store.files(statuses=[plans.UNKNOWN_AUDIO, plans.UNKNOWN_SUBTITLES], ignored=False,
                                       limit=1_000_000)
            for row in rows:
                if self.stopped_since(started) or self.busy() or self._yield:
                    return  # the next pass carries on, in 10 minutes
                if not config.detect_in_background or not row["info"]:
                    continue
                if not in_window(config.auto_tag_from, config.auto_tag_until):
                    return
                try:
                    asked = len(self.store.guesses(row["path"], row["size"], row["mtime"]))
                    found = self.guess_file(row)
                    if self.detect_pause and len(self.store.guesses(row["path"], row["size"], row["mtime"])) > asked:
                        self._stop.wait(self.detect_pause)  # Whisper was asked: give it and the disks a rest
                except detect.ServiceError as e:
                    logger.warning(f"Background language detection: {e}. Trying again in 10 minutes")
                    return
                minimum = config.auto_tag_confidence
                untagged = {t["index"] for t in row["info"].get("tracks", []) if t.get("language") == "und"}
                sure = {i: g for i, g in found.items()
                        if minimum and g["confidence"] * 100 >= minimum and int(i) in untagged}
                if not sure or (row["path"], row["size"], row["mtime"]) in self._auto_failed:
                    continue
                if row["links"] > 1 and not config.process_hardlinked:
                    continue  # still seeding: tagging would change the torrent's data, so it waits like any change
                # Only while Dubarr is idle: automatic tagging never queues up behind, or in front of, your jobs
                if self.busy() or self._yield or self.stopped_since(started):
                    continue
                if self._tag_guesses(row["path"], sure, started, automatic=True) is None:
                    # Failed: don't start a job for it every 10 minutes. It's tried again once the file changes
                    self._auto_failed.add((row["path"], row["size"], row["mtime"]))
        finally:
            self._detecting.release()

    def _tag_guesses(self, path, guesses, since, automatic=False):
        try:
            return self.tag(path, {i: g["language"] for i, g in guesses.items()}, wait=not automatic,
                            guesses=guesses, since=since, automatic=automatic)
        except Busy:
            return "Busy"  # a job started first; the suggestion stays and is tried at the next pass
        except (tagging.TagError, OSError) as e:
            logger.warning(f"Couldn't tag {path} from its suggestions: {e}")
            return None

    def acceptable(self, minimum, library=None):
        """What Accept all at this level would do: (files it would tag, the best suggestion below the level as a
        percentage or None, how many files have a suggestion at all)."""
        _, rows = self.store.files(statuses=[plans.UNKNOWN_AUDIO, plans.UNKNOWN_SUBTITLES], ignored=False,
                                   limit=1_000_000, library=library)
        files, best, suggested = 0, None, 0
        for row in rows:
            found = self.guesses(row)
            if not found:
                continue
            suggested += 1
            levels = [g["confidence"] * 100 for g in found.values()]
            if any(level >= minimum for level in levels):
                files += 1
            below = [level for level in levels if level < minimum]
            if below and (best is None or max(below) > best):
                best = max(below)
        return files, (round(best) if best is not None else None), suggested

    def accept_guesses(self, minimum, library=None, wait=True):
        """Tag every untagged track whose suggestion is at least minimum percent sure (Accept all), as one job,
        so its progress shows and Stop stops it."""
        with self.job("Accepting suggestions", wait=wait):
            _, rows = self.store.files(statuses=[plans.UNKNOWN_AUDIO, plans.UNKNOWN_SUBTITLES], ignored=False,
                                       limit=1_000_000, library=library)
            todo = []
            for row in rows:
                sure = {i: g for i, g in self.guesses(row).items() if g["confidence"] * 100 >= minimum}
                if sure:
                    todo.append((row["path"], sure))
            self.activity["total"] = len(todo)
            tagged = 0
            for path, sure in todo:
                if self._stop.is_set():
                    break
                try:
                    if self._write_tags(path, {i: g["language"] for i, g in sure.items()}, sure) != "Nothing to change":
                        tagged += 1
                except (tagging.TagError, OSError) as e:
                    logger.warning(f"Couldn't tag {path} from its suggestions: {e}")
                self.activity["done"] += 1
            stopped = " before Stop was pressed" if self._stop.is_set() else ""
            logger.log("ACTION", f"Accept all: tagged {tagged} file(s) from suggestions at least {minimum}% sure{stopped}")

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
            made = {"jellyfin": Jellyfin, "bazarr": Bazarr}.get(kind)
            self._services[kind] = (key, made(config) if made else Arr(kind, config))
        return self._services[kind][1]

    def notify(self, path):
        """Ask Radarr or Sonarr, Bazarr and Jellyfin to read the changed file again. Done in its own thread, so a
        slow answer doesn't hold up the job: the next file can start straight away."""
        with self._notices_ready:
            if path not in self._notices:
                self._notices.append(path)
            if not self._notifier or not self._notifier.is_alive():
                self._notifier = threading.Thread(target=self._send_notices, daemon=True)
                self._notifier.start()
            self._notices_ready.notify_all()

    def _send_notices(self):
        """Tell the apps in batches: wait until changes stop coming for a minute (or 5 minutes at most), then ask
        each film or series to be rescanned once, however many of its files changed. A rescan reads every file of
        a series, so one per episode tagged could swamp Sonarr and Bazarr."""
        while True:
            with self._notices_ready:
                first = time.time()
                while self._notices:
                    count = len(self._notices)
                    self._notices_ready.wait(self.notify_quiet)
                    if len(self._notices) == count or time.time() - first >= NOTIFY_MOST_SECONDS:
                        break
                if not self._notices:
                    self._notifier = None
                    self._notices_ready.notify_all()
                    return
                batch = list(self._notices)
            done = set()
            for path in batch:
                try:
                    self.notify_now(path, done)
                except Exception as e:
                    logger.warning(f"Couldn't tell the other apps about {path}: {e}")
            with self._notices_ready:
                for path in batch:
                    self._notices.remove(path)
                self._notices_ready.notify_all()

    def wait_for_notices(self, timeout=30):
        """Until every app has been told about the changes so far (for tests)."""
        with self._notices_ready:
            self._notices_ready.wait_for(lambda: not self._notices, timeout)

    def _down(self, kind, e):
        """Note an app that couldn't be reached, so it isn't asked again for 10 minutes. Returns whether that's new."""
        new = time.time() >= self._unreachable.get(kind, 0)
        if new:
            name_ = kind.capitalize()
            self.alert(name_, str(e) if name_ in str(e) else f"Couldn't reach {name_}: {e}")
        if isinstance(e, ServiceError) and ("Can't reach" in str(e) or "didn't answer in time" in str(e)):
            self._unreachable[kind] = time.time() + 600
        return new

    def notify_now(self, path, done=None):
        """Tell the apps about one changed file. done holds what's been asked already in this batch, so each film
        or series is only rescanned once."""
        done = set() if done is None else done
        found = {}
        for kind in ("radarr", "sonarr"):
            arr = self._service(kind)
            if not arr or time.time() < self._unreachable.get(kind, 0):
                continue
            try:
                hit = arr.find(path)
                if hit:
                    found[kind] = hit[0]
                    if (kind, hit[0]) in done:
                        continue
                    done.add((kind, hit[0]))
                    arr.rescan(path)
                    logger.info(f"Asked {arr.name} to rescan {hit[1]}")
            except ServiceError as e:
                if self._down(kind, e):
                    logger.warning(f"Couldn't ask {arr.name} to rescan: {e}")
        self._tell_bazarr(path, found, done)
        jellyfin = self._service("jellyfin")
        if jellyfin and time.time() >= self._unreachable.get("jellyfin", 0):
            try:
                jellyfin.updated(path)
            except ServiceError as e:
                if self._down("jellyfin", e):
                    logger.warning(f"Couldn't tell Jellyfin about the change: {e}")

    def _tell_bazarr(self, path, found, done=None):
        """Ask Bazarr to look at the film again and, if it's been left without subtitles in your languages,
        to search for some."""
        bazarr = self._service("bazarr")
        if not bazarr or time.time() < self._unreachable.get("bazarr", 0):
            return
        try:
            if "radarr" in found:
                kind, item_id = "movie", found["radarr"]
            elif "sonarr" in found:
                kind, item_id = "series", found["sonarr"]
            else:
                located = bazarr.locate(path)
                if not located:
                    logger.info(f"Bazarr doesn't know {path}, so it wasn't told about the change")
                    return
                kind, item_id = located
            done = set() if done is None else done
            if ("bazarr", kind, item_id) not in done:
                done.add(("bazarr", kind, item_id))
                bazarr.action(kind, item_id, "scan-disk")
            if (config.bazarr_search_missing and ("bazarr-search", kind, item_id) not in done
                    and not self.has_wanted_subtitles(path)):
                done.add(("bazarr-search", kind, item_id))
                bazarr.action(kind, item_id, "search-missing")
                logger.info(f"Asked Bazarr to look for subtitles for {os.path.basename(path)}")
        except ServiceError as e:
            if self._down("bazarr", e):
                logger.warning(f"Couldn't tell Bazarr about the change: {e}. Not asking it again for 10 minutes")

    def has_wanted_subtitles(self, path):
        """Whether a film has subtitles in a language you keep, inside it or beside it."""
        library = self.library_for(path)
        row = self.store.file(path)
        if not library or not row or not row["info"]:
            return True
        wanted = set(library["subtitles"])
        inside = any(t["type"] == "subtitle" and t["language"] in wanted for t in row["info"]["tracks"])
        beside = any(f["language"] in wanted for f in row["info"].get("external", []))
        return inside or beside

    def bazarr_clashes(self):
        """Languages Bazarr downloads but Dubarr removes: [(profile name, [language names])]."""
        bazarr = self._service("bazarr")
        if not bazarr:
            return []
        kept = {code for lib in config.library_list for code in lib["subtitles"]}
        clashes = []
        for profile, codes in bazarr.profiles():
            extra = sorted({normalise(c) for c in codes} - kept - {"und"})
            if extra:
                clashes.append((profile, [name(c) for c in extra]))
        return clashes

    # Main loop

    def tick(self):
        """Called every minute: scan when it's due, change files when allowed, empty the recycle folder daily."""
        now = time.time()
        if now - self.last_detect >= DETECT_EVERY_SECONDS and not self._detecting.locked():
            threading.Thread(target=self.detect_pass, daemon=True).start()
        if self.busy():
            return
        try:
            if now - self.last_scan >= config.scan_interval_hours * 3600:
                self.scan("scheduled")
            if now - self.last_apply >= APPLY_EVERY_SECONDS:
                self.apply("scheduled")
            if (config.bazarr_other_subtitles == config.OTHER_SUBTITLES_AUTO
                    and now - self.last_subtitles >= APPLY_EVERY_SECONDS):
                self.remove_other_subtitles("scheduled")
            if (config.replace_no_audio and not config.report_only
                    and now - self.last_replace >= APPLY_EVERY_SECONDS):
                self.replace_no_audio("scheduled")
            if now - self.last_purge >= 24 * 3600:
                with self.job("Emptying the recycle folder"):
                    self.purge()
        except Busy:
            pass

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
from core import swap, tagging
from core.plan import NOT_FROM_BAZARR, Plan, Rules, _removal, decide
from core.probe import PROBE_VERSION, MediaInfo, ProbeError, find_sidecars, probe
from core.remux import RemuxError, remux, supported
from core.store import Store
from services.connections import Arr, Bazarr, Jellyfin, ServiceError
from utils.languages import name, normalise
from utils.logger import logger
from utils.paths import inside

# Statuses Dubarr adds on top of the plan's
SEEDING = "seeding"   # would change, but it's hardlinked and probably still seeding
TOO_SMALL = "too_small"  # would change, but would save less than the minimum set
ERROR = "error"       # couldn't be read or changed
NOT_BAZARR = "not_bazarr"  # only subtitle files Bazarr didn't download to remove

APPLY_EVERY_SECONDS = 600


class Busy(Exception):
    pass


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
        self.activity = None          # what's happening now, for the web UI
        self.last_scan = 0.0
        self.last_apply = 0.0
        self.last_purge = 0.0
        self.last_subtitles = 0.0
        self._services = {}
        self._unreachable = {}  # Radarr, Sonarr or Jellyfin: when to try asking them again

    # Jobs

    @contextmanager
    def job(self, task, wait=False):
        if not self._job.acquire(blocking=wait):
            raise Busy(f"Dubarr is busy: {self.activity['task'] if self.activity else 'working'}")
        self._stop.clear()
        # done/total count files; bytes_done/bytes_total, when set, give a better time estimate for changes
        self.activity = {"task": task, "detail": "", "done": 0, "total": 0, "bytes_done": 0, "bytes_total": 0,
                         "started": time.time()}
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
        elapsed = time.time() - a["started"]
        eta = None
        if a["bytes_total"] and a["bytes_done"]:
            eta = elapsed / a["bytes_done"] * (a["bytes_total"] - a["bytes_done"])
        elif a["total"] and a["done"]:
            eta = elapsed / a["done"] * (a["total"] - a["done"])
        a["elapsed_seconds"] = round(elapsed)
        a["eta_seconds"] = round(eta) if eta is not None else None
        return a

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

    def rules(self, library, path=None):
        return Rules(library["audio"], library["subtitles"], config.strip_audio, config.strip_subtitles,
                     config.strip_commentary, config.one_audio_per_language, config.one_subtitle_per_language,
                     config.assume_unlabelled_is_full, config.strip_subtitle_files, config.prefer_text_files,
                     config.keep_original_audio, self.original_language(path) if path else None,
                     self.bazarr_files(path) if path else None)

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

        rules = self.rules(library, path)
        plan = decide(info, rules)
        status, message = plan.status, plan.message
        if plan.rewrites:
            saving = estimated_saving(plan)
            minimum = config.min_saving_mb * 1024 * 1024
            waiting = None
            if minimum and saving is not None and saving < minimum and not allow_small:
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
        if plan and plan.original_only:
            self.tag_original_only(path)
        return status

    def scan(self, trigger="manual", paths=None, wait=False):
        """Check every file in the libraries, or just the paths given."""
        with self.job("Scanning", wait=wait):
            started = time.time()
            counts = {}
            if paths is None:
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

        root = swap.recycle_root(library["path"], config.recycle_folder)
        saved = 0
        try:
            if plan.rewrites:
                new = remux(info, plan)
                history_id = swap.swap(self.store, run_id, path, info.size, new.size, plan.message, plan.remove,
                                       root, config.recycle_days)
                saved += info.size - new.size
                logger.log("ACTION", f"{plan.message} from {path} (saved {size(info.size - new.size)}, change #{history_id})")
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
            reason = getattr(e, "strerror", None) or str(e)
            logger.error(f"Couldn't change {path}: {reason}. The original is untouched")
            st = os.stat(path)
            self.store.save_file(path, library["path"], st.st_size, st.st_mtime, st.st_nlink,
                                 info.to_dict(), ERROR, f"Couldn't change it: {reason}", plan.to_dict(), plan.saved_bytes)
            return False

        self.check_file(library, path, reread=True)
        self.notify(path)
        return saved

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
                    new = remux(info, plan)
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

    def tag(self, path, changes, wait=False):
        """Set the language of tracks, e.g. {2: "eng", 4: "fra"}. Returns what was done."""
        with self.job("Tagging", wait=wait):
            self.activity["detail"] = path
            library = self.library_for(path)
            if not library:
                raise tagging.TagError("This file isn't in a library set up in Dubarr")
            self.check_file(library, path, reread=True)
            info = self.known_file(path)
            current = {t.index: t.language for t in info.tracks}
            wanted = {}
            for index, code in changes.items():
                index = int(index)
                track = next((t for t in info.tracks if t.index == index), None)
                if not track or track.type not in ("audio", "subtitle"):
                    raise tagging.TagError(f"Track {index} isn't an audio or subtitle track")
                if not tagging.valid_tag(code):
                    raise tagging.TagError(f"{code} isn't a language Dubarr knows")
                if current.get(index) != code:
                    wanted[index] = code
            if not wanted:
                return "Nothing to change"

            st = os.stat(path)
            if st.st_nlink > 1 and not config.process_hardlinked:
                raise tagging.TagError("The file is hardlinked, probably still seeding. Changing its tags would change "
                                       "the torrent's data too. Try again once seeding stops")

            message = tagging.describe_changes(info, wanted)
            try:
                if path.lower().endswith((".mkv", ".mk3d")):
                    tagging.tag_mkv(info, wanted)
                else:
                    new = tagging.retag_mp4(info, wanted)
                    swap.swap(self.store, None, path, info.size, new.size, message, [],
                              swap.recycle_root(library["path"], config.recycle_folder), config.recycle_days)
            except (RemuxError, swap.SwapError, OSError) as e:
                raise tagging.TagError(getattr(e, "strerror", None) or str(e))

            logger.log("ACTION", f"{message} in {path}")
            self.check_file(library, path, reread=True)
            self.notify(path)
            return message

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
        """Ask Radarr or Sonarr, Bazarr and Jellyfin to read the changed file again."""
        found = {}
        for kind in ("radarr", "sonarr"):
            arr = self._service(kind)
            if not arr:
                continue
            try:
                hit = arr.find(path)
                if hit:
                    found[kind] = hit[0]
                    arr.rescan(path)
                    logger.info(f"Asked {arr.name} to rescan {hit[1]}")
            except ServiceError as e:
                logger.warning(f"Couldn't ask {arr.name} to rescan: {e}")
        self._tell_bazarr(path, found)
        jellyfin = self._service("jellyfin")
        if jellyfin:
            try:
                jellyfin.updated(path)
            except ServiceError as e:
                logger.warning(f"Couldn't tell Jellyfin about the change: {e}")

    def _tell_bazarr(self, path, found):
        """Ask Bazarr to look at the film again and, if it's been left without subtitles in your languages,
        to search for some."""
        bazarr = self._service("bazarr")
        if not bazarr:
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
            bazarr.action(kind, item_id, "scan-disk")
            if config.bazarr_search_missing and not self.has_wanted_subtitles(path):
                bazarr.action(kind, item_id, "search-missing")
                logger.info(f"Asked Bazarr to look for subtitles for {os.path.basename(path)}")
        except ServiceError as e:
            logger.warning(f"Couldn't tell Bazarr about the change: {e}")

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
            if now - self.last_purge >= 24 * 3600:
                with self.job("Emptying the recycle folder"):
                    self.purge()
        except Busy:
            pass

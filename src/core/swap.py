"""Put the cleaned file in place, keep the original, and undo it on request.

The swap, after the cleaned copy has been written and checked:
  1. rename the original to a hidden backup next to it   (instant, same folder)
  2. rename the cleaned copy to the original's name      (instant, same folder)
  3. move the backup into the recycle folder             (instant on the same disk)

At every step one complete copy of the film sits at its usual path or right next
to it. If Dubarr stops part way, recover() finishes or undoes the swap on the
next start.
"""

import os
import shutil
import time

from core.remux import temp_path
from core.store import RESTORABLE, Store

RECYCLE_NAME = ".dubarr-recycle"


class SwapError(Exception):
    pass


def backup_path(path: str) -> str:
    folder, name = os.path.split(path)
    return os.path.join(folder, f".{name}.dubarr.orig")


def recycle_root(library: str, recycle_folder: str = "") -> str:
    return recycle_folder or os.path.join(library, RECYCLE_NAME)


def _copy_ownership(source_stat, target):
    """The new file gets the original's permissions and owner, so Radarr, Sonarr and Jellyfin can still use it."""
    try:
        os.chmod(target, source_stat.st_mode & 0o7777)
    except OSError:
        pass
    try:
        os.chown(target, source_stat.st_uid, source_stat.st_gid)
    except OSError:
        pass


def _to_recycle(store: Store, history_id: int, backup: str, root: str, keep_days: int):
    if keep_days <= 0:
        os.remove(backup)
        store.update_history(history_id, state="deleted", backup_path=None)
        return
    name = os.path.basename(backup)[1:-len(".dubarr.orig")]
    destination = os.path.join(root, str(history_id), name)
    try:
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        shutil.move(backup, destination)
        store.update_history(history_id, state=RESTORABLE, backup_path=destination)
    except OSError as e:
        # The original is still safe next to the file, and can be restored from there
        store.update_history(history_id, state=RESTORABLE, backup_path=backup,
                             note=f"Couldn't move the original to the recycle folder ({e.strerror or e}). It's kept next to the file")


def swap(store: Store, run_id: int, path: str, original_size: int, new_size: int, message: str,
         removed: list, root: str, keep_days: int) -> int:
    """Replace path with its cleaned copy (already written and checked). Returns the history id."""
    cleaned = temp_path(path)
    backup = backup_path(path)
    if not os.path.exists(cleaned):
        raise SwapError("The cleaned copy is missing")
    if os.path.exists(backup):
        raise SwapError(f"{os.path.basename(backup)} is in the way from an earlier attempt")

    _copy_ownership(os.stat(path), cleaned)
    history_id = store.add_history(run_id, path, backup, original_size, message, removed)

    os.rename(path, backup)
    try:
        os.rename(cleaned, path)
    except OSError as e:
        os.rename(backup, path)
        store.update_history(history_id, state="failed", backup_path=None, note=f"Couldn't swap the files ({e.strerror or e})")
        raise SwapError(f"Couldn't swap the files ({e.strerror or e}). The original is untouched")

    store.update_history(history_id, new_size=new_size)
    _to_recycle(store, history_id, backup, root, keep_days)
    return history_id


def recover(store: Store) -> list:
    """Finish or undo swaps that were interrupted. Returns a message for each one."""
    messages = []
    for entry in store.history_in_state("swapping"):
        path, backup = entry["path"], entry["backup_path"]
        try:
            if backup and os.path.exists(backup):
                if os.path.exists(path):
                    store.update_history(entry["id"], state=RESTORABLE,
                                         note="Dubarr stopped during the swap. The original is kept next to the file")
                    messages.append(f"Finished an interrupted change to {path}")
                else:
                    os.rename(backup, path)
                    store.update_history(entry["id"], state="failed", backup_path=None,
                                         note="Dubarr stopped during the swap, so the original was put back")
                    messages.append(f"Put back the original of {path} after an interrupted change")
            else:
                store.update_history(entry["id"], state="failed", backup_path=None,
                                     note="Dubarr stopped during the swap")
                messages.append(f"An interrupted change to {path} couldn't be checked")
            cleaned = temp_path(path)
            if os.path.exists(cleaned):
                os.remove(cleaned)
        except OSError as e:
            messages.append(f"Couldn't recover {path}: {e.strerror or e}")
    return messages


def restore(store: Store, history_id: int) -> str:
    """Put an original back. The cleaned file it replaces is deleted. Returns the path."""
    entry = store.history_entry(history_id)
    if not entry:
        raise SwapError("No such change")
    if entry["state"] != RESTORABLE:
        raise SwapError({
            "restored": "Already restored",
            "purged": "The original has been emptied from the recycle folder",
            "deleted": "The original was deleted straight away (recycle folder set to 0 days)",
            "failed": "This change didn't happen, so there's nothing to restore",
            "swapping": "This change is still in progress",
        }.get(entry["state"], "Can't restore this one"))
    backup, path = entry["backup_path"], entry["path"]
    if not backup or not os.path.exists(backup):
        raise SwapError("The original is no longer in the recycle folder")

    os.makedirs(os.path.dirname(path), exist_ok=True)
    staging = temp_path(path)
    # Copy (or rename) next to the file first, so the cleaned file stays in place until the last instant
    shutil.move(backup, staging)
    os.replace(staging, path)
    store.update_history(history_id, state="restored", restored_at=time.time(), backup_path=None)
    _remove_empty(os.path.dirname(backup))
    return path


def purge(store: Store, keep_days: int, now: float = None) -> int:
    """Delete originals that have been in the recycle folder longer than keep_days."""
    now = now or time.time()
    count = 0
    for entry in store.history_in_state(RESTORABLE):
        if keep_days > 0 and entry["changed_at"] > now - keep_days * 86400:
            continue
        backup = entry["backup_path"]
        try:
            if backup and os.path.exists(backup):
                os.remove(backup)
                _remove_empty(os.path.dirname(backup))
            store.update_history(entry["id"], state="purged", backup_path=None)
            count += 1
        except OSError:
            pass
    return count


def _remove_empty(folder):
    """Tidy up a recycle folder's numbered subfolder once it's empty."""
    if folder and os.path.basename(folder).isdigit():
        try:
            os.rmdir(folder)
        except OSError:
            pass

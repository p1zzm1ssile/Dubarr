# Changelog

## 0.2.1

- **Commentary subtitles go with the commentary.** When commentary tracks are removed, the subtitle tracks of that commentary are removed too, rather than being left behind.

## 0.2.0

- **Listen & tag.** Untagged audio and subtitle tracks can now be checked from the Dashboard: play a 30-second sample of an audio track, or read a few lines of a subtitle track, pick the language and save. The tag is written into the file (MKV in place, in a moment; MP4 as a checked copy, with the original in the recycle folder), so the file isn't flagged again. Works on any track, to fix wrong tags too.
- **Untagged subtitles** get their own Dashboard list, so they can be tagged.
- **Commentary tracks are removed**, even in your languages. Turn it off in Settings > General. The main audio is never removed.
- **Time left.** Scans and batches of changes show how far along they are and roughly how long is left.

## 0.1.0

First release.

- **Strips unwanted languages.** Removes audio and subtitle tracks in languages you don't keep from MKV and MP4 files, without re-encoding. Languages are set once, and can be overridden per library.
- **Flags instead of guessing.** Files with no audio in your languages, or with untagged audio, are left alone and listed on the Dashboard.
- **Report only by default.** See what would change and how much space it saves, then apply files one at a time or all at once.
- **Safe swap.** The cleaned copy is checked before it replaces the original, and interrupted changes are finished or undone on the next start.
- **Recycle folder and History.** Originals are kept for 7 days (configurable). Restore a single file or a whole batch.
- **Waits for seeding.** Hardlinked files are skipped until seeding stops.
- **Time window and batch size** for changing files, to go easy on the array.
- **Radarr, Sonarr and Jellyfin.** Rescans after each change, and an On Import webhook to check new files straight away.
- **Web UI** with login, Dashboard, History, Logs, Settings and Contact pages.
- **Unraid template** and Docker image on GitHub Container Registry.

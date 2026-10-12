# Changelog

## 0.3.0

- **One audio track per language.** Keeps the best-quality track in each language you keep (e.g. Dolby Atmos over the extra 5.1 and stereo copies) and removes the rest. Jellyfin converts audio on the fly for devices that can't play it, without touching the video. Audio description tracks are never picked.
- **One subtitle track per language, plus forced.** Keeps one full track per language, choosing text (SRT) over pictures (PGS) because Jellyfin shows text without transcoding the video, and normal over SDH. Forced subtitles are kept, including ones nobody flagged, and get the forced flag so Jellyfin shows them for foreign-language scenes by itself.
- **Formats on the Dashboard.** Each track shows its format, e.g. "English audio · TrueHD Atmos 7.1" or "English subs · PGS".
- Both new rules are on by default and can be turned off in Settings > General.
- The first scan after updating reads every file again, to pick up the extra details. It takes a little longer than usual.

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

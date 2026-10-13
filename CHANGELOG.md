# Changelog

## Unreleased

- **Always keep the original language audio.** A new setting in Settings > General keeps audio in a film's or show's original language alongside your languages, so an anime keeps its Japanese audio without adding Japanese (and every other language films are made in) to the list. The original language comes from Radarr or Sonarr (Sonarr v4), or else from an audio track flagged as the original. When neither says, no audio language is removed from that file, and it's listed under "Original language unknown". Off by default.
- The first scan after updating reads every file again, to pick up the original-language flag.

## 0.4.1

- **Re-check removes deleted files.** Pressing Re-check on a file that has since been deleted or renamed now takes it off the Dashboard, rather than leaving it there until the next full scan.

## 0.4.0

- **Bazarr.** A new Bazarr tab in Settings. After changing a film, Dubarr asks Bazarr to look at it again straight away, and to search for subtitles if the change left none in your languages. Test connection checks Bazarr's language profiles, and the Dashboard warns if Bazarr downloads a language Dubarr removes, as those would keep coming back.
- **Subtitle files beside films.** Files like Film.fr.srt, downloaded by Bazarr or Jellyfin, are removed when they're in a language you don't keep. It's just a file delete, so it happens even while the film itself waits for seeding to stop or is below the minimum saving. They go to the recycle folder and can be restored. Files whose name doesn't say a language are kept. On by default.
- **Remove picture subtitles when a text file is beside the film.** If Film.en.srt sits beside the film, its English picture subtitles (PGS) aren't needed, and Jellyfin won't have to transcode to show them. Forced tracks stay. Off by default.
- **Speed and CPU.** New settings in Settings > General: **Workers** (how many films are changed at once, never two on the same disk), **CPU threads per worker**, and **Run at low priority** (on by default), so Dubarr gives way to Jellyfin transcodes and everything else, for both CPU and disk.

## 0.3.1

- **Skip files that would save less than…** A slider in Settings > General (off, or 10 MB up to 5 GB). Removing any track means rewriting the whole file, so a 4K film could be rewritten to save a few KB of subtitles. Files below the amount go to a "Not worth rewriting" list, where they can still be applied one at a time. Off by default.
- **Trust SDH labels when subtitle lengths aren't recorded.** Some files don't say how long each subtitle track is, so Dubarr can't tell a short forced track from a full one and keeps every track. With this on, when one track is labelled SDH, the unlabelled one is taken to be the full subtitles and the SDH copy is removed. Off by default.

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

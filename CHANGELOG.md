# Changelog

## 0.5.0

- **Try another part of the audio.** When you tag a track by hand, Play a 30s sample becomes Try another part after the first play, for when the sample happens to have no talking. Each press plays 30 seconds from somewhere else in the film (#18).
- **Tagging and changing MP4s with a timecode track.** MP4s carrying a timecode or chapter-text data track, common in files from cameras and QuickTime, failed with "Could not write header (incorrect codec parameters ?)" when retagging or removing tracks. Those data tracks are now rebuilt by ffmpeg instead of copied, and a failed tag is written to the log too (#17).
- **Remove tracks by hand.** Check tracks on the Dashboard now has a Remove tickbox on every audio and subtitle track and on every subtitle file beside the film. Remove ticked takes out just those, even in languages you keep, the same safe way as any change: a checked copy, with the original in the recycle folder to restore from History. The last audio track can't be removed, and tracks aren't removed from a file that's still seeding.
- **Where each subtitle file came from.** With Bazarr connected, subtitle files beside a film are marked as Bazarr's, with the site it got them from (OpenSubtitles.com, SubDL and so on), or as not from Bazarr.
- **Apply all in one library.** With more than one library, a library picker next to Apply all on the Dashboard lists, and applies, just that library's files, e.g. your anime folder and not your films.
- **Only keep subtitles Bazarr downloaded.** A new setting in Settings > Bazarr for subtitle files beside a film or episode that Bazarr didn't download, such as ones that came with the release, whatever their language: keep them (the default), list them on the Dashboard under "Subtitles not from Bazarr" to remove by hand, or remove them by themselves about every 10 minutes, even with Report only on. Bazarr's own history says which files it downloaded; films Bazarr doesn't know are left alone, and nothing is removed while that history is empty or Bazarr can't be reached. Removed files go to the recycle folder and can be restored from History. Subtitle tracks inside the video aren't affected.
- **Keys and passwords are kept out of the logs.** Any API key or password, and any `apikey=`, `token=` or `password=` in a web address quoted by an error, is replaced by `***` before it's written. Log files, and the file that holds the login, can now only be read by the account the app runs as.
- **Always keep the original language audio.** A new setting in Settings > General keeps audio in a film's or show's original language alongside your languages, so an anime keeps its Japanese audio without adding Japanese (and every other language films are made in) to the list. The original language comes from Radarr or Sonarr (Sonarr v4), or else from an audio track flagged as the original. When neither says, no audio language is removed from that file, and it's listed under "Original language unknown". Off by default.
- **Original language only.** With that setting on, a film or series that only comes in its original language (Japanese-only anime, say) is no longer flagged as "No wanted audio". It gets its own Dashboard list, "Original language only", and other audio languages are still removed from it. It's tagged `original-language-only` in Radarr, Sonarr and Jellyfin, so you can filter by it there; the tag can be changed, or left empty to not tag, in Settings > General.
- The first scan after updating reads every file again, to pick up the original-language flag.

## 0.4.2

- **Addresses start with http://.** Radarr, Sonarr, Bazarr and Jellyfin addresses start empty on a new install, with an example such as `http://192.168.1.10:7878`, instead of `localhost`, which inside a container is the container itself. Addresses must start with `http://` or `https://`; ones saved before this get `http://` added by themselves, so nothing needs re-entering.
- **No folder mappings filled in on install.** The Unraid template's Movies and TV folders start empty, so you map your own.

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

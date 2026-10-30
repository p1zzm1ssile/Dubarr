# Changelog

## 0.9.1

- **Music or effects only is fine.** A file whose only audio is tagged No speech (music or effects only) now counts as Already clean, with nothing to add in Settings. Next to audio in a language you don't keep it still shows under No wanted audio (#36).
- **Show some lines for subtitle files.** Subtitle files beside a film have Show some lines under Listen & tag, like subtitle tracks, to check a language before trusting Detect.
- **Listen & tag shows what needs you.** The panel opens on the tracks and files that are untagged or will be removed. Show all brings back the rest.
- **Every job shows it's moving.** Until there's enough to estimate the time left, the progress line says how long the job has been running.
- **Pop-ups for problems with the other apps.** When Radarr, Sonarr, Bazarr or Jellyfin can't be reached, or files with no audio couldn't be replaced, a note pops up in the corner until closed. Each scan also checks the folders Radarr and Sonarr keep films in: one Dubarr can't see, the usual sign of a missing Folder mapping (common with TRaSH guide paths), pops up with a suggested mapping.

## 0.9.0

- **Subtitle files beside a film work like its tracks.** Under Listen & tag, each subtitle file has a language list, Detect (reads the file and suggests its language), Put in and Remove, next to the film's own tracks. A file can't hold a language tag, so choosing its language renames it to say so: Film.srt becomes Film.en.srt, keeping forced and SDH in the name. Use suggestions does the same with Dubarr's guess (#29).
- **A subtitle file whose name doesn't say its language needs you.** Films with one now go under Untagged subtitles instead of Already clean, with what the file looks like as a suggestion, until it's named, put in or removed. Film.forced.srt now counts as forced with no language, instead of being ignored (#29).
- **Put in any subtitle file.** Put in, in the panel, takes the file you pick, whatever list the film is on (Will be changed included), and even when the film already has a track in that language (#29).
- **Suggestions survive a rewrite.** Changing a film or putting subtitles in used to drop the suggestions already made for its tracks, so untagged audio had to be detected again. They're now carried over to the new copy (#29).
- **Blu-ray picture subtitles can be checked.** Show some lines on a picture subtitle track (PGS, VobSub) shows a few stills of the film with the subtitles on, so you can read them and choose the language. Detect isn't offered for these, as it would need text recognition.

## 0.8.9

- **Progress inside a big film.** Rewriting a film now counts the new copy as it's written, so the bar and the time left move during a 4K film instead of sitting at "0 file(s) so far" until it's done. Removing tracks by hand and Put subtitle file in show how many GB of the film are done, e.g. "31.2 GB of 58.4 GB (53%) · about 6 min left".

## 0.8.8

- **A subtitle file's language read from its text is a suggestion.** When a subtitle file's name doesn't say its language, the track it becomes goes into the film untagged, so the film shows under Untagged subtitles with the language Dubarr read as a suggestion to confirm. It's only tagged straight away when Tag automatically (Settings > Language detection) is set at or below how sure Dubarr is (#29).
- **Put subtitle files in from any list.** Films with a subtitle file beside them that could go in, such as one under Untagged audio, or any film while Put subtitle files beside films into them is off, have a Put subtitle file in button (#29).
- **Subtitle files show their language before you decide.** A file whose name doesn't say its language shows what its text looks like, e.g. "Unknown subs (looks English, 98%)". Remove subtitle files beside films in other languages still never removes these (#29).

## 0.8.7

- **No more 7-day release.** Files under Tagged automatically now wait until you press Looks right, however long that takes (#18).
- **The trust level defaults to 100.** Change files straight away when at least this sure (Settings > Language detection) now starts at 100%, which means files tagged automatically or with Accept all always wait for Looks right before tracks are removed. Lower it to let very sure guesses skip the wait. A level you already saved is kept (#18).
- **Changing the trust level checks the waiting files again.** Lowering it to 90% moves the files whose guesses were all at least 90% sure on to Will be changed, and raising it holds them again if they haven't been changed yet (#18).
- **Use suggestions shows its progress in the file's row.** The row says "Waiting to save the tags" or "Saving the tags" until it's done, and says why if they couldn't be saved (#18).
- **Clearer Accept all question.** It shows the trust level itself instead of pointing to Settings, and leaves it out when every file will wait (#18).

## 0.8.6

- **Put subtitle files into the film (optional).** A new setting in Settings > General, Put subtitle files beside films into them, off by default. Text subtitle files (SRT, ASS, VTT) beside a film, in a language you keep, go into the film as a track, and the file goes to the recycle folder so it can be restored from History. The language comes from the file's name, or is read from its text when the name doesn't say, and older Windows-1252 files are read correctly. A film that already has that language inside is left alone (#29).
- **Accept all shows its progress.** It runs as one job, Accepting suggestions, with a count of files done and time left, instead of a string of separate Tagging jobs that read "0 file(s) so far". Stop stops it at the next file (#30).
- **Where files tagged by Accept all go.** Only files with tracks to remove wait under Tagged automatically, and guesses at least as sure as the level in Settings > Language detection (97% unless changed) skip the wait. Files with nothing to remove go to their usual list, such as Already clean. The setting's help now says so (#30).
- **Playing a sample keeps the suggestion.** Under Listen & tag, Play a 30s sample and Show some lines no longer clear Whisper's suggestion and its Use button (#31).
- **Much lighter on the server.** Every tagged or changed file used to ask Sonarr (or Radarr) to rescan its whole series, and Bazarr to rescan it too, so tagging 50 episodes of a show meant 50 full rescans of that show at once. Changes are now gathered up and each film or series is rescanned once, a minute after the changes stop.
- **Background detection keeps out of the way.** It only runs while Dubarr isn't doing anything else, stopping as soon as a job starts, rests a few seconds after each file it sends to Whisper, and keeps to the hours set in Settings > Language detection (now Only work in the background from / Until, covering detection as well as automatic tagging).

## 0.8.5

- **Replace works when Radarr or Sonarr has the film as missing.** A file with no audio that Radarr or Sonarr never imported (it shows as Missing there, with no file) couldn't be replaced, as they had no file to delete. Dubarr now deletes it itself, into the recycle folder so it can be restored from History, and asks them to search for the film or episode. A leftover copy beside a film they do have is still left for you to Delete.
- **Automatic tagging gives way to you.** It only runs while Dubarr is idle, shows as "Tagging automatically", and never queues up behind a job you started. Starting a job while it's tagging no longer says Dubarr is busy: your job goes next, and automatic tagging waits for its next pass.
- **Hours for automatic tagging.** Settings > Language detection has Only tag automatically from / Until (hour), e.g. 1 to 7 for overnight. Suggestions are still made at any time. Tag automatically stays off unless you set a level.
- **Tagging no longer waits on Bazarr, Radarr, Sonarr or Jellyfin.** Telling them about a change now happens alongside, so the next file starts straight away. Each tag was taking 30 seconds while Bazarr didn't answer.
- **Bazarr being slow isn't an error.** Bazarr only answers a rescan once it's finished, which for a big series can take minutes, so "Couldn't tell Bazarr about the change: Bazarr didn't answer in time" filled the log. Not hearing back is now fine. If Bazarr can't be reached at all, it's said once and Bazarr isn't asked again for 10 minutes; the same goes for Radarr, Sonarr and Jellyfin.

## 0.8.4

- **MP4s are tagged in a moment.** Setting a track's language on an MP4 used to copy the whole file, a minute or more for a big film, while MKVs took a second. MP4s are now tagged in place too: only the two bytes holding each track's language change, and the file is read back to check. The few laid out in a way that can't be done safely are still copied as before.
- **Accept all says why it has nothing to do.** When no suggestion was sure enough, Accept all ran anyway and logged "tagged 0 file(s)". It now says so straight away, with the surest suggestion left (e.g. "The surest one left is 87%"), or that the files have no suggestions yet. With Tag automatically turned on, the suggestions above its level are already tagged, so Accept all at the same level finds none. When there is something to do, it says how many files it's tagging.

## 0.8.3

- **Why a file with no audio wasn't replaced.** Replace only works for files Radarr or Sonarr have as theirs. Files they don't, such as a leftover copy beside the film or a folder they don't know, were silently left on the No audio list. Each one now says why, e.g. "Radarr has Film, but not this file" or "Radarr and Sonarr don't have it. If they should, check the Folder mapping in Settings".
- **Delete the ones that can't be replaced.** Those files get a Delete button, and the list gets Delete the N that can't be replaced. They go to the recycle folder, so they can be restored from History, and Radarr, Sonarr and Jellyfin are told.

## 0.8.2

- **Automatic tagging no longer keeps starting.** With Tag automatically turned on, a file it couldn't tag, most often one still seeding, started a Tagging job again every 10 minutes. Seeding files now wait for seeding to stop, like any other change, and a file whose tagging fails isn't tried again until it changes.

## 0.8.1

- **Stop means stop.** Pressing Stop during a job, such as replacing files with no audio, let automatic tagging that was waiting its turn start straight after, so it looked as if the job had restarted. Stop now also drops automatic tagging and Accept all waiting behind the job; background tagging picks up again at its next pass, 10 minutes later. Tags you saved yourself are still written.
- **Stop all.** While jobs are waiting their turn behind the one running (tags being saved, a scan after a settings change, automatic tagging), the Dashboard shows what is waiting and a Stop all button. It stops after the current file and cancels everything waiting, tags you saved that haven't been written yet included.

## 0.8.0

- **Replace files with no audio.** Files under No audio on the Dashboard now have a Replace button, and the list has Replace all. Radarr or Sonarr deletes the file, blocklists the release it came from when their history shows it (so the same broken copy isn't downloaded again), and searches for another copy. For TV, only that episode is searched. Each one is listed in History. Files Radarr and Sonarr don't have, like extras, are left alone, and every file is read again first, so only one that really has no audio goes.
- **Or by themselves.** Replace files with no audio by themselves, in Settings > General, does it every 10 minutes. Off by default, and Report only stops it too.

## 0.7.0

- **Suggestions are waiting for you.** Dubarr now detects the language of untagged tracks in the background, at low priority, so the Untagged audio and Untagged subtitles lists already show each film's suggestion. Use suggestions confirms them in one click (#18).
- **Detect again listens elsewhere.** Pressing Detect a second time tries parts of the film it hasn't heard yet, in case music fooled it the first time.
- **Accept all, or tag automatically.** Accept all on the Untagged lists tags every suggestion at least as sure as the level you pick. Settings > Language detection can also tag by itself above a level (off by default). Every automatic tag is listed in History.
- **Tagged automatically waits for a check.** A wrong tag could get the wrong track removed, so a file tagged from a guess is listed under Tagged automatically and nothing is removed from it until you press Looks right, or for 7 days. Guesses at least 97% sure (changeable in Settings) skip the wait. Tagging a file yourself counts as checking it.
- **Saving tags no longer blocks.** Tags are saved in turn in the background, so you can move straight on to the next film while an MP4 is copied with its new tags.

## 0.6.0

- **Detect a track's language.** A new Language detection tab in Settings adds a Detect button to tracks under Listen & tag. Audio is sent to your own Whisper server (whisper-asr-webservice), which says what language is spoken, trying other parts of the film when a sample has no talking. Subtitles are read by a small detector built into Dubarr, with nothing to set up, or by your own Ollama. The suggestion shows how sure it is and is never saved until you choose it and press Save tags. The tab recommends which Whisper and Ollama models to use (#18).
- **Keys for Whisper and Ollama stay private.** They're optional, sent only as a Bearer token to an https:// address (Dubarr won't save one with an http:// address), never passed on through a redirect, never sent to the browser, and kept out of the logs. Changing the address means typing the key again, so a saved key can't be sent to another server.

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

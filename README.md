<p align="center"><img src="docs/banner.jpg" alt="Dubarr" width="100%"></p>

# Dubarr

Dubarr removes audio and subtitle tracks in languages you don't want from your movies and TV, and flags files that have no audio in your language. Nothing is re-encoded: the video and the tracks you keep are copied bit for bit, so there's no quality loss and no GPU needed.

It's built to be safe to leave running:

- **Report only to start with.** Dubarr lists what it would change and how much space that saves. Nothing changes until you apply it, or turn report-only off.
- **Never guesses.** A file with no audio in your languages is left alone and flagged (*"No English audio, only Korean + Spanish"*). So is a file with an audio track that has no language tag.
- **Swap only after a check.** The cleaned copy is written next to the original and checked (same video, same length, exactly the planned tracks) before it takes the original's place.
- **Recycle folder with restore.** Originals are kept for 7 days (you choose). Restore one file, or a whole batch, from the History page.
- **Waits for seeding.** Hardlinked files, which are usually still seeding, are skipped until the torrent is gone, so a film is never stored twice.
- **Tells everyone.** After a change, Radarr, Sonarr and Jellyfin are asked to rescan the file.

## What it keeps and removes

| The file has | Dubarr |
|---|---|
| Only languages you keep | Leaves it alone |
| Languages you keep, plus others | Removes the others (audio and subtitles, each can be turned off) |
| No audio in a language you keep | Leaves it alone, flags it |
| An audio track with no language tag | Leaves it alone, flags it until you tag it |
| Audio in the film's original language, e.g. Japanese for anime | Keeps it too, if *Always keep the original language audio* is on |
| Commentary tracks | Removes them (can be turned off) |
| Several audio tracks in one language | Keeps the best-quality one (can be turned off) |
| Subtitle files beside the film (Film.fr.srt) in other languages | Removes them, no rewrite needed (can be turned off) |
| Subtitle files beside the film that Bazarr didn't download | Keeps them, or lists or removes them if you choose (Settings > Bazarr) |
| Several subtitle tracks in one language | Keeps one full track, preferring text (SRT) and normal over SDH, plus the forced one (can be turned off) |

Video, chapters, fonts and anything that isn't audio or subtitles is always kept. Subtitles with no language tag are always kept. Forced subtitles in your languages are kept; forced subtitles in other languages are removed. A "multiple languages" audio track counts as wanted.

**Untagged tracks:** press *Listen & tag* on the Dashboard to hear a 30-second sample of an audio track, or read a few lines of a subtitle track, then choose its language. Dubarr writes the tag into the file (MKV files in place, in a moment), so it isn't flagged again. Picture-based subtitles (Blu-ray PGS) can't be shown as text. Files that are still seeding aren't tagged, as that would change the torrent's data.

**Detect the language:** with language detection set up (Settings > Language detection), untagged tracks get a *Detect* button. Audio goes to your own [Whisper](https://github.com/ahmetoner/whisper-asr-webservice) server, which listens to a sample and tries other parts of the film if there's no talking. Subtitles are read by a small detector built into Dubarr, or by your own Ollama if you choose. Either way it's a suggestion with how sure it is: nothing is tagged until you choose it and press *Save tags*.

| | CPU | Low-power box | GPU |
|---|---|---|---|
| Whisper (`ASR_MODEL`) | `small` | `base` | `medium` or `large-v3` |
| Ollama (subtitles only) | `qwen2.5:3b` or `gemma3:4b` | `qwen2.5:3b` | either |

Never use Whisper's `.en` models, which only know English. Avoid 1B Ollama models, which make up language codes. Whisper and Ollama don't need a key; if yours is behind a proxy that asks for one, Dubarr only sends it to an `https://` address, never follows redirects with it, and keeps it out of the logs and the browser.

**Removing a track by hand:** press *Check tracks* on the Dashboard, tick *Remove* on any audio or subtitle track, or subtitle file, and press *Remove ticked*. With Bazarr connected, each subtitle file says whether Bazarr downloaded it, and from where (OpenSubtitles.com, say).

**One library at a time:** with more than one library, pick one next to *Apply all* on the Dashboard to list and apply just its files.

Languages are set once for everything, and can be overridden per library, e.g. Japanese + English audio for an anime folder.

**Original language audio:** turn on *Always keep the original language audio* in Settings > General to keep a film's original audio alongside your languages, without listing every language films are made in. Dubarr asks Radarr or Sonarr for the original language, or failing that uses an audio track flagged as the original. If neither says, Dubarr removes no audio languages from that file and lists it under *Original language unknown*. A film with none of your languages but with its original audio, like Japanese-only anime, isn't flagged: it's listed under *Original language only*, and other languages are still removed from it. It's also tagged `original-language-only` in Radarr, Sonarr and Jellyfin (change or turn off the tag in Settings > General), so you can filter by it there.

MKV files are rewritten with mkvmerge (keeps Dolby Vision, HDR10+, fonts, chapters and tags). MP4 files are rewritten with ffmpeg.

## Install on Unraid

1. Add the template from `unraid/dubarr.xml` (or search Community Apps for Dubarr once it's listed).
2. Map **Movies** and **TV** to your library folders, read/write. Map the same folders Radarr and Sonarr use, so the paths match.
3. Start it, open the web UI (port 9892), and create your login with the setup code from the container's log.
4. Check **Settings > Libraries** and **Settings > General** (languages), then press **Scan now** on the Dashboard.

## Install with Docker

```bash
docker run -d --name dubarr \
  -p 9892:9892 \
  -v /path/to/appdata/dubarr:/config \
  -v /path/to/movies:/movies \
  -v /path/to/tv:/tv \
  ghcr.io/p1zzm1ssile/dubarr:latest
```

Every setting is in the web UI. To pre-fill them on first start, see `.env.tmpl` for the environment variables.

## Radarr, Sonarr, Bazarr and Jellyfin

All optional, under Settings:

- **Bazarr:** after a change, Bazarr looks at the film again straight away, and searches for subtitles if the change left none in your languages. Dubarr also warns when a Bazarr language profile downloads a language Dubarr removes. Let Bazarr be the only thing downloading subtitles: in each Jellyfin library, untick every language under **Subtitle downloads**.

- **Only Bazarr's subtitles:** Settings > Bazarr > *Subtitle files Bazarr didn't download* can list subtitle files beside your films that Bazarr didn't download (ones that came with the release, say), for you to remove from the Dashboard, or remove them by themselves. Bazarr's history says which are its own; anything Bazarr doesn't look after is left alone.

- **Rescans:** add the address and API key, and Dubarr asks them to rescan each file it changes.
- **Check new imports straight away:** in Radarr or Sonarr, Settings > Connect > + > Webhook, tick *On Import* and *On Upgrade*, method POST, and paste the address shown on Dubarr's Radarr or Sonarr settings tab. Without it, new files are picked up at the next scheduled scan.
- **Different folders:** if Radarr sees `/data/media/movies` where Dubarr sees `/movies`, set the folder mapping to `/data/media/movies=/movies`.

## Good to know

- **Where originals go:** a hidden `.dubarr-recycle` folder at the top of each library, so moving them is instant on the same disk. You can set your own folder instead; on another disk, moving is a full copy.
- **Restored files are left alone.** After a restore, Dubarr won't strip that file again until you press *Stop leaving alone* on the Dashboard.
- **Array writes:** rewriting a big remux on a parity array takes a while. Set the hours Dubarr may change files (e.g. 1 to 7 overnight) and how many files it changes at a time.
- **CPU and disk:** Dubarr runs ffmpeg and mkvmerge at low priority by default, so a Jellyfin transcode always comes first. With films spread over several disks, raise **Workers** to change more at once; Dubarr never puts two workers on the same disk.
- **Progress:** scans and batches show how far along they are and roughly how long is left. For changes the estimate is based on file sizes, so it settles after the first few files.
- **Interrupted changes are recovered.** If the container stops in the middle of a swap, Dubarr finishes or undoes it on the next start.
- **Forgotten password:** delete `auth.json` from the config folder and restart.
- **Keep it on your home network.** Use a VPN such as WireGuard or Tailscale for remote access, rather than a reverse proxy.

## Development

```bash
pip install -r src/requirements.txt pytest
sudo apt install ffmpeg mkvtoolnix   # the tests make real MKV and MP4 files
python -m pytest tests
cd src && CONFIG_DIR=../config LIBRARIES=/path/to/test/library python main.py
```

## Contact

Bugs and ideas: [open an issue](https://github.com/p1zzm1ssile/Dubarr/issues/new?title=%5BDubarr%5D%20). For anything private, email [p1zzm1ssilerepo@gmail.com](mailto:p1zzm1ssilerepo@gmail.com?subject=Dubarr). Both are also on the Contact page in the web UI.

## Free forever

Every feature in Dubarr is free and always will be. There are no paid tiers, no Pro account, no subscriptions and no telemetry, and new features will be free too. Services Dubarr works with, like Radarr, Sonarr, Bazarr and Jellyfin, have their own terms.

## About the developer

I'm an ISO 27001 Lead Auditor with combined-type ADHD, dyslexia and dyscalculia. I can read code and follow its logic and security well, but writing it out myself isn't possible. So I design what Dubarr does and how it works, and use AI as an assistive tool to write the code from that design. I review and test every change before it's released.

## Licence

[MIT](LICENSE). The Docker image also contains ffmpeg and MKVToolNix, which come under their own licences.

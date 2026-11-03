# Setting up Dubarr on Unraid

This guide is for an Unraid server laid out the way the [TRaSH guides](https://trash-guides.info/File-and-Folder-Structure/) suggest, with or without a cache pool. Following it avoids the two problems that hurt most on a first run: cleaned films piling up on the cache SSD, and the recycle folders growing to several terabytes.

## 1. The folder layout

The TRaSH guides put downloads and media in one share, usually called `data`, so the Arrs can hardlink a finished download into the library instead of copying it:

```
/mnt/user/data
├── torrents/      (or usenet/)
└── media/
    ├── movies/
    ├── tv/
    └── anime/     (if you keep anime apart from TV)
```

Radarr and Sonarr map the whole share, `/mnt/user/data` to `/data`, so they see `/data/media/movies`, `/data/media/tv` and `/data/media/anime`. Dubarr only needs the media folders, one mapping each.

Dubarr doesn't need the torrents folder. It tells a file is still seeding because it's hardlinked, and leaves it until the torrent is removed (Settings > General > *Change files that are still seeding* is off by default).

## 2. Map the libraries through /mnt/user0 if the share has a cache pool

| Container path | Host path in the template |
|---|---|
| `/movies` | `/mnt/user0/data/media/movies` |
| `/tv` | `/mnt/user0/data/media/tv` |
| `/anime` | `/mnt/user0/data/media/anime` |
| `/config` | `/mnt/user/appdata/dubarr` |

**Why `/mnt/user0`:** Dubarr writes each cleaned copy next to the original, then swaps them. Through `/mnt/user`, Unraid creates new files on the share's primary storage, so on a share whose primary storage is a cache pool every cleaned film lands on the SSD, while its original goes to the recycle folder on the array disk. The mover then has to copy every film back, which is slow while Dubarr is also writing. On one server this left 680 GB of cleaned films on a 1 TB SSD and slowed the mover from 168 MB/s to 30 MB/s. `/mnt/user0` is Unraid's view of the same folders on the array disks only, so new copies are written straight to the array.

Good to know:

- **Only the host path changes.** Keep the container paths (`/movies`, `/tv`, `/anime`) as they are. Radarr and Sonarr keep using `/mnt/user/data`; nothing changes for them.
- **New downloads wait for the mover.** A file that's still on the cache isn't visible under `/mnt/user0` yet. Dubarr checks it at the first scan after the mover has run. That's no loss: changing it on the cache would only give the mover more to do.
- **No cache pool on the media share?** Then `/mnt/user` and `/mnt/user0` are the same, and either works.
- **Never map a pool directly** (`/mnt/cache/...` or `/mnt/<pool>/...`), or Dubarr only sees the films that happen to be on it.

## 3. Folder mappings for Radarr and Sonarr

Radarr and Sonarr see `/data/media/...`, Dubarr sees `/movies`, `/tv` and `/anime`, so tell Dubarr how they match. Without this, Dubarr can't ask them to rescan a changed file, find a film's original language, or replace a file with no audio.

| Where | Folder mapping |
|---|---|
| Settings > Radarr | `/data/media/movies=/movies` |
| Settings > Sonarr | `/data/media/tv=/tv, /data/media/anime=/anime` |

Sonarr usually has two root folders, TV and anime, so it needs both mappings, separated by a comma. If one is missing, Dubarr pops up a note after a scan with the mapping it suggests. Jellyfin and Bazarr have their own Folder mapping in the same way, if their paths differ too.

## 4. Libraries and languages

In Settings > Libraries, check that `/movies`, `/tv` and `/anime` are listed. The template's folders are added on first start; if you add the Anime mount to an existing install, add `/anime` here yourself. Set your languages once in Settings > General. For anime, either override the anime library's languages (e.g. Japanese + English audio, English subtitles) or turn on *Always keep the original language audio*, which keeps the Japanese audio without listing it everywhere.

## 5. The first pass: plan for the recycle folder

Dubarr keeps every original in a recycle folder for the days you choose, so you can restore it. On a big library the first pass changes a lot in a short time, and the originals add up fast: one server rewrote 6.5 TB in 48 hours, and with *Keep originals for* set to 7 days the recycle folders reached 7.7 TB, 3.1 TB of it on one disk.

Before turning *Report only* off:

1. **Let a scan finish in Report only**, and look at how much *Will be changed* covers. That's roughly how much will pass through the recycle folders.
2. **Set a size limit:** Settings > General > *Most space originals may take (GB)*. Past it, the oldest originals are emptied first, whatever their age. As one disk can hold most of the recycle folder, choose less than the free space on your fullest disk.
3. **Keep fewer days for the first pass**, e.g. 1 or 2, and put it back to 7 once the backlog is done.
4. **Spread it out:** *Only change files from / Until* (e.g. 1 to 7 overnight), *Most files changed at a time*, and one library at a time from the Dashboard all slow the pass down to what your disks and free space can take.
5. **Check a few films** after the first batch (play them, look at the tracks) before relying on it.

If the recycle folders do get too big, go to **History > Empty recycle folder now**. Choose *Every original* or *Older than N days*. It starts straight away, even while a scan or a batch of changes is running, and the log says how much it freed. Changing the days or the size limit in Settings also empties what they no longer keep straight away, and every daily run is logged, even when there's nothing to empty.

## 6. Array speed

- **Turbo write** (Settings > Disk Settings > *Tunable (md_write_method)*: reconstruct write) speeds up writes to a parity array a lot while Dubarr is busy, at the cost of spinning up every disk.
- **Workers:** with films spread over several disks, raise Workers to change more at once. Dubarr never puts two workers on the same disk.
- **Low priority** is on by default, so a Jellyfin transcode always comes first.
- The log says how fast each film was changed (e.g. "took 6.2 min at 140 MB/s"). Much below 100 MB/s on a modern disk usually means parity writes or the mover are in the way.

## Checklist

- [ ] Media share laid out as `data/media/{movies,tv,anime}`
- [ ] Movies, TV and Anime mapped through `/mnt/user0` if the share has a cache pool
- [ ] Folder mappings set in Settings > Radarr and Settings > Sonarr (both TV and anime)
- [ ] Languages set, anime library overridden or *Always keep the original language audio* on
- [ ] Report only scan checked, recycle size limit and days set for the first pass
- [ ] Hours and batch size set so the first pass doesn't swamp the array

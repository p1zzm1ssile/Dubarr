"""Decide what to do with one file. No disk access here, so every rule can be tested.

The rules:
  * Keep video, fonts, chapters and anything that isn't audio or subtitles.
  * Keep audio in a wanted language. Remove the rest.
  * If no audio is in a wanted language, change nothing and warn.
  * If any audio track has no language tag, change nothing and warn:
    there's no way to know it isn't the one you want.
  * With keep_original_audio, audio in the film's original language is kept too, as if it were wanted.
    The original language comes from Radarr or Sonarr, or failing that from a track flagged as the
    original. If neither says, no audio is removed for its language, since it might be the original.
    A file with none of your languages but with original-language audio isn't a warning: it's listed as
    original language only, and other languages are removed from it as usual.
  * Commentary tracks are removed too, unless that's turned off: commentary
    audio and the subtitles of the commentary. The last non-commentary audio
    in a wanted language is never removed.
  * Keep subtitles in a wanted language, including forced ones. Remove the rest.
    Subtitles with no language tag are always kept, and flagged so they can be tagged.
  * Subtitle files beside the video (Film.fr.srt) in languages you don't keep are
    removed too. That's just a file delete, no rewrite. Files whose name doesn't
    say a language are kept.
  * With bazarr_files (the subtitle files Bazarr downloaded, for a film Bazarr knows),
    every other subtitle file beside the video is removed, whatever its language.
  * Duplicates (both can be turned off), picked for Jellyfin:
      audio: keep the best-quality track per wanted language. Jellyfin converts
      audio on the fly for devices that can't play it, which is cheap, so a
      lower-quality copy isn't needed.
      subtitles: keep one full track per wanted language, plus the forced one.
      Text beats pictures (Jellyfin shows text without transcoding the video),
      normal beats SDH.
"""

import posixpath
from dataclasses import dataclass, field

from core.probe import MediaInfo
from utils.languages import MULTIPLE, NO_LANGUAGE, UNDEFINED, describe

# What Dubarr found, from the most to the least urgent
CHANGE = "change"                  # has unwanted tracks to remove
NO_WANTED_AUDIO = "no_wanted_audio"  # warning: none of the audio is in a wanted language
UNKNOWN_AUDIO = "unknown_audio"    # warning: an audio track isn't tagged with a language
NO_AUDIO = "no_audio"              # the file has no audio at all
UNKNOWN_SUBTITLES = "unknown_subtitles"  # nothing to remove, but a subtitle track isn't tagged
UNKNOWN_ORIGINAL = "unknown_original"  # audio is kept only because the original language isn't known
ORIGINAL_ONLY = "original_only"    # none of your languages, but the original is there and kept (Japanese-only anime)
CLEAN = "clean"                    # only wanted tracks, nothing to do

NOT_FROM_BAZARR = "Not downloaded by Bazarr"  # the reason given for a subtitle file Bazarr didn't download

WARNINGS = (NO_WANTED_AUDIO, UNKNOWN_AUDIO, NO_AUDIO, UNKNOWN_SUBTITLES, UNKNOWN_ORIGINAL)


@dataclass
class Rules:
    audio_languages: list
    subtitle_languages: list
    strip_audio: bool = True
    strip_subtitles: bool = True
    strip_commentary: bool = True
    one_audio_per_language: bool = True
    one_subtitle_per_language: bool = True
    assume_unlabelled_is_full: bool = False
    strip_subtitle_files: bool = True
    prefer_text_files: bool = False
    keep_original_audio: bool = False
    original_language: str = None  # for this file, from Radarr or Sonarr; None if they don't know
    bazarr_files: frozenset = None  # subtitle files Bazarr downloaded, to keep only those; None to not check
    mux_subtitle_files: bool = False  # put subtitle files beside the film, in languages kept, into it


def original_language(info: MediaInfo, rules: Rules):
    """The film's original language: from Radarr or Sonarr, or else from audio flagged as the original.
    None if neither says, or the flagged tracks disagree."""
    if rules.original_language and rules.original_language not in (UNDEFINED, MULTIPLE, NO_LANGUAGE):
        return rules.original_language
    flagged = _languages([t for t in info.of("audio") if t.original and t.language not in (UNDEFINED, MULTIPLE)])
    return flagged[0] if len(flagged) == 1 else None


@dataclass
class Plan:
    status: str
    message: str
    remove: list = field(default_factory=list)   # tracks to remove, as dicts with a "reason"
    keep: list = field(default_factory=list)     # indexes of the tracks that stay
    default_audio: int = None                    # index to mark as the default audio track, if it needs changing
    mark_forced: list = field(default_factory=list)  # subtitle tracks that are forced but not flagged as such
    saved_bytes: int = 0                         # roughly how much smaller the file gets (0 if unknown)
    remove_files: list = field(default_factory=list)  # subtitle files beside the video to remove
    original_only: str = None                    # the original language, when it's the only audio kept
    add_files: list = field(default_factory=list)  # subtitle files beside the video to put into it
    can_add: list = field(default_factory=list)    # subtitle files that could go in, if you choose to

    @property
    def rewrites(self):
        """Whether tracks come out of or go into the video itself, which means writing a new copy of it."""
        return bool(self.remove or self.add_files)

    @property
    def changes(self):
        return self.status == CHANGE

    def to_dict(self):
        return {
            "status": self.status, "message": self.message, "remove": self.remove,
            "keep": self.keep, "default_audio": self.default_audio, "saved_bytes": self.saved_bytes,
            "mark_forced": self.mark_forced, "remove_files": self.remove_files, "original_only": self.original_only,
            "add_files": self.add_files, "can_add": self.can_add,
        }


def mergeable(info, rules, skip=(), removed=()):
    """The text subtitle files beside a film that could go into it: in a language you keep, going by the name or,
    when the name doesn't say, by its text (guessed is then True), and not already inside."""
    subtitles = [t for t in info.tracks if t.type == "subtitle" and t.index not in removed]
    inside = {(t.language, t.forced) for t in subtitles}
    found = []
    for f in info.external:
        language = f["language"] if f["language"] != UNDEFINED else f.get("guess")
        if (f["path"] in skip or not f["text"] or language not in rules.subtitle_languages
                or (language, f["forced"]) in inside):
            continue
        found.append({**f, "language": language, "guessed": f["language"] == UNDEFINED})
        inside.add((language, f["forced"]))
    return found


def _languages(tracks):
    seen = []
    for t in tracks:
        if t.language not in seen:
            seen.append(t.language)
    return seen


def _file_languages(files):
    seen = []
    for f in files:
        if f["language"] not in seen:
            seen.append(f["language"])
    return seen


def _files_text(remove_files):
    """["2 French subtitle files", "1 subtitle file Bazarr didn't download"]"""
    parts = []
    by_language = [f for f in remove_files if f["reason"] != NOT_FROM_BAZARR]
    if by_language:
        n = len(by_language)
        parts.append(f"{n} {describe(_file_languages(by_language))} subtitle file{'s' if n > 1 else ''}")
    n = len(remove_files) - len(by_language)
    if n:
        parts.append(f"{n} subtitle file{'s' if n > 1 else ''} Bazarr didn't download")
    return parts


def _join(parts):
    return ", ".join(parts[:-1]) + " and " + parts[-1] if len(parts) > 1 else parts[0]


def _removal(track, reason):
    return {"index": track.index, "type": track.type, "language": track.language, "codec": track.codec,
            "title": track.title, "forced": track.forced, "size": track.size, "reason": reason}


# Higher is better. Lossless first, then by how much of the original sound survives.
AUDIO_CODECS = {"truehd": 9, "mlp": 9, "flac": 8, "pcm": 8, "alac": 8, "dts": 6, "eac3": 5, "ac3": 4,
                "opus": 3, "vorbis": 3, "aac": 3, "mp3": 2, "mp2": 1}
# Lower is better for Jellyfin: plain text it can show on any device, then styled text, then pictures
SUBTITLE_CODECS = {"subrip": 0, "srt": 0, "mov_text": 1, "webvtt": 1, "text": 1, "tx3g": 1, "ass": 2, "ssa": 2}
PICTURE_SUBTITLES = 5


def audio_quality(t):
    codec = t.codec.lower()
    if codec.startswith("pcm"):
        codec = "pcm"
    score = AUDIO_CODECS.get(codec, 0)
    profile = t.profile.lower()
    if codec == "dts":
        if "hd ma" in profile or "dts:x" in profile:
            score = 8  # DTS-HD Master Audio and DTS:X are lossless
        elif "hd" in profile:
            score = 7
    if "atmos" in profile or "joc" in profile:
        score += 0.5
    return (score, t.channels, t.size)


def best_audio(tracks):
    """The track to keep: not a description for the blind, then the best sound."""
    return max(tracks, key=lambda t: (not t.visual_impaired, audio_quality(t), -t.index))


def _length(track, same_language):
    """How much a subtitle track holds: its number of lines, or failing that its size. None if unknown."""
    if all(t.frames for t in same_language):
        return track.frames
    # Sizes only compare like with like: text subtitles are a fraction of the size of picture ones
    is_text = {t.codec.lower() in SUBTITLE_CODECS for t in same_language}
    if all(t.size for t in same_language) and len(is_text) == 1:
        return track.size
    return None


def likely_forced(track, same_language):
    """Forced subtitles only cover foreign-language scenes, so they're a fraction of the length of the full ones,
    even when nobody flagged them."""
    if track.forced:
        return True
    length = _length(track, same_language)
    most = max(_length(t, same_language) for t in same_language)
    return bool(length and most and length < most * 0.2)


def subtitle_rank(t):
    """Lower is better."""
    return (SUBTITLE_CODECS.get(t.codec.lower(), PICTURE_SUBTITLES), t.hearing_impaired, not t.default, t.index)


def _duplicates(tracks, languages, removing, pick):
    """For each language, the tracks to remove so only pick(tracks) stay."""
    extra = []
    for code in languages:
        same = [t for t in tracks if t.language == code and t.index not in removing and not t.commentary]
        if len(same) > 1:
            keep = pick(same)
            extra += [t for t in same if t not in keep]
    return extra


def _subtitles_to_keep(same, assume_unlabelled_is_full=False):
    if _length(same[0], same) is None:
        # Without line counts or sizes an unflagged forced track can't be told apart, so keep everything,
        # unless you've chosen to trust the labels: next to an SDH track, an unlabelled one is the full subtitles
        labelled_sdh = [t for t in same if t.hearing_impaired]
        if not (assume_unlabelled_is_full and labelled_sdh and len(labelled_sdh) < len(same)):
            return same
        forced = [t for t in same if t.forced]
    else:
        forced = [t for t in same if likely_forced(t, same)]
    full = [t for t in same if t not in forced]
    keep = [min(full, key=subtitle_rank)] if full else []
    if forced:
        keep.append(min(forced, key=subtitle_rank))
    return keep


def decide(info: MediaInfo, rules: Rules, tracks: bool = True) -> Plan:
    """What to do with a file. With tracks=False only subtitle files beside it are considered, for when
    rewriting the video has to wait (still seeding, or not worth it) but the files can go now."""
    audio = info.of("audio")
    subtitles = info.of("subtitle")
    every = [t.index for t in info.tracks]

    if not audio:
        return Plan(NO_AUDIO, "No audio tracks in this file", keep=every)

    untagged = [t for t in audio if t.language == UNDEFINED]
    if untagged:
        others = [t for t in audio if t.language != UNDEFINED]
        extra = f", plus {describe(_languages(others))}" if others else ""
        count = f"{len(untagged)} audio track{'s have' if len(untagged) > 1 else ' has'}"
        return Plan(UNKNOWN_AUDIO, f"{count} no language tag{extra}. Check it by hand", keep=every)

    # The original language is kept alongside yours. When it isn't known, any language might be it
    keep_audio = list(rules.audio_languages)
    original = original_language(info, rules) if rules.keep_original_audio else None
    unsure = rules.keep_original_audio and not original
    if original and original not in keep_audio:
        keep_audio.append(original)

    # "Multiple languages" might well include yours, so it counts as wanted
    wanted_audio = [t for t in audio if t.language in rules.audio_languages or t.language == MULTIPLE]
    original_only = None  # e.g. Japanese-only anime: nothing in your languages, but the original is there
    if not wanted_audio and original:
        wanted_audio = [t for t in audio if t.language == original]
        if wanted_audio:
            original_only = f"Only {describe([original])} audio, the original language"
    if not wanted_audio:
        return Plan(NO_WANTED_AUDIO,
                    f"No {describe(rules.audio_languages)} audio, only {describe(_languages(audio))}",
                    keep=every)

    remove = []
    kept_unsure = []  # languages kept only because the original language isn't known
    if tracks and rules.strip_audio:
        for t in audio:
            if t in wanted_audio or t.language in keep_audio or t.language == NO_LANGUAGE:
                continue
            if unsure:
                if t.language not in kept_unsure:
                    kept_unsure.append(t.language)
                continue
            remove.append(_removal(t, "Unwanted audio language"))
    unsure_note = (f"Keeps {describe(kept_unsure)} audio, as the original language isn't known"
                   if kept_unsure else "")

    # Commentary goes too, as long as a proper wanted-language track stays
    if tracks and rules.strip_commentary and any(not t.commentary for t in wanted_audio):
        removing = {r["index"] for r in remove}
        for t in audio:
            if t.commentary and t.index not in removing:
                remove.append(_removal(t, "Commentary"))

    if tracks and rules.strip_subtitles:
        for t in subtitles:
            if t.language in rules.subtitle_languages or t.language in (UNDEFINED, MULTIPLE, NO_LANGUAGE):
                continue
            remove.append(_removal(t, "Unwanted subtitle language"))

    # Subtitles of a commentary are no use once the commentary is gone
    if tracks and rules.strip_commentary:
        removing = {r["index"] for r in remove}
        for t in subtitles:
            if t.commentary and t.index not in removing:
                remove.append(_removal(t, "Commentary subtitles"))

    # Duplicates in the languages you keep
    if tracks and rules.one_audio_per_language:
        removing = {r["index"] for r in remove}
        for t in _duplicates(audio, keep_audio, removing, lambda same: [best_audio(same)]):
            remove.append(_removal(t, "Duplicate audio"))
    if tracks and rules.one_subtitle_per_language:
        removing = {r["index"] for r in remove}
        pick = lambda same: _subtitles_to_keep(same, rules.assume_unlabelled_is_full)
        for t in _duplicates(subtitles, rules.subtitle_languages, removing, pick):
            remove.append(_removal(t, "Duplicate subtitles"))

    # Picture subtitles are redundant once a text version sits beside the film: Jellyfin shows text without
    # transcoding the video. Forced tracks stay, as a full subtitle file doesn't replace them.
    if tracks and rules.prefer_text_files:
        removing = {r["index"] for r in remove}
        text_files = {f["language"] for f in info.external if f["text"] and not f["forced"]}
        for t in subtitles:
            if (t.index not in removing and t.language in text_files and t.language in rules.subtitle_languages
                    and not t.forced and t.codec.lower() not in SUBTITLE_CODECS):
                remove.append(_removal(t, "Picture subtitles, with a text file beside the film"))

    # Subtitle files beside the film
    remove_files = []
    if rules.strip_subtitle_files:
        for f in info.external:
            if f["language"] not in rules.subtitle_languages and f["language"] not in (UNDEFINED, MULTIPLE, NO_LANGUAGE):
                remove_files.append({**f, "reason": "Unwanted subtitle file"})
    if rules.bazarr_files is not None:
        removing = {f["path"] for f in remove_files}
        for f in info.external:
            if f["path"] not in removing and posixpath.normpath(f["path"]) not in rules.bazarr_files:
                remove_files.append({**f, "reason": NOT_FROM_BAZARR})

    # Text subtitle files in a language you keep go into the film, unless it already has that language inside
    add_files = []
    if tracks and rules.mux_subtitle_files:
        removing_files = {f["path"] for f in remove_files}
        removed = {r["index"] for r in remove}
        add_files = mergeable(info, rules, skip=removing_files, removed=removed)

    untagged_subs = [t for t in subtitles if t.language == UNDEFINED]
    # A subtitle file whose name doesn't say its language is untagged too: naming it settles it
    handled = {f["path"] for f in remove_files + add_files}
    unnamed = [f for f in info.external if f["language"] == UNDEFINED and f["path"] not in handled]
    if not remove and not remove_files and not add_files:
        if untagged_subs or unnamed:
            said = []
            if untagged_subs:
                count = len(untagged_subs)
                said.append(f"{count} subtitle track{'s have' if count > 1 else ' has'} no language tag")
            if unnamed:
                count = len(unnamed)
                said.append(f"{count} subtitle file{'s' if count > 1 else ''} beside it "
                            f"{'don' if count > 1 else 'doesn'}'t say {'their' if count > 1 else 'its'} language in the name")
            count = len(untagged_subs) + len(unnamed)
            return Plan(UNKNOWN_SUBTITLES,
                        f"{_join(said)}. Choose {'their languages' if count > 1 else 'its language'} so Dubarr knows "
                        f"whether to keep {'them' if count > 1 else 'it'}",
                        keep=every)
        if unsure_note:
            return Plan(UNKNOWN_ORIGINAL, unsure_note, keep=every)
        if original_only:
            return Plan(ORIGINAL_ONLY, original_only, keep=every, original_only=original)
        return Plan(CLEAN, "Only wanted languages", keep=every)

    removed = {r["index"] for r in remove}
    keep = [i for i in every if i not in removed]
    if not remove and not add_files:
        message = "Remove " + _join(_files_text(remove_files))
        return Plan(CHANGE, message + (f". {original_only}" if original_only else ""),
                    keep=keep, remove_files=remove_files, original_only=original if original_only else None)

    # Flag forced subtitles that nobody flagged, so Jellyfin shows them for foreign-language scenes by itself
    mark_forced = []
    for code in rules.subtitle_languages:
        same = [t for t in subtitles if t.language == code and t.index not in removed and not t.commentary]
        if len(same) > 1 and _length(same[0], same) is not None:
            mark_forced += [t.index for t in same if not t.forced and likely_forced(t, same)]

    # If the default audio track is being removed, make the best remaining one the default,
    # so players don't fall back to whatever comes first
    default_audio = None
    kept_audio = [t for t in audio if t.index not in removed]
    if not any(t.default for t in kept_audio):
        order = {code: n for n, code in enumerate(keep_audio)}
        best = min(kept_audio, key=lambda t: (t.commentary, order.get(t.language, len(order)), t.index))
        default_audio = best.index

    reasons = {r["index"]: r["reason"] for r in remove}
    removed_audio = _languages([t for t in audio if reasons.get(t.index) == "Unwanted audio language"])
    removed_subs = _languages([t for t in subtitles if reasons.get(t.index) == "Unwanted subtitle language"])
    commentaries = sum(1 for t in audio if reasons.get(t.index) == "Commentary")
    commentary_subs = sum(1 for t in subtitles if reasons.get(t.index) == "Commentary subtitles")
    duplicate_audio = _languages([t for t in audio if reasons.get(t.index) == "Duplicate audio"])
    duplicate_subs = _languages([t for t in subtitles if reasons.get(t.index) == "Duplicate subtitles"])
    parts = []
    if removed_audio:
        parts.append(f"{describe(removed_audio)} audio")
    if commentaries:
        parts.append(f"{commentaries} commentary track{'s' if commentaries > 1 else ''}")
    if commentary_subs:
        parts.append(f"{commentary_subs} commentary subtitle track{'s' if commentary_subs > 1 else ''}")
    if removed_subs:
        parts.append(f"{describe(removed_subs)} subtitles")
    if duplicate_audio:
        n = sum(1 for r in remove if r["reason"] == "Duplicate audio")
        parts.append(f"{n} extra {describe(duplicate_audio)} audio track{'s' if n > 1 else ''}")
    if duplicate_subs:
        n = sum(1 for r in remove if r["reason"] == "Duplicate subtitles")
        parts.append(f"{n} extra {describe(duplicate_subs)} subtitle track{'s' if n > 1 else ''}")
    picture = sum(1 for r in remove if r["reason"].startswith("Picture subtitles"))
    if picture:
        parts.append(f"{picture} picture subtitle track{'s' if picture > 1 else ''} (a text file replaces "
                     f"{'them' if picture > 1 else 'it'})")
    parts += _files_text(remove_files)
    sentences = ["Remove " + _join(parts)] if parts else []
    if add_files:
        n = len(add_files)
        sentences.append(f"Put the {describe(_file_languages(add_files))} subtitle file{'s' if n > 1 else ''} "
                         "beside it into the film")
    if unsure_note:
        sentences.append(unsure_note)
    if original_only:
        sentences.append(original_only)
    return Plan(CHANGE, ". ".join(sentences), remove=remove, keep=keep,
                default_audio=default_audio, mark_forced=mark_forced, saved_bytes=sum(r["size"] for r in remove),
                remove_files=remove_files, original_only=original if original_only else None, add_files=add_files)

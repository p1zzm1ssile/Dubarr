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
  * Commentary tracks are removed too, unless that's turned off: commentary
    audio and the subtitles of the commentary. The last non-commentary audio
    in a wanted language is never removed.
  * Keep subtitles in a wanted language, including forced ones. Remove the rest.
    Subtitles with no language tag are always kept, and flagged so they can be tagged.
  * Subtitle files beside the video (Film.fr.srt) in languages you don't keep are
    removed too. That's just a file delete, no rewrite. Files whose name doesn't
    say a language are kept.
  * Duplicates (both can be turned off), picked for Jellyfin:
      audio: keep the best-quality track per wanted language. Jellyfin converts
      audio on the fly for devices that can't play it, which is cheap, so a
      lower-quality copy isn't needed.
      subtitles: keep one full track per wanted language, plus the forced one.
      Text beats pictures (Jellyfin shows text without transcoding the video),
      normal beats SDH.
"""

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
CLEAN = "clean"                    # only wanted tracks, nothing to do

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

    @property
    def rewrites(self):
        """Whether tracks come out of the video itself, which means writing a new copy of it."""
        return bool(self.remove)

    @property
    def changes(self):
        return self.status == CHANGE

    def to_dict(self):
        return {
            "status": self.status, "message": self.message, "remove": self.remove,
            "keep": self.keep, "default_audio": self.default_audio, "saved_bytes": self.saved_bytes,
            "mark_forced": self.mark_forced, "remove_files": self.remove_files,
        }


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

    # "Multiple languages" might well include yours, so it counts as wanted
    wanted_audio = [t for t in audio if t.language in rules.audio_languages or t.language == MULTIPLE]
    if not wanted_audio:
        return Plan(NO_WANTED_AUDIO,
                    f"No {describe(rules.audio_languages)} audio, only {describe(_languages(audio))}",
                    keep=every)

    # The original language is kept alongside yours. When it isn't known, any language might be it
    keep_audio = list(rules.audio_languages)
    unsure = False
    if rules.keep_original_audio:
        original = original_language(info, rules)
        if original and original not in keep_audio:
            keep_audio.append(original)
        unsure = not original

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

    untagged_subs = [t for t in subtitles if t.language == UNDEFINED]
    if not remove and not remove_files:
        if untagged_subs:
            count = len(untagged_subs)
            return Plan(UNKNOWN_SUBTITLES,
                        f"{count} subtitle track{'s have' if count > 1 else ' has'} no language tag. Tag "
                        f"{'them' if count > 1 else 'it'} so Dubarr knows whether to keep {'them' if count > 1 else 'it'}",
                        keep=every)
        if unsure_note:
            return Plan(UNKNOWN_ORIGINAL, unsure_note, keep=every)
        return Plan(CLEAN, "Only wanted languages", keep=every)

    removed = {r["index"] for r in remove}
    keep = [i for i in every if i not in removed]
    if not remove:
        n = len(remove_files)
        message = f"Remove {n} {describe(_file_languages(remove_files))} subtitle file{'s' if n > 1 else ''}"
        return Plan(CHANGE, message + (f". {unsure_note}" if unsure_note else ""),
                    keep=keep, remove_files=remove_files)

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
    if remove_files:
        n = len(remove_files)
        parts.append(f"{n} {describe(_file_languages(remove_files))} subtitle file{'s' if n > 1 else ''}")
    text = ", ".join(parts[:-1]) + " and " + parts[-1] if len(parts) > 1 else parts[0]
    if unsure_note:
        text += f". {unsure_note}"
    return Plan(CHANGE, "Remove " + text, remove=remove, keep=keep,
                default_audio=default_audio, mark_forced=mark_forced, saved_bytes=sum(r["size"] for r in remove),
                remove_files=remove_files)

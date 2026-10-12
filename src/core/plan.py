"""Decide what to do with one file. No disk access here, so every rule can be tested.

The rules:
  * Keep video, fonts, chapters and anything that isn't audio or subtitles.
  * Keep audio in a wanted language. Remove the rest.
  * If no audio is in a wanted language, change nothing and warn.
  * If any audio track has no language tag, change nothing and warn:
    there's no way to know it isn't the one you want.
  * Commentary tracks are removed too, unless that's turned off. The last
    non-commentary audio in a wanted language is never removed.
  * Keep subtitles in a wanted language, including forced ones. Remove the rest.
    Subtitles with no language tag are always kept, and flagged so they can be tagged.
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
CLEAN = "clean"                    # only wanted tracks, nothing to do

WARNINGS = (NO_WANTED_AUDIO, UNKNOWN_AUDIO, NO_AUDIO, UNKNOWN_SUBTITLES)


@dataclass
class Rules:
    audio_languages: list
    subtitle_languages: list
    strip_audio: bool = True
    strip_subtitles: bool = True
    strip_commentary: bool = True


@dataclass
class Plan:
    status: str
    message: str
    remove: list = field(default_factory=list)   # tracks to remove, as dicts with a "reason"
    keep: list = field(default_factory=list)     # indexes of the tracks that stay
    default_audio: int = None                    # index to mark as the default audio track, if it needs changing
    saved_bytes: int = 0                         # roughly how much smaller the file gets (0 if unknown)

    @property
    def changes(self):
        return self.status == CHANGE

    def to_dict(self):
        return {
            "status": self.status, "message": self.message, "remove": self.remove,
            "keep": self.keep, "default_audio": self.default_audio, "saved_bytes": self.saved_bytes,
        }


def _languages(tracks):
    seen = []
    for t in tracks:
        if t.language not in seen:
            seen.append(t.language)
    return seen


def _removal(track, reason):
    return {"index": track.index, "type": track.type, "language": track.language, "codec": track.codec,
            "title": track.title, "forced": track.forced, "size": track.size, "reason": reason}


def decide(info: MediaInfo, rules: Rules) -> Plan:
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

    remove = []
    if rules.strip_audio:
        for t in audio:
            if t in wanted_audio or t.language == NO_LANGUAGE:
                continue
            remove.append(_removal(t, "Unwanted audio language"))

    # Commentary goes too, as long as a proper wanted-language track stays
    if rules.strip_commentary and any(not t.commentary for t in wanted_audio):
        removing = {r["index"] for r in remove}
        for t in audio:
            if t.commentary and t.index not in removing:
                remove.append(_removal(t, "Commentary"))

    if rules.strip_subtitles:
        for t in subtitles:
            if t.language in rules.subtitle_languages or t.language in (UNDEFINED, MULTIPLE, NO_LANGUAGE):
                continue
            remove.append(_removal(t, "Unwanted subtitle language"))

    untagged_subs = [t for t in subtitles if t.language == UNDEFINED]
    if not remove:
        if untagged_subs:
            count = len(untagged_subs)
            return Plan(UNKNOWN_SUBTITLES,
                        f"{count} subtitle track{'s have' if count > 1 else ' has'} no language tag. Tag "
                        f"{'them' if count > 1 else 'it'} so Dubarr knows whether to keep {'them' if count > 1 else 'it'}",
                        keep=every)
        return Plan(CLEAN, "Only wanted languages", keep=every)

    removed = {r["index"] for r in remove}
    keep = [i for i in every if i not in removed]

    # If the default audio track is being removed, make the best remaining one the default,
    # so players don't fall back to whatever comes first
    default_audio = None
    kept_audio = [t for t in audio if t.index not in removed]
    if not any(t.default for t in kept_audio):
        order = {code: n for n, code in enumerate(rules.audio_languages)}
        best = min(kept_audio, key=lambda t: (t.commentary, order.get(t.language, len(order)), t.index))
        default_audio = best.index

    removed_audio = _languages([t for t in audio if t.index in removed and not t.commentary])
    removed_subs = _languages([t for t in subtitles if t.index in removed])
    commentaries = sum(1 for t in audio if t.index in removed and t.commentary)
    parts = []
    if removed_audio:
        parts.append(f"{describe(removed_audio)} audio")
    if commentaries:
        parts.append(f"{commentaries} commentary track{'s' if commentaries > 1 else ''}")
    if removed_subs:
        parts.append(f"{describe(removed_subs)} subtitles")
    text = ", ".join(parts[:-1]) + " and " + parts[-1] if len(parts) > 1 else parts[0]
    return Plan(CHANGE, "Remove " + text, remove=remove, keep=keep,
                default_audio=default_audio, saved_bytes=sum(r["size"] for r in remove))

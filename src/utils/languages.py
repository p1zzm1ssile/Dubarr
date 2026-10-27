"""Language codes as they appear in video files.

Files tag tracks with ISO 639-2 codes, and some languages have two of those
("fre" and "fra" are both French). Everything is normalised to one code per
language, so "fre", "fra", "fr", "French" and "français" all match each other.
"""

# code -> (name, other codes and names that mean the same language)
LANGUAGES = {
    "eng": ("English", ["en", "english"]),
    "fra": ("French", ["fre", "fr", "french", "francais", "français"]),
    "deu": ("German", ["ger", "de", "german", "deutsch"]),
    "spa": ("Spanish", ["es", "spanish", "espanol", "español", "castilian"]),
    "ita": ("Italian", ["it", "italian", "italiano"]),
    "por": ("Portuguese", ["pt", "portuguese", "portugues", "português"]),
    "nld": ("Dutch", ["dut", "nl", "dutch", "flemish"]),
    "swe": ("Swedish", ["sv", "swedish"]),
    "nor": ("Norwegian", ["no", "nb", "nob", "nn", "nno", "norwegian"]),
    "dan": ("Danish", ["da", "danish"]),
    "fin": ("Finnish", ["fi", "finnish"]),
    "isl": ("Icelandic", ["ice", "is", "icelandic"]),
    "pol": ("Polish", ["pl", "polish"]),
    "ces": ("Czech", ["cze", "cs", "czech"]),
    "slk": ("Slovak", ["slo", "sk", "slovak"]),
    "slv": ("Slovenian", ["sl", "slovenian", "slovene"]),
    "hun": ("Hungarian", ["hu", "hungarian"]),
    "ron": ("Romanian", ["rum", "ro", "romanian", "moldavian"]),
    "bul": ("Bulgarian", ["bg", "bulgarian"]),
    "hrv": ("Croatian", ["hr", "croatian"]),
    "srp": ("Serbian", ["sr", "serbian"]),
    "bos": ("Bosnian", ["bs", "bosnian"]),
    "mkd": ("Macedonian", ["mac", "mk", "macedonian"]),
    "sqi": ("Albanian", ["alb", "sq", "albanian"]),
    "ell": ("Greek", ["gre", "el", "greek"]),
    "tur": ("Turkish", ["tr", "turkish"]),
    "rus": ("Russian", ["ru", "russian"]),
    "ukr": ("Ukrainian", ["uk", "ukrainian"]),
    "bel": ("Belarusian", ["be", "belarusian"]),
    "lit": ("Lithuanian", ["lt", "lithuanian"]),
    "lav": ("Latvian", ["lv", "latvian"]),
    "est": ("Estonian", ["et", "estonian"]),
    "kat": ("Georgian", ["geo", "ka", "georgian"]),
    "hye": ("Armenian", ["arm", "hy", "armenian"]),
    "aze": ("Azerbaijani", ["az", "azerbaijani"]),
    "kaz": ("Kazakh", ["kk", "kazakh"]),
    "uzb": ("Uzbek", ["uz", "uzbek"]),
    "ara": ("Arabic", ["ar", "arabic"]),
    "heb": ("Hebrew", ["he", "iw", "hebrew"]),
    "fas": ("Persian", ["per", "fa", "persian", "farsi"]),
    "kur": ("Kurdish", ["ku", "kurdish"]),
    "hin": ("Hindi", ["hi", "hindi"]),
    "urd": ("Urdu", ["ur", "urdu"]),
    "ben": ("Bengali", ["bn", "bengali", "bangla"]),
    "pan": ("Punjabi", ["pa", "punjabi", "panjabi"]),
    "guj": ("Gujarati", ["gu", "gujarati"]),
    "mar": ("Marathi", ["mr", "marathi"]),
    "tam": ("Tamil", ["ta", "tamil"]),
    "tel": ("Telugu", ["te", "telugu"]),
    "kan": ("Kannada", ["kn", "kannada"]),
    "mal": ("Malayalam", ["ml", "malayalam"]),
    "sin": ("Sinhala", ["si", "sinhala", "sinhalese"]),
    "nep": ("Nepali", ["ne", "nepali"]),
    "zho": ("Chinese", ["chi", "zh", "chinese", "mandarin", "cmn"]),
    "yue": ("Cantonese", ["cantonese"]),
    "jpn": ("Japanese", ["ja", "japanese"]),
    "kor": ("Korean", ["ko", "korean"]),
    "vie": ("Vietnamese", ["vi", "vietnamese"]),
    "tha": ("Thai", ["th", "thai"]),
    "ind": ("Indonesian", ["id", "indonesian"]),
    "msa": ("Malay", ["may", "ms", "malay"]),
    "fil": ("Filipino", ["tgl", "tl", "filipino", "tagalog"]),
    "khm": ("Khmer", ["km", "khmer"]),
    "lao": ("Lao", ["lo", "lao"]),
    "mya": ("Burmese", ["bur", "my", "burmese"]),
    "mon": ("Mongolian", ["mn", "mongolian"]),
    "swa": ("Swahili", ["sw", "swahili"]),
    "amh": ("Amharic", ["am", "amharic"]),
    "yor": ("Yoruba", ["yo", "yoruba"]),
    "zul": ("Zulu", ["zu", "zulu"]),
    "afr": ("Afrikaans", ["af", "afrikaans"]),
    "cat": ("Catalan", ["ca", "catalan"]),
    "eus": ("Basque", ["baq", "eu", "basque"]),
    "glg": ("Galician", ["gl", "galician"]),
    "cym": ("Welsh", ["wel", "cy", "welsh"]),
    "gle": ("Irish", ["ga", "irish", "gaelic"]),
    "gla": ("Scottish Gaelic", ["gd", "scottish gaelic"]),
    "lat": ("Latin", ["la", "latin"]),
    "epo": ("Esperanto", ["eo", "esperanto"]),
    "tlh": ("Klingon", ["klingon"]),
}

# Tags that aren't a single real language. "und" (undefined) means nobody tagged
# the track, so Dubarr can't know what it is and never removes it.
UNDEFINED = "und"
MULTIPLE = "mul"     # several languages in one track
NO_LANGUAGE = "zxx"  # no speech, e.g. music or effects only
SPECIAL = {
    UNDEFINED: "Unknown",
    MULTIPLE: "Multiple languages",
    NO_LANGUAGE: "No speech",
    "mis": "Unknown",
    "qaa": "Unknown",
}

_ALIASES = {}
for _code, (_name, _others) in LANGUAGES.items():
    for _alias in [_code, _name.lower(), *_others]:
        _ALIASES[_alias.lower()] = _code


def short_code(code: str) -> str:
    """The code to put in a file name, two letters where there are some: Film.en.srt."""
    entry = LANGUAGES.get(code)
    if isinstance(entry, tuple):
        two = [o for o in entry[1] if len(o) == 2 and o.isalpha()]
        if two:
            return two[0]
    return code


def normalise(value) -> str:
    """The language code for a track tag, setting or name. Unrecognised tags count as unknown."""
    value = (value or "").strip().lower()
    if not value or value in SPECIAL:
        return UNDEFINED if value in ("", "mis", "qaa") else value
    # IETF tags like "en-US" or "pt-BR": the part before the dash is the language
    base = value.replace("_", "-").split("-")[0]
    return _ALIASES.get(value) or _ALIASES.get(base) or UNDEFINED


# Matroska and MP4 tags traditionally use these older codes where a language has two
BIBLIOGRAPHIC = {
    "fra": "fre", "deu": "ger", "nld": "dut", "ces": "cze", "slk": "slo", "ron": "rum", "mkd": "mac",
    "sqi": "alb", "ell": "gre", "kat": "geo", "hye": "arm", "fas": "per", "zho": "chi", "msa": "may",
    "mya": "bur", "eus": "baq", "cym": "wel", "isl": "ice",
}


def bibliographic(code: str) -> str:
    """The code to write into a file's track tag."""
    return BIBLIOGRAPHIC.get(code, code)


def name(code: str) -> str:
    if code in LANGUAGES:
        return LANGUAGES[code][0]
    return SPECIAL.get(code, code)


def parse_list(value):
    """Turn "English, jpn, fr" into ["eng", "jpn", "fra"]. Raises ValueError naming any it doesn't know."""
    if isinstance(value, (list, tuple)):
        items = value
    else:
        items = (value or "").replace(";", ",").split(",")
    codes, unknown = [], []
    for item in items:
        item = str(item).strip()
        if not item:
            continue
        code = normalise(item)
        if code not in LANGUAGES:
            unknown.append(item)
        elif code not in codes:
            codes.append(code)
    if unknown:
        raise ValueError("has a language Dubarr doesn't recognise: " + ", ".join(unknown))
    return codes


def describe(codes) -> str:
    """["kor", "spa"] -> "Korean + Spanish"."""
    return " + ".join(name(c) for c in codes) or "none"

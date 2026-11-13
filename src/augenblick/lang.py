"""The few texts the server words itself, in the languages of the interface.

Nearly everything a person reads is worded in the browser (static/i18n*.js). What is left for the
server: the names of memories with their dates, and what the AI is asked – so that it answers in the
language of whoever asked. German is the language of the programme and the fallback.
"""

from __future__ import annotations

from datetime import date

from .prefs import LANGUAGES

NAMES = {"de": "Deutsch", "en": "English", "fr": "français", "it": "italiano"}

MONTHS = {
    "de": ("Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September", "Oktober",
           "November", "Dezember"),
    "en": ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
           "November", "December"),
    "fr": ("janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre",
           "novembre", "décembre"),
    "it": ("gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio", "agosto", "settembre", "ottobre",
           "novembre", "dicembre"),
}

WORDS = {
    "de": {"and": "und", "away": "Unterwegs", "back1": "Diese Woche vor einem Jahr", "back": "Diese Woche vor {n} Jahren"},
    "en": {"and": "and", "away": "Away", "back1": "This week a year ago", "back": "This week {n} years ago"},
    "fr": {"and": "et", "away": "En voyage", "back1": "Cette semaine il y a un an", "back": "Cette semaine il y a {n} ans"},
    "it": {"and": "e", "away": "In viaggio", "back1": "Questa settimana un anno fa", "back": "Questa settimana {n} anni fa"},
}


def known(lang: str | None) -> str:
    return lang if lang in LANGUAGES else "de"


def word(lang: str, key: str, **values) -> str:
    return WORDS[known(lang)][key].format(**values)


def span(first: date, last: date, lang: str = "de") -> str:
    """A stretch of days the way one says it: 12.–18. Juli 2019, 12–18 July 2019."""
    lang = known(lang)
    months, dot = MONTHS[lang], "." if lang == "de" else ""
    if first == last:
        return f"{first.day}{dot} {months[first.month - 1]} {first.year}"
    if (first.year, first.month) == (last.year, last.month):
        return f"{first.day}{dot}–{last.day}{dot} {months[first.month - 1]} {first.year}"
    if first.year == last.year:
        return f"{first.day}{dot} {months[first.month - 1]} – {last.day}{dot} {months[last.month - 1]} {first.year}"
    return (f"{first.day}{dot} {months[first.month - 1]} {first.year} – "
            f"{last.day}{dot} {months[last.month - 1]} {last.year}")


# ------------------------------------------------------------------ what the AI is asked

# the instruction on offer until an administrator writes own ones: (name, text)
PHOTO_TASK = {
    "de": ("Beschreiben",
           "Beschreibe das Foto in ein bis zwei sachlichen Sätzen auf Deutsch: was zu sehen ist und, wenn erkennbar, wo. "
           "Nenne drei bis acht Schlagworte auf Deutsch (Hauptwörter in der Einzahl); nimm bevorzugt Schlagworte, die im "
           "Archiv schon vorkommen. Erfinde keine Namen von Personen. Hat das Bild kein Aufnahmedatum, schätze, wann es "
           "entstanden ist."),
    "en": ("Describe",
           "Describe the photo in one or two plain sentences in English: what is to be seen and, if it can be told, where. "
           "Give three to eight keywords in English (nouns in the singular); prefer keywords the archive already uses. "
           "Do not make up names of people. If the picture has no date taken, estimate when it was made."),
    "fr": ("Décrire",
           "Décris la photo en une ou deux phrases sobres en français : ce que l’on voit et, si cela se reconnaît, où. "
           "Donne trois à huit mots-clés en français (noms au singulier) ; préfère les mots-clés déjà utilisés dans "
           "l’archive. N’invente aucun nom de personne. Si l’image n’a pas de date de prise de vue, estime quand elle "
           "a été faite."),
    "it": ("Descrivere",
           "Descrivi la foto in una o due frasi sobrie in italiano: che cosa si vede e, se si riconosce, dove. "
           "Indica da tre a otto parole chiave in italiano (sostantivi al singolare); preferisci le parole chiave già "
           "usate nell’archivio. Non inventare nomi di persone. Se l’immagine non ha una data di scatto, stima quando "
           "è stata fatta."),
}

# added to what the AI is told when the asker does not read German: the fixed parts of the request are German
ANSWER_IN = {
    "de": "",
    "en": "\n\nWrite every text of your answer in English.",
    "fr": "\n\nRédige tous les textes de ta réponse en français.",
    "it": "\n\nScrivi tutti i testi della tua risposta in italiano.",
}

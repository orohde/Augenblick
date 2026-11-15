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
           "Schreibe eine Bildunterschrift auf Deutsch, wie man sie in ein Fotoalbum schreibt: ein kurzer Satz, höchstens "
           "100 Zeichen, mit dem, was das Bild ausmacht – Anlass oder Tätigkeit und Ort. Stehen in den Angaben Namen der "
           "Personen im Bild, nenne sie beim Namen. Beschreibe nie Alter, Aussehen oder Kleidung von Menschen und zähle "
           "nicht auf, was ohnehin jeder sieht; ohne Namen genügt „zwei Personen“ oder „Familie“.\n"
           "Nenne dazu fünf bis zehn Schlagworte auf Deutsch (Hauptwörter in der Einzahl), nach denen man das Bild später "
           "suchen würde: die Umgebung (etwa Meer, Strand, Berg, Wald, See, Stadt, Garten, Wohnzimmer), das Motiv "
           "(etwa Hund, Auto, Kirche, Essen), der Anlass (etwa Geburtstag, Hochzeit, Urlaub, Wanderung) und die Stimmung "
           "des Lichts, wenn sie auffällt (etwa Sonnenuntergang, Schnee, Nacht). Nimm bevorzugt Schlagworte, die im Archiv "
           "schon vorkommen; keine Namen von Personen und nichts Vages wie „Foto“ oder „Mensch“. "
           "Hat das Bild kein Aufnahmedatum, schätze, wann es entstanden ist."),
    "en": ("Describe",
           "Write a caption in English as one would put it into a photo album: one short sentence, 100 characters at "
           "most, saying what makes the picture – the occasion or activity and the place. If the facts name the people "
           "in the picture, call them by their names. Never describe the age, looks or clothing of people, and do not "
           "list what anyone can see; without names “two people” or “family” will do.\n"
           "Add five to ten keywords in English (nouns in the singular) one would later search the picture by: the "
           "surroundings (such as sea, beach, mountain, forest, lake, town, garden, living room), the subject (such as "
           "dog, car, church, food), the occasion (such as birthday, wedding, holiday, hike) and the light if it stands "
           "out (such as sunset, snow, night). Prefer keywords the archive already uses; no names of people and nothing "
           "vague like “photo” or “person”. If the picture has no date taken, estimate when it was made."),
    "fr": ("Décrire",
           "Écris une légende en français comme on la met dans un album photo : une phrase courte, 100 caractères au "
           "plus, qui dit ce qui fait l’image – l’occasion ou l’activité et le lieu. Si les indications nomment les "
           "personnes sur l’image, appelle-les par leur nom. Ne décris jamais l’âge, l’apparence ni les vêtements des "
           "gens et n’énumère pas ce que chacun voit ; sans noms, « deux personnes » ou « famille » suffit.\n"
           "Ajoute cinq à dix mots-clés en français (noms au singulier) par lesquels on chercherait l’image plus tard : "
           "le cadre (par exemple mer, plage, montagne, forêt, lac, ville, jardin, salon), le sujet (par exemple chien, "
           "voiture, église, repas), l’occasion (par exemple anniversaire, mariage, vacances, randonnée) et la lumière "
           "si elle frappe (par exemple coucher de soleil, neige, nuit). Préfère les mots-clés déjà utilisés dans "
           "l’archive ; pas de noms de personnes et rien de vague comme « photo » ou « personne ». Si l’image n’a pas "
           "de date de prise de vue, estime quand elle a été faite."),
    "it": ("Descrivere",
           "Scrivi una didascalia in italiano come la si mette in un album di foto: una frase breve, al massimo 100 "
           "caratteri, che dica ciò che fa l’immagine – l’occasione o l’attività e il luogo. Se le indicazioni nominano "
           "le persone nell’immagine, chiamale per nome. Non descrivere mai età, aspetto o vestiti delle persone e non "
           "elencare ciò che chiunque vede; senza nomi basta «due persone» o «famiglia».\n"
           "Aggiungi da cinque a dieci parole chiave in italiano (sostantivi al singolare) con cui si cercherebbe "
           "l’immagine in seguito: l’ambiente (per esempio mare, spiaggia, montagna, bosco, lago, città, giardino, "
           "soggiorno), il soggetto (per esempio cane, auto, chiesa, cibo), l’occasione (per esempio compleanno, "
           "matrimonio, vacanza, escursione) e la luce se colpisce (per esempio tramonto, neve, notte). Preferisci le "
           "parole chiave già usate nell’archivio; niente nomi di persone e nulla di vago come «foto» o «persona». "
           "Se l’immagine non ha una data di scatto, stima quando è stata fatta."),
}
# what the built-in instruction said before 2026.11.69 – whoever still has it stored gets today's wording
PHOTO_TASK_BEFORE = frozenset((
    'Beschreibe das Foto in ein bis zwei sachlichen Sätzen auf Deutsch: was zu sehen ist und, wenn erkennbar, wo. Nenne drei bis acht Schlagworte auf Deutsch (Hauptwörter in der Einzahl); nimm bevorzugt Schlagworte, die im Archiv schon vorkommen. Erfinde keine Namen von Personen. Hat das Bild kein Aufnahmedatum, schätze, wann es entstanden ist.',
    'Describe the photo in one or two plain sentences in English: what is to be seen and, if it can be told, where. Give three to eight keywords in English (nouns in the singular); prefer keywords the archive already uses. Do not make up names of people. If the picture has no date taken, estimate when it was made.',
    'Décris la photo en une ou deux phrases sobres en français : ce que l’on voit et, si cela se reconnaît, où. Donne trois à huit mots-clés en français (noms au singulier) ; préfère les mots-clés déjà utilisés dans l’archive. N’invente aucun nom de personne. Si l’image n’a pas de date de prise de vue, estime quand elle a été faite.',
    'Descrivi la foto in una o due frasi sobrie in italiano: che cosa si vede e, se si riconosce, dove. Indica da tre a otto parole chiave in italiano (sostantivi al singolare); preferisci le parole chiave già usate nell’archivio. Non inventare nomi di persone. Se l’immagine non ha una data di scatto, stima quando è stata fatta.',
))

# added to what the AI is told when the asker does not read German: the fixed parts of the request are German
ANSWER_IN = {
    "de": "",
    "en": "\n\nWrite every text of your answer in English.",
    "fr": "\n\nRédige tous les textes de ta réponse en français.",
    "it": "\n\nScrivi tutti i testi della tua risposta in italiano.",
}

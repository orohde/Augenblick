"""Languages: every text of the interface has its wording in each language, and the server words
the little it says itself in the language of whoever asks."""

import importlib.util
import re
from datetime import date
from pathlib import Path

import pytest

from augenblick.lang import MONTHS, PHOTO_TASK, WORDS, span
from augenblick.memories import looks_back
from augenblick.prefs import LANGUAGES
from augenblick.security import TEXTS

spec = importlib.util.spec_from_file_location("keys", Path(__file__).parents[1] / "tools" / "i18n" / "keys.py")
keys = importlib.util.module_from_spec(spec)
spec.loader.exec_module(keys)

GAPS = re.compile(r"\{\d+\}")


def edge(text: str) -> tuple[str, str]:
    return text[:len(text) - len(text.lstrip())], text[len(text.rstrip()):]


@pytest.mark.parametrize("lang", keys.LANGS)
def test_every_text_is_translated_and_nothing_is_left_over(lang):
    wanted, have = keys.keys(), keys.table(lang)
    assert [text for text in wanted if text not in have] == []
    assert [text for text in have if text not in wanted] == []


@pytest.mark.parametrize("lang", keys.LANGS)
def test_a_translation_keeps_the_gaps_and_the_spaces_around_it(lang):
    for text, worded in keys.table(lang).items():
        assert worded.strip() or not text.strip(), text
        assert sorted(GAPS.findall(worded)) == sorted(GAPS.findall(text)), text
        assert edge(worded) == edge(text), text


def test_the_page_is_read_like_the_browser_reads_it():
    found = keys.page_keys('<p title="Ein Hinweis">Benutzer beliebig (z. B. <code>upload</code>)</p>'
                           '<p>Vorne <span id="n"></span> hinten <b>fett</b></p><label>Name <input placeholder="Dein Name"></label>'
                           '<script>var x = "Kein Text";</script>')
    assert found == {"Ein Hinweis", "Benutzer beliebig (z. B. <code>upload</code>)", "Vorne", "hinten", "fett", "Name",
                     "Dein Name"}


def test_the_script_hands_its_texts_to_t():
    found = keys.script_keys("a = t('Ein Bild'); b = tn(n, '1 Bild', '{0} Bilder'); c = t('Geht\\'s?', [x]); list(',')")
    assert found == {"Ein Bild", "1 Bild", "{0} Bilder", "Geht's?"}


def test_the_server_knows_every_language():
    for table in (MONTHS, WORDS, PHOTO_TASK, TEXTS):
        assert set(table) == set(LANGUAGES)
    assert all(set(TEXTS[lang]) == set(TEXTS["de"]) for lang in LANGUAGES)
    assert all(set(WORDS[lang]) == set(WORDS["de"]) for lang in LANGUAGES)


def test_dates_and_looks_back_in_other_languages():
    assert span(date(2019, 7, 12), date(2019, 7, 18)) == "12.–18. Juli 2019"
    assert span(date(2019, 7, 12), date(2019, 7, 18), "en") == "12–18 July 2019"
    assert span(date(2018, 12, 28), date(2019, 1, 3), "fr") == "28 décembre 2018 – 3 janvier 2019"
    assert span(date(2019, 7, 12), date(2019, 7, 12), "xx") == "12. Juli 2019"            # unknown: German
    assert [m["title"] for m in looks_back(date(2026, 11, 13), [2025, 2019], "en")] == [
        "This week a year ago", "This week 7 years ago"]


def test_the_language_belongs_to_the_account(admin):
    assert admin.get("/api/me").json()["language"] == ""
    assert admin.put("/api/me/prefs", json={"language": "xx"}).status_code == 400
    assert admin.put("/api/me/prefs", json={"language": "fr"}).json()["language"] == "fr"
    assert admin.get("/api/me").json()["language"] == "fr"


def test_the_ai_is_offered_its_task_in_the_language_of_the_asker(admin):
    assert admin.get("/api/ai").json()["prompts"][0]["name"] == "Beschreiben"
    described = admin.get("/api/ai", headers={"X-Language": "en"}).json()["prompts"][0]
    assert described["name"] == "Describe" and "in English" in described["text"]
    assert admin.get("/api/ai", headers={"X-Language": "nonsense"}).json()["prompts"][0]["name"] == "Beschreiben"

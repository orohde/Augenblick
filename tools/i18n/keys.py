"""Every text of the interface that wants a translation – the list the language files are checked against.

    python tools/i18n/keys.py            prints the German texts, one JSON string per line
    python tools/i18n/keys.py missing    says per language which texts have no translation yet, and which
                                         translations belong to no text any more

The texts come from two places: what app.js hands to t() and tn(), and what stands in index.html. The page
is read here the way i18n.js reads it in the browser (applyI18n): a paragraph with only plain formatting
inside is one text, markup included; everything else is taken piece by piece.
"""

from __future__ import annotations

import html
import json
import re
import sys
from html.parser import HTMLParser
from pathlib import Path

STATIC = Path(__file__).resolve().parents[2] / "src" / "augenblick" / "static"
LANGS = ("en", "fr", "it")
ATTRS = ("title", "placeholder", "aria-label", "alt")
INLINE = {"b", "i", "em", "strong", "code", "kbd", "br", "a", "span", "small", "u"}
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
FORM = {"input", "select", "textarea", "button"}
WORD = re.compile(r"[^\W\d_]{2}")
STRING = r"'((?:[^'\\]|\\.)*)'"


def squeeze(text: str) -> str:
    return " ".join(text.split())


def unquote(text: str) -> str:
    return re.sub(r"\\(.)", lambda m: {"n": "\n", "t": "\t"}.get(m[1], m[1]), text)


def script_keys(source: str) -> set[str]:
    found = {unquote(m[1]) for m in re.finditer(r"(?<![\w$.])t\(" + STRING, source)}
    for m in re.finditer(r"(?<![\w$.])tn\([^,]+?,\s*" + STRING + r",\s*" + STRING, source):
        found.update((unquote(m[1]), unquote(m[2])))
    return found


class Node:
    def __init__(self, tag: str, attrs: dict, parent: "Node | None", inner: int):
        self.tag, self.attrs, self.parent, self.inner, self.end = tag, attrs, parent, inner, inner
        self.children: list[Node] = []
        self.texts: list[str] = []


class Page(HTMLParser):
    def __init__(self, source: str):
        super().__init__(convert_charrefs=False)
        self.source = source
        self.starts = [0]
        for line in source.splitlines(keepends=True):
            self.starts.append(self.starts[-1] + len(line))
        self.root = self.at = Node("#root", {}, None, 0)
        self.feed(source)

    def where(self) -> int:
        line, column = self.getpos()
        return self.starts[line - 1] + column

    def handle_starttag(self, tag, attrs):
        node = Node(tag, {k: v or "" for k, v in attrs}, self.at, self.where() + len(self.get_starttag_text()))
        self.at.children.append(node)
        if tag not in VOID:
            self.at = node

    def handle_endtag(self, tag):
        node = self.at
        while node.parent and node.tag != tag:
            node = node.parent
        if node.parent:
            node.end = self.where()
            self.at = node.parent

    def handle_data(self, data):
        self.at.texts.append(data)

    def handle_entityref(self, name):
        self.at.texts.append(html.unescape(f"&{name};"))

    def handle_charref(self, name):
        self.at.texts.append(html.unescape(f"&#{name};"))


def page_keys(source: str) -> set[str]:
    page, found = Page(source), set()

    def holds(node: Node, test) -> bool:
        return any(test(child) or holds(child, test) for child in node.children)

    def plain(node: Node) -> bool:
        return all(child.tag in INLINE and "id" not in child.attrs and not (child.tag == "span" and child.attrs.get("class"))
                   and not holds(child, lambda n: "id" in n.attrs or n.tag in FORM) and plain(child)
                   for child in node.children)

    def walk(node: Node) -> None:
        if node.tag in ("script", "style") or node.attrs.get("translate") == "no":
            return
        for name in ATTRS:
            value = node.attrs.get(name, "")
            if WORD.search(value):
                found.add(squeeze(value))
        texts = [text for text in node.texts if WORD.search(text)]
        if texts and node.children and plain(node):
            # as the browser gives it back: numbered characters written out, the rest as in the file
            inner = re.sub(r"&#(\d+|x[0-9a-fA-F]+);", lambda m: html.unescape(m[0]), page.source[node.inner:node.end])
            found.add(squeeze(inner))
            return
        if not node.children:
            texts = ["".join(node.texts)] if WORD.search("".join(node.texts)) else []      # one run of text
        found.update(node.attrs.get("data-i18n") or squeeze(text) for text in texts)
        for child in node.children:
            walk(child)

    walk(page.root)
    return found


def keys() -> list[str]:
    return sorted(script_keys((STATIC / "app.js").read_text(encoding="utf-8"))
                  | page_keys((STATIC / "index.html").read_text(encoding="utf-8")))


def table(lang: str) -> dict[str, str]:
    """The translations of one language. The file is a script, but between its braces it is plain JSON."""
    source = (STATIC / f"i18n-{lang}.js").read_text(encoding="utf-8")
    return json.loads(source[source.index("{"):source.rindex("}") + 1])


if __name__ == "__main__":
    wanted = keys()
    if sys.argv[1:] == ["missing"]:
        for lang in LANGS:
            have = table(lang)
            for text in wanted:
                if text not in have:
                    print(lang, "fehlt:", json.dumps(text, ensure_ascii=False))
            for text in have:
                if text not in wanted:
                    print(lang, "ohne Text:", json.dumps(text, ensure_ascii=False))
    else:
        for text in wanted:
            print(json.dumps(text, ensure_ascii=False))

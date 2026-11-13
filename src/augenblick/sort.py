"""Folders and file names by rule.

A rule says: below this folder, photos belong into a folder and carry a name built from
their date. Without a rule nothing is ever moved or renamed. Moving is a rename on the
same disk; it never overwrites, and every move is in the journal.
"""

from __future__ import annotations

import errno
import os
import re
import secrets
import shutil
import threading
from pathlib import Path

from .index import Index
from .journal import Journal
from .media import file_hash

TOKEN = re.compile(r"\{([^{}]*)\}")
FORBIDDEN = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
MODES = ("ask", "auto")


def _date(photo: dict) -> tuple[str, str, str, str, str, str]:
    taken = photo["taken"]
    return taken[0:4], taken[5:7], taken[8:10], taken[11:13], taken[14:16], taken[17:19]


TOKENS = {
    "Jahr": lambda p: _date(p)[0],
    "Monat": lambda p: _date(p)[1],
    "Tag": lambda p: _date(p)[2],
    "JJJJMMTT": lambda p: "".join(_date(p)[:3]),
    "JJJJ-MM-TT": lambda p: "-".join(_date(p)[:3]),
    "HHMMSS": lambda p: "".join(_date(p)[3:]),
    "HH-MM-SS": lambda p: "-".join(_date(p)[3:]),
    "Ordner": lambda p: p["folder"].rsplit("/", 1)[-1],
    "Kamera": lambda p: p.get("model") or "ohne Kamera",
    "Alt": lambda p: p["name"].rsplit(".", 1)[0],
}


class SortError(Exception):
    pass


def check_template(template: str, levels: bool) -> str:
    """A template as a person typed it, tidied – or an error that says what is wrong."""
    template = template.strip().strip("/") if levels else template.strip()
    for name in TOKEN.findall(template):
        if name not in TOKENS:
            raise SortError(f"unknown building block: {{{name}}}")
    rest = TOKEN.sub("", template)
    if "{" in rest or "}" in rest:
        raise SortError("unbalanced braces")
    if FORBIDDEN.search(rest.replace("/", "") if levels else rest):
        raise SortError("characters that file names cannot have")
    if levels and any(part in ("", ".", "..") for part in template.split("/") if template):
        raise SortError("invalid folder")
    return template


def render(template: str, photo: dict) -> str:
    def piece(match: re.Match) -> str:
        return FORBIDDEN.sub("_", TOKENS[match.group(1)](photo)).strip() or "_"
    return TOKEN.sub(piece, template).strip(" .")


def clean_rules(rules) -> list[dict]:
    result, seen = [], set()
    for rule in rules or []:
        scope = "/".join(part for part in str(rule.get("scope") or "").split("/") if part)
        if not scope or any(part in (".", "..") for part in scope.split("/")):
            raise SortError("a rule needs a folder it applies to")
        if scope.casefold() in seen:
            raise SortError(f"two rules for the same folder: {scope}")
        seen.add(scope.casefold())
        result.append({"scope": scope, "folder": check_template(str(rule.get("folder") or ""), True),
                       "name": check_template(str(rule.get("name") or ""), False)})
    return sorted(result, key=lambda r: r["scope"].lower())


class Rules:
    def __init__(self, rules: list[dict] | None = None):
        self.rules = sorted(rules or [], key=lambda r: -len(r["scope"]))     # the most specific first

    def rule_for(self, folder: str) -> dict | None:
        for rule in self.rules:
            if folder == rule["scope"] or folder.startswith(rule["scope"] + "/"):
                return rule
        return None

    def target(self, photo: dict) -> str:
        """Where the photo belongs, as a path below the photo root – '' if no rule has a say.

        Only photos that carry their own date are sorted; a file date is no ground for it.
        """
        rule = self.rule_for(photo["folder"])
        if not rule or photo.get("taken_src") != "exif" or not (rule["folder"] or rule["name"]):
            return ""
        folder = f"{rule['scope']}/{render(rule['folder'], photo)}" if rule["folder"] else photo["folder"]
        stem, dot, ext = photo["name"].rpartition(".")
        if not dot:
            stem, ext = photo["name"], ""
        if rule["name"]:
            stem = render(rule["name"], photo)
        return f"{folder}/{stem}{'.' + ext if ext else ''}"

    @staticmethod
    def fits(path: str, target: str) -> bool:
        """In place – also when it carries the number that keeps two photos of one second apart."""
        if not target or path == target:
            return True
        folder, _, name = path.rpartition("/")
        want_folder, _, want_name = target.rpartition("/")
        if folder != want_folder:
            return False
        stem, ext = os.path.splitext(name)
        want_stem, want_ext = os.path.splitext(want_name)
        return ext == want_ext and re.fullmatch(re.escape(want_stem) + r"-\d+", stem) is not None


class Sorter:
    def __init__(self, root: Path, index: Index, journal: Journal, lock: threading.Lock):
        self.root = root
        self.index = index
        self.journal = journal
        self._lock = lock          # shared with the editor and the trash

    def _free(self, target: str, company: list[tuple[dict, str]] = ()) -> str:
        """The wished path, or the same with -2, -3 … if something else lies there – or where what
        goes with the photo (the film of a Live Photo, the original of a picture worked on) would
        have to go, which takes the name of its photo."""
        stem, ext = os.path.splitext(target)

        def taken(name: str) -> bool:
            if (self.root / f"{name}{ext}").exists():
                return True
            return any(self._beside(f"{name}{ext}", mate, added) != mate["path"]
                       and (self.root / self._beside(f"{name}{ext}", mate, added)).exists() for mate, added in company)

        if not taken(stem):
            return target
        number = 2
        while taken(f"{stem}-{number}"):
            number += 1
        return f"{stem}-{number}{ext}"

    @staticmethod
    def _beside(photo_path: str, mate: dict, added: str = "") -> str:
        """Where a file that goes with a photo lies once the photo lies at this path: beside it, under its name."""
        return os.path.splitext(photo_path)[0] + added + os.path.splitext(mate["path"])[1]

    def plan(self, photo: dict) -> str:
        """Where a move would really put the photo – '' if it stays."""
        target = photo.get("target") or ""
        if Rules.fits(photo["path"], target):
            return ""
        return self._free(target, self.index.company(photo))

    def _go(self, current: dict, new: str, user: str, batch: str | None, across: bool = False) -> dict:
        """Move one photo, and what goes with it. All or nothing."""
        old = current["path"]
        company = [(mate["path"], self._beside(new, mate, added)) for mate, added in self.index.company(current)]
        self._rename(old, new, across)
        done: list[tuple[str, str]] = []
        try:
            for was, now in company:
                if was != now:
                    self._rename(was, now, across)
                    done.append((was, now))
        except SortError:
            for was, now in reversed(done):             # the photo does not go without what belongs to it
                self._rename(now, was, across)
            self._rename(new, old, across)
            raise
        for was, now in done:
            self.index.moved(was, now)
        before, after = {"path": old}, {"path": new}
        if done:
            before["with"], after["with"] = [was for was, _ in done], [now for _, now in done]
        self.journal.repath(old, new)
        record = self.index.moved(old, new)
        self.journal.add(user=user, batch=batch or secrets.token_hex(6), action="move", path=new,
                         before=before, after=after, tags={}, sha_after=file_hash(self.root / new))
        return record

    def _rename(self, old: str, new: str, across: bool = False) -> None:
        root = self.root.resolve()
        src, dst = (root / old).resolve(), (root / new)
        if root not in src.parents or not src.is_file():
            raise SortError("file not found")
        if root not in dst.resolve().parents or (not across and old.split("/", 1)[0] != new.split("/", 1)[0]):
            raise SortError("photos stay inside their area")
        if dst.exists():
            raise SortError("the place is taken")
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.rename(src, dst)
            except OSError as exc:
                if exc.errno != errno.EXDEV:
                    raise
                self._carry(src, dst)       # the areas lie on different disks
        except OSError as exc:
            raise SortError("file is not writable") from exc
        self._tidy(src.parent, root / old.split("/", 1)[0])

    @staticmethod
    def _carry(src: Path, dst: Path) -> None:
        """Move to another disk: copy beside the goal, compare, put in place – only then the source goes."""
        temp = dst.with_name(f".augenblick-{secrets.token_hex(4)}{dst.suffix}")
        try:
            shutil.copy2(src, temp)
            if file_hash(temp) != file_hash(src):
                raise OSError(errno.EIO, "the copy differs from the file")
            if dst.exists():                # never overwrite
                raise OSError(errno.EEXIST, "the place is taken")
            os.rename(temp, dst)
        finally:
            temp.unlink(missing_ok=True)
        src.unlink()

    @staticmethod
    def _tidy(folder: Path, area: Path) -> None:
        """Remove folders that are left without a single file – never the area itself."""
        while folder != area and area in folder.parents:
            try:
                folder.rmdir()         # refuses unless really empty
            except OSError:
                return
            folder = folder.parent

    def move(self, photo: dict, user: str, batch: str | None = None) -> dict:
        with self._lock:
            current = self.index.by_path(photo["path"])
            if not current:
                raise SortError("file not found")
            new = self.plan(current)
            if not new:
                return current
            return self._go(current, new, user, batch)

    def transfer(self, photo: dict, folder: str, user: str, batch: str | None = None) -> dict:
        """Put a photo into another folder, of its own area or another one. Where a rule has a say
        over that folder, the rule decides where exactly it lands; else it lies in the folder itself."""
        with self._lock:
            current = self.index.by_path(photo["path"])
            if not current:
                raise SortError("file not found")
            parts = folder.split("/")
            if not folder or any(not part or part.startswith((".", "@")) or part == ".." for part in parts) \
                    or not (self.root / folder).is_dir():
                raise SortError("unknown folder")
            planner = self.index.planner
            there = dict(current, area=parts[0], folder=folder, path=f"{folder}/{current['name']}")
            want = (planner.target(there) if planner else "") or there["path"]
            old = current["path"]
            if want == old or Rules.fits(old, want):
                return current                  # it lies there already
            return self._go(current, self._free(want, self.index.company(current)), user, batch, across=True)

    def undo(self, entry: dict, user: str) -> dict:
        if entry["undone"]:
            raise SortError("already taken back")
        with self._lock:
            now, old = entry["path"], entry["before"]["path"]
            # where a file lies does not depend on what is in it: later changes to the photo do not block this
            if not (self.root / now).is_file():
                raise SortError("file not found")
            mates = list(zip(entry["after"].get("with") or [], entry["before"].get("with") or []))
            if entry["after"].get("film"):                  # written like this for a short while
                mates.append((entry["after"]["film"], entry["before"]["film"]))
            mates = [(there, home) for there, home in mates if (self.root / there).is_file()]
            if any((self.root / home).exists() for _, home in mates):
                raise SortError("the place is taken")
            for there, home in mates:                       # what went with the photo goes back with it
                self._rename(there, home, across=True)
                self.index.moved(there, home)
            self._rename(now, old, across=True)      # back to where the journal says it came from
            self.journal.repath(now, old)
            self.journal.mark_undone(entry["id"], user)
            return self.index.moved(now, old)

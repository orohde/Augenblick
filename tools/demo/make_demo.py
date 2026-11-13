"""A made-up photo library for screenshots and trying things out: drawn pictures, no real photo.

    python tools/demo/make_demo.py ZIELORDNER [de|en] [BILDER]

Writes ZIELORDNER/fotos with the areas Familie, Eingang and Bildschirmfotos (in English: Family, Inbox,
Screenshots – with English albums, keywords and descriptions, for the screenshots of the English pages). Every picture is drawn
(sky, hills, water, houses), carries a date, a camera, most of them a place, some an album, keywords,
a description and a rating – written with exiftool, as Augenblick! itself would.

BILDER, if given, is a folder of pictures of your own to show instead of the drawn ones: one subfolder per
kind of scene (strand, berge, schnee, stadt, see, wald, garten, abend; schnee falls back to berge). Only what
is to be seen is taken from them – date, place and camera stay the made-up ones of the demo, nothing of the
pictures' own data is kept. A scene without pictures stays drawn.
"""

from __future__ import annotations

import io
import random
import subprocess
import sys
from datetime import date
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

W, H = 1600, 1200


def sky(draw, top, bottom, horizon):
    for y in range(horizon):
        t = y / max(1, horizon - 1)
        draw.line([(0, y), (W, y)], fill=tuple(int(a + (b - a) * t) for a, b in zip(top, bottom)))


def sun(draw, rnd, colour=(255, 236, 170)):
    x, y, r = rnd.randint(200, W - 200), rnd.randint(120, 380), rnd.randint(50, 90)
    for grow, alpha in ((60, 40), (30, 80), (0, 255)):
        draw.ellipse([x - r - grow, y - r - grow, x + r + grow, y + r + grow], fill=colour + (alpha,))


def hills(draw, rnd, base, colours, rough=1.0):
    for n, colour in enumerate(colours):
        y = base + n * 90
        points = [(0, H)]
        x = 0
        while x <= W:
            points.append((x, y + rnd.randint(int(-110 * rough), int(60 * rough))))
            x += rnd.randint(120, 260)
        points += [(W, y), (W, H)]
        draw.polygon(points, fill=colour)


def strand(rnd):
    image = Image.new("RGB", (W, H))
    draw = ImageDraw.Draw(image, "RGBA")
    sky(draw, (96, 170, 232), (206, 232, 250), 560)
    sun(draw, rnd)
    draw.rectangle([0, 560, W, 820], fill=(38, 118, 176))
    for _ in range(40):
        y = rnd.randint(570, 810)
        x = rnd.randint(0, W)
        draw.line([(x, y), (x + rnd.randint(60, 220), y)], fill=(230, 244, 252, 120), width=3)
    draw.polygon([(0, 800), (W, 760), (W, H), (0, H)], fill=(232, 212, 160))
    for _ in range(3):      # beach chairs
        x = rnd.randint(100, W - 260)
        colour = rnd.choice([(214, 74, 60), (52, 110, 190), (240, 240, 240)])
        draw.rectangle([x, 880, x + 150, 1040], fill=colour)
        draw.rectangle([x, 850, x + 150, 890], fill=(250, 250, 250))
    return image


def berge(rnd, snow=False):
    image = Image.new("RGB", (W, H))
    draw = ImageDraw.Draw(image, "RGBA")
    sky(draw, (70, 130, 210), (190, 220, 245), 700)
    sun(draw, rnd, (255, 250, 220))
    for n in range(4):
        x = rnd.randint(-100, W - 300)
        w, h = rnd.randint(500, 800), rnd.randint(380, 600)
        colour = (110 - n * 12, 122 - n * 10, 140 - n * 8)
        draw.polygon([(x, 760), (x + w // 2, 760 - h), (x + w, 760)], fill=colour)
        draw.polygon([(x + w // 2 - 70, 760 - h + 120), (x + w // 2, 760 - h), (x + w // 2 + 70, 760 - h + 120),
                      (x + w // 2 + 20, 760 - h + 90), (x + w // 2 - 20, 760 - h + 130)], fill=(245, 248, 252))
    ground = [(236, 240, 246), (220, 228, 238)] if snow else [(74, 132, 84), (52, 104, 66)]
    hills(draw, rnd, 760, ground, 0.5)
    for _ in range(14):
        x, y = rnd.randint(0, W), rnd.randint(860, 1120)
        draw.polygon([(x, y), (x + 34, y - 120), (x + 68, y)], fill=(28, 70, 48))
    return image


def stadt(rnd):
    image = Image.new("RGB", (W, H))
    draw = ImageDraw.Draw(image, "RGBA")
    sky(draw, (120, 150, 200), (236, 214, 190), 800)
    x = 0
    while x < W:
        w, h = rnd.randint(110, 220), rnd.randint(260, 620)
        colour = rnd.choice([(196, 150, 120), (176, 164, 150), (150, 110, 100), (210, 190, 160), (120, 130, 150)])
        draw.rectangle([x, 900 - h, x + w, 900], fill=colour)
        draw.polygon([(x - 6, 900 - h), (x + w // 2, 900 - h - rnd.randint(40, 110)), (x + w + 6, 900 - h)], fill=(120, 60, 50))
        for wy in range(900 - h + 40, 860, 70):
            for wx in range(x + 20, x + w - 30, 46):
                draw.rectangle([wx, wy, wx + 22, wy + 34], fill=rnd.choice([(250, 236, 170), (70, 86, 110)]))
        x += w + rnd.randint(0, 14)
    draw.rectangle([0, 900, W, H], fill=(92, 96, 104))
    draw.rectangle([0, 1010, W, 1030], fill=(240, 220, 120))
    return image


def see(rnd):
    image = Image.new("RGB", (W, H))
    draw = ImageDraw.Draw(image, "RGBA")
    sky(draw, (250, 170, 110), (252, 226, 180), 620)
    sun(draw, rnd, (255, 244, 200))
    hills(draw, rnd, 560, [(70, 84, 110), (52, 70, 92)], 0.7)
    draw.rectangle([0, 700, W, H], fill=(64, 104, 150))
    for _ in range(60):
        y = rnd.randint(710, H - 10)
        x = rnd.randint(0, W)
        draw.line([(x, y), (x + rnd.randint(40, 180), y)], fill=(250, 214, 170, 110), width=3)
    x = rnd.randint(300, W - 500)       # a sailing boat
    draw.polygon([(x, 900), (x + 220, 900), (x + 180, 950), (x + 30, 950)], fill=(240, 240, 240))
    draw.polygon([(x + 110, 900), (x + 110, 700), (x + 210, 890)], fill=(250, 250, 250))
    return image


def wald(rnd):
    image = Image.new("RGB", (W, H))
    draw = ImageDraw.Draw(image, "RGBA")
    sky(draw, (150, 200, 230), (220, 240, 230), 500)
    hills(draw, rnd, 480, [(60, 120, 70), (44, 100, 58), (34, 84, 48)], 0.6)
    for _ in range(46):
        x, y = rnd.randint(-40, W), rnd.randint(620, 1180)
        size = int(80 + (y - 600) * 0.35)
        green = rnd.choice([(24, 78, 46), (30, 92, 54), (40, 110, 60), (190, 120, 40)])
        draw.rectangle([x + size // 2 - 8, y, x + size // 2 + 8, y + size // 2], fill=(84, 60, 40))
        draw.polygon([(x, y), (x + size // 2, y - size * 2), (x + size, y)], fill=green)
    return image


def garten(rnd):
    image = Image.new("RGB", (W, H))
    draw = ImageDraw.Draw(image, "RGBA")
    sky(draw, (120, 190, 240), (210, 236, 250), 520)
    draw.rectangle([0, 520, W, H], fill=(98, 160, 80))
    draw.rectangle([180, 300, 760, 640], fill=(236, 226, 206))        # the house
    draw.polygon([(150, 300), (470, 110), (790, 300)], fill=(150, 66, 52))
    draw.rectangle([420, 470, 520, 640], fill=(110, 76, 50))
    for wx in (240, 600):
        draw.rectangle([wx, 380, wx + 110, 470], fill=(160, 204, 232))
    for _ in range(120):
        x, y = rnd.randint(0, W), rnd.randint(680, H - 10)
        colour = rnd.choice([(236, 80, 90), (250, 214, 70), (240, 240, 250), (190, 110, 210)])
        draw.ellipse([x, y, x + 16, y + 16], fill=colour)
    draw.ellipse([1040, 240, 1440, 600], fill=(52, 118, 62))
    draw.rectangle([1220, 560, 1260, 760], fill=(92, 66, 44))
    return image


def abend(rnd):
    image = Image.new("RGB", (W, H))
    draw = ImageDraw.Draw(image, "RGBA")
    sky(draw, (40, 40, 90), (250, 130, 70), 760)
    sun(draw, rnd, (255, 200, 120))
    hills(draw, rnd, 720, [(40, 36, 60), (24, 22, 40), (12, 12, 24)], 0.8)
    return image


def muster(rnd):
    """A screen background: nothing a camera took."""
    image = Image.new("RGB", (W, H), rnd.choice([(18, 24, 44), (30, 16, 46), (10, 36, 44)]))
    draw = ImageDraw.Draw(image, "RGBA")
    for _ in range(26):
        x, y, r = rnd.randint(0, W), rnd.randint(0, H), rnd.randint(60, 320)
        draw.ellipse([x - r, y - r, x + r, y + r], fill=rnd.choice([(90, 84, 255, 60), (0, 112, 242, 60), (255, 176, 32, 50), (140, 92, 255, 60)]))
    return image.filter(ImageFilter.GaussianBlur(18))


SCENES = {"strand": strand, "berge": berge, "schnee": lambda rnd: berge(rnd, True), "stadt": stadt, "see": see,
          "wald": wald, "garten": garten, "abend": abend}

# when, how many, scene, place (name only for us), lat, lon, camera, album, keywords, description of the first, rating of the first
EVENTS = [
    ("2026-08-14 10:12", 7, "strand", 54.9079, 8.3108, "iPhone 15", "Urlaub/Nordsee 2026", ["Strand", "Urlaub"], "Erster Tag am Strand von Westerland", 5),
    ("2026-08-16 20:41", 3, "abend", 54.9079, 8.3108, "iPhone 15", "Urlaub/Nordsee 2026", ["Abendstimmung", "Urlaub"], "Sonnenuntergang hinter den Dünen", 4),
    ("2026-06-21 15:30", 5, "garten", 49.8414, 7.8671, "iPhone 15", "Garten", ["Garten", "Sommer"], "Sommerfest im Garten", 0),
    ("2026-03-08 11:05", 4, "stadt", 50.1109, 8.6821, "DMC-GM5", "", ["Stadt"], "", 0),
    ("2025-12-28 13:20", 6, "schnee", 47.4210, 10.9850, "DMC-GM5", "Urlaub/Alpen 2025", ["Schnee", "Berge", "Urlaub"], "Blick von der Hütte ins Tal", 5),
    ("2025-12-30 11:05", 5, "schnee", 47.4210, 10.9850, "DMC-GM5", "Urlaub/Alpen 2025", ["Schnee", "Berge", "Urlaub"], "", 0),
    ("2025-09-13 16:48", 5, "wald", 49.8400, 7.6700, "iPhone 13", "", ["Wald", "Wandern"], "Wanderung im Soonwald", 3),
    ("2025-07-19 09:55", 6, "berge", 46.4983, 11.3548, "DMC-GM5", "Urlaub/Alpen 2025", ["Berge", "Wandern", "Urlaub"], "", 0),
    ("2025-05-01 18:10", 3, "see", 47.7200, 11.7570, "iPhone 13", "", ["See"], "Abend am Tegernsee", 4),
    ("2024-10-05 14:02", 4, "stadt", 52.3676, 4.9041, "iPhone 13", "Städtereisen/Amsterdam 2024", ["Stadt", "Urlaub"], "Grachten in Amsterdam", 0),
    ("2024-08-02 12:30", 6, "strand", 42.8140, 10.3160, "iPhone 13", "Urlaub/Elba 2024", ["Strand", "Urlaub"], "", 4),
    ("2024-08-04 19:50", 5, "abend", 42.8140, 10.3160, "iPhone 13", "Urlaub/Elba 2024", ["Abendstimmung", "Urlaub"], "Abend am Hafen von Portoferraio", 0),
    ("2024-04-14 10:40", 4, "garten", 49.8414, 7.8671, "iPhone 13", "Garten", ["Garten", "Frühling"], "", 0),
    ("2023-12-24 17:45", 3, "abend", None, None, "", "", ["Weihnachten"], "", 0),
    ("2023-07-22 11:15", 5, "see", 54.3233, 10.1228, "DMC-GM5", "", ["Wasser", "Segeln"], "Kieler Förde", 3),
    ("2022-09-10 15:00", 4, "wald", None, None, "DMC-GM5", "", ["Wald"], "", 0),
]


# the same library in English: names of areas, albums, keywords and descriptions
ENGLISH = {
    "Familie": "Family", "Eingang": "Inbox", "Bildschirmfotos": "Screenshots", "Hintergründe": "Wallpapers", "Spiele": "Games",
    "hintergrund": "wallpaper",
    "Urlaub/Nordsee 2026": "Holidays/North Sea 2026", "Urlaub/Alpen 2025": "Holidays/Alps 2025", "Urlaub/Elba 2024": "Holidays/Elba 2024",
    "Städtereisen/Amsterdam 2024": "City trips/Amsterdam 2024", "Garten": "Garden",
    "Strand": "Beach", "Urlaub": "Holiday", "Abendstimmung": "Evening light", "Sommer": "Summer", "Stadt": "City", "Schnee": "Snow",
    "Berge": "Mountains", "Wald": "Forest", "Wandern": "Hiking", "See": "Lake", "Frühling": "Spring", "Weihnachten": "Christmas",
    "Wasser": "Water", "Segeln": "Sailing",
    "Erster Tag am Strand von Westerland": "First day on the beach at Westerland",
    "Sonnenuntergang hinter den Dünen": "Sunset behind the dunes", "Sommerfest im Garten": "Summer party in the garden",
    "Blick von der Hütte ins Tal": "View from the hut into the valley", "Wanderung im Soonwald": "Hike in the Soonwald",
    "Abend am Tegernsee": "Evening at the Tegernsee", "Grachten in Amsterdam": "Canals in Amsterdam",
    "Abend am Hafen von Portoferraio": "Evening at the harbour of Portoferraio", "Kieler Förde": "Kiel Fjord",
    "Derselbe Garten, drei Jahre früher": "The same garden, three years earlier",
}


class Own:
    """Pictures of one's own instead of the drawn ones, scene by scene, in the order of their names.
    When a scene runs out it starts again – mirrored, then cut closer, so that no two files are alike."""

    def __init__(self, folder: Path | None):
        self.files = {scene: sorted(p for p in (folder / scene).glob("*") if p.suffix.lower() in (".jpg", ".jpeg", ".png"))
                      for scene in SCENES} if folder else {}
        self.used: dict[str, int] = {}

    def next(self, scene: str) -> Image.Image | None:
        files = self.files.get(scene) or (self.files.get("berge") if scene == "schnee" else None)
        if not files:
            return None
        key = "berge" if not self.files.get(scene) else scene
        n = self.used.get(key, 0)
        self.used[key] = n + 1
        with Image.open(files[n % len(files)]) as opened:
            image = opened.convert("RGB")
        again = n // len(files)
        if again % 2:
            image = image.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
        if again >= 2:
            w, h = image.size
            image = image.crop((w // 10, h // 10, w - w // 10, h - h // 10))
        image.thumbnail((W, W))
        return image


def jpeg(image: Image.Image, taken: str | None, model: str, lat, lon) -> bytes:
    exif = Image.Exif()
    if model:
        exif[0x010F] = "Apple" if model.startswith("iPhone") else "Panasonic"
        exif[0x0110] = model
    if taken:
        exif[0x8769] = {0x9003: taken, 0x9004: taken}
    if lat is not None:
        def dms(value):
            value = abs(value)
            deg = int(value)
            minutes = int((value - deg) * 60)
            return (float(deg), float(minutes), round((value - deg - minutes / 60) * 3600, 2))
        exif[0x8825] = {1: "N" if lat >= 0 else "S", 2: dms(lat), 3: "E" if lon >= 0 else "W", 4: dms(lon)}
    out = io.BytesIO()
    image.save(out, "JPEG", quality=86, exif=exif.tobytes())
    return out.getvalue()


def tag(path: Path, *, album="", keywords=(), description="", rating=0) -> None:
    args = ["exiftool", "-q", "-overwrite_original"]
    for word in keywords:
        args.append(f"-XMP-dc:Subject+={word}")
    if album:
        args.append("-XMP-lr:HierarchicalSubject+=Album|" + album.replace("/", "|"))
    if description:
        args += [f"-IFD0:ImageDescription={description}", f"-XMP-dc:Description={description}"]
    if rating:
        args.append(f"-XMP-xmp:Rating={rating}")
    if len(args) > 3:
        subprocess.run(args + [str(path)], check=True)


def main(target: Path, lang: str = "de", pictures: Path | None = None) -> None:
    rnd = random.Random(20261112)
    own = Own(pictures)
    fotos = target / "fotos"
    count = 0
    say = (lambda text: ENGLISH.get(text, text)) if lang == "en" else (lambda text: text)
    # a few pictures from this very week three years ago: something to look back at, whenever the demo is made
    today = date.today()
    back = today.replace(year=today.year - 3, day=min(today.day, 28))
    events = EVENTS + [(f"{back.isoformat()} 16:20", 6, "garten", 49.8414, 7.8671, "iPhone 13", "Garten", ["Garten"],
                        "Derselbe Garten, drei Jahre früher", 4)]
    # everyday pictures from home, a day here and there: without them the holiday would look like home
    for year in (2024, 2025, 2026):
        for month, scene in ((2, "wald"), (4, "garten"), (5, "garten"), (9, "wald"), (10, "garten")):
            events.append((f"{year}-{month:02d}-{3 + month * 2:02d} 1{month % 4}:15", 1, scene, 49.8414, 7.8671, "iPhone 13",
                           "", [], "", 0))
    for when, number, scene, lat, lon, model, album, keywords, description, rating in events:
        album, keywords, description = say(album), [say(word) for word in keywords], say(description)
        day, clock = when.split(" ")
        hour, minute = (int(part) for part in clock.split(":"))
        for n in range(number):
            total = hour * 60 + minute + n * rnd.randint(2, 9)
            second = rnd.randint(0, 59)
            stamp = f"{day.replace('-', ':')} {total // 60 % 24:02d}:{total % 60:02d}:{second:02d}"
            name = f"{day.replace('-', '')}_{total // 60 % 24:02d}{total % 60:02d}{second:02d}.jpg"
            path = fotos / say("Familie") / day[:4] / name
            path.parent.mkdir(parents=True, exist_ok=True)
            jitter = (rnd.uniform(-0.004, 0.004), rnd.uniform(-0.004, 0.004))
            path.write_bytes(jpeg(own.next(scene) or SCENES[scene](rnd), stamp, model, None if lat is None else lat + jitter[0],
                                  None if lon is None else lon + jitter[1]))
            tag(path, album=album, keywords=keywords, description=description if n == 0 else "", rating=rating if n == 0 else 0)
            count += 1
    # three photos the phone has just uploaded, still in the inbox
    for n, scene in enumerate(("garten", "abend", "stadt")):
        path = fotos / say("Eingang") / f"IMG_47{11 + n}.jpg"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(jpeg(own.next(scene) or SCENES[scene](rnd), f"2026:11:1{n} 1{n}:2{n}:0{n}", "iPhone 15", 49.8414 + n * 0.002, 7.8671))
        count += 1
    # screen backgrounds: no date from a camera, no place
    for n in range(4):
        path = fotos / say("Bildschirmfotos") / say("Hintergründe") / f"{say('hintergrund')}-{n + 1}.jpg"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(jpeg(muster(rnd), None, "", None, None))
        count += 1
    (fotos / say("Bildschirmfotos") / say("Spiele")).mkdir(parents=True, exist_ok=True)
    print(f"{count} gezeichnete Bilder in {fotos}")


if __name__ == "__main__":
    if len(sys.argv) not in (2, 3, 4) or sys.argv[2:3] not in ([], ["de"], ["en"]):
        print(__doc__, file=sys.stderr)
        sys.exit(2)
    main(Path(sys.argv[1]), *sys.argv[2:3], *(Path(extra) for extra in sys.argv[3:4]))

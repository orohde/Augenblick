"""Screenshots of a running demo installation (see make_demo.py), taken with the Chrome on this machine.

    python tools/demo/screenshots.py http://localhost:8493 BENUTZER PASSWORT ZIELORDNER [de|en]

With "en" the interface is switched to English and the names of the English demo library are used
(make_demo.py ZIELORDNER en).

Talks to a headless Chrome over its debugging port – nothing to install but the `websockets`
package that comes with uvicorn. Writes PNGs, 2880 pixels wide (desktop) and 1290 (phone).
"""

from __future__ import annotations

import asyncio
import base64
import json
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import websockets

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
PORT = 9339


class Tab:
    def __init__(self, socket):
        self.socket = socket
        self.next = 0

    async def send(self, method: str, **params):
        self.next += 1
        number = self.next
        await self.socket.send(json.dumps({"id": number, "method": method, "params": params}))
        while True:
            answer = json.loads(await self.socket.recv())
            if answer.get("id") == number:
                if "error" in answer:
                    raise RuntimeError(f"{method}: {answer['error']}")
                return answer.get("result", {})

    async def js(self, code: str):
        result = await self.send("Runtime.evaluate", expression=f"(async () => {{ {code} }})()", awaitPromise=True,
                                 returnByValue=True)
        if "exceptionDetails" in result:
            raise RuntimeError(result["exceptionDetails"].get("exception", {}).get("description", str(result["exceptionDetails"])))
        return result["result"].get("value")

    async def size(self, width: int, height: int, scale: float, mobile: bool = False):
        await self.send("Emulation.setDeviceMetricsOverride", width=width, height=height, deviceScaleFactor=scale, mobile=mobile)
        await self.send("Emulation.setEmulatedMedia", features=[{"name": "prefers-color-scheme", "value": "dark"}])

    async def snap(self, path: Path, wait: float = 1.2):
        await asyncio.sleep(wait)
        shot = await self.send("Page.captureScreenshot", format="png")
        path.write_bytes(base64.b64decode(shot["data"]))
        print("  ", path.name)


# what the demo library calls its areas, folders and albums – German and English (see make_demo.py)
NAMES = {
    "de": {"family": "Familie", "inbox": "Eingang", "shots": "Bildschirmfotos", "holidays": "Urlaub", "alps": "Urlaub/Alpen 2025",
           "phones": ["Gretas iPhone", "Pauls iPhone"]},
    "en": {"family": "Family", "inbox": "Inbox", "shots": "Screenshots", "holidays": "Holidays", "alps": "Holidays/Alps 2025",
           "phones": ["Greta’s iPhone", "Paul’s iPhone"]},
}


async def shoot(base: str, user: str, password: str, out: Path, lang: str = "de") -> None:
    names = NAMES[lang]
    out.mkdir(parents=True, exist_ok=True)
    profile = tempfile.mkdtemp(prefix="augenblick-chrome-")
    chrome = subprocess.Popen([CHROME, "--headless=new", f"--remote-debugging-port={PORT}", f"--user-data-dir={profile}",
                               "--remote-allow-origins=*", "--no-first-run", "--hide-scrollbars", "about:blank"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            try:
                pages = json.load(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json", timeout=2))
                break
            except OSError:
                time.sleep(0.2)
        target = next(page for page in pages if page["type"] == "page")
        async with websockets.connect(target["webSocketDebuggerUrl"], max_size=None) as socket:
            tab = Tab(socket)
            await tab.send("Page.enable")
            await tab.size(1440, 900, 2)
            await tab.send("Page.navigate", url=base)
            await asyncio.sleep(1.5)
            await tab.js(f"""await fetch('api/login', {{method: 'POST', headers: {{'Content-Type': 'application/json'}},
                body: JSON.stringify({{username: {json.dumps(user)}, password: {json.dumps(password)}}})}});
                localStorage.setItem('augenblick.theme', JSON.stringify('dark'));
                localStorage.setItem('augenblick.lang', JSON.stringify({json.dumps(lang)}));
                localStorage.setItem('augenblick.open', JSON.stringify({json.dumps([names["family"], names["shots"]])}));
                localStorage.setItem('augenblick.openAlbums', JSON.stringify({json.dumps([names["holidays"]])}));
                // the door for the phone: the inbox, and a key for each of two phones
                const post = (path, method, body) => fetch('api/' + path, {{method, headers: {{'Content-Type': 'application/json'}}, body: JSON.stringify(body)}});
                await post('upload', 'PUT', {{area: {json.dumps(names["inbox"])}}});
                const door = await (await fetch('api/upload')).json();
                if (!door.keys.length) for (const name of {json.dumps(names["phones"])}) await post('upload/keys', 'POST', {{name}});""")
            await tab.send("Page.reload")
            await asyncio.sleep(3)
            await tab.js("window.confirm = () => true; window.prompt = () => null;")

            # 1 timeline with the tree
            await tab.js("location.hash = '#/';")
            await tab.snap(out / "01-zeitleiste.png", 3)
            # 2 one photo with what is known about it
            await tab.js("""const items = await api('photos?month=2026-08&' + query());
                await openViewer(items[items.length - 1].key, '2026-08');""")
            await tab.snap(out / "02-einzelansicht.png", 2.5)
            # 3 the edit form
            await tab.js("$('v-edit').click();")
            await tab.snap(out / "03-bearbeiten.png", 1.5)
            await tab.js("closeViewer();")
            # 4 the map
            await tab.js("$('view-map').click();")
            await tab.snap(out / "04-karte.png", 6)
            await tab.js("setView('timeline'); await new Promise((r) => setTimeout(r, 2500));")
            # 5 several at once, with the menu of the right mouse button
            await tab.js("""setSelecting(true);
                const tiles = [...document.querySelectorAll('.tile')];
                for (const tile of tiles.slice(1, 6)) tile.click();
                const box = tiles[3].getBoundingClientRect();
                tileMenu(tiles[3], box.left + box.width * 0.6, box.top + box.height * 0.5);""")
            await tab.snap(out / "05-auswahl.png", 1.5)
            await tab.js("$('tree-menu').hidden = true; setSelecting(false);")
            # 6 an album and where its pictures lie
            await tab.js(f"location.hash = '#/album/' + {json.dumps(names['alps'])};")
            await tab.snap(out / "06-album.png", 3)
            # 7 the folder tree with its menu
            await tab.js(f"""const shots = {json.dumps(names['shots'])};
                location.hash = '#/ordner/' + shots;
                await new Promise((r) => setTimeout(r, 1500));
                const row = [...document.querySelectorAll('#tree .tree-row')].find((r) => r.dataset.path === shots);
                row.scrollIntoView({{block: 'center'}});
                const box = row.getBoundingClientRect();
                treeMenu(State.tree.find((n) => n.path === shots), box.left + 120, box.top + 14);""")
            await tab.snap(out / "07-ordner.png", 1.5)
            await tab.js("$('tree-menu').hidden = true;")
            # 8 users and areas
            await tab.js("location.hash = '#/einstellungen/benutzer';")
            await tab.snap(out / "08-bereiche.png", 2.5)
            # 9 the door for the phone
            await tab.js("""location.hash = '#/einstellungen/api';
                await new Promise((r) => setTimeout(r, 1500));
                $('up-url').textContent = 'https://fotos.example.org/dav/';       // not the address of this test machine
                $('up-area').closest('.card').scrollIntoView({block: 'center'});""")
            await tab.snap(out / "09-upload.png", 1.5)
            # 10 what was changed, to be taken back
            await tab.js("location.hash = '#/einstellungen/aenderungen';")
            await tab.snap(out / "10-aenderungen.png", 2.5)
            # 11 the bin
            await tab.js("location.hash = '#/papierkorb';")
            await tab.snap(out / "11-papierkorb.png", 2.5)
            # 12 the search
            await tab.js("""location.hash = '#/';
                await new Promise((r) => setTimeout(r, 800));
                $('search').value = 'Tegernsee'; $('search').dispatchEvent(new Event('input', {bubbles: true}));
                $('search').dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', bubbles: true}));
                $('search').form && $('search').form.requestSubmit && $('search').form.requestSubmit();""")
            await tab.snap(out / "12-suche.png", 3)
            await tab.js("$('search').value = ''; $('search').dispatchEvent(new Event('input', {bubbles: true}));")
            # 14 working on a picture: the frame of the cut in the picture, the sliders beside it
            await tab.js("""location.hash = '#/';
                await new Promise((r) => setTimeout(r, 1200));
                const items = await api('photos?month=2026-08&' + query());
                await openViewer(items[items.length - 3].key, '2026-08');
                await new Promise((r) => setTimeout(r, 1500));
                $('v-develop').click();
                await new Promise((r) => setTimeout(r, 800));
                const set = (id, value) => { $(id).value = value; $(id).dispatchEvent(new Event('input', {bubbles: true})); $(id).dispatchEvent(new Event('change', {bubbles: true})); };
                set('d-ratio', '3:2'); set('d-angle', 2.5); set('d-brightness', 12); set('d-saturation', 15);""")
            await tab.snap(out / "14-bildbearbeiten.png", 3)
            await tab.js("closeViewer();")
            # 15 a category that fills itself
            await tab.js("location.hash = '#/auto/favoriten';")
            await tab.snap(out / "15-kategorien.png", 3)
            # 16 memories: journeys and looks back
            await tab.js("location.hash = '#/erinnerungen';")
            await tab.snap(out / "16-erinnerungen.png", 3)
            # 17 the inbox
            await tab.js("location.hash = '#/eingang';")
            await tab.snap(out / "17-eingang.png", 3)
            # 18 the start page: what came in last, memories, a pinned album
            await tab.js(f"""State.pins = (await api('me/prefs', {{method: 'PUT', body: JSON.stringify({{pins: [{json.dumps(names['alps'])}]}})}})).pins;
                location.hash = '#/start';""")
            await tab.snap(out / "18-startseite.png", 3)
            # 19 an album full screen, with date and place shown
            await tab.js(f"""location.hash = '#/album/' + {json.dumps(names['alps'])};
                await new Promise((r) => setTimeout(r, 1500));
                await openShow(() => api('sequence?' + query()));
                if (!Show.info) $('show-info').click();""")
            await tab.snap(out / "19-ansehen.png", 3)
            await tab.js("closeShow(); location.hash = '#/';")

            # 13 on the phone
            await tab.size(430, 932, 3, True)
            await tab.send("Page.reload")
            await asyncio.sleep(3)
            await tab.js("location.hash = '#/';")
            await tab.snap(out / "13-handy.png", 3)
    finally:
        chrome.terminate()
        chrome.wait(timeout=10)
        shutil.rmtree(profile, ignore_errors=True)         # Chrome's throwaway profile is tens of megabytes


if __name__ == "__main__":
    if len(sys.argv) not in (5, 6) or sys.argv[5:] not in ([], ["de"], ["en"]):
        print(__doc__, file=sys.stderr)
        sys.exit(2)
    asyncio.run(shoot(sys.argv[1].rstrip("/"), sys.argv[2], sys.argv[3], Path(sys.argv[4]), *sys.argv[5:]))

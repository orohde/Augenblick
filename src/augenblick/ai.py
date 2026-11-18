"""Optional AI provider that looks at a photo and proposes a description, keywords and a date.

Two kinds of providers:

* ``anthropic`` – Claude via the official Anthropic SDK
* ``ollama``    – a local Ollama server (its OpenAI-compatible ``/v1`` API)
* ``openai``    – anything else that speaks the OpenAI chat-completions API
                  (Ollama, LM Studio, vLLM, …), called over plain HTTP

Settings live in ``ai.json`` in the data folder (mode 600). The API key is
never sent back to the browser. Every call is metered; a monthly limit in
US dollars stops further calls once reached.
"""

from __future__ import annotations

import base64
import json
import math
import os
import re
import threading
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .lang import ANSWER_IN, PHOTO_TASK, PHOTO_TASK_BEFORE, known

# USD per million tokens (input, output) – Anthropic first-party list prices
PRICES: dict[str, tuple[float, float]] = {
    "claude-fable-5-2": (8.00, 40.00),
    "claude-fable-5-1": (10.00, 50.00),
    "claude-opus-5-5": (4.00, 20.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-haiku-5-5": (0.10, 0.50),
}
MODEL_LABELS: dict[str, str] = {
    "claude-opus-5-5": "Claude Opus 5.5",
    "claude-sonnet-5-5": "Claude Sonnet 5.5",
    "claude-haiku-5-5": "Claude Haiku 5.5",
    "claude-fable-5-2": "Claude Fable 5.2",
    "claude-fable-5-1": "Claude Fable 5.1",
}
DEFAULT_MODEL = "claude-opus-5-5"
# models that accept the server-side refusal fallback ("default" form)
FALLBACK_MODELS = {"claude-fable-5-2", "claude-fable-5-1", "claude-opus-5-5", "claude-sonnet-5-5", "claude-opus-5"}
PROVIDERS = ("anthropic", "ollama", "openai")  # ollama = OpenAI-compatible API of Ollama


class AIError(RuntimeError):
    pass


MAX_PROMPTS = 20
MAX_PROMPT_NAME = 60
MAX_PROMPT_TEXT = 6000


def clean_prompts(prompts) -> list[dict]:
    """Named task texts for "KI fragen": names unique (any case), text required."""
    result, seen = [], set()
    for p in prompts or []:
        name = " ".join(str(p.get("name") or "").split())
        text = str(p.get("text") or "").strip()
        if not name and not text:
            continue
        if not name:
            raise AIError("instruction without a name")
        if not text:
            raise AIError(f"instruction without text: {name}")
        if len(name) > MAX_PROMPT_NAME or len(text) > MAX_PROMPT_TEXT:
            raise AIError(f"instruction too long: {name}")
        if name.casefold() in seen:
            raise AIError(f"instruction name used twice: {name}")
        seen.add(name.casefold())
        result.append({"name": name, "text": text})
    if len(result) > MAX_PROMPTS:
        raise AIError("too many instructions")
    return result


@dataclass
class AIConfig:
    enabled: bool = False
    provider: str = "anthropic"
    model: str = DEFAULT_MODEL
    api_key: str = ""
    base_url: str = ""
    monthly_limit_usd: float = 10.0
    excluded_areas: list[str] = field(default_factory=list)
    prompts: list[dict] = field(default_factory=list)  # [{name, text}] for "KI fragen"; empty = the built-in one
    default_prompt: str = ""      # name of the instruction that is preselected
    memories: bool = False        # may be asked to name a memory and choose the picture that stands for it
    inbox_auto: bool = False      # looks at what arrives in the inbox by itself and proposes, as in Ordnung!
    usage: dict[str, dict] = field(default_factory=dict)  # "YYYY-MM" -> {calls, input, output, cost}

    def public(self) -> dict:
        data = asdict(self)
        data.pop("api_key")
        data["key_set"] = bool(self.api_key)
        data["key_hint"] = ("…" + self.api_key[-4:]) if len(self.api_key) >= 8 else ""
        data["month"] = this_month()
        data["month_usage"] = self.usage.get(this_month(), {"calls": 0, "input": 0, "output": 0, "cost": 0.0})
        data["prices"] = {m: {"input": p[0], "output": p[1]} for m, p in PRICES.items()}
        data["models"] = [{"id": m, "label": label} for m, label in MODEL_LABELS.items()]
        return data


@dataclass
class Completion:
    text: str
    model: str
    input_tokens: int
    output_tokens: int
    cost: float
    seconds: float


def normalize_model(provider: str, model: str) -> str:
    """Accept friendly names like "Opus 5.5" for Claude and return the model ID."""
    model = (model or "").strip()
    if provider != "anthropic":
        return model
    if not model:
        return DEFAULT_MODEL
    if model in MODEL_LABELS or model.startswith("claude-"):
        return model.lower()
    key = model.lower().replace("claude", "").replace(".", "-").replace(" ", "-").strip("-")
    while "--" in key:
        key = key.replace("--", "-")
    for model_id in MODEL_LABELS:
        if model_id.endswith("-" + key):
            return model_id
    raise AIError(f"unknown model: {model}")


_THINK_RE = re.compile(r"<think>.*?(</think>|$)", re.DOTALL | re.IGNORECASE)


def _strip_thinking(text: str) -> str:
    """Local reasoning models (qwen3, deepseek-r1 …) put their reasoning in <think> tags."""
    return _THINK_RE.sub("", text).strip()


def this_month() -> str:
    return time.strftime("%Y-%m")


def cost_of(model: str, input_tokens: int, output_tokens: int) -> float:
    price = PRICES.get(model)
    if not price:
        return 0.0
    return input_tokens / 1e6 * price[0] + output_tokens / 1e6 * price[1]


class AIService:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()

    # ------------------------------------------------------------ config

    def load(self) -> AIConfig:
        if not self.path.exists():
            return AIConfig()
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        known = AIConfig.__dataclass_fields__.keys()
        config = AIConfig(**{k: v for k, v in raw.items() if k in known})
        try:
            config.model = normalize_model(config.provider, config.model)
        except AIError:
            pass  # shown to the user when the model is used
        return config

    def _save(self, config: AIConfig) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(config), indent=2, ensure_ascii=False), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    def update(self, changes: dict) -> AIConfig:
        with self._lock:
            config = self.load()
            target = (config.provider, config.base_url)
            if "provider" in changes and changes["provider"] not in PROVIDERS:
                raise AIError("unknown provider")
            for key in ("enabled", "provider", "model", "base_url", "monthly_limit_usd", "excluded_areas",
                        "default_prompt", "memories", "inbox_auto"):
                if key in changes and changes[key] is not None:
                    setattr(config, key, changes[key])
            if changes.get("prompts") is not None:
                config.prompts = clean_prompts(changes["prompts"])
            names = [p["name"] for p in config.prompts]
            if names and config.default_prompt.casefold() not in {n.casefold() for n in names}:
                config.default_prompt = names[0]
            config.model = normalize_model(config.provider, config.model)
            config.base_url = (config.base_url or "").strip().rstrip("/")
            if config.provider == "ollama" and config.base_url and not config.base_url.endswith("/v1"):
                config.base_url += "/v1"  # Ollama's OpenAI-compatible endpoints live under /v1
            config.monthly_limit_usd = max(0.0, float(config.monthly_limit_usd))
            if changes.get("clear_key") or ((config.provider, config.base_url) != target and not changes.get("api_key")):
                # a key belongs to the address it was given for: it never goes on to a new one
                config.api_key = ""
            elif changes.get("api_key"):
                key = changes["api_key"].strip()
                if config.provider == "anthropic" and not key.startswith("sk-ant-"):
                    raise AIError("not an Anthropic key")
                config.api_key = key
            self._save(config)
            return config

    def _record(self, completion: Completion) -> None:
        with self._lock:
            config = self.load()
            month = config.usage.setdefault(this_month(), {"calls": 0, "input": 0, "output": 0, "cost": 0.0})
            month["calls"] += 1
            month["input"] += completion.input_tokens
            month["output"] += completion.output_tokens
            month["cost"] = round(month["cost"] + completion.cost, 6)
            self._save(config)

    # ------------------------------------------------------------- models

    def list_models(self) -> dict:
        """Ask the provider which models are available.

        Falls back to the built-in Claude list when the provider cannot be
        asked (no key yet, offline, …); ``error`` then says why.
        """
        config = self.load()
        try:
            if config.provider == "anthropic":
                models = self._anthropic_models(config)
            else:
                models = self._openai_models(config)
            return {"source": "provider", "models": models, "error": ""}
        except AIError as exc:
            fallback = [{"id": m, "label": label} for m, label in MODEL_LABELS.items()] \
                if config.provider == "anthropic" else []
            return {"source": "builtin", "models": fallback, "error": str(exc)}

    def _anthropic_models(self, config: AIConfig) -> list[dict]:
        import anthropic

        if not config.api_key:
            raise AIError("no API key")
        client = anthropic.Anthropic(api_key=config.api_key, timeout=20.0, max_retries=1)
        try:
            models = [m for m in client.models.list() if m.id.startswith("claude-")]
        except anthropic.AuthenticationError as exc:
            raise AIError("API key rejected") from exc
        except anthropic.APIStatusError as exc:
            raise AIError(f"API error {exc.status_code}: {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise AIError("cannot reach the Anthropic API") from exc
        models.sort(key=lambda m: m.created_at, reverse=True)
        return [{"id": m.id, "label": m.display_name or m.id} for m in models]

    def _openai_models(self, config: AIConfig) -> list[dict]:
        if not config.base_url:
            raise AIError("no server address")
        request = urllib.request.Request(config.base_url + "/models")
        if config.api_key:
            request.add_header("Authorization", "Bearer " + config.api_key)
        try:
            with urllib.request.urlopen(request, timeout=20) as res:
                data = json.load(res)
        except urllib.error.HTTPError as exc:
            raise AIError(f"server error {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise AIError(f"cannot reach {config.base_url}") from exc
        ids = sorted({str(m.get("id")) for m in data.get("data", []) if m.get("id")})
        if config.provider == "ollama":
            ids = [i for i in ids if "embed" not in i]  # embedding models cannot chat
        return [{"id": i, "label": i} for i in ids]

    # -------------------------------------------------------------- calls

    def allowed_for(self, area: str) -> bool:
        return area not in self.load().excluded_areas

    def complete(self, system: str, prompt: str, max_tokens: int = 2000, effort: str = "low",
                 force: bool = False, images: list[bytes] | None = None, model: str = "") -> Completion:
        """One question, one answer. ``images`` are JPEG files the model gets to see before the text;
        ``model`` asks another model than the one in the settings, for this one call."""
        config = self.load()
        if model:
            config.model = model
        if not config.enabled and not force:
            raise AIError("AI is disabled")
        spent = config.usage.get(this_month(), {}).get("cost", 0.0)
        if config.monthly_limit_usd and spent >= config.monthly_limit_usd:
            raise AIError("monthly limit reached")
        started = time.time()
        if config.provider == "anthropic":
            completion = self._anthropic(config, system, prompt, max_tokens, effort, images or [])
        else:
            completion = self._openai(config, system, prompt, max_tokens, images or [])
        completion.seconds = round(time.time() - started, 2)
        self._record(completion)
        return completion

    def _anthropic(self, config: AIConfig, system: str, prompt: str, max_tokens: int, effort: str,
                   images: list[bytes]) -> Completion:
        import anthropic

        if not config.api_key:
            raise AIError("no API key")
        client = anthropic.Anthropic(api_key=config.api_key, timeout=180.0, max_retries=2)
        params = dict(
            model=config.model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": [
                *({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                               "data": base64.standard_b64encode(image).decode("ascii")}}
                  for image in images),
                {"type": "text", "text": prompt}]}],
            output_config={"effort": effort},
        )
        try:
            if config.model in FALLBACK_MODELS:
                response = client.beta.messages.create(
                    **params, betas=["server-side-fallback-2026-07-01"], fallbacks="default",
                )
            else:
                response = client.messages.create(**params)
        except anthropic.AuthenticationError as exc:
            raise AIError("API key rejected") from exc
        except anthropic.NotFoundError as exc:
            raise AIError(f"unknown model: {config.model}") from exc
        except anthropic.RateLimitError as exc:
            raise AIError("rate limit – please try again later") from exc
        except anthropic.APIStatusError as exc:
            raise AIError(f"API error {exc.status_code}: {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise AIError("cannot reach the Anthropic API") from exc
        if response.stop_reason == "refusal":
            raise AIError("the model declined this request")
        text = "".join(block.text for block in response.content if block.type == "text")
        usage = response.usage
        model = response.model or config.model
        return Completion(
            text=text.strip(),
            model=model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cost=cost_of(config.model, usage.input_tokens, usage.output_tokens),
            seconds=0.0,
        )

    def _openai(self, config: AIConfig, system: str, prompt: str, max_tokens: int, images: list[bytes]) -> Completion:
        if not config.base_url:
            raise AIError("no server address")
        body = json.dumps({
            "model": config.model,
            "max_tokens": max_tokens,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": [
                *({"type": "image_url", "image_url": {
                    "url": "data:image/jpeg;base64," + base64.standard_b64encode(image).decode("ascii")}}
                  for image in images),
                {"type": "text", "text": prompt}] if images else prompt}],
        }).encode()
        request = urllib.request.Request(config.base_url + "/chat/completions", data=body, method="POST")
        request.add_header("Content-Type", "application/json")
        if config.api_key:
            request.add_header("Authorization", "Bearer " + config.api_key)
        try:
            with urllib.request.urlopen(request, timeout=300) as res:
                data = json.load(res)
        except urllib.error.HTTPError as exc:
            raise AIError(f"server error {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise AIError(f"cannot reach {config.base_url}") from exc
        try:
            text = _strip_thinking(data["choices"][0]["message"]["content"] or "")
        except (KeyError, IndexError, TypeError) as exc:
            raise AIError("unexpected answer from server") from exc
        usage = data.get("usage") or {}
        return Completion(
            text=text.strip(),
            model=data.get("model", config.model),
            input_tokens=int(usage.get("prompt_tokens", 0)),
            output_tokens=int(usage.get("completion_tokens", 0)),
            cost=0.0,
            seconds=0.0,
        )


# ------------------------------------------------------------------ photos

DEFAULT_PROMPT_NAME, DEFAULT_PROMPT = PHOTO_TASK["de"]
PHOTO_SYSTEM = (
    "Du hilfst, ein privates Fotoarchiv zu beschriften. Du bekommst ein Foto, einige Angaben dazu und einen Auftrag. "
    "Antworte ausschließlich mit einem JSON-Objekt in genau dieser Form, ohne Text davor oder danach:\n"
    '{"description": "Text oder leer", "keywords": ["Schlagwort", "..."], '
    '"date": "JJJJ-MM-TT, JJJJ-MM, JJJJ oder null", '
    '"place": {"name": "Ort, Land", "lat": Breite, "lon": Länge} oder null, '
    '"note": "kurzer Hinweis für den Menschen oder leer"}\n'
    "\"date\" nur füllen, wenn das Bild laut den Angaben kein Aufnahmedatum hat und du eines schätzen kannst; "
    "in \"note\" steht dann, woran du die Schätzung festmachst.\n"
    "\"place\" nur füllen, wenn du den Ort am Bild selbst oder an den Angaben (etwa dem Ordner) erkennst – ein Bauwerk, "
    "ein Schild, eine bekannte Landschaft –, mit den ungefähren Koordinaten in Dezimalgrad; sonst null. "
    "Stehen in den Angaben schon Koordinaten, fülle \"place\" nur, wenn das Bild deutlich woanders entstanden ist, "
    "und schreibe in \"note\", woran du das siehst. "
    "Erkennst du einen der festen Orte des Archivs wieder, nimm genau dessen Namen und Koordinaten."
)
_JSON = re.compile(r"\{.*\}", re.DOTALL)
_DATE = re.compile(r"^(\d{4})(?:-(\d\d))?(?:-(\d\d))?$")


def prompts_of(config: AIConfig, lang: str = "de") -> tuple[list[dict], str]:
    """The instructions on offer and the preselected one; without own ones the built-in,
    in the language of whoever asks."""
    name, text = PHOTO_TASK[known(lang)]
    if config.prompts:
        # a stored copy of the old built-in wording is nobody's own text: it follows the built-in one
        offered = [{**p, "text": text} if p["text"].strip() in PHOTO_TASK_BEFORE else p for p in config.prompts]
        return offered, config.default_prompt or offered[0]["name"]
    return [{"name": name, "text": text}], name


def in_language(system: str, lang: str = "de") -> str:
    """What the AI is told, with the wish to answer in the language of whoever asks."""
    return system + ANSWER_IN[known(lang)]


def photo_prompt(photo: dict, task: str, known_keywords: list[str], spots: list[dict] | None = None) -> str:
    facts = [f"Dateiname: {photo['name']}", f"Ordner: {photo['folder']}"]
    facts.append(f"Aufnahmedatum: {photo['taken']}" if photo["taken_src"] == "exif"
                 else "Aufnahmedatum: keines im Bild")
    if photo.get("lat") is not None:
        facts.append(f"Ort (Koordinaten): {photo['lat']:.5f}, {photo['lon']:.5f}")
        named = ", ".join(part for part in (photo.get("city"), photo.get("state"), photo.get("country")) if part)
        if named:
            facts.append(f"Nächster Ort laut Koordinaten: {named}")
    people = [name for name in (photo.get("people") or []) if name]
    if people:
        facts.append("Personen im Bild (von Menschen benannt, verlässlich): " + ", ".join(people))
    if photo.get("description"):
        facts.append(f"Bisherige Beschreibung: {photo['description']}")
    if photo.get("keywords"):
        facts.append("Bisherige Schlagworte: " + ", ".join(photo["keywords"]))
    if known_keywords:
        facts.append("Schlagworte, die im Archiv schon vorkommen: " + ", ".join(known_keywords))
    if spots:
        facts.append("Feste Orte des Archivs (Name: Breite, Länge): "
                     + "; ".join(f"{s['name']}: {s['lat']:.5f}, {s['lon']:.5f}" for s in spots))
    return "Angaben zum Foto:\n" + "\n".join(facts) + "\n\nAuftrag:\n" + task.strip()


def parse_suggestion(text: str) -> dict:
    """What the model answered, tidied up. Anything that is not the agreed JSON is an error."""
    found = _JSON.search(text or "")
    try:
        data = json.loads(found.group(0)) if found else None
    except ValueError:
        data = None
    if not isinstance(data, dict):
        raise AIError("the answer was not in the agreed form")
    words = data.get("keywords") if isinstance(data.get("keywords"), list) else []
    keywords: list[str] = []
    for word in words:
        word = " ".join(str(word).split())[:100]
        if word and word not in keywords:
            keywords.append(word)
    date = None
    match = _DATE.match(str(data.get("date") or "").strip())
    if match:
        year, month, day = match.group(1), match.group(2) or "01", match.group(3) or "01"
        try:
            time.strptime(f"{year}-{month}-{day}", "%Y-%m-%d")
            date = {"taken": f"{year}-{month}-{day}T12:00:00",
                    "precision": "day" if match.group(3) else "month" if match.group(2) else "year"}
        except ValueError:
            date = None
    return {"description": " ".join(str(data.get("description") or "").split())[:2000], "keywords": keywords[:30],
            "date": date, "place": _place(data.get("place")), "note": " ".join(str(data.get("note") or "").split())[:500]}


def _place(data) -> dict | None:
    """Where the model thinks the picture was taken: a name and coordinates, or nothing."""
    if not isinstance(data, dict):
        return None
    try:
        lat, lon = float(data.get("lat")), float(data.get("lon"))
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(lat) and math.isfinite(lon)) or abs(lat) > 90 or abs(lon) > 180 or (lat == 0 and lon == 0):
        return None
    return {"name": " ".join(str(data.get("name") or "").split())[:120], "lat": round(lat, 5), "lon": round(lon, 5)}


MEMORY_SYSTEM = (
    "Du siehst einige Fotos aus einer privaten Sammlung, die zusammengehören: eine Reise oder dieselbe Woche "
    "in einem früheren Jahr. Gib ihnen einen kurzen Titel und einen Satz, so wie man es in ein Fotoalbum schreibt: "
    "schlicht, warm, ohne Übertreibung. Erfinde nichts, was auf den Bildern nicht zu sehen ist oder "
    "in den Angaben steht; schätze kein Alter und bewerte keine Personen. Wähle das Bild, das am besten für alle steht. "
    'Antworte nur mit JSON: {"title": "höchstens 40 Zeichen", "text": "ein Satz, höchstens 160 Zeichen", "cover": Nummer des Bildes}'
)


def memory_prompt(memory: dict, photos: list[dict]) -> str:
    lines = [f"Gefunden wurde: {memory['title']}, {memory['sub']}, {memory['count']} Bilder."]
    for number, photo in enumerate(photos, 1):
        where = ", ".join(part for part in (photo.get("city"), photo.get("country")) if part)
        who = ", ".join(photo.get("people") or [])
        lines.append(f"Bild {number}: {photo['taken'][:10]}" + (f", {where}" if where else "") + (f", darauf: {who}" if who else ""))
    return "\n".join(lines)


def parse_memory(text: str, keys: list[str]) -> dict:
    """What the model said about a memory, tidied up – the cover as the key of the picture it chose."""
    found = _JSON.search(text or "")
    try:
        data = json.loads(found.group(0)) if found else None
    except ValueError:
        data = None
    if not isinstance(data, dict) or not str(data.get("title") or "").strip():
        raise AIError("the answer was not in the agreed form")
    try:
        cover = keys[int(data.get("cover")) - 1] if 1 <= int(data.get("cover")) <= len(keys) else ""
    except (TypeError, ValueError):
        cover = ""
    return {"title": " ".join(str(data["title"]).split())[:60], "text": " ".join(str(data.get("text") or "").split())[:240],
            "cover": cover}

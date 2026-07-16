"""Claude-API-Wrapper: Text-, JSON- und Vision-Aufrufe + Kostenerfassung.

API-Key kommt aus .env (ANTHROPIC_API_KEY). Alle Aufrufe protokollieren ihren
Tokenverbrauch nach output/costs.json des jeweiligen Projekts.
"""

from __future__ import annotations

import base64
import json
import os
import threading
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from . import paths

COSTS_FILE = "costs.json"
DEFAULT_MODEL = "claude-sonnet-4-6"

# USD pro Million Tokens (Input, Output) – für die Kosten-Anzeige im Dashboard
MODEL_PRICES_USD_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-haiku-4-5": (1.0, 5.0),
}

_cost_lock = threading.Lock()


class ApiGuthabenLeer(RuntimeError):
    """Anthropic-Guthaben aufgebraucht (oder Key ungültig): Jeder weitere
    Aufruf würde genauso scheitern. Stoppt Pipeline UND Warteschlange
    sofort - nach dem Aufladen einfach fortsetzen, bereits erledigte
    Schritte/Projekte bleiben erhalten."""


def _ist_guthaben_fehler(exc: Exception) -> bool:
    """Fehler, bei denen Weiterprobieren sinnlos ist (kein Guthaben,
    Spend-Limit erreicht, Key ungültig) - im Gegensatz zu vorübergehenden
    Fehlern wie Rate-Limits oder Netzwerk-Aussetzern."""
    try:
        import anthropic
    except ImportError:
        return False
    if isinstance(exc, anthropic.AuthenticationError):
        return True
    text = str(exc).lower()
    if isinstance(exc, (anthropic.BadRequestError,
                        anthropic.PermissionDeniedError)):
        return ("credit" in text or "billing" in text or "balance" in text
                or "spend" in text)
    if isinstance(exc, anthropic.RateLimitError):
        # normales Rate-Limit = vorübergehend; Monats-Ausgabenlimit nicht
        return "spend limit" in text
    return False


def estimate_cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    prices = MODEL_PRICES_USD_PER_MTOK.get(model)
    if prices is None:
        prices = MODEL_PRICES_USD_PER_MTOK[DEFAULT_MODEL]
    return input_tokens / 1e6 * prices[0] + output_tokens / 1e6 * prices[1]


def record_usage(project: str | None, zweck: str, model: str,
                 input_tokens: int, output_tokens: int) -> None:
    if not project:
        return
    cost = estimate_cost_usd(model, input_tokens, output_tokens)
    f = paths.output_dir(project) / COSTS_FILE
    with _cost_lock:
        data = {"aufrufe": [], "summe_usd": 0.0}
        if f.is_file():
            data = json.loads(f.read_text(encoding="utf-8"))
        data["aufrufe"].append({
            "zweck": zweck,
            "modell": model,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "kosten_usd": round(cost, 6),
        })
        data["summe_usd"] = round(sum(a["kosten_usd"] for a in data["aufrufe"]), 4)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def load_costs(project: str) -> dict:
    f = paths.output_dir(project) / COSTS_FILE
    if not f.is_file():
        return {"aufrufe": [], "summe_usd": 0.0}
    return json.loads(f.read_text(encoding="utf-8"))


def extract_json(text: str) -> Any:
    """Erstes JSON-Objekt/-Array aus einer Antwort extrahieren (robust
    gegen Prosa oder ```json-Zäune drumherum)."""
    decoder = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch in "[{":
            try:
                obj, _ = decoder.raw_decode(text[i:])
                return obj
            except json.JSONDecodeError:
                continue
    raise ValueError(f"Keine gültige JSON-Struktur in der Antwort:\n{text[:500]}")


class ClaudeClient:
    """Dünner Wrapper um anthropic.Anthropic mit Projekt-Kostenerfassung."""

    def __init__(self, model: str = DEFAULT_MODEL, project: str | None = None):
        load_dotenv(paths.REPO_ROOT / ".env")
        self.model = os.environ.get("AUTOEDIT_CLAUDE_MODEL", model)
        self.project = project
        self._client = None

    @property
    def client(self):
        if self._client is None:
            if not os.environ.get("ANTHROPIC_API_KEY"):
                raise RuntimeError(
                    "ANTHROPIC_API_KEY fehlt. Bitte .env anlegen "
                    "(siehe .env.example)."
                )
            import anthropic

            self._client = anthropic.Anthropic()
        return self._client

    def _create(self, zweck: str, messages: list[dict], system: str | None = None,
                max_tokens: int = 4096):
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": messages,
        }
        if system:
            kwargs["system"] = system
        try:
            response = self.client.messages.create(**kwargs)
        except Exception as exc:
            if _ist_guthaben_fehler(exc):
                raise ApiGuthabenLeer(
                    "Anthropic-API-Guthaben aufgebraucht oder Key ungültig "
                    "– Lauf gestoppt, damit nichts sinnlos weiterläuft. "
                    "Nach dem Aufladen einfach wieder ▶ Alles ausführen "
                    "bzw. die Warteschlange starten: es geht genau beim "
                    f"letzten Stand weiter. (Original-Fehler: {exc})"
                ) from exc
            raise
        usage = getattr(response, "usage", None)
        if usage is not None:
            record_usage(
                self.project, zweck, self.model,
                int(getattr(usage, "input_tokens", 0) or 0),
                int(getattr(usage, "output_tokens", 0) or 0),
            )
        return response

    @staticmethod
    def _text(response) -> str:
        parts = [b.text for b in response.content if getattr(b, "type", "") == "text"]
        return "\n".join(parts).strip()

    def complete_text(self, zweck: str, prompt: str, system: str | None = None,
                      max_tokens: int = 4096) -> str:
        resp = self._create(zweck, [{"role": "user", "content": prompt}],
                            system=system, max_tokens=max_tokens)
        return self._text(resp)

    def complete_json(self, zweck: str, prompt: str, system: str | None = None,
                      max_tokens: int = 4096) -> Any:
        text = self.complete_text(zweck, prompt, system=system, max_tokens=max_tokens)
        try:
            return extract_json(text)
        except ValueError:
            # Ein Wiederholungsversuch mit explizitem Hinweis
            retry = self.complete_text(
                zweck + "_retry",
                prompt + "\n\nWICHTIG: Antworte ausschließlich mit gültigem JSON, "
                         "ohne Erklärtext und ohne Markdown-Zäune.",
                system=system, max_tokens=max_tokens,
            )
            return extract_json(retry)

    def describe_images(self, zweck: str, image_paths: list[Path], prompt: str,
                        max_tokens: int = 1024) -> str:
        """Vision-Aufruf mit mehreren JPEG/PNG-Frames."""
        content: list[dict] = []
        for p in image_paths:
            media_type = "image/png" if p.suffix.lower() == ".png" else "image/jpeg"
            data = base64.standard_b64encode(p.read_bytes()).decode("ascii")
            content.append({
                "type": "image",
                "source": {"type": "base64", "media_type": media_type, "data": data},
            })
        content.append({"type": "text", "text": prompt})
        resp = self._create(zweck, [{"role": "user", "content": content}],
                            max_tokens=max_tokens)
        return self._text(resp)

    def describe_images_json(self, zweck: str, image_paths: list[Path], prompt: str,
                             max_tokens: int = 1024) -> Any:
        return extract_json(self.describe_images(zweck, image_paths, prompt,
                                                 max_tokens=max_tokens))

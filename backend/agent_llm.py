"""Turn a shopper's sentence into structured requirements.

Mock mode is deterministic and makes no network call. LLM mode calls an
OpenAI-compatible chat endpoint configured with environment variables.
"""

import json
import logging
import os
import re
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import httpx2
from dotenv import load_dotenv
from pydantic import ValidationError

from agent_schema import ExtractedShoppingRequest, ProductNeed
from settings import DEFAULT_DATABASE, DEFAULT_ENV_FILE, ROOT

logger = logging.getLogger("nulixpay.agent")

CLARIFYING_QUESTION = (
    "Which product should I look for: headphones, smartphones, laptops, "
    "accessories, or smartwatches? A budget in euros helps too."
)
_CATEGORY_PATTERNS = (
    ("headphones", r"\bheadphones?\b|\bearbuds?\b|\bbuds\b"),
    ("laptops", r"\blaptops?\b"),
    ("smartwatches", r"\bsmartwatches\b|\bsmartwatch\b|\bwatches\b|\bwatch\b"),
    ("smartphones", r"\bsmartphones?\b|\bphones?\b"),
    ("accessories", r"\bchargers?\b|\bhubs?\b|\bsleeves?\b|\baccessories\b|\baccessory\b"),
)
_FEATURE_PATTERNS = (
    ("noise", r"\bnoise(?:\s+cancell?ing)?\b|\banc\b"),
    ("bluetooth", r"\bbluetooth\b"),
    ("gps", r"\bgps\b"),
    ("hdmi", r"\bhdmi\b"),
    ("usb", r"\busb(?:-c)?\b"),
    ("battery", r"\bbattery\b"),
    ("lightweight", r"\blightweight\b|\blight\b"),
    ("camera", r"\bcameras?\b"),
    ("charger", r"\bcharg(?:er|e)\b"),
    ("always-on", r"\balways[- ]on\b"),
    ("compact", r"\bcompact\b"),
)


class AgentConfigError(Exception):
    """Raised when agent mode or the LLM settings are unusable."""


class AgentLLMError(Exception):
    """Raised when the model call fails. The message is safe to show."""


@dataclass(frozen=True)
class AgentSettings:
    mode: str
    api_base: str
    api_key: str
    model: str
    database_path: Path
    timeout_seconds: float = 20.0


def load_agent_settings(env_file: Path | None = None) -> AgentSettings:
    """Read agent settings. The API key is not logged or returned to clients."""
    load_dotenv(DEFAULT_ENV_FILE if env_file is None else env_file, override=False)
    mode = os.environ.get("AGENT_MODE", "mock").strip().lower() or "mock"
    if mode not in {"mock", "llm"}:
        raise AgentConfigError("AGENT_MODE must be mock or llm.")
    api_key = os.environ.get("LLM_API_KEY", "").strip()
    if mode == "llm" and not api_key:
        raise AgentConfigError(
            "LLM_API_KEY is missing. Add it on the server, or set AGENT_MODE=mock."
        )
    database = os.environ.get("PAYPAL_DB_PATH", "").strip()
    database_path = Path(database) if database else DEFAULT_DATABASE
    if not database_path.is_absolute():
        database_path = ROOT / database_path
    api_base = os.environ.get("LLM_API_BASE", "https://api.openai.com/v1").strip().rstrip("/")
    model = os.environ.get("LLM_MODEL", "gpt-4o-mini").strip()
    if mode == "llm" and (not api_base.startswith("https://") or not model):
        raise AgentConfigError("LLM_API_BASE must be an https URL and LLM_MODEL must be set.")
    return AgentSettings(
        mode=mode,
        api_base=api_base or "https://api.openai.com/v1",
        api_key=api_key,
        model=model or "gpt-4o-mini",
        database_path=database_path,
    )


class MockShoppingExtractor:
    """Rule-based extractor used in tests and when AGENT_MODE=mock."""

    def extract(self, message: str, history: list[str] | None = None) -> ExtractedShoppingRequest:
        text = " ".join([*(history or []), message]).casefold()
        budget = _budget(text)
        needs: list[ProductNeed] = []
        for clause in re.split(r"\band\b", text):
            category = _category(clause)
            features = _features(clause)
            priorities = _priorities(clause)
            if category is None and not features:
                continue
            needs.append(
                ProductNeed(
                    category=category,
                    features=features,
                    priorities=priorities,
                    quantity=1,
                )
            )
        if not needs:
            return ExtractedShoppingRequest(
                budget_eur=budget,
                needs=[],
                ambiguous=True,
                clarifying_question=CLARIFYING_QUESTION,
            )
        return ExtractedShoppingRequest(
            budget_eur=budget,
            needs=needs,
            ambiguous=False,
            clarifying_question=None,
        )


class LLMShoppingExtractor:
    """OpenAI-compatible chat call. The shopper message cannot change the URL."""

    def __init__(self, settings: AgentSettings, client: object | None = None):
        self._settings = settings
        self._owns_client = client is None
        self._client = client or httpx2.Client(
            timeout=httpx2.Timeout(settings.timeout_seconds),
            follow_redirects=False,
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def extract(self, message: str, history: list[str] | None = None) -> ExtractedShoppingRequest:
        messages = [{"role": "system", "content": _SYSTEM_PROMPT}]
        for earlier in history or []:
            messages.append({"role": "user", "content": earlier[:2000]})
        messages.append({"role": "user", "content": message[:2000]})
        url = f"{self._settings.api_base}/chat/completions"
        try:
            response = self._client.post(
                url,
                headers={
                    "Authorization": f"Bearer {self._settings.api_key}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                json={
                    "model": self._settings.model,
                    "temperature": 0,
                    "messages": messages,
                    "response_format": {"type": "json_object"},
                },
            )
        except httpx2.TimeoutException as exc:
            raise AgentLLMError("The shopping model did not respond in time.") from exc
        except httpx2.TransportError as exc:
            raise AgentLLMError("The shopping model could not be reached.") from exc
        logger.info("LLM chat completion -> %s", response.status_code)
        if response.status_code != 200:
            raise AgentLLMError("The shopping model could not read the request.")
        try:
            content = response.json()["choices"][0]["message"]["content"]
            payload = json.loads(_json_text(content))
        except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise AgentLLMError("The shopping model returned an unreadable reply.") from exc
        try:
            return ExtractedShoppingRequest.model_validate(payload)
        except ValidationError as exc:
            raise AgentLLMError(
                "The shopping model returned requirements the catalog cannot use."
            ) from exc


def _budget(text: str) -> Decimal | None:
    match = re.search(
        r"(?:under|below|moins de|maximum of|maximum|max|budget of|budget)\s*(\d+(?:[.,]\d{1,2})?)",
        text,
    )
    if match is None:
        match = re.search(r"(\d+(?:[.,]\d{1,2})?)\s*(?:euros|euro|eur|€)", text)
    if match is None:
        return None
    return Decimal(match.group(1).replace(",", "."))


def _category(text: str) -> str | None:
    for category, pattern in _CATEGORY_PATTERNS:
        if re.search(pattern, text):
            return category
    return None


def _features(text: str) -> list[str]:
    found: list[str] = []
    for feature, pattern in _FEATURE_PATTERNS:
        if re.search(pattern, text):
            found.append(feature)
    return found


def _priorities(text: str) -> list[str]:
    if re.search(r"\bcheap(?:est)?\b|\blowest price\b|\baffordable\b", text):
        return ["lower price"]
    if re.search(r"\bbest\b|\bpremium\b", text):
        return ["more matching features"]
    return []


def _json_text(content: object) -> str:
    if not isinstance(content, str):
        raise ValueError("model content is not text")
    text = content.strip()
    fenced = re.search(r"```(?:json)?\s*(.+?)```", text, flags=re.DOTALL)
    if fenced:
        return fenced.group(1).strip()
    return text


_SYSTEM_PROMPT = """
You extract shopping requirements for NuLixPay. Reply with one JSON object and no other text.
Use only these categories: headphones, smartphones, laptops, accessories, smartwatches.
Do not invent or return product names, prices, stock, or product ids.
Fields:
- budget_eur: total cart budget as a number, or null
- needs: list of objects with category, features, priorities, quantity, max_unit_price_eur
- priorities may only contain "lower price" or "more matching features"
- quantity is an integer from 1 to 5
- ambiguous: true when the product type is unclear
- clarifying_question: a short question when ambiguous, otherwise null
Add one need for each distinct product the shopper asked for.
""".strip()

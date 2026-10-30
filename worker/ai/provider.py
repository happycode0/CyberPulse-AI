"""Which gateway the AI layer calls: OpenRouter, or MixRoute when AI_PROVIDER=mixroute.

Both take OpenAI-style chat completions, but they differ where the AI layer's discipline lives:

- OpenRouter publishes every model's routes, prices and capabilities, routes one request down a
  `models` chain itself, reports what each call cost, and says what the key has left
  (`GET /api/v1/key`).
- MixRoute lists model ids only. The prices are ours, reviewed in config/models.mixroute.yaml;
  the client walks the chain itself; a call's cost is its tokens at those prices; and what the
  month has left is read from our own cost ledger. Its key's quota, set in MixRoute's console,
  is the hard stop: a spent one answers 403.

The rest of the AI layer asks this module, so the choice is made in one place.
"""

from pydantic import SecretStr

from worker.settings import Settings

OPENROUTER = "openrouter"
MIXROUTE = "mixroute"

_KEY_NAMES = {OPENROUTER: "OPENROUTER_API_KEY", MIXROUTE: "MIX_ROUTER_KEY"}
_LABELS = {OPENROUTER: "OpenRouter", MIXROUTE: "MixRoute"}


def is_mixroute(settings: Settings) -> bool:
    return settings.ai_provider == MIXROUTE


def api_key(settings: Settings) -> SecretStr | None:
    """The provider's key, or None when it is unset or empty."""
    key = settings.mix_router_key if is_mixroute(settings) else settings.openrouter_api_key
    return key if key is not None and key.get_secret_value() else None


def key_name(settings: Settings) -> str:
    """The environment variable the key comes from, for log lines that say it is missing."""
    return _KEY_NAMES[settings.ai_provider]


def label(settings: Settings) -> str:
    return _LABELS[settings.ai_provider]

"""The two gates every published payload passes: JSON Schema and the secret scan.

Both are fail-closed. A payload that cannot be positively shown to match its schema and to
be free of credentials is not published.
"""

import functools
import json
import math
import os
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import unquote, unquote_plus, urlsplit

import jsonschema
from dotenv import dotenv_values
from pydantic import SecretStr
from referencing import Registry, Resource

from worker.settings import Settings

SCHEMAS_DIR = Path(__file__).resolve().parents[2] / "schemas"

MAX_REPORTED_ERRORS = 5
MAX_MESSAGE_CHARS = 200

# Env values shorter than this are not treated as secrets: a 1-3 character "value" would
# match ordinary content and block every publish. Real credentials are far longer.
MIN_SECRET_LENGTH = 8

_REDACTED = "<redacted: contains a suspected secret>"
_ROOT = "$"


class ValidationFailure(Exception):
    """A payload failed the schema or the secret scan and must not be published."""


# --- Secret detection --------------------------------------------------------------------

_CREDENTIAL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("OpenRouter API key", re.compile(r"sk-or-[A-Za-z0-9_\-]{16,}")),
    ("OpenAI-style API key", re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}")),
    ("Tavily API key", re.compile(r"tvly-[A-Za-z0-9_\-]{16,}")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}")),
    ("GitHub fine-grained token", re.compile(r"github_pat_[A-Za-z0-9_]{20,}")),
    ("Telegram bot token", re.compile(r"\b\d{6,12}:[A-Za-z0-9_\-]{30,}")),
    ("AWS access key id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}")),
    ("private key block", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")),
    (
        "JSON Web Token",
        re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]+"),
    ),
    (
        "URL with embedded password",
        re.compile(r"\b[A-Za-z][A-Za-z0-9+.\-]*://[^\s/:@?#]+:[^\s/@?#]+@"),
    ),
)

_GENERIC_TOKEN = re.compile(r"[A-Za-z0-9_\-]{32,}")
_HEX = re.compile(r"[0-9a-fA-F]+")
# MD5, SHA-1, SHA-224, SHA-256, SHA-384 and SHA-512 digests are ordinary IOC content.
_HASH_LENGTHS = frozenset({32, 40, 56, 64, 96, 128})
_MIN_TOKEN_ENTROPY = 3.5
_ALPHA_RUN = re.compile(r"[A-Za-z]+")
# Title-case words, lower-case words (2+ letters) and acronyms; an acronym directly before
# a Title-case word ("IEXObfuscation") gives up its last capital to that word.
_WORD_RUN = re.compile(r"(?:[A-Z][a-z]+|[a-z]{2,}|[A-Z]{2,}(?![a-z]))+")
_WORD_PIECE = re.compile(r"[A-Z][a-z]+|[a-z]{2,}|[A-Z]{2,}(?![a-z])")
_MIN_WORD_FRACTION = 0.8
_MIN_AVG_WORD_LENGTH = 3.25

_SECRET_ENV_NAME = re.compile(
    r"(?:^|_)(?:API_?KEY|KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIALS?|PRIVATE_KEY)$"
)


def _entropy(token: str) -> float:
    counts = {c: token.count(c) for c in set(token)}
    return -sum(n / len(token) * math.log2(n / len(token)) for n in counts.values())


def _looks_like_words(token: str) -> bool:
    """Advisory-title-shaped: mostly dictionary-shaped words, acronyms and numbers.

    Titles and URL slugs ("Log4Shell-Exploitation-Guidance", "Exchange2019", "KB5031354",
    "PowerShellInvokeExpression2026Campaign") are made of alphabetic runs that split cleanly
    into Title-case words, lower-case words and acronyms, joined by digits or hyphens. A
    random base62/base64 secret has no such structure: its case flips mid-run, so its runs
    do not split into words of a natural length. Runs shorter than three letters (the
    "v2", "x86", "0d" noise between digits) are ignored; at least 80% of the letters in the
    longer runs must sit in word-shaped runs.
    """
    runs = [r for r in _ALPHA_RUN.findall(token) if len(r) >= 3]
    if not runs:
        return False
    word_runs = [r for r in runs if _WORD_RUN.fullmatch(r)]
    if sum(len(r) for r in word_runs) < _MIN_WORD_FRACTION * sum(len(r) for r in runs):
        return False
    # Any string of letters can be cut into 1-3 letter "words" at its case flips; real
    # titles are made of longer ones.
    pieces = [p for r in word_runs for p in _WORD_PIECE.findall(r)]
    return sum(map(len, pieces)) / len(pieces) >= _MIN_AVG_WORD_LENGTH


def _looks_like_generic_secret(token: str) -> bool:
    if _HEX.fullmatch(token) and len(token) in _HASH_LENGTHS:
        return False
    if _looks_like_words(token):
        return False
    return (
        any(c.isupper() for c in token)
        and any(c.islower() for c in token)
        and any(c.isdigit() for c in token)
        and _entropy(token) >= _MIN_TOKEN_ENTROPY
    )


def _url_password(url: str) -> str | None:
    try:
        return urlsplit(url).password
    except ValueError:
        return None


def _settings_secret_env_names() -> set[str]:
    """Upper-case env names of every secret field on `Settings` (and the database URL)."""
    names = {"DATABASE_URL"}
    for name, field in Settings.model_fields.items():
        if SecretStr in getattr(field.annotation, "__args__", ()) or field.annotation is SecretStr:
            names.add(name.upper())
    return names


def _env_file_values() -> dict[str, str | None]:
    """Values from the `.env` file(s) `Settings` reads, parsed directly.

    Deliberately independent of `Settings()`: that class validates the whole variable set
    and raises if any required one is missing, and the scan must not go blind because of
    that. A missing file is normal (containers get their variables from the environment);
    a file that exists but cannot be read means secrets we cannot see, so fail closed.
    """
    configured = Settings.model_config.get("env_file")
    if not configured:
        return {}
    paths = [configured] if isinstance(configured, str | Path) else list(configured)
    values: dict[str, str | None] = {}
    for path in map(Path, paths):
        if not path.is_file():
            continue
        try:
            values.update(dotenv_values(path))
        except Exception as exc:  # noqa: BLE001 - any failure to read means an unscanned source
            raise ValidationFailure(
                f"secret scan cannot read {path} ({type(exc).__name__}); refusing to publish"
            ) from exc
    return values


def _secret_literals() -> list[tuple[str, str]]:
    """(source name, value) for every populated secret this process holds.

    Read fresh on every call, never cached: the environment is the source of truth and a
    stale copy would defeat the point. Sources are the process environment and the `.env`
    file, filtered by name: the `Settings` secret fields, any name that says it is a
    credential, and the password inside any database URL. Catching the literal value is what
    catches a key whose shape no regex anticipated.
    """
    found: dict[str, str] = {}
    known = _settings_secret_env_names()

    def add(name: str, value: str | None) -> None:
        if value and len(value) >= MIN_SECRET_LENGTH:
            found.setdefault(value, name)

    def add_value(name: str, value: str | None) -> None:
        add(name, value)
        if value and "://" in value:
            add(f"{name} (password)", _url_password(value))

    for source in (_env_file_values(), os.environ):
        for name, value in source.items():
            upper = name.upper()
            if upper in known or _SECRET_ENV_NAME.search(upper):
                add_value(upper, value)

    return [(name, value) for value, name in found.items()]


def _views(text: str) -> tuple[str, ...]:
    """The string plus its URL-decoded forms, so an encoded secret still matches."""
    views = [text]
    for decode in (unquote, unquote_plus):
        decoded = decode(text)
        if decoded not in views:
            views.append(decoded)
    return tuple(views)


def _scan_string(text: str, literals: list[tuple[str, str]]) -> list[str]:
    """Labels (never values) of every secret found in `text`."""
    labels: list[str] = []

    def hit(label: str) -> None:
        if label not in labels:
            labels.append(label)

    for view in _views(text):
        for label, pattern in _CREDENTIAL_PATTERNS:
            if pattern.search(view):
                hit(label)
        if any(_looks_like_generic_secret(t) for t in _GENERIC_TOKEN.findall(view)):
            hit("high-entropy token")
        for name, value in literals:
            if value in view:
                hit(f"value of {name}")
    return labels


def _walk(
    obj: Any, path: str, literals: list[tuple[str, str]]
) -> Iterator[tuple[str, list[str]]]:
    """Yield (path, labels) for every string anywhere in `obj`, keys included.

    A key that is itself a secret is reported as `<redacted key>`, so a finding's path can
    never carry the secret it points at.
    """
    if obj is None or isinstance(obj, bool):
        return
    if isinstance(obj, dict):
        for index, (key, value) in enumerate(obj.items()):
            key_text = key if isinstance(key, str) else str(key)
            key_labels = _scan_string(key_text, literals)
            if key_labels:
                yield f"{path}.<redacted key #{index}>", key_labels
                child = f"{path}.<redacted key #{index}>"
            else:
                child = f"{path}.{key_text}"
            yield from _walk(value, child, literals)
    elif isinstance(obj, list | tuple | set | frozenset):
        for index, value in enumerate(obj):
            yield from _walk(value, f"{path}[{index}]", literals)
    else:
        text = obj if isinstance(obj, str) else str(obj)
        labels = _scan_string(text, literals)
        if labels:
            yield path, labels


def scan_for_secrets(payload: Any) -> list[str]:
    """Findings for every suspected secret anywhere in `payload`; empty means clean.

    Each finding is `"<path>: <what matched>"`. It never contains the secret itself, so the
    result is safe to log and to put in an exception.
    """
    literals = _secret_literals()
    return [
        f"{path}: {', '.join(labels)}" for path, labels in _walk(payload, _ROOT, literals)
    ]


def scan_text_for_secrets(text: str) -> list[str]:
    """Like `scan_for_secrets` for already-serialised output (the bytes that hit disk)."""
    labels = _scan_string(text, _secret_literals())
    return [f"<serialised output>: {', '.join(labels)}"] if labels else []


# --- Schema validation -------------------------------------------------------------------


@functools.lru_cache(maxsize=1)
def _registry() -> Registry:
    registry = Registry()
    for path in sorted(SCHEMAS_DIR.glob("*.schema.json")):
        contents = json.loads(path.read_text())
        registry = registry.with_resource(path.name, Resource.from_contents(contents))
    return registry


@functools.lru_cache(maxsize=None)
def _validator(schema_name: str) -> jsonschema.Draft202012Validator:
    path = SCHEMAS_DIR / f"{schema_name}.schema.json"
    if not re.fullmatch(r"[a-z0-9\-]+", schema_name) or not path.is_file():
        raise ValidationFailure(f"unknown schema {schema_name!r}")
    schema = json.loads(path.read_text())
    jsonschema.Draft202012Validator.check_schema(schema)
    return jsonschema.Draft202012Validator(schema, registry=_registry())


def _describe(error: jsonschema.ValidationError) -> str:
    where = ".".join(str(p) for p in error.absolute_path) or "<root>"
    message = error.message
    if len(message) > MAX_MESSAGE_CHARS:
        message = message[:MAX_MESSAGE_CHARS] + "..."
    # jsonschema messages quote the offending value; if that value is a secret it must
    # not travel into an exception, a log line or a Telegram alert.
    if scan_for_secrets({"message": message}) or scan_for_secrets({"where": where}):
        message, where = _REDACTED, "<redacted>"
    return f"{where}: {message}"


def validate_payload(payload: dict[str, Any], schema_name: str) -> None:
    """Raise `ValidationFailure` unless `payload` matches `schemas/<schema_name>.schema.json`."""
    validator = _validator(schema_name)
    errors = sorted(validator.iter_errors(payload), key=lambda e: list(map(str, e.absolute_path)))
    if not errors:
        return
    shown = "; ".join(_describe(e) for e in errors[:MAX_REPORTED_ERRORS])
    extra = len(errors) - MAX_REPORTED_ERRORS
    more = f" (+{extra} more)" if extra > 0 else ""
    raise ValidationFailure(f"{schema_name} payload failed schema validation: {shown}{more}")

"""Synchronous OpenRouter acquisition client for strict JSON completions."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
import http.client
import json
import math
import os
import re
from types import MappingProxyType
from typing import Any
import urllib.error
import urllib.request


OPENROUTER_ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_API_KEY_ENV = "OPENROUTER_API_KEY"
OPENROUTER_TIMEOUT_SECONDS = 120.0
OPENROUTER_MAX_BYTES = 4 * 1024 * 1024
OPENROUTER_USER_AGENT = "draftomen-openrouter/1 (+https://github.com/andreagrandi/draftomen)"
OPENROUTER_ACCEPT = "application/json"
OPENROUTER_ACCEPT_ENCODING = "identity"
OPENROUTER_METADATA_HEADER = "X-OpenRouter-Metadata"
OPENROUTER_METADATA_VALUE = "enabled"

REASONING_EFFORTS = frozenset({"none", "minimal", "low", "medium", "high", "xhigh", "max"})

_REQUEST_MAX_BYTES = 4 * 1024 * 1024
_MAX_SCHEMA_NODES = _REQUEST_MAX_BYTES
_MAX_CONTAINERS = 64
_MAX_COST_CHARS = 128
_CHUNK_BYTES = 64 * 1024
_MISSING = object()
_MODEL_RE = re.compile(r"[^\s/]+/[^\s/]+")
_SCHEMA_NAME_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
_TOKEN_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_ERROR_MESSAGES = MappingProxyType(
    {
        "invalid_config": "OpenRouter acquisition failed (invalid_config).",
        "missing_api_key": "OpenRouter acquisition failed (missing_api_key).",
        "unsafe_redirect": "OpenRouter acquisition failed (unsafe_redirect).",
        "timeout": "OpenRouter acquisition failed (timeout).",
        "transport": "OpenRouter acquisition failed (transport).",
        "api": "OpenRouter acquisition failed (api).",
        "refusal": "OpenRouter acquisition failed (refusal).",
        "truncated": "OpenRouter acquisition failed (truncated).",
        "malformed_response": "OpenRouter acquisition failed (malformed_response).",
        "too_large": "OpenRouter acquisition failed (too_large).",
        "credential_leak": "OpenRouter acquisition failed (credential_leak).",
    }
)


@dataclass(frozen=True, slots=True)
class OpenRouterResponse:
    """Represent one accepted completion and its trusted accounting."""

    content: str
    model: str
    provider: str | None
    input_tokens: int | None
    cached_input_tokens: int | None
    output_tokens: int | None
    reasoning_tokens: int | None
    cost_usd: str | None


class OpenRouterClientError(RuntimeError):
    """Represent a bounded, classified OpenRouter acquisition failure."""

    code: str

    def __init__(self, *, code: str) -> None:
        try:
            message = _ERROR_MESSAGES[code]
        except (KeyError, TypeError):
            raise ValueError("Unknown OpenRouter acquisition error code.") from None
        self.code = code
        super().__init__(message)


def _raise(code: str) -> None:
    raise OpenRouterClientError(code=code) from None


def _call_guarded(function: Callable[..., Any], *args: Any, code: str) -> Any:
    """Invoke one guarded accessor, translating any failure to a fixed code."""
    value: Any = None
    failed = False
    try:
        value = function(*args)
    except OpenRouterClientError:
        raise
    except Exception:
        failed = True
    if failed:
        _raise(code)
    return value


def _validate_prompt(value: Any) -> None:
    """Require one nonempty prompt without rewriting its content."""
    if not isinstance(value, str) or not value.strip():
        _raise("invalid_config")


def _environment_credential() -> str:
    """Read the single fixed credential source without retaining it."""
    value = os.environ.get(OPENROUTER_API_KEY_ENV)
    if value is None or not value.strip():
        _raise("missing_api_key")
    if any(ord(character) < 33 or ord(character) > 126 for character in value):
        _raise("invalid_config")
    return value


def _ensure_no_credential(value: Any, credential: str) -> None:
    """Reject any string key or value that repeats the credential."""
    if isinstance(value, str):
        if credential in value:
            _raise("credential_leak")
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            _ensure_no_credential(key, credential)
            _ensure_no_credential(item, credential)
        return
    if isinstance(value, list):
        for item in value:
            _ensure_no_credential(item, credential)


def _reject_oversize_strings(value: Any, *, limit: int) -> None:
    """Reject any string whose JSON encoding cannot fit the ceiling."""
    if isinstance(value, str):
        if len(value) > limit:
            _raise("too_large")
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            _reject_oversize_strings(key, limit=limit)
            _reject_oversize_strings(item, limit=limit)
        return
    if isinstance(value, list):
        for item in value:
            _reject_oversize_strings(item, limit=limit)


def _canonical_bytes(value: Any, *, limit: int, code: str) -> bytes:
    """Serialize canonical JSON, rejecting payloads beyond a fixed ceiling."""
    _reject_oversize_strings(value, limit=limit)
    encoder = json.JSONEncoder(
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    fragments: list[str] = []
    size = 0
    exceeded = False
    failed = False
    try:
        for fragment in encoder.iterencode(value):
            size += len(fragment.encode("utf-8"))
            if size > limit:
                exceeded = True
                break
            fragments.append(fragment)
    except (TypeError, ValueError, RecursionError):
        failed = True
    if exceeded:
        _raise("too_large")
    if failed:
        _raise(code)
    return "".join(fragments).encode("utf-8")


class _SchemaBudget:
    """Bound one schema snapshot traversal to a fixed node count."""

    __slots__ = ("_remaining",)

    def __init__(self) -> None:
        self._remaining = _MAX_SCHEMA_NODES

    def spend(self) -> None:
        """Charge one schema node against the traversal budget."""
        self._remaining -= 1
        if self._remaining < 0:
            _raise("too_large")


def _check_schema_string(value: str) -> None:
    """Reject one schema string that cannot fit inside the request ceiling."""
    if len(value) > _REQUEST_MAX_BYTES:
        _raise("too_large")


def _snapshot_schema_node(
    value: Any,
    *,
    depth: int,
    seen: set[int],
    budget: _SchemaBudget,
) -> Any:
    """Validate and detach one JSON-native schema node inside a visit budget."""
    budget.spend()
    if value is None or isinstance(value, (str, bool)):
        if isinstance(value, str):
            _check_schema_string(value)
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            _raise("invalid_config")
        return value
    if isinstance(value, Mapping):
        if depth > _MAX_CONTAINERS:
            _raise("invalid_config")
        marker = id(value)
        if marker in seen:
            _raise("invalid_config")
        seen.add(marker)
        try:
            snapshot: dict[str, Any] = {}
            for key, item in value.items():
                if not isinstance(key, str):
                    _raise("invalid_config")
                budget.spend()
                _check_schema_string(key)
                snapshot[key] = _snapshot_schema_node(
                    item,
                    depth=depth + 1,
                    seen=seen,
                    budget=budget,
                )
            return snapshot
        finally:
            seen.discard(marker)
    if isinstance(value, list):
        if depth > _MAX_CONTAINERS:
            _raise("invalid_config")
        marker = id(value)
        if marker in seen:
            _raise("invalid_config")
        seen.add(marker)
        try:
            return [
                _snapshot_schema_node(item, depth=depth + 1, seen=seen, budget=budget)
                for item in value
            ]
        finally:
            seen.discard(marker)
    _raise("invalid_config")


def _snapshot_schema(schema: Any) -> dict[str, Any]:
    """Validate one caller schema and detach it from later mutation."""
    if not isinstance(schema, Mapping) or not schema:
        _raise("invalid_config")
    return _snapshot_schema_node(schema, depth=1, seen=set(), budget=_SchemaBudget())


def _check_nesting(text: str) -> None:
    """Reject containers nested deeper than the fixed ceiling."""
    depth = 0
    quoted = False
    escaped = False
    for character in text:
        if quoted:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                quoted = False
        elif character == '"':
            quoted = True
        elif character in "[{":
            depth += 1
            if depth > _MAX_CONTAINERS:
                _raise("malformed_response")
        elif character in "]}":
            depth -= 1


def _object_pairs_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Build one JSON object while rejecting duplicate keys."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _raise("malformed_response")
        result[key] = value
    return result


def _reject_constant(value: str) -> Any:
    """Reject the NaN and Infinity JSON constants."""
    _raise("malformed_response")


def _decode_json(text: str) -> Any:
    """Decode strict JSON without duplicate keys, non-finite numbers or deep nesting."""
    if not text:
        _raise("malformed_response")
    _check_nesting(text)
    document: Any = None
    failed = False
    try:
        document = json.loads(
            text,
            parse_float=Decimal,
            parse_constant=_reject_constant,
            object_pairs_hook=_object_pairs_hook,
        )
    except OpenRouterClientError:
        raise
    except (ValueError, UnicodeError, RecursionError, ArithmeticError):
        failed = True
    if failed:
        _raise("malformed_response")
    return document


def _decode_envelope(text: str) -> dict[str, Any]:
    """Decode one response envelope that must be a JSON object."""
    document = _decode_json(text)
    if not isinstance(document, dict):
        _raise("malformed_response")
    return document


def _completion_content(document: Mapping[str, Any]) -> str:
    """Classify one completion envelope and isolate its assistant content."""
    if document.get("error") is not None:
        _raise("api")
    choices = document.get("choices")
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], Mapping):
        _raise("malformed_response")
    choice = choices[0]
    finish_reason = choice.get("finish_reason", _MISSING)
    if choice.get("error") is not None or finish_reason == "error":
        _raise("api")
    message = choice.get("message")
    refusal = message.get("refusal") if isinstance(message, Mapping) else None
    if refusal is not None and not isinstance(refusal, str):
        _raise("malformed_response")
    if refusal or finish_reason == "content_filter":
        _raise("refusal")
    if finish_reason is None or finish_reason == "length":
        _raise("truncated")
    if not isinstance(finish_reason, str) or finish_reason != "stop":
        _raise("malformed_response")
    if not isinstance(message, Mapping):
        _raise("malformed_response")
    if message.get("role") != "assistant":
        _raise("malformed_response")
    content = message.get("content")
    if not isinstance(content, str) or not content:
        _raise("malformed_response")
    tool_calls = message.get("tool_calls")
    if tool_calls is not None and (not isinstance(tool_calls, list) or tool_calls):
        _raise("malformed_response")
    return content


def _provider_name(document: Mapping[str, Any]) -> str | None:
    """Return the uniquely selected provider without guessing a route."""
    metadata = document.get("openrouter_metadata")
    if metadata is None:
        return None
    if not isinstance(metadata, Mapping):
        _raise("malformed_response")
    endpoints = metadata.get("endpoints")
    if endpoints is None:
        return None
    if not isinstance(endpoints, Mapping):
        _raise("malformed_response")
    available = endpoints.get("available")
    if available is None:
        return None
    if not isinstance(available, list):
        _raise("malformed_response")
    selected: list[Mapping[str, Any]] = []
    for endpoint in available:
        if not isinstance(endpoint, Mapping):
            _raise("malformed_response")
        if "selected" not in endpoint:
            continue
        flag = endpoint["selected"]
        if not isinstance(flag, bool):
            _raise("malformed_response")
        if flag:
            selected.append(endpoint)
    if len(selected) != 1:
        return None
    provider = selected[0].get("provider")
    if not isinstance(provider, str) or not provider:
        _raise("malformed_response")
    return provider


def _token_count(value: Any) -> int | None:
    """Validate one optional nonnegative JSON token counter."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        _raise("malformed_response")
    return value


def _detail_token_count(usage: Mapping[str, Any], details_key: str, counter_key: str) -> int | None:
    """Read one optional counter from a nested usage detail object."""
    details = usage.get(details_key)
    if details is None:
        return None
    if not isinstance(details, Mapping):
        _raise("malformed_response")
    return _token_count(details.get(counter_key))


def _normalize_cost(value: Any) -> str | None:
    """Normalize one optional billed cost to an exact decimal string."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        _raise("malformed_response")
    if isinstance(value, Decimal):
        amount = value
    else:
        try:
            amount = Decimal(value)
        except (ArithmeticError, ValueError):
            amount = None
    if amount is None or not amount.is_finite() or amount < 0:
        _raise("malformed_response")
    if amount == 0:
        return "0"
    if abs(amount.adjusted()) > _MAX_COST_CHARS:
        _raise("malformed_response")
    text = format(amount, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    if not text or len(text) > _MAX_COST_CHARS:
        _raise("malformed_response")
    return text


def _accounting(document: Mapping[str, Any]) -> dict[str, Any]:
    """Extract trusted token counts and the billed account charge."""
    accounting: dict[str, Any] = {
        "input_tokens": None,
        "cached_input_tokens": None,
        "output_tokens": None,
        "reasoning_tokens": None,
        "cost_usd": None,
    }
    usage = document.get("usage")
    if usage is None:
        return accounting
    if not isinstance(usage, Mapping):
        _raise("malformed_response")
    accounting["input_tokens"] = _token_count(usage.get("prompt_tokens"))
    accounting["output_tokens"] = _token_count(usage.get("completion_tokens"))
    accounting["cached_input_tokens"] = _detail_token_count(
        usage, "prompt_tokens_details", "cached_tokens"
    )
    accounting["reasoning_tokens"] = _detail_token_count(
        usage, "completion_tokens_details", "reasoning_tokens"
    )
    accounting["cost_usd"] = _normalize_cost(usage.get("cost"))
    cached_input_tokens = accounting["cached_input_tokens"]
    input_tokens = accounting["input_tokens"]
    if (
        cached_input_tokens is not None
        and input_tokens is not None
        and cached_input_tokens > input_tokens
    ):
        _raise("malformed_response")
    reasoning_tokens = accounting["reasoning_tokens"]
    output_tokens = accounting["output_tokens"]
    if (
        reasoning_tokens is not None
        and output_tokens is not None
        and reasoning_tokens > output_tokens
    ):
        _raise("malformed_response")
    return accounting


def _build_response(document: Mapping[str, Any], *, credential: str) -> OpenRouterResponse:
    """Extract one accepted response, discarding routing and reasoning detail."""
    content = _completion_content(document)
    parsed_content = _decode_json(content)
    _ensure_no_credential(content, credential)
    _ensure_no_credential(parsed_content, credential)
    model = document.get("model")
    if not isinstance(model, str) or not model:
        _raise("malformed_response")
    provider = _provider_name(document)
    accounting = _accounting(document)
    _ensure_no_credential(model, credential)
    _ensure_no_credential(provider, credential)
    _ensure_no_credential(accounting["cost_usd"], credential)
    return OpenRouterResponse(content=content, model=model, provider=provider, **accounting)


def _close_response(response: Any) -> None:
    """Close one response or error stream without leaking close failures."""
    close: Any = None
    try:
        close = getattr(response, "close", None)
    except Exception:
        return
    if callable(close):
        try:
            close()
        except Exception:
            pass


def _response_url(response: Any) -> str:
    """Read the final response URL."""
    getter = _call_guarded(getattr, response, "geturl", None, code="malformed_response")
    if not callable(getter):
        _raise("malformed_response")
    value = _call_guarded(getter, code="malformed_response")
    if not isinstance(value, str) or not value:
        _raise("malformed_response")
    return value


def _response_status(response: Any) -> int:
    """Read one response status code."""
    value = _call_guarded(getattr, response, "status", _MISSING, code="malformed_response")
    if value is _MISSING or value is None:
        getter = _call_guarded(getattr, response, "getcode", None, code="malformed_response")
        if not callable(getter):
            _raise("malformed_response")
        value = _call_guarded(getter, code="malformed_response")
    if isinstance(value, bool) or not isinstance(value, int):
        _raise("malformed_response")
    return value


def _headers(response: Any) -> Any:
    """Read the response header mapping."""
    headers = _call_guarded(getattr, response, "headers", _MISSING, code="malformed_response")
    if headers is _MISSING or headers is None:
        getter = _call_guarded(getattr, response, "info", None, code="malformed_response")
        if not callable(getter):
            _raise("malformed_response")
        headers = _call_guarded(getter, code="malformed_response")
    get_all = _call_guarded(getattr, headers, "get_all", None, code="malformed_response")
    if not callable(get_all):
        _raise("malformed_response")
    return headers


def _header_values(headers: Any, name: str) -> list[Any] | None:
    """Read every value of one repeated response header."""
    getter = _call_guarded(getattr, headers, "get_all", None, code="malformed_response")
    if not callable(getter):
        _raise("malformed_response")
    values = _call_guarded(getter, name, code="malformed_response")
    if values is None:
        return None
    if isinstance(values, str) or not isinstance(values, (list, tuple)):
        _raise("malformed_response")
    return list(values)


def _split_parameters(value: str) -> list[str]:
    """Split one header value on semicolons outside quoted strings."""
    parts: list[str] = []
    current: list[str] = []
    quoted = False
    escaped = False
    for character in value:
        if quoted:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                quoted = False
        elif character == '"':
            quoted = True
        elif character == ";":
            parts.append("".join(current))
            current = []
            continue
        current.append(character)
    if quoted:
        _raise("malformed_response")
    parts.append("".join(current))
    return parts


def _strip_optional_whitespace(value: str) -> str:
    """Trim only the HTTP optional whitespace characters SP and HTAB."""
    return value.strip(" \t")


def _reject_control_characters(value: str) -> None:
    """Reject any control character that is not HTTP optional whitespace."""
    for character in value:
        if (ord(character) < 32 and character not in "\t ") or ord(character) == 127:
            _raise("malformed_response")


def _unquote_parameter(raw_value: str) -> str:
    """Return one trimmed parameter value with optional quotes removed."""
    value = _strip_optional_whitespace(raw_value)
    if not value:
        _raise("malformed_response")
    if value.startswith('"'):
        if len(value) < 2 or not value.endswith('"'):
            _raise("malformed_response")
        return value[1:-1]
    if '"' in value:
        _raise("malformed_response")
    return value


def _validate_content_type(headers: Any) -> None:
    """Require exactly one JSON content type with an optional UTF-8 charset."""
    values = _header_values(headers, "Content-Type")
    if values is None or len(values) != 1 or not isinstance(values[0], str):
        _raise("malformed_response")
    _reject_control_characters(values[0])
    parts = _split_parameters(values[0])
    media_type = _strip_optional_whitespace(parts[0])
    type_part, slash, subtype_part = media_type.partition("/")
    if (
        not slash
        or "/" in subtype_part
        or not _TOKEN_RE.fullmatch(type_part)
        or not _TOKEN_RE.fullmatch(subtype_part)
        or media_type.casefold() != "application/json"
    ):
        _raise("malformed_response")
    charset_seen = False
    for parameter in parts[1:]:
        name, separator, raw_value = parameter.partition("=")
        if charset_seen or not separator:
            _raise("malformed_response")
        parameter_name = _strip_optional_whitespace(name)
        if not _TOKEN_RE.fullmatch(parameter_name) or parameter_name.casefold() != "charset":
            _raise("malformed_response")
        charset = _unquote_parameter(raw_value)
        if not _TOKEN_RE.fullmatch(charset) or charset.casefold() != "utf-8":
            _raise("malformed_response")
        charset_seen = True


def _validate_content_headers(headers: Any, *, max_bytes: int) -> int | None:
    """Validate one identity-encoded JSON response and its declared length."""
    _validate_content_type(headers)
    encodings = _header_values(headers, "Content-Encoding")
    if encodings is not None:
        if len(encodings) != 1 or not isinstance(encodings[0], str):
            _raise("malformed_response")
        _reject_control_characters(encodings[0])
        if _strip_optional_whitespace(encodings[0]).casefold() != "identity":
            _raise("malformed_response")
    lengths = _header_values(headers, "Content-Length")
    if lengths is None:
        return None
    if len(lengths) != 1 or not isinstance(lengths[0], str):
        _raise("malformed_response")
    _reject_control_characters(lengths[0])
    value = _strip_optional_whitespace(lengths[0])
    if not value or any(character not in "0123456789" for character in value):
        _raise("malformed_response")
    normalized = value.lstrip("0") or "0"
    limit = str(max_bytes)
    if len(normalized) > len(limit) or (len(normalized) == len(limit) and normalized > limit):
        _raise("too_large")
    return int(normalized)


def _read_bounded(response: Any, *, max_bytes: int, declared_length: int | None) -> bytes:
    """Read one bounded response body in fixed-size chunks."""
    payload = bytearray()
    while True:
        remaining = max_bytes - len(payload)
        chunk: Any = None
        code: str | None = None
        try:
            chunk = response.read(min(_CHUNK_BYTES, remaining + 1))
        except OpenRouterClientError:
            raise
        except http.client.IncompleteRead:
            code = "truncated"
        except TimeoutError:
            code = "timeout"
        except urllib.error.URLError as error:
            code = "timeout" if isinstance(error.reason, TimeoutError) else "transport"
        except OSError:
            code = "transport"
        except Exception:
            code = "transport"
        if code is not None:
            _raise(code)
        if not isinstance(chunk, (bytes, bytearray, memoryview)):
            _raise("malformed_response")
        if not chunk:
            break
        if len(payload) + len(chunk) > max_bytes:
            _raise("too_large")
        if declared_length is not None and len(payload) + len(chunk) > declared_length:
            _raise("malformed_response")
        payload.extend(chunk)
    if declared_length is not None and len(payload) < declared_length:
        _raise("truncated")
    if not payload:
        _raise("malformed_response")
    return bytes(payload)


def _decode_utf8(payload: bytes) -> str:
    """Decode one bounded response body as strict UTF-8."""
    text = ""
    failed = False
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        failed = True
    if failed:
        _raise("malformed_response")
    return text


def _open_transport(opener: Callable[..., Any], request: Any, *, timeout: float) -> Any:
    """Invoke the opener exactly once, translating failures to fixed codes."""
    response: Any = None
    code: str | None = None
    try:
        response = opener(request, timeout=timeout)
    except OpenRouterClientError:
        raise
    except urllib.error.HTTPError as error:
        _close_response(error)
        status = error.code
        if isinstance(status, int) and not isinstance(status, bool) and 300 <= status < 400:
            code = "unsafe_redirect"
        else:
            code = "api"
    except TimeoutError:
        code = "timeout"
    except urllib.error.URLError as error:
        code = "timeout" if isinstance(error.reason, TimeoutError) else "transport"
    except OSError:
        code = "transport"
    except Exception:
        code = "transport"
    if code is not None:
        _raise(code)
    return response


def _reject_redirect(fp: Any) -> Any:
    """Close one redirect stream and fail without reading its target."""
    _close_response(fp)
    _raise("unsafe_redirect")


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Reject every redirect instead of repeating the authenticated POST."""

    def http_error_301(self, req: Any, fp: Any, code: int, msg: str, headers: Any) -> Any:
        """Reject one permanent redirect without parsing its Location header."""
        return _reject_redirect(fp)

    def http_error_302(self, req: Any, fp: Any, code: int, msg: str, headers: Any) -> Any:
        """Reject one found redirect without parsing its Location header."""
        return _reject_redirect(fp)

    def http_error_303(self, req: Any, fp: Any, code: int, msg: str, headers: Any) -> Any:
        """Reject one see-other redirect without parsing its Location header."""
        return _reject_redirect(fp)

    def http_error_307(self, req: Any, fp: Any, code: int, msg: str, headers: Any) -> Any:
        """Reject one temporary redirect without parsing its Location header."""
        return _reject_redirect(fp)

    def http_error_308(self, req: Any, fp: Any, code: int, msg: str, headers: Any) -> Any:
        """Reject one permanent-redirect-with-method without parsing Location."""
        return _reject_redirect(fp)


class OpenRouterClient:
    """Submit one strict JSON-schema completion to OpenRouter."""

    def __init__(
        self,
        *,
        model: str,
        schema_name: str,
        schema: Mapping[str, Any],
        reasoning_effort: str,
        max_tokens: int,
        opener: Callable[..., Any] | None = None,
        timeout_seconds: float = OPENROUTER_TIMEOUT_SECONDS,
        max_bytes: int = OPENROUTER_MAX_BYTES,
    ) -> None:
        if (
            not isinstance(model, str)
            or not model
            or model != model.strip()
            or not _MODEL_RE.fullmatch(model)
            or any(ord(character) < 33 or ord(character) > 126 for character in model)
        ):
            _raise("invalid_config")
        if not isinstance(schema_name, str) or not _SCHEMA_NAME_RE.fullmatch(schema_name):
            _raise("invalid_config")
        if not isinstance(reasoning_effort, str) or reasoning_effort not in REASONING_EFFORTS:
            _raise("invalid_config")
        if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens <= 0:
            _raise("invalid_config")
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
            _raise("invalid_config")
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)):
            _raise("invalid_config")
        timeout_value = 0.0
        try:
            timeout_value = float(timeout_seconds)
        except (OverflowError, ValueError):
            timeout_value = 0.0
        if not math.isfinite(timeout_value) or timeout_value <= 0:
            _raise("invalid_config")
        if opener is not None and not callable(opener):
            _raise("invalid_config")
        snapshot = _snapshot_schema(schema)
        _canonical_bytes(snapshot, limit=_REQUEST_MAX_BYTES, code="invalid_config")
        # Later requests embed this detached snapshot rather than a retained
        # canonical string, so caller mutation after construction cannot reach
        # the request body.
        self.model = model
        self.schema_name = schema_name
        self.reasoning_effort = reasoning_effort
        self.max_tokens = max_tokens
        self.opener = opener
        self.timeout_seconds = timeout_value
        self.max_bytes = max_bytes
        self._schema = snapshot

    def complete(self, *, system_prompt: str, user_prompt: str) -> OpenRouterResponse:
        """Submit exactly one authenticated completion request."""

        _validate_prompt(system_prompt)
        _validate_prompt(user_prompt)
        credential = _environment_credential()
        body = self._request_body(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            credential=credential,
        )
        request = urllib.request.Request(
            OPENROUTER_ENDPOINT,
            data=body,
            headers={
                "Authorization": "Bearer " + credential,
                "Content-Type": "application/json",
                "Accept": OPENROUTER_ACCEPT,
                "Accept-Encoding": OPENROUTER_ACCEPT_ENCODING,
                "User-Agent": OPENROUTER_USER_AGENT,
                OPENROUTER_METADATA_HEADER: OPENROUTER_METADATA_VALUE,
            },
            method="POST",
        )
        opener = self.opener
        if opener is None:
            opener = urllib.request.build_opener(_NoRedirectHandler()).open
        response: Any = None
        try:
            response = _open_transport(opener, request, timeout=self.timeout_seconds)
            final_url = _response_url(response)
            if final_url != OPENROUTER_ENDPOINT:
                _raise("unsafe_redirect")
            status = _response_status(response)
            if 300 <= status < 400:
                _raise("unsafe_redirect")
            if status != 200:
                _raise("api")
            headers = _headers(response)
            declared_length = _validate_content_headers(headers, max_bytes=self.max_bytes)
            payload = _read_bounded(
                response,
                max_bytes=self.max_bytes,
                declared_length=declared_length,
            )
            return _build_response(_decode_envelope(_decode_utf8(payload)), credential=credential)
        finally:
            _close_response(response)

    def _request_body(self, *, system_prompt: str, user_prompt: str, credential: str) -> bytes:
        """Build one credential-free canonical request body."""
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": self.schema_name,
                    "strict": True,
                    "schema": self._schema,
                },
            },
            "reasoning": {"effort": self.reasoning_effort},
            "provider": {"require_parameters": True},
            "max_tokens": self.max_tokens,
            "stream": False,
        }
        _ensure_no_credential(body, credential)
        return _canonical_bytes(body, limit=_REQUEST_MAX_BYTES, code="invalid_config")


__all__ = [
    "OPENROUTER_ACCEPT",
    "OPENROUTER_ACCEPT_ENCODING",
    "OPENROUTER_API_KEY_ENV",
    "OPENROUTER_ENDPOINT",
    "OPENROUTER_MAX_BYTES",
    "OPENROUTER_METADATA_HEADER",
    "OPENROUTER_METADATA_VALUE",
    "OPENROUTER_TIMEOUT_SECONDS",
    "OPENROUTER_USER_AGENT",
    "REASONING_EFFORTS",
    "OpenRouterClient",
    "OpenRouterClientError",
    "OpenRouterResponse",
]

"""Offline boundary tests for the OpenRouter acquisition client."""

from __future__ import annotations

import builtins
from collections.abc import Callable
from dataclasses import FrozenInstanceError, asdict
from email.message import Message
import http.client
import io
import json
import logging
from pathlib import Path
import socket
import traceback
from typing import Any
from urllib.error import HTTPError, URLError
import urllib.request

import pytest

from draftomen.openrouter_client import (
    OPENROUTER_ACCEPT,
    OPENROUTER_ACCEPT_ENCODING,
    OPENROUTER_API_KEY_ENV,
    OPENROUTER_ENDPOINT,
    OPENROUTER_MAX_BYTES,
    OPENROUTER_METADATA_HEADER,
    OPENROUTER_METADATA_VALUE,
    OPENROUTER_TIMEOUT_SECONDS,
    OPENROUTER_USER_AGENT,
    REASONING_EFFORTS,
    OpenRouterClient,
    OpenRouterClientError,
    OpenRouterResponse,
)


_FIXTURE = Path(__file__).parent / "fixtures" / "openrouter" / "completion.json"
_FIXTURE_BYTES = _FIXTURE.read_bytes()
_FIXTURE_CONTENT = '{"answer":"fixture","usage":{"cost":999}}'
_REQUEST_LIMIT = 4 * 1024 * 1024
_CHUNK_LIMIT = 64 * 1024
_WIDE_SCHEMA_KEYS = _REQUEST_LIMIT // 16
_OVERFLOWING_DECIMAL = "1e9999999999999999999"
_KEY = "sentinel-openrouter-key-123"
_OTHER_KEY = "second-sentinel-key"
_MODEL = "openai/fixture-model"
_SYSTEM_PROMPT = "Return the fixture answer as JSON."
_USER_PROMPT = "What is the fixture answer?"
_CODES = (
    "invalid_config",
    "missing_api_key",
    "unsafe_redirect",
    "timeout",
    "transport",
    "api",
    "refusal",
    "truncated",
    "malformed_response",
    "too_large",
    "credential_leak",
)


@pytest.fixture(autouse=True)
def _offline_sentinel_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provide a synthetic credential and fail every real network attempt."""

    monkeypatch.setenv(OPENROUTER_API_KEY_ENV, _KEY)

    def blocked(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("OpenRouter acquisition attempted a network connection")

    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket.socket, "connect", blocked)


class _Response:
    def __init__(
        self,
        payload: bytes | Any,
        *,
        url: str = OPENROUTER_ENDPOINT,
        status: int | Any = 200,
        headers: Message | None = None,
        read_error: BaseException | None = None,
        read_chunk: Any = None,
        close_error: BaseException | None = None,
    ) -> None:
        self._stream = io.BytesIO(payload) if isinstance(payload, bytes) else None
        self._payload = payload
        self._read_error = read_error
        self._read_chunk = read_chunk
        self._close_error = close_error
        self.read_sizes: list[int] = []
        self.url = url
        self.status = status
        self.code = status
        self.reason = "OK"
        self.msg = "OK"
        self.headers = headers if headers is not None else Message()
        self.closed = False

    def read(self, size: int = -1) -> Any:
        self.read_sizes.append(size)
        if self._read_error is not None:
            raise self._read_error
        if self._read_chunk is not None:
            chunk, self._read_chunk = self._read_chunk, None
            return chunk
        if self._stream is None:
            return self._payload
        return self._stream.read(size)

    def geturl(self) -> str:
        return self.url

    def getcode(self) -> Any:
        return self.status

    def info(self) -> Message:
        return self.headers

    def close(self) -> None:
        self.closed = True
        if self._close_error is not None:
            raise self._close_error


class _RecordingBytesIO(io.BytesIO):
    """Record every read attempt so an unread error body stays provable."""

    def __init__(self, payload: bytes) -> None:
        super().__init__(payload)
        self.reads: list[int] = []

    def read(self, size: int = -1) -> bytes:
        self.reads.append(size)
        return super().read(size)


class _HostileAttributeResponse:
    """Proxy one response while making a single attribute lookup fail."""

    def __init__(self, response: _Response, attribute: str) -> None:
        self._response = response
        self._attribute = attribute

    def __getattr__(self, name: str) -> Any:
        if name == self._attribute:
            raise RuntimeError(f"hostile {name} lookup for {_KEY}")
        return getattr(self._response, name)


def _headers(
    content_type: str | None = "application/json",
    *,
    content_encoding: str | None = None,
    content_length: int | str | None = None,
    duplicate_content_type: bool = False,
    duplicate_content_length: bool = False,
) -> Message:
    headers = Message()
    if content_type is not None:
        headers["Content-Type"] = content_type
        if duplicate_content_type:
            headers["Content-Type"] = content_type
    if content_encoding is not None:
        headers["Content-Encoding"] = content_encoding
    if content_length is not None:
        headers["Content-Length"] = str(content_length)
        if duplicate_content_length:
            headers["Content-Length"] = str(content_length)
    return headers


def _redirect_headers(target: str = "https://evil.example/next") -> Message:
    headers = _headers()
    headers["Location"] = target
    return headers


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {"answer": {"type": "string"}},
        "required": ["answer"],
        "additionalProperties": False,
    }


def _client(**overrides: Any) -> OpenRouterClient:
    options: dict[str, Any] = {
        "model": _MODEL,
        "schema_name": "fixture_answer",
        "schema": _schema(),
        "reasoning_effort": "low",
        "max_tokens": 1024,
    }
    options.update(overrides)
    return OpenRouterClient(**options)


def _expected_body(
    *,
    schema: dict[str, Any] | None = None,
    model: str = _MODEL,
    schema_name: str = "fixture_answer",
    reasoning_effort: str = "low",
    max_tokens: int = 1024,
    system_prompt: str = _SYSTEM_PROMPT,
    user_prompt: str = _USER_PROMPT,
) -> dict[str, Any]:
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": schema_name,
                "strict": True,
                "schema": _schema() if schema is None else schema,
            },
        },
        "reasoning": {"effort": reasoning_effort},
        "provider": {"require_parameters": True},
        "max_tokens": max_tokens,
        "stream": False,
    }


def _envelope(**overrides: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "model": _MODEL,
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": "{}"},
            }
        ],
    }
    value.update(overrides)
    return value


def _choice(**overrides: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "index": 0,
        "finish_reason": "stop",
        "message": {"role": "assistant", "content": "{}"},
    }
    value.update(overrides)
    return value


def _fixture_response(**overrides: Any) -> _Response:
    headers = _headers(content_length=len(_FIXTURE_BYTES))
    return _Response(_FIXTURE_BYTES, headers=headers, **overrides)


def _json_response(value: Any, **overrides: Any) -> _Response:
    payload = _json_bytes(value)
    headers = _headers(content_length=len(payload))
    return _Response(payload, headers=headers, **overrides)


def _raw_envelope(*, content: str = "{}", trailing: str = "") -> _Response:
    """Build one envelope verbatim so a raw numeric literal survives the round trip."""
    payload = (
        '{"model":"openai/fixture-model","choices":[{"index":0,"finish_reason":"stop",'
        '"message":{"role":"assistant","content":' + json.dumps(content) + "}}]" + trailing + "}"
    ).encode("utf-8")
    return _Response(payload, headers=_headers(content_length=len(payload)))


def _cost_response(literal: str) -> _Response:
    """Build one accepted envelope whose billed cost is a raw numeric literal."""
    return _raw_envelope(trailing=',"usage":{"cost":' + literal + "}")


def _record(
    calls: list[dict[str, Any]],
    request: Any,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> None:
    timeout = kwargs.get("timeout", args[0] if args else None)
    calls.append({"request": request, "timeout": timeout})


def _opener(response: _Response, calls: list[dict[str, Any]]) -> Callable[..., _Response]:
    def open_url(request: Any, *args: Any, **kwargs: Any) -> _Response:
        _record(calls, request, args, kwargs)
        return response

    return open_url


def _fresh_opener(factory: Callable[[], _Response], calls: list[dict[str, Any]]) -> Any:
    def open_url(request: Any, *args: Any, **kwargs: Any) -> _Response:
        _record(calls, request, args, kwargs)
        return factory()

    return open_url


def _raising_opener(failure: BaseException, calls: list[dict[str, Any]]) -> Any:
    def open_url(request: Any, *args: Any, **kwargs: Any) -> Any:
        _record(calls, request, args, kwargs)
        raise failure

    return open_url


def _request_headers(request: Any) -> dict[str, str]:
    return {name.lower(): value for name, value in request.header_items()}


def _complete(
    client: OpenRouterClient,
    *,
    system: str = _SYSTEM_PROMPT,
    user: str = _USER_PROMPT,
) -> OpenRouterResponse:
    return client.complete(system_prompt=system, user_prompt=user)


def _assert_no_credential(error: OpenRouterClientError, *, key: str = _KEY) -> None:
    assert key not in str(error)
    assert key not in repr(error)
    assert all(key not in str(argument) for argument in error.args)
    assert key not in "".join(traceback.format_exception(error))
    assert error.__cause__ is None
    assert error.__context__ is None


def _deep_schema(depth: int = 80) -> dict[str, Any]:
    root: dict[str, Any] = {}
    current = root
    for _ in range(depth):
        nested: dict[str, Any] = {}
        current["nested"] = nested
        current = nested
    return root


def _deep_content(depth: int = 80) -> str:
    return "[" * depth + "0" + "]" * depth


_CYCLIC_SCHEMA: dict[str, Any] = {"type": "object"}
_CYCLIC_SCHEMA["self"] = _CYCLIC_SCHEMA
_DEEP_SCHEMA = _deep_schema()
_DEEP_ENVELOPE = (
    b'{"model": "openai/fixture-model", "choices": [{"index": 0, "finish_reason": "stop",'
    b' "message": {"role": "assistant", "content": "{}"}}], "extra": '
    + b"[" * 80
    + b"0"
    + b"]" * 80
    + b"}"
)
_ESCAPED_KEY_CONTENT = json.dumps({"note": _KEY}).replace("sentinel", "\\u0073entinel", 1)


def test_public_constants_match_the_wire_contract() -> None:
    assert OPENROUTER_ENDPOINT == "https://openrouter.ai/api/v1/chat/completions"
    assert OPENROUTER_API_KEY_ENV == "OPENROUTER_API_KEY"
    assert OPENROUTER_TIMEOUT_SECONDS == 120.0
    assert OPENROUTER_MAX_BYTES == 4 * 1024 * 1024
    assert OPENROUTER_USER_AGENT == (
        "draftomen-openrouter/1 (+https://github.com/andreagrandi/draftomen)"
    )
    assert OPENROUTER_ACCEPT == "application/json"
    assert OPENROUTER_ACCEPT_ENCODING == "identity"
    assert OPENROUTER_METADATA_HEADER == "X-OpenRouter-Metadata"
    assert OPENROUTER_METADATA_VALUE == "enabled"
    assert isinstance(REASONING_EFFORTS, frozenset)
    assert REASONING_EFFORTS == {
        "none",
        "minimal",
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
    }


def test_complete_sends_the_pinned_request_exactly() -> None:
    calls: list[dict[str, Any]] = []
    client = _client(opener=_opener(_fixture_response(), calls))

    response = _complete(client)

    assert response.content == _FIXTURE_CONTENT
    assert len(calls) == 1
    request = calls[0]["request"]
    assert calls[0]["timeout"] == OPENROUTER_TIMEOUT_SECONDS
    assert request.full_url == OPENROUTER_ENDPOINT
    assert request.get_method() == "POST"
    expected = _expected_body()
    assert json.loads(request.data.decode("utf-8")) == expected
    assert request.data == _canonical(expected)
    assert _KEY.encode("utf-8") not in request.data
    assert _request_headers(request) == {
        "authorization": f"Bearer {_KEY}",
        "content-type": "application/json",
        "accept": OPENROUTER_ACCEPT,
        "accept-encoding": OPENROUTER_ACCEPT_ENCODING,
        "user-agent": OPENROUTER_USER_AGENT,
        OPENROUTER_METADATA_HEADER.lower(): OPENROUTER_METADATA_VALUE,
    }


def test_configured_values_are_pinned_at_construction() -> None:
    calls: list[dict[str, Any]] = []
    client = _client(
        opener=_fresh_opener(lambda: _fixture_response(), calls),
        model="org/other-model",
        schema_name="other_name",
        reasoning_effort="high",
        max_tokens=17,
        timeout_seconds=2.5,
        max_bytes=OPENROUTER_MAX_BYTES,
    )

    response = _complete(client)

    request = calls[0]["request"]
    assert calls[0]["timeout"] == 2.5
    body = json.loads(request.data.decode("utf-8"))
    assert body["model"] == "org/other-model"
    assert body["response_format"]["json_schema"]["name"] == "other_name"
    assert body["reasoning"] == {"effort": "high"}
    assert body["max_tokens"] == 17
    assert response.model == _MODEL


def test_prompts_are_sent_verbatim() -> None:
    calls: list[dict[str, Any]] = []
    client = _client(opener=_opener(_fixture_response(), calls))
    system = "  keep leading and trailing whitespace  "
    user = "\nQuestion?\t"

    _complete(client, system=system, user=user)

    body = json.loads(calls[0]["request"].data.decode("utf-8"))
    assert body["messages"] == [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def test_every_declared_reasoning_effort_is_accepted() -> None:
    for effort in sorted(REASONING_EFFORTS):
        calls: list[dict[str, Any]] = []
        client = _client(
            opener=_opener(_fixture_response(), calls),
            reasoning_effort=effort,
        )

        _complete(client)

        body = json.loads(calls[0]["request"].data.decode("utf-8"))
        assert body["reasoning"] == {"effort": effort}


def test_reasoning_effort_changes_only_the_configured_field() -> None:
    bodies: list[dict[str, Any]] = []
    for effort in ("low", "high"):
        calls: list[dict[str, Any]] = []
        client = _client(
            opener=_opener(_fixture_response(), calls),
            reasoning_effort=effort,
        )
        _complete(client)
        bodies.append(json.loads(calls[0]["request"].data.decode("utf-8")))

    low, high = bodies
    assert low["reasoning"] == {"effort": "low"}
    assert high["reasoning"] == {"effort": "high"}
    low.pop("reasoning")
    high.pop("reasoning")
    assert low == high


def test_schema_snapshot_ignores_post_construction_mutation() -> None:
    schema = _schema()
    calls: list[dict[str, Any]] = []
    client = _client(opener=_opener(_fixture_response(), calls), schema=schema)

    schema["type"] = "array"
    schema["properties"]["answer"]["type"] = "number"
    schema["properties"]["added"] = {"type": "boolean"}

    _complete(client)

    body = json.loads(calls[0]["request"].data.decode("utf-8"))
    assert body["response_format"]["json_schema"]["schema"] == {
        "type": "object",
        "properties": {"answer": {"type": "string"}},
        "required": ["answer"],
        "additionalProperties": False,
    }
    assert body == _expected_body()


def test_repeated_calls_rebuild_identical_requests() -> None:
    calls: list[dict[str, Any]] = []
    client = _client(opener=_fresh_opener(lambda: _fixture_response(), calls))

    first = _complete(client)
    second = _complete(client)

    assert first == second
    assert len(calls) == 2
    assert calls[0]["request"] is not calls[1]["request"]
    assert calls[0]["request"].data == calls[1]["request"].data
    assert calls[0]["request"].full_url == OPENROUTER_ENDPOINT
    assert calls[1]["request"].full_url == OPENROUTER_ENDPOINT


_INVALID_CONFIGURATION = (
    pytest.param({"model": 1}, id="model-int"),
    pytest.param({"model": True}, id="model-bool"),
    pytest.param({"model": None}, id="model-none"),
    pytest.param({"model": ""}, id="model-empty"),
    pytest.param({"model": "   "}, id="model-blank"),
    pytest.param({"model": " lead/model"}, id="model-leading-space"),
    pytest.param({"model": "trail/model "}, id="model-trailing-space"),
    pytest.param({"model": "org /model"}, id="model-inner-space"),
    pytest.param({"model": "org/mo\tdel"}, id="model-tab"),
    pytest.param({"model": "noslash"}, id="model-no-slash"),
    pytest.param({"model": "/model"}, id="model-empty-org"),
    pytest.param({"model": "org/"}, id="model-empty-name"),
    pytest.param({"model": "a/b/c"}, id="model-two-slashes"),
    pytest.param({"schema_name": ""}, id="schema-name-empty"),
    pytest.param({"schema_name": "bad name"}, id="schema-name-space"),
    pytest.param({"schema_name": "bad.name"}, id="schema-name-dot"),
    pytest.param({"schema_name": "a" * 65}, id="schema-name-long"),
    pytest.param({"schema_name": 7}, id="schema-name-int"),
    pytest.param({"schema_name": True}, id="schema-name-bool"),
    pytest.param({"reasoning_effort": "LOW"}, id="effort-case"),
    pytest.param({"reasoning_effort": ""}, id="effort-empty"),
    pytest.param({"reasoning_effort": "extreme"}, id="effort-unknown"),
    pytest.param({"reasoning_effort": None}, id="effort-none"),
    pytest.param({"reasoning_effort": 3}, id="effort-int"),
    pytest.param({"max_tokens": True}, id="max-tokens-bool"),
    pytest.param({"max_tokens": 0}, id="max-tokens-zero"),
    pytest.param({"max_tokens": -1}, id="max-tokens-negative"),
    pytest.param({"max_tokens": 1.5}, id="max-tokens-float"),
    pytest.param({"max_tokens": "1024"}, id="max-tokens-string"),
    pytest.param({"max_tokens": None}, id="max-tokens-none"),
    pytest.param({"max_bytes": True}, id="max-bytes-bool"),
    pytest.param({"max_bytes": 0}, id="max-bytes-zero"),
    pytest.param({"max_bytes": -1}, id="max-bytes-negative"),
    pytest.param({"max_bytes": 1.5}, id="max-bytes-float"),
    pytest.param({"max_bytes": "4096"}, id="max-bytes-string"),
    pytest.param({"timeout_seconds": True}, id="timeout-bool"),
    pytest.param({"timeout_seconds": 0}, id="timeout-zero"),
    pytest.param({"timeout_seconds": -1.0}, id="timeout-negative"),
    pytest.param({"timeout_seconds": float("inf")}, id="timeout-infinity"),
    pytest.param({"timeout_seconds": float("nan")}, id="timeout-nan"),
    pytest.param({"timeout_seconds": "10"}, id="timeout-string"),
    pytest.param({"timeout_seconds": None}, id="timeout-none"),
    pytest.param({"opener": object()}, id="opener-object"),
    pytest.param({"opener": 42}, id="opener-int"),
    pytest.param({"schema": None}, id="schema-none"),
    pytest.param({"schema": []}, id="schema-list"),
    pytest.param({"schema": "schema"}, id="schema-string"),
    pytest.param({"schema": 3}, id="schema-int"),
    pytest.param({"schema": {}}, id="schema-empty"),
    pytest.param({"schema": {"a": object()}}, id="schema-value-object"),
    pytest.param({"schema": {"a": (1, 2)}}, id="schema-value-tuple"),
    pytest.param({"schema": {1: "a"}}, id="schema-key-int"),
    pytest.param({"schema": {"a": float("nan")}}, id="schema-value-nan"),
    pytest.param({"schema": {"a": float("inf")}}, id="schema-value-infinity"),
    pytest.param({"schema": {"a": [1, {"b": {2: 3}}]}}, id="schema-nested-key-int"),
)


@pytest.mark.parametrize("overrides", _INVALID_CONFIGURATION)
def test_invalid_configuration_fails_before_transport(overrides: dict[str, Any]) -> None:
    calls: list[dict[str, Any]] = []
    options = {"opener": _opener(_fixture_response(), calls), **overrides}
    with pytest.raises(OpenRouterClientError) as raised:
        _client(**options)
    assert raised.value.code == "invalid_config"
    assert calls == []


def test_constructor_rejects_cyclic_and_over_deep_schemas() -> None:
    calls: list[dict[str, Any]] = []
    for schema in (_CYCLIC_SCHEMA, _DEEP_SCHEMA):
        with pytest.raises(OpenRouterClientError) as raised:
            _client(opener=_opener(_fixture_response(), calls), schema=schema)
        assert raised.value.code == "invalid_config"
    assert calls == []


def test_oversized_schema_fails_before_transport() -> None:
    calls: list[dict[str, Any]] = []
    with pytest.raises(OpenRouterClientError) as raised:
        _client(
            opener=_opener(_fixture_response(), calls),
            schema={"payload": "x" * (_REQUEST_LIMIT + 1)},
        )
    assert raised.value.code == "too_large"
    assert calls == []


def test_large_schema_within_the_request_ceiling_is_accepted() -> None:
    # The ceiling applies to the whole request, so leave headroom for the
    # envelope keys, prompts and schema wrapper around a near-limit payload.
    schema = {"payload": "x" * (_REQUEST_LIMIT - 4096)}
    calls: list[dict[str, Any]] = []
    client = _client(opener=_opener(_fixture_response(), calls), schema=schema)

    _complete(client)

    body = json.loads(calls[0]["request"].data.decode("utf-8"))
    assert body["response_format"]["json_schema"]["schema"] == schema


def test_exponentially_shared_schema_is_rejected_without_expanding() -> None:
    # ``level`` stays a 26-object graph while its canonical JSON would expand to
    # 2**25 leaves, so an unbounded walk cannot finish in this process.
    level: Any = ["x"]
    for _ in range(25):
        level = [level, level]
    calls: list[dict[str, Any]] = []

    with pytest.raises(OpenRouterClientError) as raised:
        _client(opener=_opener(_fixture_response(), calls), schema={"payload": level})

    assert raised.value.code == "too_large"
    assert calls == []


def test_wide_schema_beyond_the_ceiling_is_rejected() -> None:
    schema = {"payload": {f"k{index:014d}": 0 for index in range(_WIDE_SCHEMA_KEYS)}}
    calls: list[dict[str, Any]] = []

    with pytest.raises(OpenRouterClientError) as raised:
        _client(opener=_opener(_fixture_response(), calls), schema=schema)

    assert raised.value.code == "too_large"
    assert calls == []


def test_modest_wide_schema_round_trips_into_the_request() -> None:
    wide = {f"k{index:014d}": 0 for index in range(20_000)}
    calls: list[dict[str, Any]] = []
    client = _client(opener=_opener(_fixture_response(), calls), schema={"payload": wide})

    _complete(client)

    body = json.loads(calls[0]["request"].data.decode("utf-8"))
    assert body["response_format"]["json_schema"]["schema"] == {"payload": wide}


def test_oversized_request_body_fails_before_transport() -> None:
    calls: list[dict[str, Any]] = []
    client = _client(opener=_opener(_fixture_response(), calls))

    with pytest.raises(OpenRouterClientError) as raised:
        _complete(client, user=_USER_PROMPT + "x" * (_REQUEST_LIMIT + 1))
    assert raised.value.code == "too_large"
    assert calls == []


@pytest.mark.parametrize(
    ("system", "user"),
    (
        pytest.param(1, _USER_PROMPT, id="system-int"),
        pytest.param(None, _USER_PROMPT, id="system-none"),
        pytest.param(b"prompt", _USER_PROMPT, id="system-bytes"),
        pytest.param("", _USER_PROMPT, id="system-empty"),
        pytest.param("   ", _USER_PROMPT, id="system-blank"),
        pytest.param("\n\t ", _USER_PROMPT, id="system-whitespace-only"),
        pytest.param(_SYSTEM_PROMPT, 2, id="user-int"),
        pytest.param(_SYSTEM_PROMPT, "", id="user-empty"),
        pytest.param(_SYSTEM_PROMPT, " \n ", id="user-blank"),
    ),
)
def test_invalid_prompts_fail_before_transport(system: Any, user: Any) -> None:
    calls: list[dict[str, Any]] = []
    client = _client(opener=_opener(_fixture_response(), calls))
    with pytest.raises(OpenRouterClientError) as raised:
        client.complete(system_prompt=system, user_prompt=user)
    assert raised.value.code == "invalid_config"
    assert calls == []


@pytest.mark.parametrize("code", _CODES)
def test_error_message_is_exactly_the_classified_code(code: str) -> None:
    error = OpenRouterClientError(code=code)
    message = f"OpenRouter acquisition failed ({code})."
    assert error.code == code
    assert str(error) == message
    assert error.args == (message,)
    assert repr(error) == f"OpenRouterClientError({message!r})"
    assert error.__cause__ is None
    assert set(getattr(error, "__dict__", {})) <= {"code"}


@pytest.mark.parametrize(
    "code",
    ("", "unknown", "API", "missing-key", "malformed", None, 5, True),
)
def test_unknown_error_code_is_rejected(code: Any) -> None:
    with pytest.raises(ValueError) as raised:
        OpenRouterClientError(code=code)
    assert str(raised.value) == "Unknown OpenRouter acquisition error code."
    assert raised.value.__cause__ is None


def test_error_code_is_keyword_only() -> None:
    with pytest.raises(TypeError):
        OpenRouterClientError("invalid_config")  # type: ignore[misc]


@pytest.mark.parametrize(
    ("credential", "code"),
    (
        pytest.param(None, "missing_api_key", id="unset"),
        pytest.param("", "missing_api_key", id="empty"),
        pytest.param("   ", "missing_api_key", id="blank"),
        pytest.param("\t\n", "missing_api_key", id="whitespace-only"),
        pytest.param("key with space", "invalid_config", id="inner-space"),
        pytest.param(" key", "invalid_config", id="leading-space"),
        pytest.param("key\nline", "invalid_config", id="newline"),
        pytest.param("key\x1f", "invalid_config", id="unit-separator"),
        pytest.param("key\x7f", "invalid_config", id="delete"),
        pytest.param("kéy", "invalid_config", id="non-ascii"),
        pytest.param("ключ", "invalid_config", id="non-latin"),
    ),
)
def test_invalid_credentials_fail_before_transport(
    monkeypatch: pytest.MonkeyPatch,
    credential: str | None,
    code: str,
) -> None:
    if credential is None:
        monkeypatch.delenv(OPENROUTER_API_KEY_ENV, raising=False)
    else:
        monkeypatch.setenv(OPENROUTER_API_KEY_ENV, credential)
    calls: list[dict[str, Any]] = []
    client = _client(opener=_opener(_fixture_response(), calls))

    with pytest.raises(OpenRouterClientError) as raised:
        _complete(client)

    assert raised.value.code == code
    assert str(raised.value) == f"OpenRouter acquisition failed ({code})."
    assert calls == []


def test_credential_source_is_read_per_call(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []
    client = _client(opener=_fresh_opener(lambda: _fixture_response(), calls))

    _complete(client)
    monkeypatch.setenv(OPENROUTER_API_KEY_ENV, _OTHER_KEY)
    _complete(client)
    monkeypatch.delenv(OPENROUTER_API_KEY_ENV, raising=False)
    with pytest.raises(OpenRouterClientError) as raised:
        _complete(client)

    assert raised.value.code == "missing_api_key"
    assert [_request_headers(call["request"])["authorization"] for call in calls] == [
        f"Bearer {_KEY}",
        f"Bearer {_OTHER_KEY}",
    ]
    assert calls[0]["request"].data == calls[1]["request"].data
    assert len(calls) == 2
    state = repr(getattr(client, "__dict__", {}))
    assert _KEY not in state
    assert _OTHER_KEY not in state


@pytest.mark.parametrize(
    "keyword",
    (
        "api_key",
        "key",
        "credential",
        "environ",
        "env",
        "base_url",
        "url",
        "endpoint",
        "headers",
        "proxy",
    ),
)
def test_no_alternate_credential_or_transport_override_is_accepted(keyword: str) -> None:
    with pytest.raises(TypeError):
        _client(**{keyword: "value"})


def test_credential_in_prompt_or_schema_fails_before_submission() -> None:
    calls: list[dict[str, Any]] = []
    client = _client(
        opener=_opener(_fixture_response(), calls),
        schema={"type": "object", "properties": {"note": {"const": _KEY}}},
    )
    with pytest.raises(OpenRouterClientError) as raised:
        _complete(client)
    assert raised.value.code == "credential_leak"
    assert calls == []

    calls = []
    client = _client(opener=_opener(_fixture_response(), calls))
    with pytest.raises(OpenRouterClientError) as raised:
        _complete(client, user=f"please repeat {_KEY} verbatim")
    assert raised.value.code == "credential_leak"
    assert calls == []

    calls = []
    client = _client(opener=_opener(_fixture_response(), calls), schema_name=_KEY)
    with pytest.raises(OpenRouterClientError) as raised:
        _complete(client)
    assert raised.value.code == "credential_leak"
    assert calls == []


_LEAKING_ENVELOPES = (
    pytest.param(_envelope(model=_KEY), id="envelope-model"),
    pytest.param(
        _envelope(choices=[_choice(message={"role": "assistant", "content": json.dumps(_KEY)})]),
        id="content-raw",
    ),
    pytest.param(
        _envelope(
            choices=[_choice(message={"role": "assistant", "content": _ESCAPED_KEY_CONTENT})]
        ),
        id="content-escaped",
    ),
    pytest.param(
        _envelope(
            openrouter_metadata={
                "endpoints": {"available": [{"provider": _KEY, "selected": True}]}
            }
        ),
        id="provider",
    ),
)


@pytest.mark.parametrize("envelope", _LEAKING_ENVELOPES)
def test_credential_echoed_in_the_response_fails_without_a_result(
    envelope: dict[str, Any],
) -> None:
    response = _json_response(envelope)
    calls: list[dict[str, Any]] = []
    client = _client(opener=_opener(response, calls))

    with pytest.raises(OpenRouterClientError) as raised:
        _complete(client)

    assert raised.value.code == "credential_leak"
    assert str(raised.value) == "OpenRouter acquisition failed (credential_leak)."
    assert response.closed
    _assert_no_credential(raised.value)
    assert len(calls) == 1


def test_failures_never_retain_the_credential(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    body = _RecordingBytesIO(f"secret body {_KEY}".encode("utf-8"))
    error = HTTPError(OPENROUTER_ENDPOINT, 500, f"failure {_KEY}", _headers(), body)
    calls: list[dict[str, Any]] = []
    client = _client(opener=_raising_opener(error, calls))

    with pytest.raises(OpenRouterClientError) as raised:
        _complete(client)

    assert raised.value.code == "api"
    _assert_no_credential(raised.value)
    assert body.reads == []
    assert body.closed
    assert _KEY not in calls[0]["request"].full_url
    assert _KEY.encode("utf-8") not in calls[0]["request"].data

    calls = []
    client = _client(opener=_raising_opener(URLError(_KEY), calls))
    with pytest.raises(OpenRouterClientError) as raised:
        _complete(client)

    assert raised.value.code == "transport"
    _assert_no_credential(raised.value)
    assert _KEY not in caplog.text
    assert len(calls) == 1


def test_close_failures_never_leak_or_mask_a_result() -> None:
    response = _Response(
        b"never read",
        status=500,
        headers=_headers(),
        close_error=RuntimeError(_KEY),
    )
    client = _client(opener=_opener(response, []))
    with pytest.raises(OpenRouterClientError) as raised:
        _complete(client)
    assert raised.value.code == "api"
    assert response.closed
    _assert_no_credential(raised.value)

    success = _fixture_response(close_error=RuntimeError(_KEY))
    client = _client(opener=_opener(success, []))
    result = _complete(client)
    assert result.content == _FIXTURE_CONTENT
    assert result.cost_usd == "0.00123"
    assert success.closed


def test_raising_close_lookup_never_masks_a_successful_result() -> None:
    response = _HostileAttributeResponse(_fixture_response(), "close")

    result = _complete(_client(opener=_opener(response, [])))

    assert result.content == _FIXTURE_CONTENT
    assert result.cost_usd == "0.00123"


@pytest.mark.parametrize("attribute", ("geturl", "status", "headers"))
def test_raising_response_attribute_lookups_are_malformed(attribute: str) -> None:
    inner = _fixture_response()
    response = _HostileAttributeResponse(inner, attribute)

    with pytest.raises(OpenRouterClientError) as raised:
        _complete(_client(opener=_opener(response, [])))

    assert raised.value.code == "malformed_response"
    _assert_no_credential(raised.value)
    assert inner.closed


@pytest.mark.parametrize(
    ("failure", "code"),
    (
        pytest.param(TimeoutError("boom"), "timeout", id="timeout"),
        pytest.param(URLError(TimeoutError("boom")), "timeout", id="wrapped-timeout"),
        pytest.param(URLError("boom"), "transport", id="url-error"),
        pytest.param(OSError("boom"), "transport", id="os-error"),
        pytest.param(ConnectionResetError("boom"), "transport", id="connection-reset"),
    ),
)
def test_injected_transport_failures_are_classified(failure: BaseException, code: str) -> None:
    calls: list[dict[str, Any]] = []
    client = _client(opener=_raising_opener(failure, calls), timeout_seconds=2.5)

    with pytest.raises(OpenRouterClientError) as raised:
        _complete(client)

    assert raised.value.code == code
    assert str(raised.value) == f"OpenRouter acquisition failed ({code})."
    assert calls[0]["timeout"] == 2.5
    assert len(calls) == 1
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


@pytest.mark.parametrize(
    ("status", "code"),
    (
        pytest.param(300, "unsafe_redirect", id="300"),
        pytest.param(301, "unsafe_redirect", id="301"),
        pytest.param(302, "unsafe_redirect", id="302"),
        pytest.param(303, "unsafe_redirect", id="303"),
        pytest.param(307, "unsafe_redirect", id="307"),
        pytest.param(308, "unsafe_redirect", id="308"),
        pytest.param(399, "unsafe_redirect", id="399"),
        pytest.param(400, "api", id="400"),
        pytest.param(401, "api", id="401"),
        pytest.param(429, "api", id="429"),
        pytest.param(500, "api", id="500"),
        pytest.param(503, "api", id="503"),
    ),
)
def test_non_success_statuses_are_classified_without_reading_the_body(
    status: int,
    code: str,
) -> None:
    response = _Response(b"never read", status=status, headers=_headers())
    response._read_error = AssertionError("status body must not be consumed")
    calls: list[dict[str, Any]] = []
    client = _client(opener=_opener(response, calls))

    with pytest.raises(OpenRouterClientError) as raised:
        _complete(client)

    assert raised.value.code == code
    assert response.read_sizes == []
    assert response.closed
    assert len(calls) == 1


@pytest.mark.parametrize(
    ("status", "code"),
    (
        pytest.param(301, "unsafe_redirect", id="301"),
        pytest.param(302, "unsafe_redirect", id="302"),
        pytest.param(307, "unsafe_redirect", id="307"),
        pytest.param(308, "unsafe_redirect", id="308"),
        pytest.param(400, "api", id="400"),
        pytest.param(500, "api", id="500"),
    ),
)
def test_http_error_bodies_are_never_read(status: int, code: str) -> None:
    body = _RecordingBytesIO(b"secret response body")
    error = HTTPError(OPENROUTER_ENDPOINT, status, "secret reason", _headers(), body)
    with pytest.raises(OpenRouterClientError) as raised:
        _complete(_client(opener=_raising_opener(error, [])))
    assert raised.value.code == code
    assert body.reads == []
    assert body.closed


def test_injected_final_url_cannot_bypass_the_endpoint() -> None:
    for url in (
        "https://evil.example/next",
        "https://openrouter.ai/api/v1/chat/completions/",
        "http://openrouter.ai/api/v1/chat/completions",
    ):
        response = _fixture_response(url=url)
        with pytest.raises(OpenRouterClientError) as raised:
            _complete(_client(opener=_opener(response, [])))
        assert raised.value.code == "unsafe_redirect"
        assert response.closed
        assert response.read_sizes == []


def _default_opener_client(
    monkeypatch: pytest.MonkeyPatch,
    factory: Callable[[], _Response],
    calls: list[dict[str, Any]],
) -> OpenRouterClient:
    """Route the client's real opener chain through a fake HTTPS connection."""

    class _FakeHTTPSConnection:
        sock = None
        debuglevel = 0
        _http_vsn = 11

        def __init__(
            self,
            host: str,
            timeout: float | None = None,
            context: Any = None,
            **kwargs: Any,
        ) -> None:
            self.host = host
            self.timeout = timeout

        def set_debuglevel(self, level: int) -> None:
            pass

        def request(
            self,
            method: str,
            selector: str,
            body: Any,
            headers: Any,
            encode_chunked: bool = False,
        ) -> None:
            calls.append(
                {
                    "host": self.host,
                    "timeout": self.timeout,
                    "method": method,
                    "selector": selector,
                    "body": body,
                    "headers": dict(headers),
                }
            )

        def getresponse(self) -> _Response:
            return factory()

        def close(self) -> None:
            pass

    monkeypatch.setattr(http.client, "HTTPSConnection", _FakeHTTPSConnection)
    monkeypatch.setattr(urllib.request, "getproxies", lambda: {})
    return _client()


@pytest.mark.parametrize("status", (301, 302, 303, 307, 308))
def test_redirect_statuses_are_refused_by_the_default_opener(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
) -> None:
    calls: list[dict[str, Any]] = []
    redirect = _Response(b"redirect body", status=status, headers=_redirect_headers())
    redirect._read_error = AssertionError("redirect body must not be consumed")
    client = _default_opener_client(monkeypatch, lambda: redirect, calls)

    with pytest.raises(OpenRouterClientError) as raised:
        _complete(client)

    assert raised.value.code == "unsafe_redirect"
    assert [call["method"] for call in calls] == ["POST"]
    assert [call["selector"] for call in calls] == ["/api/v1/chat/completions"]
    assert calls[0]["body"] == _canonical(_expected_body())
    assert redirect.closed
    assert redirect.read_sizes == []


def test_redirect_without_location_is_refused_without_a_second_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []
    redirect = _Response(b"redirect body", status=302, headers=_headers())
    redirect._read_error = AssertionError("redirect body must not be consumed")
    client = _default_opener_client(monkeypatch, lambda: redirect, calls)

    with pytest.raises(OpenRouterClientError) as raised:
        _complete(client)

    assert raised.value.code == "unsafe_redirect"
    assert len(calls) == 1
    assert redirect.closed
    assert redirect.read_sizes == []


@pytest.mark.parametrize(
    "location",
    (
        pytest.param("https://[", id="invalid-ipv6-host"),
        pytest.param("https://evil.example/\x01next", id="control-character"),
    ),
)
def test_malformed_redirect_targets_are_refused_before_location_parsing(
    monkeypatch: pytest.MonkeyPatch,
    location: str,
) -> None:
    calls: list[dict[str, Any]] = []
    redirect = _Response(b"redirect body", status=302, headers=_redirect_headers(location))
    redirect._read_error = AssertionError("redirect body must not be consumed")
    client = _default_opener_client(monkeypatch, lambda: redirect, calls)

    with pytest.raises(OpenRouterClientError) as raised:
        _complete(client)

    assert raised.value.code == "unsafe_redirect"
    assert len(calls) == 1
    assert redirect.closed
    assert redirect.read_sizes == []
    _assert_no_credential(raised.value)


def test_default_opener_returns_the_fixture_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []
    client = _default_opener_client(monkeypatch, _fixture_response, calls)

    response = _complete(client)

    assert response.content == _FIXTURE_CONTENT
    assert response.provider == "FixtureProvider"
    assert response.cost_usd == "0.00123"
    assert len(calls) == 1
    assert calls[0]["host"] == "openrouter.ai"
    assert calls[0]["timeout"] == OPENROUTER_TIMEOUT_SECONDS
    assert calls[0]["body"] == _canonical(_expected_body())
    assert calls[0]["headers"]["Authorization"] == f"Bearer {_KEY}"


def test_failures_never_retry() -> None:
    responses = (
        _Response(b"never read", status=503, headers=_headers()),
        _Response(b"never read", status=302, headers=_headers()),
        _json_response(_envelope(error={"message": "boom"})),
        _json_response(_envelope(choices=[])),
        _fixture_response(url="https://evil.example/next"),
    )
    for response in responses:
        calls: list[dict[str, Any]] = []
        client = _client(opener=_opener(response, calls))
        with pytest.raises(OpenRouterClientError):
            _complete(client)
        assert len(calls) == 1

    calls = []
    client = _client(opener=_raising_opener(URLError("boom"), calls))
    with pytest.raises(OpenRouterClientError):
        _complete(client)
    assert len(calls) == 1


_MALFORMED_BODIES = (
    pytest.param(b"", id="empty-body"),
    pytest.param(b"not json", id="non-json"),
    pytest.param(b'{"model": "m"}{"extra": 1}', id="trailing-data"),
    pytest.param(b"[1, 2]", id="non-object-envelope"),
    pytest.param(b'"text"', id="json-string"),
    pytest.param(b"\xff\xfe", id="invalid-utf8"),
    pytest.param(b'{"a": 1, "a": 2}', id="duplicate-keys"),
    pytest.param(b'{"a": NaN}', id="nan"),
    pytest.param(b'{"a": Infinity}', id="infinity"),
    pytest.param(b'{"a": -Infinity}', id="negative-infinity"),
    pytest.param(_DEEP_ENVELOPE, id="deep-nesting"),
)


@pytest.mark.parametrize("payload", _MALFORMED_BODIES)
def test_malformed_envelopes_are_rejected(payload: bytes) -> None:
    response = _Response(payload, headers=_headers(content_length=len(payload)))
    with pytest.raises(OpenRouterClientError) as raised:
        _complete(_client(opener=_opener(response, [])))
    assert raised.value.code == "malformed_response"
    assert response.closed


_ARITHMETIC_OVERFLOW_ENVELOPES = (
    pytest.param(
        _raw_envelope(trailing=',"usage":{"cost":' + _OVERFLOWING_DECIMAL + "}"),
        id="billed-cost",
    ),
    pytest.param(
        _raw_envelope(trailing=',"unknown_field":' + _OVERFLOWING_DECIMAL),
        id="unknown-envelope-field",
    ),
    pytest.param(
        _raw_envelope(content='{"cost":' + _OVERFLOWING_DECIMAL + "}"),
        id="generated-content",
    ),
)


@pytest.mark.parametrize("response", _ARITHMETIC_OVERFLOW_ENVELOPES)
def test_overflowing_decimal_literals_are_rejected_as_malformed(response: _Response) -> None:
    with pytest.raises(OpenRouterClientError) as raised:
        _complete(_client(opener=_opener(response, [])))
    assert raised.value.code == "malformed_response"
    assert response.closed


_ENVELOPE_SHAPES = (
    pytest.param({"choices": []}, id="choices-empty"),
    pytest.param({"choices": {}}, id="choices-object"),
    pytest.param({"choices": [_choice(), _choice()]}, id="choices-two"),
    pytest.param({"choices": ["stop"]}, id="choice-not-object"),
    pytest.param({"choices": [{"index": 0, "finish_reason": "stop"}]}, id="message-missing"),
    pytest.param(
        {"choices": [{"index": 0, "finish_reason": "stop", "message": "text"}]},
        id="message-not-object",
    ),
    pytest.param(
        {
            "choices": [
                {"index": 0, "finish_reason": "stop", "message": {"role": "assistant"}}
            ]
        },
        id="content-missing",
    ),
    pytest.param(
        {
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": ""},
                }
            ]
        },
        id="content-empty",
    ),
    pytest.param(
        {
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": 5},
                }
            ]
        },
        id="content-not-string",
    ),
    pytest.param(
        {
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": "{oops"},
                }
            ]
        },
        id="content-invalid-json",
    ),
    pytest.param(
        {
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": '{"a": 1, "a": 2}'},
                }
            ]
        },
        id="content-duplicate-keys",
    ),
    pytest.param(
        {
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": "NaN"},
                }
            ]
        },
        id="content-nan",
    ),
    pytest.param(
        {
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": _deep_content()},
                }
            ]
        },
        id="content-deep-nesting",
    ),
    pytest.param(
        {
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": "```json\n{}\n```"},
                }
            ]
        },
        id="content-fenced",
    ),
    pytest.param(
        {
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "content": "{}",
                        "tool_calls": [{"id": "call-1"}],
                    },
                }
            ]
        },
        id="tool-calls-nonempty",
    ),
    pytest.param(
        {"choices": [{"index": 0, "message": {"role": "assistant", "content": "{}"}}]},
        id="finish-reason-absent",
    ),
    pytest.param(
        {
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "weird",
                    "message": {"role": "assistant", "content": "{}"},
                }
            ]
        },
        id="finish-reason-unknown",
    ),
    pytest.param(
        {
            "choices": [
                {
                    "index": 0,
                    "finish_reason": 5,
                    "message": {"role": "assistant", "content": "{}"},
                }
            ]
        },
        id="finish-reason-not-string",
    ),
    pytest.param(
        {
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": "{}", "refusal": 5},
                }
            ]
        },
        id="refusal-not-string",
    ),
    pytest.param({"model": None}, id="model-null"),
    pytest.param({"model": ""}, id="model-empty"),
    pytest.param({"model": 5}, id="model-not-string"),
)


@pytest.mark.parametrize("overrides", _ENVELOPE_SHAPES)
def test_malformed_envelope_shapes_are_rejected(overrides: dict[str, Any]) -> None:
    response = _json_response(_envelope(**overrides))
    with pytest.raises(OpenRouterClientError) as raised:
        _complete(_client(opener=_opener(response, [])))
    assert raised.value.code == "malformed_response"
    assert response.closed


@pytest.mark.parametrize(
    ("message", "expected"),
    (
        pytest.param({"role": "assistant", "content": "{}"}, None, id="assistant"),
        pytest.param({"content": "{}"}, "malformed_response", id="role-absent"),
        pytest.param({"role": "user", "content": "{}"}, "malformed_response", id="role-user"),
        pytest.param({"role": "system", "content": "{}"}, "malformed_response", id="role-system"),
        pytest.param({"role": "", "content": "{}"}, "malformed_response", id="role-empty"),
        pytest.param({"role": 5, "content": "{}"}, "malformed_response", id="role-int"),
        pytest.param({"role": None, "content": "{}"}, "malformed_response", id="role-null"),
        pytest.param({"role": True, "content": "{}"}, "malformed_response", id="role-bool"),
    ),
)
def test_success_requires_an_assistant_message_role(
    message: dict[str, Any],
    expected: str | None,
) -> None:
    response = _json_response(_envelope(choices=[_choice(message=message)]))

    if expected is None:
        assert _complete(_client(opener=_opener(response, []))).content == "{}"
        return

    with pytest.raises(OpenRouterClientError) as raised:
        _complete(_client(opener=_opener(response, [])))
    assert raised.value.code == expected
    assert response.closed


@pytest.mark.parametrize(
    ("overrides", "code"),
    (
        pytest.param({"error": {"code": 500, "message": "boom"}}, "api", id="top-level-error"),
        pytest.param({"error": "boom"}, "api", id="top-level-error-string"),
        pytest.param(
            {"choices": [{"error": {"message": "boom"}, "finish_reason": "stop"}]},
            "api",
            id="choice-error",
        ),
        pytest.param(
            {"choices": [{"finish_reason": "error"}]},
            "api",
            id="finish-reason-error",
        ),
        pytest.param(
            {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "refusal": "I cannot."},
                    }
                ]
            },
            "refusal",
            id="refusal",
        ),
        pytest.param(
            {"choices": [{"finish_reason": "content_filter"}]},
            "refusal",
            id="content-filter",
        ),
        pytest.param(
            {
                "choices": [
                    {
                        "finish_reason": "content_filter",
                        "message": {"role": "assistant", "refusal": "I cannot."},
                    }
                ]
            },
            "refusal",
            id="refusal-before-accounting",
        ),
        pytest.param({"choices": [{"finish_reason": "length"}]}, "truncated", id="length"),
        pytest.param({"choices": [{"finish_reason": None}]}, "truncated", id="null-finish-reason"),
        pytest.param(
            {
                "choices": [
                    {
                        "finish_reason": None,
                        "message": {"role": "assistant", "content": "{}"},
                    }
                ]
            },
            "truncated",
            id="null-finish-reason-with-content",
        ),
        pytest.param(
            {
                "choices": [
                    {
                        "finish_reason": "length",
                        "message": {"role": "assistant", "content": "partial"},
                    }
                ]
            },
            "truncated",
            id="length-with-content",
        ),
    ),
)
def test_api_refusal_and_truncation_classification(
    overrides: dict[str, Any],
    code: str,
) -> None:
    response = _json_response(_envelope(**overrides))
    with pytest.raises(OpenRouterClientError) as raised:
        _complete(_client(opener=_opener(response, [])))
    assert raised.value.code == code
    assert response.closed


def test_nonempty_tool_calls_are_rejected() -> None:
    envelope = _envelope(
        choices=[
            _choice(
                finish_reason="stop",
                message={
                    "role": "assistant",
                    "content": "{}",
                    "tool_calls": [{"id": "call-1"}],
                },
            )
        ]
    )
    response = _json_response(envelope)
    with pytest.raises(OpenRouterClientError) as raised:
        _complete(_client(opener=_opener(response, [])))
    assert raised.value.code == "malformed_response"
    assert response.closed


def test_empty_refusal_is_not_a_refusal() -> None:
    envelope = _envelope(
        choices=[
            _choice(
                finish_reason="stop",
                message={"role": "assistant", "content": "{}", "refusal": ""},
            )
        ]
    )
    response = _json_response(envelope)
    result = _complete(_client(opener=_opener(response, [])))
    assert result.content == "{}"


def test_content_is_preserved_verbatim() -> None:
    content = '{\n  "answer": "fixture",\n  "escaped": "\\u00e9"\n}'
    envelope = _envelope(
        choices=[_choice(message={"role": "assistant", "content": content})]
    )
    response = _json_response(envelope)

    result = _complete(_client(opener=_opener(response, [])))

    assert result.content == content


def test_body_byte_bound_is_exact() -> None:
    size = len(_FIXTURE_BYTES)
    exact = _Response(_FIXTURE_BYTES, headers=_headers())
    exact_calls: list[dict[str, Any]] = []
    client = _client(opener=_opener(exact, exact_calls), max_bytes=size)

    result = _complete(client)

    assert result.content == _FIXTURE_CONTENT
    assert exact.closed
    assert exact.read_sizes[0] == min(_CHUNK_LIMIT, size + 1)
    assert all(read_size <= _CHUNK_LIMIT + 1 for read_size in exact.read_sizes)

    overflow = _Response(_FIXTURE_BYTES, headers=_headers())
    with pytest.raises(OpenRouterClientError) as raised:
        _complete(_client(opener=_opener(overflow, []), max_bytes=size - 1))
    assert raised.value.code == "too_large"
    assert overflow.closed


def test_declared_content_length_bounds_short_bodies_and_extra_bytes() -> None:
    size = len(_FIXTURE_BYTES)

    oversized = _Response(b"never read", headers=_headers(content_length=size + 1))
    with pytest.raises(OpenRouterClientError) as raised:
        _complete(_client(opener=_opener(oversized, []), max_bytes=size))
    assert raised.value.code == "too_large"
    assert oversized.read_sizes == []
    assert oversized.closed

    huge = _Response(b"never read", headers=_headers(content_length="9" * 5000))
    with pytest.raises(OpenRouterClientError) as raised:
        _complete(_client(opener=_opener(huge, []), max_bytes=size))
    assert raised.value.code == "too_large"
    assert huge.read_sizes == []
    assert huge.closed

    short = _Response(b"1234", headers=_headers(content_length=size))
    with pytest.raises(OpenRouterClientError) as raised:
        _complete(_client(opener=_opener(short, [])))
    assert raised.value.code == "truncated"
    assert short.closed

    empty_declared = _Response(b"", headers=_headers(content_length=size))
    with pytest.raises(OpenRouterClientError) as raised:
        _complete(_client(opener=_opener(empty_declared, [])))
    assert raised.value.code == "truncated"
    assert empty_declared.closed

    empty_undeclared = _Response(b"", headers=_headers())
    with pytest.raises(OpenRouterClientError) as raised:
        _complete(_client(opener=_opener(empty_undeclared, [])))
    assert raised.value.code == "malformed_response"
    assert empty_undeclared.closed

    padded = _Response(b"{}", headers=_headers(content_length=0))
    with pytest.raises(OpenRouterClientError) as raised:
        _complete(_client(opener=_opener(padded, [])))
    assert raised.value.code == "malformed_response"
    assert padded.closed

    declared = _Response(b"true", headers=_headers(content_length=4))
    with pytest.raises(OpenRouterClientError) as raised:
        _complete(_client(opener=_opener(declared, [])))
    assert raised.value.code == "malformed_response"
    assert declared.closed


def test_incomplete_reads_are_truncated() -> None:
    response = _Response(
        b"unused",
        headers=_headers(),
        read_error=http.client.IncompleteRead(b'{"model": "openai/fixtur'),
    )
    with pytest.raises(OpenRouterClientError) as raised:
        _complete(_client(opener=_opener(response, [])))
    assert raised.value.code == "truncated"
    assert response.closed


@pytest.mark.parametrize(
    ("failure", "code"),
    (
        pytest.param(TimeoutError("boom"), "timeout", id="timeout"),
        pytest.param(URLError(TimeoutError("boom")), "timeout", id="wrapped-timeout"),
        pytest.param(URLError("boom"), "transport", id="url-error"),
        pytest.param(OSError("boom"), "transport", id="os-error"),
    ),
)
def test_read_failures_are_classified(failure: BaseException, code: str) -> None:
    response = _Response(b"unused", headers=_headers(), read_error=failure)
    with pytest.raises(OpenRouterClientError) as raised:
        _complete(_client(opener=_opener(response, [])))
    assert raised.value.code == code
    assert response.closed


def test_non_bytes_read_chunks_are_rejected() -> None:
    response = _Response(b"unused", headers=_headers(), read_chunk="not bytes")
    with pytest.raises(OpenRouterClientError) as raised:
        _complete(_client(opener=_opener(response, [])))
    assert raised.value.code == "malformed_response"
    assert response.closed


def test_supported_response_headers_are_accepted() -> None:
    for headers in (
        _headers(),
        _headers(content_type="application/json; charset=utf-8"),
        _headers(content_type="application/json; charset=UTF-8"),
        _headers(content_type='application/json; charset="utf-8"'),
        _headers(content_type='application/json; charset="UTF-8"'),
        _headers(content_type="Application/Json"),
        _headers(content_type="application/json\t; charset=utf-8"),
        _headers(content_type="application/json;charset= utf-8"),
        _headers(content_length=len(_FIXTURE_BYTES)),
        _headers(content_encoding="identity"),
        _headers(content_encoding="identity", content_length=len(_FIXTURE_BYTES)),
        _headers(content_encoding=" identity"),
        _headers(content_length=" " + str(len(_FIXTURE_BYTES)) + " "),
    ):
        response = _Response(_FIXTURE_BYTES, headers=headers)
        result = _complete(_client(opener=_opener(response, [])))
        assert result.content == _FIXTURE_CONTENT
        assert response.closed


_MALFORMED_HEADERS = (
    pytest.param(_headers(duplicate_content_type=True), id="duplicate-content-type"),
    pytest.param(_headers(content_type="text/plain"), id="unsupported-content-type"),
    pytest.param(_headers(content_type=""), id="empty-content-type"),
    pytest.param(_headers(content_type=None), id="absent-content-type"),
    pytest.param(
        _headers(content_type="application/json; charset=iso-8859-1"),
        id="unsupported-charset",
    ),
    pytest.param(
        _headers(content_type="application/json; charset=utf-16"),
        id="utf-16-charset",
    ),
    pytest.param(
        _headers(content_type='application/json; charset="utf-8'),
        id="unbalanced-charset-quote",
    ),
    pytest.param(
        _headers(content_type="application/json; charset=utf-8; charset=utf-8"),
        id="duplicate-charset",
    ),
    pytest.param(
        _headers(content_type="application/json; boundary=fixture"),
        id="unknown-parameter",
    ),
    pytest.param(_headers(content_type="application/json; charset="), id="empty-charset"),
    pytest.param(
        _headers(content_type="application/json;\x0bcharset=utf-8"),
        id="vertical-tab-in-content-type",
    ),
    pytest.param(
        _headers(content_type="application/json; charset=\x0cutf-8"),
        id="form-feed-in-content-type",
    ),
    pytest.param(
        _headers(content_type="application/ json"),
        id="space-in-media-type",
    ),
    pytest.param(_headers(content_encoding="gzip"), id="unsupported-encoding"),
    pytest.param(
        _headers(content_encoding="\x0bidentity"),
        id="vertical-tab-in-content-encoding",
    ),
    pytest.param(_headers(content_length="abc"), id="non-numeric-length"),
    pytest.param(
        _headers(content_length="\x0b" + str(len(_FIXTURE_BYTES))),
        id="vertical-tab-in-content-length",
    ),
    pytest.param(_headers(content_length="-1"), id="negative-length"),
    pytest.param(_headers(content_length=""), id="empty-length"),
    pytest.param(
        _headers(content_length=len(_FIXTURE_BYTES), duplicate_content_length=True),
        id="duplicate-length",
    ),
)


@pytest.mark.parametrize("headers", _MALFORMED_HEADERS)
def test_malformed_or_conflicting_headers_are_rejected(headers: Message) -> None:
    response = _Response(_FIXTURE_BYTES, headers=headers)
    with pytest.raises(OpenRouterClientError) as raised:
        _complete(_client(opener=_opener(response, [])))
    assert raised.value.code == "malformed_response"
    assert response.closed


def test_fixture_accounting_and_billed_cost() -> None:
    calls: list[dict[str, Any]] = []
    response = _fixture_response()
    client = _client(opener=_opener(response, calls))

    result = _complete(client)

    assert isinstance(result, OpenRouterResponse)
    assert result.content == _FIXTURE_CONTENT
    assert result.model == _MODEL
    assert result.provider == "FixtureProvider"
    assert result.input_tokens == 100
    assert result.cached_input_tokens == 60
    assert result.output_tokens == 20
    assert result.reasoning_tokens == 8
    assert result.cost_usd == "0.00123"
    assert "999" not in result.cost_usd
    serialized = json.dumps(asdict(result))
    assert json.loads(serialized)["cost_usd"] == "0.00123"
    assert _KEY not in serialized
    assert response.closed
    assert len(calls) == 1


def test_result_is_frozen_slotted_and_serializable() -> None:
    result = OpenRouterResponse(
        content="{}",
        model=_MODEL,
        provider=None,
        input_tokens=None,
        cached_input_tokens=None,
        output_tokens=None,
        reasoning_tokens=None,
        cost_usd=None,
    )

    assert json.loads(json.dumps(asdict(result))) == {
        "content": "{}",
        "model": _MODEL,
        "provider": None,
        "input_tokens": None,
        "cached_input_tokens": None,
        "output_tokens": None,
        "reasoning_tokens": None,
        "cost_usd": None,
    }
    with pytest.raises(FrozenInstanceError):
        result.content = "changed"  # type: ignore[misc]
    assert not hasattr(result, "__dict__")


def test_reported_model_comes_from_the_envelope() -> None:
    response = _json_response(_envelope(model="vendor/reported-model"))
    result = _complete(_client(opener=_opener(response, [])))
    assert result.model == "vendor/reported-model"


_ABSENT_ACCOUNTING = (
    pytest.param(_envelope(), id="no-usage"),
    pytest.param(_envelope(usage=None), id="null-usage"),
    pytest.param(_envelope(usage={}), id="empty-usage"),
    pytest.param(
        _envelope(
            usage={
                "prompt_tokens": None,
                "completion_tokens": None,
                "prompt_tokens_details": None,
                "completion_tokens_details": None,
                "cost": None,
            }
        ),
        id="null-fields",
    ),
    pytest.param(
        _envelope(usage={"prompt_tokens_details": {}, "completion_tokens_details": {}}),
        id="empty-details",
    ),
)


@pytest.mark.parametrize("envelope", _ABSENT_ACCOUNTING)
def test_absent_accounting_stays_unknown(envelope: dict[str, Any]) -> None:
    response = _json_response(envelope)
    result = _complete(_client(opener=_opener(response, [])))
    assert result.input_tokens is None
    assert result.cached_input_tokens is None
    assert result.output_tokens is None
    assert result.reasoning_tokens is None
    assert result.cost_usd is None


def test_reported_zeroes_are_preserved() -> None:
    response = _json_response(
        _envelope(
            usage={
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "prompt_tokens_details": {"cached_tokens": 0},
                "completion_tokens_details": {"reasoning_tokens": 0},
                "cost": 0,
            }
        )
    )
    result = _complete(_client(opener=_opener(response, [])))
    assert result.input_tokens == 0
    assert result.cached_input_tokens == 0
    assert result.output_tokens == 0
    assert result.reasoning_tokens == 0
    assert result.cost_usd == "0"


_INVALID_USAGE = (
    pytest.param([], id="usage-list"),
    pytest.param("usage", id="usage-string"),
    pytest.param({"prompt_tokens": True}, id="bool-counter"),
    pytest.param({"prompt_tokens": -1}, id="negative-counter"),
    pytest.param({"prompt_tokens": "5"}, id="string-counter"),
    pytest.param({"prompt_tokens": 1.5}, id="float-counter"),
    pytest.param({"prompt_tokens_details": []}, id="details-list"),
    pytest.param({"prompt_tokens_details": {"cached_tokens": "6"}}, id="cached-string"),
    pytest.param({"completion_tokens_details": "x"}, id="details-string"),
    pytest.param(
        {"completion_tokens_details": {"reasoning_tokens": True}},
        id="reasoning-bool",
    ),
    pytest.param(
        {"completion_tokens": 5, "completion_tokens_details": {"reasoning_tokens": 6}},
        id="reasoning-exceeds-output",
    ),
    pytest.param(
        {"prompt_tokens": 5, "prompt_tokens_details": {"cached_tokens": 6}},
        id="cached-exceeds-input",
    ),
)


@pytest.mark.parametrize("usage", _INVALID_USAGE)
def test_invalid_accounting_fails(usage: Any) -> None:
    response = _json_response(_envelope(usage=usage))
    with pytest.raises(OpenRouterClientError) as raised:
        _complete(_client(opener=_opener(response, [])))
    assert raised.value.code == "malformed_response"
    assert response.closed


@pytest.mark.parametrize(
    ("cost", "expected"),
    (
        pytest.param(0, "0", id="zero"),
        pytest.param(0.0, "0", id="zero-float"),
        pytest.param(3, "3", id="integer"),
        pytest.param(1.5, "1.5", id="fraction"),
        pytest.param(0.00123, "0.00123", id="fraction-small"),
        pytest.param(12.5, "12.5", id="fraction-short"),
        pytest.param(1e-3, "0.001", id="exponent-negative"),
        pytest.param(1e30, "1" + "0" * 30, id="exponent-positive"),
        pytest.param(1e120, "1" + "0" * 120, id="exponent-large"),
    ),
)
def test_cost_is_normalized_to_an_exact_decimal_string(cost: Any, expected: str) -> None:
    response = _json_response(_envelope(usage={"cost": cost}))
    result = _complete(_client(opener=_opener(response, [])))
    assert result.cost_usd == expected


@pytest.mark.parametrize(
    ("literal", "expected"),
    (
        pytest.param("1." + "0" * 128, "1", id="trailing-fractional-zeroes"),
        pytest.param("1e127", "1" + "0" * 127, id="normalized-128-characters"),
    ),
)
def test_cost_normalization_is_bounded_by_the_normalized_text(
    literal: str,
    expected: str,
) -> None:
    result = _complete(_client(opener=_opener(_cost_response(literal), [])))
    assert result.cost_usd == expected


@pytest.mark.parametrize(
    "literal",
    (
        pytest.param("1e128", id="normalized-129-characters"),
        pytest.param("1e-127", id="fractional-129-characters"),
    ),
)
def test_costs_whose_normalized_text_exceeds_the_limit_are_rejected(literal: str) -> None:
    response = _cost_response(literal)
    with pytest.raises(OpenRouterClientError) as raised:
        _complete(_client(opener=_opener(response, [])))
    assert raised.value.code == "malformed_response"
    assert response.closed


@pytest.mark.parametrize(
    "cost",
    (
        pytest.param(True, id="bool"),
        pytest.param(-1, id="negative"),
        pytest.param(-0.5, id="negative-fraction"),
        pytest.param("0.5", id="numeric-string"),
        pytest.param([], id="list"),
        pytest.param({}, id="object"),
    ),
)
def test_invalid_cost_values_fail(cost: Any) -> None:
    response = _json_response(_envelope(usage={"cost": cost}))
    with pytest.raises(OpenRouterClientError) as raised:
        _complete(_client(opener=_opener(response, [])))
    assert raised.value.code == "malformed_response"
    assert response.closed


@pytest.mark.parametrize("exponent", ("200", "999999999"))
def test_unbounded_cost_exponents_fail_without_allocating(exponent: str) -> None:
    response = _cost_response("1e" + exponent)
    with pytest.raises(OpenRouterClientError) as raised:
        _complete(_client(opener=_opener(response, [])))
    assert raised.value.code == "malformed_response"
    assert response.closed


def test_non_finite_json_costs_fail() -> None:
    for literal in ("NaN", "Infinity", "-Infinity"):
        response = _cost_response(literal)
        with pytest.raises(OpenRouterClientError) as raised:
            _complete(_client(opener=_opener(response, [])))
        assert raised.value.code == "malformed_response"
        assert response.closed


@pytest.mark.parametrize(
    ("metadata", "provider"),
    (
        pytest.param(None, "FixtureProvider", id="fixture-default"),
        pytest.param({"endpoints": {"available": []}}, None, id="empty-available"),
        pytest.param({"endpoints": {"available": None}}, None, id="null-available"),
        pytest.param({"endpoints": {}}, None, id="no-available"),
        pytest.param({"endpoints": None}, None, id="null-endpoints"),
        pytest.param({}, None, id="empty-metadata"),
        pytest.param(
            {"endpoints": {"available": [{"provider": "A"}]}},
            None,
            id="no-selected",
        ),
        pytest.param(
            {
                "endpoints": {
                    "available": [
                        {"provider": "A", "selected": False},
                        {"provider": "B", "selected": False},
                    ]
                }
            },
            None,
            id="none-selected",
        ),
        pytest.param(
            {
                "endpoints": {
                    "available": [
                        {"provider": "A", "selected": True},
                        {"provider": "B", "selected": True},
                    ]
                }
            },
            None,
            id="multiple-selected",
        ),
        pytest.param(
            {
                "endpoints": {
                    "available": [
                        {"provider": "Ignored", "selected": False},
                        {"provider": "Chosen", "selected": True},
                    ]
                }
            },
            "Chosen",
            id="unique-selected",
        ),
    ),
)
def test_provider_requires_an_unambiguous_selected_endpoint(
    metadata: dict[str, Any] | None,
    provider: str | None,
) -> None:
    if metadata is None:
        envelope = _envelope(
            openrouter_metadata={
                "endpoints": {
                    "total": 1,
                    "available": [
                        {"model": _MODEL, "provider": "FixtureProvider", "selected": True}
                    ],
                }
            }
        )
    else:
        envelope = _envelope(openrouter_metadata=metadata)
    response = _json_response(envelope)
    result = _complete(_client(opener=_opener(response, [])))
    assert result.provider == provider


@pytest.mark.parametrize(
    "metadata",
    (
        pytest.param("metadata", id="metadata-string"),
        pytest.param([], id="metadata-list"),
        pytest.param({"endpoints": "endpoints"}, id="endpoints-string"),
        pytest.param({"endpoints": {"available": 5}}, id="available-int"),
        pytest.param({"endpoints": {"available": [1]}}, id="available-non-object"),
        pytest.param(
            {"endpoints": {"available": [{"provider": "A", "selected": "true"}]}},
            id="selected-string",
        ),
        pytest.param(
            {"endpoints": {"available": [{"provider": "A", "selected": 1}]}},
            id="selected-int",
        ),
        pytest.param(
            {"endpoints": {"available": [{"selected": True}]}},
            id="provider-missing",
        ),
        pytest.param(
            {"endpoints": {"available": [{"provider": "", "selected": True}]}},
            id="provider-empty",
        ),
        pytest.param(
            {"endpoints": {"available": [{"provider": 5, "selected": True}]}},
            id="provider-not-string",
        ),
    ),
)
def test_malformed_routing_metadata_fails(metadata: Any) -> None:
    response = _json_response(_envelope(openrouter_metadata=metadata))
    with pytest.raises(OpenRouterClientError) as raised:
        _complete(_client(opener=_opener(response, [])))
    assert raised.value.code == "malformed_response"
    assert response.closed


def test_undocumented_provider_and_cost_locations_are_ignored() -> None:
    envelope = _envelope(
        provider="TopLevelProvider",
        usage={"upstream_inference_cost": 9},
        choices=[
            _choice(
                message={
                    "role": "assistant",
                    "content": "{}",
                    "provider": "MessageProvider",
                    "usage": {"cost": 999},
                }
            )
        ],
    )
    response = _json_response(envelope)

    result = _complete(_client(opener=_opener(response, [])))

    assert result.provider is None
    assert result.cost_usd is None
    assert result.input_tokens is None
    assert result.output_tokens is None


def test_billed_cost_wins_over_upstream_inference_cost() -> None:
    response = _json_response(
        _envelope(usage={"cost": 2, "upstream_inference_cost": 9})
    )
    result = _complete(_client(opener=_opener(response, [])))
    assert result.cost_usd == "2"


def test_public_client_has_no_filesystem_or_network_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _FIXTURE.read_bytes()
    profile = tmp_path / "profile.json"
    artifact = tmp_path / "production-artifact.json"
    profile.write_bytes(b"profile sentinel")
    artifact.write_bytes(b"artifact sentinel")
    before = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    responses = iter(
        (
            _Response(fixture, headers=_headers(content_length=len(fixture))),
            _json_response(
                _envelope(
                    choices=[_choice(message={"role": "assistant", "refusal": "I cannot."})]
                )
            ),
        )
    )
    calls: list[dict[str, Any]] = []
    real_open = io.open

    def guarded_open(file: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        if any(flag in mode for flag in ("w", "a", "x", "+")):
            raise AssertionError("OpenRouter acquisition attempted a filesystem write")
        return real_open(file, mode, *args, **kwargs)

    def opener(request: Any, *args: Any, **kwargs: Any) -> Any:
        _record(calls, request, args, kwargs)
        return next(responses)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(io, "open", guarded_open)
    monkeypatch.setattr(builtins, "open", guarded_open)
    client = _client(opener=opener)

    result = _complete(client)

    assert result.content == _FIXTURE_CONTENT
    assert result.model == _MODEL
    assert result.provider == "FixtureProvider"
    assert result.input_tokens == 100
    assert result.cached_input_tokens == 60
    assert result.output_tokens == 20
    assert result.reasoning_tokens == 8
    assert result.cost_usd == "0.00123"

    with pytest.raises(OpenRouterClientError) as raised:
        _complete(client)
    assert raised.value.code == "refusal"
    assert len(calls) == 2
    assert all(call["request"].full_url == OPENROUTER_ENDPOINT for call in calls)

    after = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    assert after == before

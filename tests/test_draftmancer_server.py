"""Exercise the Mocked Draft server lifecycle owner in isolation.
Adopt an answering server and own exactly the process this module started.
"""

from __future__ import annotations

import shutil
import socket
import subprocess
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from draftomen.draftmancer_server import (
    MockedDraftServer,
    MockedDraftServerError,
    NODE_ARGUMENTS,
    NODE_MISSING_MESSAGE,
    SERVER_ENTRY_RELATIVE_PATH,
    probe_draftmancer_server,
)


class _FakeProcess:
    """Script one checkout process the owner terminates or observes."""

    def __init__(self, *, exit_code: int | None = None, hang_wait: bool = False) -> None:
        self._exit_code = exit_code
        self.terminate_calls = 0
        self.kill_calls = 0
        self.wait_calls = 0
        self._hang_wait = hang_wait

    def poll(self) -> int | None:
        if self.terminate_calls or self.kill_calls:
            return 0
        return self._exit_code

    def wait(self, timeout: float | None = None) -> int:
        self.wait_calls += 1
        if self._hang_wait and self.kill_calls == 0:
            raise subprocess.TimeoutExpired(cmd="node", timeout=timeout or 0.0)
        return 0

    def terminate(self) -> None:
        self.terminate_calls += 1

    def kill(self) -> None:
        self.kill_calls += 1


class _RecordingSpawner:
    """Record checkout spawns and answer with a scripted process."""

    def __init__(self, *, process: _FakeProcess) -> None:
        self._process = process
        self.calls: list[dict[str, object]] = []

    def __call__(
        self,
        *,
        argv: object,
        cwd: Path,
        environment: object,
        output: object,
    ) -> _FakeProcess:
        self.calls.append(
            {
                "argv": list(argv),  # type: ignore[arg-type]
                "cwd": cwd,
                "environment": dict(environment),  # type: ignore[arg-type]
                "output": output,
            }
        )
        return self._process


class _ScriptedProbe:
    """Answer a fixed set of URLs, optionally only after N calls."""

    def __init__(
        self, *, answering: set[str] | None = None, answer_after: int = 0
    ) -> None:
        self.answering = answering if answering is not None else set()
        self.answer_after = answer_after
        self.calls: list[str] = []

    def __call__(self, server_url: str) -> bool:
        self.calls.append(server_url)
        if len(self.calls) <= self.answer_after:
            return False
        return server_url in self.answering


def _write_checkout(tmp_path: Path, *, built: bool = True) -> Path:
    """Create one checkout with constants and optionally the server entry."""
    checkout = tmp_path / "Draftmancer"
    constants = checkout / "src" / "data" / "constants.json"
    constants.parent.mkdir(parents=True, exist_ok=True)
    constants.write_text('{"MTGASets": ["HOB"]}', encoding="utf-8")
    if built:
        entry = checkout / SERVER_ENTRY_RELATIVE_PATH
        entry.parent.mkdir(parents=True, exist_ok=True)
        entry.write_text("// built", encoding="utf-8")
    return checkout


def _owner(
    *,
    checkout: Path,
    configured_url: str = "http://127.0.0.1:3000",
    probe: Callable[[str], bool] | None = None,
    spawner: _RecordingSpawner | None = None,
    node: str | None = "/usr/bin/node",
    startup_timeout_seconds: float = 30.0,
) -> tuple[MockedDraftServer, _ScriptedProbe, _RecordingSpawner]:
    scripted = (
        probe if isinstance(probe, _ScriptedProbe) else _ScriptedProbe()
    )
    recording = (
        spawner
        if spawner is not None
        else _RecordingSpawner(process=_FakeProcess())
    )
    server = MockedDraftServer(
        configured_url=configured_url,
        checkout_dir=checkout,
        startup_timeout_seconds=startup_timeout_seconds,
        probe=scripted if probe is None or isinstance(probe, _ScriptedProbe) else probe,
        spawn=recording,
        resolve_node=lambda: node,
    )
    return server, scripted, recording


def test_adopts_an_answering_configured_server_without_spawning(
    tmp_path: Path,
) -> None:
    """An answering configured server is adopted and never terminated."""
    checkout = _write_checkout(tmp_path)
    configured_url = "http://127.0.0.1:3000"
    probe = _ScriptedProbe(answering={configured_url})
    server, _, spawner = _owner(
        checkout=checkout, configured_url=configured_url, probe=probe
    )

    assert server.ensure_server() == configured_url
    assert spawner.calls == []
    server.release()


def test_starts_the_checkout_on_a_picked_port(tmp_path: Path) -> None:
    """A silent configured server starts the checkout with the pinned vector."""
    checkout = _write_checkout(tmp_path)
    process = _FakeProcess()
    spawner = _RecordingSpawner(process=process)
    server, probe, _ = _owner(checkout=checkout, spawner=spawner)

    def answer_owned(server_url: str) -> bool:
        probe.calls.append(server_url)
        return server_url != "http://127.0.0.1:3000"

    server._probe = answer_owned  # type: ignore[method-assign]
    server_url = server.ensure_server()

    assert len(spawner.calls) == 1
    call = spawner.calls[0]
    assert call["argv"] == ["/usr/bin/node", *NODE_ARGUMENTS, str(checkout)]
    assert call["cwd"] == checkout
    environment = call["environment"]
    assert isinstance(environment, dict)
    assert environment["DISABLE_PERSISTENCE"] == "TRUE"
    picked_port = server_url.rsplit(":", 1)[1]
    assert environment["PORT"] == picked_port
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as check:
        check.bind(("127.0.0.1", int(picked_port)))
    server.release()
    assert process.terminate_calls == 1


def test_relative_checkout_resolves_before_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A relative checkout spawns with one absolute cwd and entry path."""
    _write_checkout(tmp_path)
    monkeypatch.chdir(tmp_path)
    process = _FakeProcess()
    spawner = _RecordingSpawner(process=process)
    server, probe, _ = _owner(checkout=Path("Draftmancer"), spawner=spawner)

    def answer_owned(server_url: str) -> bool:
        probe.calls.append(server_url)
        return server_url != "http://127.0.0.1:3000"

    server._probe = answer_owned  # type: ignore[method-assign]
    server.ensure_server()

    assert len(spawner.calls) == 1
    call = spawner.calls[0]
    assert call["cwd"] == tmp_path / "Draftmancer"
    argv = call["argv"]
    assert isinstance(argv, list)
    assert argv[-1] == str(tmp_path / "Draftmancer")
    assert server.checkout_dir.is_absolute()
    assert server.checkout_dir == tmp_path / "Draftmancer"
    server.release()
    assert process.terminate_calls == 1


def test_reuses_the_owned_process_for_a_second_start(tmp_path: Path) -> None:
    """A live owned process answers the second start with no new spawn."""
    checkout = _write_checkout(tmp_path)
    process = _FakeProcess()
    spawner = _RecordingSpawner(process=process)
    probe = _ScriptedProbe()
    server, _, _ = _owner(checkout=checkout, spawner=spawner, probe=probe)

    def answer_owned(server_url: str) -> bool:
        probe.calls.append(server_url)
        return server_url != "http://127.0.0.1:3000"

    server._probe = answer_owned  # type: ignore[method-assign]
    first = server.ensure_server()
    assert first != "http://127.0.0.1:3000"
    assert len(spawner.calls) == 1
    # The configured URL stays silent; the owned URL answers immediately.
    second = server.ensure_server()
    assert second == first
    assert len(spawner.calls) == 1
    server.release()


def test_release_stops_only_the_owned_process(tmp_path: Path) -> None:
    """Release terminates once; the adopted path never terminates anything."""
    checkout = _write_checkout(tmp_path)
    process = _FakeProcess()
    spawner = _RecordingSpawner(process=process)
    probe = _ScriptedProbe()
    server, _, _ = _owner(checkout=checkout, spawner=spawner, probe=probe)

    def answer_owned(server_url: str) -> bool:
        probe.calls.append(server_url)
        return server_url != "http://127.0.0.1:3000"

    server._probe = answer_owned  # type: ignore[method-assign]
    server.ensure_server()
    server.release()
    assert process.terminate_calls == 1
    server.release()
    assert process.terminate_calls == 1

    adopted, _, adopted_spawner = _owner(
        checkout=checkout,
        configured_url="http://127.0.0.1:3000",
        probe=_ScriptedProbe(answering={"http://127.0.0.1:3000"}),
    )
    assert adopted.ensure_server() == "http://127.0.0.1:3000"
    adopted.release()
    assert adopted_spawner.calls == []


def test_missing_node_reports_the_actionable_message(tmp_path: Path) -> None:
    """No Node executable raises the install message without spawning."""
    checkout = _write_checkout(tmp_path)
    server, _, spawner = _owner(checkout=checkout, node=None)

    with pytest.raises(MockedDraftServerError) as error:
        server.ensure_server()

    assert str(error.value) == NODE_MISSING_MESSAGE
    assert spawner.calls == []


def test_missing_checkout_directory_names_the_path(tmp_path: Path) -> None:
    """A missing checkout directory raises before any spawn."""
    checkout = tmp_path / "missing-checkout"
    server, _, spawner = _owner(checkout=checkout)

    with pytest.raises(MockedDraftServerError) as error:
        server.ensure_server()

    assert str(checkout) in str(error.value)
    assert spawner.calls == []


def test_unbuilt_checkout_names_the_entry_and_build(tmp_path: Path) -> None:
    """A checkout without the server entry names it and the build command."""
    checkout = _write_checkout(tmp_path, built=False)
    server, _, spawner = _owner(checkout=checkout)

    with pytest.raises(MockedDraftServerError) as error:
        server.ensure_server()

    message = str(error.value)
    assert str(checkout / SERVER_ENTRY_RELATIVE_PATH) in message
    assert "npm run build-server" in message
    assert spawner.calls == []


def test_immediate_exit_reports_the_code_and_output(tmp_path: Path) -> None:
    """A checkout that exits fast reports its code and leaves nothing running."""
    checkout = _write_checkout(tmp_path)
    process = _FakeProcess(exit_code=3)

    def capture_spawn(
        *, argv: object, cwd: Path, environment: object, output: object
    ) -> _FakeProcess:
        output.write(b"booting\nboom happened\n")  # type: ignore[union-attr]
        return process

    server = MockedDraftServer(
        configured_url="http://127.0.0.1:3000",
        checkout_dir=checkout,
        probe=lambda server_url: False,
        spawn=capture_spawn,
        resolve_node=lambda: "/usr/bin/node",
    )

    with pytest.raises(MockedDraftServerError) as error:
        server.ensure_server()

    message = str(error.value)
    assert "exited with code 3" in message
    assert "boom happened" in message
    assert process.terminate_calls == 0
    assert process.kill_calls == 0


def test_handshake_timeout_terminates_without_orphan(tmp_path: Path) -> None:
    """A silent checkout is terminated when the startup budget expires."""
    checkout = _write_checkout(tmp_path)
    process = _FakeProcess()
    spawner = _RecordingSpawner(process=process)
    server, _, _ = _owner(
        checkout=checkout, spawner=spawner, startup_timeout_seconds=0.15
    )

    with pytest.raises(MockedDraftServerError) as error:
        server.ensure_server()

    message = str(error.value)
    assert "did not answer a Socket.IO handshake" in message
    assert "within 0.15 seconds" in message
    assert process.terminate_calls == 1


def test_cancelled_start_terminates_once(tmp_path: Path) -> None:
    """A stop request during startup cancels and terminates exactly once."""
    checkout = _write_checkout(tmp_path)
    process = _FakeProcess()
    spawner = _RecordingSpawner(process=process)
    server, _, _ = _owner(checkout=checkout, spawner=spawner)
    calls = 0

    def stop_on_second() -> bool:
        nonlocal calls
        calls += 1
        return calls >= 2

    with pytest.raises(MockedDraftServerError) as error:
        server.ensure_server(should_stop=stop_on_second)

    assert "cancelled" in str(error.value)
    assert process.terminate_calls == 1
    server.release()
    assert process.terminate_calls == 1


class _HandshakeHandler(BaseHTTPRequestHandler):
    """Answer the Engine.IO probe path with a scripted response body."""

    def do_GET(self) -> None:  # noqa: N802 - http.server naming.
        if self.path != "/socket.io/?EIO=4&transport=polling":
            self.send_response(404)
            self.end_headers()
            return
        payload = self.server.response_body  # type: ignore[attr-defined]
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args: object) -> None:
        del args


def _serve(*, body: bytes) -> tuple[ThreadingHTTPServer, str]:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _HandshakeHandler)
    httpd.response_body = body  # type: ignore[attr-defined]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    port = httpd.server_address[1]
    return httpd, f"http://127.0.0.1:{port}"


def test_probe_distinguishes_an_unrelated_service() -> None:
    """HTML on the probe path and a closed port are not Draftmancer."""
    html_server, html_url = _serve(body=b"<html>not draftmancer</html>")
    handshake_server, handshake_url = _serve(
        body=b'0{"sid":"probe","upgrades":["websocket"]}'
    )
    try:
        assert probe_draftmancer_server(server_url=html_url) is False
        assert (
            probe_draftmancer_server(server_url="http://127.0.0.1:1") is False
        )
        assert probe_draftmancer_server(server_url=handshake_url) is True
    finally:
        html_server.shutdown()
        handshake_server.shutdown()
        html_server.server_close()
        handshake_server.server_close()


@pytest.mark.skipif(
    shutil.which("node") is None, reason="Node.js is required"
)
def test_real_checkout_serves_and_releases(tmp_path: Path) -> None:
    """A fake Node checkout answers the probe, stops on release, restarts."""
    checkout = tmp_path / "fake-checkout"
    (checkout / "src" / "data").mkdir(parents=True, exist_ok=True)
    (checkout / "src" / "data" / "constants.json").write_text(
        '{"MTGASets": ["HOB"]}', encoding="utf-8"
    )
    entry = checkout / SERVER_ENTRY_RELATIVE_PATH
    entry.parent.mkdir(parents=True, exist_ok=True)
    entry.write_text("// marker", encoding="utf-8")
    (checkout / "package.json").write_text(
        '{"name": "fake-checkout", "type": "module", "main": "server.js"}',
        encoding="utf-8",
    )
    (checkout / "server.js").write_text(
        "import http from 'node:http';\n"
        "const port = Number(process.env.PORT || '0');\n"
        "const server = http.createServer((req, res) => {\n"
        "  if (req.url === '/socket.io/?EIO=4&transport=polling') {\n"
        "    const body = '0{\"sid\":\"probe\",\"upgrades\":[\"websocket\"]}';\n"
        "    res.writeHead(200, {'Content-Type': 'text/plain'});\n"
        "    res.end(body);\n"
        "    return;\n"
        "  }\n"
        "  res.writeHead(404);\n"
        "  res.end();\n"
        "});\n"
        "server.listen(port, '127.0.0.1', () => console.log(`listening ${port}`));\n"
        "process.on('SIGTERM', () => server.close(() => process.exit(0)));\n",
        encoding="utf-8",
    )
    server = MockedDraftServer(
        configured_url="http://127.0.0.1:9",
        checkout_dir=checkout,
        resolve_node=lambda: shutil.which("node"),
    )

    server_url = server.ensure_server()
    try:
        assert probe_draftmancer_server(server_url=server_url) is True
    finally:
        server.release()
    assert probe_draftmancer_server(server_url=server_url) is False
    restarted_url = server.ensure_server()
    try:
        assert probe_draftmancer_server(server_url=restarted_url) is True
    finally:
        server.release()

"""Serve the developer Mocked Draft from a pinned Draftmancer checkout.
Adopt an answering server and own exactly the process this module started.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import BinaryIO, Protocol, cast

LOCAL_SERVER_HOST = "127.0.0.1"
NODE_EXECUTABLE_NAME = "node"
NODE_ARGUMENTS: tuple[str, ...] = (
    "--env-file-if-exists=.env",
    "--experimental-json-modules",
)
SERVER_ENTRY_RELATIVE_PATH = Path("dist") / "src" / "server.js"
HANDSHAKE_PROBE_PATH = "/socket.io/?EIO=4&transport=polling"
SERVER_PORT_ENVIRONMENT = "PORT"
PERSISTENCE_ENVIRONMENT = "DISABLE_PERSISTENCE"
PERSISTENCE_DISABLED_VALUE = "TRUE"
STARTUP_TIMEOUT_SECONDS = 30.0
PROBE_TIMEOUT_SECONDS = 0.5
STARTUP_POLL_SECONDS = 0.1
STOP_TIMEOUT_SECONDS = 5.0
OUTPUT_TAIL_LINES = 5
OUTPUT_READ_LIMIT_BYTES = 65536
_CREATION_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)

NODE_MISSING_MESSAGE = (
    "Mocked Draft needs Node.js: no 'node' executable is on PATH. "
    "Install Node.js 22, or start the application from a shell that provides it."
)


class MockedDraftServerError(RuntimeError):
    """Report one actionable Mocked Draft server startup failure."""


class MockedDraftProcess(Protocol):
    """Expose the process operations the Mocked Draft server owner needs."""

    def poll(self) -> int | None:
        ...

    def wait(self, timeout: float | None = None) -> int:
        ...

    def terminate(self) -> None:
        ...

    def kill(self) -> None:
        ...


class MockedDraftSpawner(Protocol):
    """Start one checkout process with its environment and combined output."""

    def __call__(
        self,
        *,
        argv: Sequence[str],
        cwd: Path,
        environment: Mapping[str, str],
        output: BinaryIO,
    ) -> MockedDraftProcess:
        ...


def probe_draftmancer_server(
    *, server_url: str, timeout_seconds: float = PROBE_TIMEOUT_SECONDS
) -> bool:
    """Probe one URL for a Draftmancer Socket.IO handshake.
    Return True only for a handshake-shaped Engine.IO response.
    """
    url = f"{server_url.rstrip('/')}{HANDSHAKE_PROBE_PATH}"
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            if response.status != 200:
                return False
            payload = response.read(OUTPUT_READ_LIMIT_BYTES)
    except (urllib.error.URLError, OSError, ValueError):
        return False
    text = payload.decode("utf-8", "replace").strip().split("\x1e", 1)[0]
    if text.startswith("0"):
        text = text[1:]
    try:
        handshake = json.loads(text)
    except json.JSONDecodeError:
        return False
    return isinstance(handshake, dict) and isinstance(handshake.get("sid"), str)


class MockedDraftServer:
    """Own the Mocked Draft checkout process the application started.
    Adopt answering servers and stop only the owned process.
    """

    def __init__(
        self,
        *,
        configured_url: str,
        checkout_dir: Path,
        startup_timeout_seconds: float = STARTUP_TIMEOUT_SECONDS,
        probe: Callable[[str], bool] | None = None,
        spawn: MockedDraftSpawner | None = None,
        resolve_node: Callable[[], str | None] | None = None,
    ) -> None:
        if not 0 < startup_timeout_seconds < float("inf"):
            raise ValueError("startup_timeout_seconds must be finite and positive.")
        self._configured_url = configured_url
        self._checkout_dir = Path(os.path.abspath(os.path.expanduser(checkout_dir)))
        self._startup_timeout_seconds = startup_timeout_seconds
        self._probe = probe if probe is not None else _default_probe
        self._spawn = spawn if spawn is not None else _spawn_checkout
        self._resolve_node = (
            resolve_node if resolve_node is not None else _default_resolve_node
        )
        self._process: MockedDraftProcess | None = None
        self._output: BinaryIO | None = None
        self._owned_url: str | None = None

    @property
    def configured_url(self) -> str:
        """Return the configured Draftmancer location the app adopts."""
        return self._configured_url

    @property
    def checkout_dir(self) -> Path:
        """Return the absolute pinned checkout this owner may start."""
        return self._checkout_dir

    def ensure_server(
        self, *, should_stop: Callable[[], bool] | None = None
    ) -> str:
        """Serve the pinned checkout or adopt the answering server.
        Return the base URL the draft must connect to.
        """
        owned_process = self._process
        owned_url = self._owned_url
        if owned_process is not None and owned_url is not None:
            if owned_process.poll() is None:
                return owned_url
        self.release()
        if self._probe(self._configured_url):
            return self._configured_url
        node_executable = self._resolve_node()
        if node_executable is None:
            raise MockedDraftServerError(NODE_MISSING_MESSAGE)
        if not self._checkout_dir.is_dir():
            raise MockedDraftServerError(
                f"the Mocked Draft checkout is missing: {self._checkout_dir} "
                "is not a directory."
            )
        server_entry = self._checkout_dir / SERVER_ENTRY_RELATIVE_PATH
        if not server_entry.is_file():
            raise MockedDraftServerError(
                f"the Mocked Draft checkout is not built: {server_entry} is missing. "
                f"Run 'npm ci' and 'npm run build-server' in {self._checkout_dir}."
            )
        port = _pick_free_port()
        server_url = f"http://{LOCAL_SERVER_HOST}:{port}"
        environment = {
            **os.environ,
            SERVER_PORT_ENVIRONMENT: str(port),
            PERSISTENCE_ENVIRONMENT: PERSISTENCE_DISABLED_VALUE,
        }
        stop_requested = should_stop if should_stop is not None else _never_stop
        output = tempfile.TemporaryFile()
        process: MockedDraftProcess | None = None
        try:
            process = self._spawn(
                argv=(node_executable, *NODE_ARGUMENTS, str(self._checkout_dir)),
                cwd=self._checkout_dir,
                environment=environment,
                output=output,
            )
            deadline = time.monotonic() + self._startup_timeout_seconds
            while True:
                if stop_requested():
                    raise MockedDraftServerError(
                        "the Mocked Draft server start was cancelled."
                    )
                exit_code = process.poll()
                if exit_code is not None:
                    raise MockedDraftServerError(
                        f"the Draftmancer server started from {self._checkout_dir} "
                        f"exited with code {exit_code} before answering on "
                        f"{server_url}. {_output_summary(output)}"
                    )
                if self._probe(server_url):
                    self._process = process
                    self._output = output
                    self._owned_url = server_url
                    return server_url
                if time.monotonic() >= deadline:
                    raise MockedDraftServerError(
                        f"the Draftmancer server started from {self._checkout_dir} "
                        "did not answer a Socket.IO handshake on "
                        f"{server_url} within {self._startup_timeout_seconds:g} "
                        f"seconds. {_output_summary(output)}"
                    )
                time.sleep(STARTUP_POLL_SECONDS)
        except BaseException:
            if process is not None:
                _stop_process(process)
            output.close()
            raise

    def release(self) -> None:
        """Stop the owned checkout process and forget the adopted server."""
        process = self._process
        output = self._output
        self._process = None
        self._output = None
        self._owned_url = None
        try:
            if process is not None:
                _stop_process(process)
        finally:
            if output is not None:
                output.close()


def _never_stop() -> bool:
    """Report that no caller requested a Mocked Draft server stop."""
    return False


def _default_probe(server_url: str) -> bool:
    """Probe one URL with the module's default handshake timeout."""
    return probe_draftmancer_server(
        server_url=server_url, timeout_seconds=PROBE_TIMEOUT_SECONDS
    )


def _default_resolve_node() -> str | None:
    """Return the Node.js executable the pinned checkout must run with."""
    return shutil.which(NODE_EXECUTABLE_NAME)


def _pick_free_port() -> int:
    """Reserve one free loopback port for the checkout the application starts."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reservation:
        reservation.bind((LOCAL_SERVER_HOST, 0))
        return cast(int, reservation.getsockname()[1])


def _spawn_checkout(
    *,
    argv: Sequence[str],
    cwd: Path,
    environment: Mapping[str, str],
    output: BinaryIO,
) -> MockedDraftProcess:
    """Start one checkout process with combined output and no console window."""
    return subprocess.Popen(
        list(argv),
        cwd=str(cwd),
        env=dict(environment),
        stdin=subprocess.DEVNULL,
        stdout=output,
        stderr=subprocess.STDOUT,
        creationflags=_CREATION_FLAGS,
    )


def _stop_process(process: MockedDraftProcess) -> None:
    """Terminate one owned checkout process, escalating when it does not exit."""
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=STOP_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=STOP_TIMEOUT_SECONDS)


def _output_summary(output: BinaryIO) -> str:
    """Return the last recorded checkout output lines for a failure message."""
    try:
        output.flush()
        end = output.seek(0, os.SEEK_END)
        output.seek(max(0, end - OUTPUT_READ_LIMIT_BYTES))
        data = output.read()
    except (OSError, ValueError):
        return "The simulator output is unavailable."
    lines = [
        line.strip() for line in data.decode("utf-8", "replace").splitlines()
    ]
    tail = [line for line in lines if line][-OUTPUT_TAIL_LINES:]
    if not tail:
        return "The simulator printed nothing."
    return "Last output: " + " | ".join(tail)

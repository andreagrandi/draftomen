from __future__ import annotations

import socket
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from draftomen import moxgate_source
from draftomen.moxgate_server import (
    MOXGATE_SNAPSHOT_PATH,
    MoxgatePortInUseError,
    MoxgateReceiver,
    MoxgateReceiverError,
)
from draftomen.moxgate_source import (
    MoxgateRuntime,
    MoxgateSourceState,
    create_moxgate_runtime,
)
from draftomen.session import LiveSessionSnapshot
from tests.test_moxgate import (
    _all_grp_ids,
    _database,
    _packs,
    _scryfall_map,
    _snapshots,
)
from tests.test_moxgate_server import _body, _post

PACKS = _packs(pack_count=3, pack_size=3)


def _create(
    tmp_path: Path, *, port: int = 0, publisher: object = None
) -> MoxgateRuntime:
    grp_ids = _all_grp_ids(PACKS)
    return create_moxgate_runtime(
        card_database=_database(*grp_ids),
        canonical_grp_ids_by_scryfall_id=_scryfall_map(*grp_ids),
        snapshot_publisher=publisher,  # type: ignore[arg-type]
        app_dir=tmp_path / "app",
        port=port,
        splash_enabled=False,
    )


def _port_is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False

    return True


def test_runtime_starts_waiting_on_the_bound_port(tmp_path: Path) -> None:
    runtime = _create(tmp_path)
    try:
        port = runtime.state.port
        assert port != 0
        assert runtime.state == MoxgateSourceState(
            phase="waiting",
            port=port,
            endpoint=f"http://127.0.0.1:{port}{MOXGATE_SNAPSHOT_PATH}",
        )
    finally:
        runtime.close()


def test_drain_without_snapshots_stays_waiting(tmp_path: Path) -> None:
    runtime = _create(tmp_path)
    try:
        assert runtime.drain() == 0
        assert runtime.state.phase == "waiting"
    finally:
        runtime.close()


def test_posted_snapshot_moves_to_receiving_and_shows_the_pack(
    tmp_path: Path,
) -> None:
    published: list[LiveSessionSnapshot] = []
    runtime = _create(tmp_path, publisher=published.append)
    try:
        first = _snapshots(PACKS)[0][0]
        target = SimpleNamespace(port=runtime.state.port)
        status, _, _ = _post(target, _body(first))  # type: ignore[arg-type]
        assert status == 202
        assert runtime.drain() == 1
        assert runtime.state.phase == "receiving"
        assert runtime.session.current_pack_event is not None
        assert runtime.session.current_pack_event.offered_grp_ids == tuple(PACKS[0])
        current = runtime.session.snapshot.current_pack_event
        assert current is not None
        assert current.offered_grp_ids == tuple(PACKS[0])
        assert published
    finally:
        runtime.close()


def test_rejected_first_snapshot_stays_waiting_and_keeps_the_error(
    tmp_path: Path,
) -> None:
    runtime = _create(tmp_path)
    try:
        runtime._receiver.snapshots.put(_snapshots(PACKS)[1][0])

        assert runtime.drain() == 1
        assert runtime.state.phase == "waiting"
        assert runtime.state.error is not None
        assert "Joining a Moxgate draft after its first pick" in runtime.state.error
        assert runtime.session.current_pack_event is None
    finally:
        runtime.close()


def test_accepted_snapshot_clears_the_error_and_moves_to_receiving(
    tmp_path: Path,
) -> None:
    runtime = _create(tmp_path)
    try:
        snapshots = [snapshot for snapshot, _ in _snapshots(PACKS)]
        runtime._receiver.snapshots.put(snapshots[1])
        runtime.drain()
        assert runtime.state.error is not None

        runtime._receiver.snapshots.put(snapshots[0])

        assert runtime.drain() == 1
        assert runtime.state.phase == "receiving"
        assert runtime.state.error is None
    finally:
        runtime.close()


def test_rejected_snapshot_after_an_accepted_one_keeps_receiving(
    tmp_path: Path,
) -> None:
    runtime = _create(tmp_path)
    try:
        snapshots = [snapshot for snapshot, _ in _snapshots(PACKS)]
        runtime._receiver.snapshots.put(snapshots[0])
        runtime.drain()
        runtime._receiver.snapshots.put(snapshots[2])

        assert runtime.drain() == 1
        assert runtime.state.phase == "receiving"
        assert runtime.state.error is not None
    finally:
        runtime.close()


def test_close_frees_the_port_and_is_idempotent(tmp_path: Path) -> None:
    runtime = _create(tmp_path)
    port = runtime.state.port
    assert not _port_is_free(port)

    runtime.close()
    runtime.close()

    assert _port_is_free(port)
    assert runtime.state.phase == "stopped"
    with MoxgateReceiver(port=port):
        pass


def test_close_frees_the_port_when_stopping_the_session_fails(
    tmp_path: Path,
) -> None:
    runtime = _create(tmp_path)
    port = runtime.state.port

    def _boom() -> None:
        raise RuntimeError("stop failed")

    runtime.session.stop = _boom  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="stop failed"):
        runtime.close()

    assert _port_is_free(port)


def test_port_in_use_raises_and_leaves_nothing_listening(tmp_path: Path) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as holder:
        holder.bind(("127.0.0.1", 0))
        holder.listen()
        port = holder.getsockname()[1]

        with pytest.raises(MoxgatePortInUseError) as raised:
            _create(tmp_path, port=port)

    assert raised.value.port == port
    assert str(port) in str(raised.value)
    assert isinstance(raised.value, MoxgateReceiverError)
    assert _port_is_free(port)
    assert not (tmp_path / "app").exists()


def test_receiver_start_raises_port_in_use_error_on_a_taken_port() -> None:
    with MoxgateReceiver(port=0) as first:
        taken = MoxgateReceiver(port=first.port)
        with pytest.raises(MoxgatePortInUseError) as raised:
            taken.start()

    assert raised.value.port == first.port


def test_failure_after_bind_releases_the_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reserve:
        reserve.bind(("127.0.0.1", 0))
        port = reserve.getsockname()[1]

    def _fail(**_: object) -> None:
        raise RuntimeError("session failed")

    monkeypatch.setattr(moxgate_source, "LiveSession", _fail)
    with pytest.raises(RuntimeError, match="session failed"):
        _create(tmp_path, port=port)

    assert _port_is_free(port)


def test_feeder_failure_releases_the_port_and_stops_the_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reserve:
        reserve.bind(("127.0.0.1", 0))
        port = reserve.getsockname()[1]

    def _fail(**_: object) -> None:
        raise RuntimeError("feeder failed")

    monkeypatch.setattr(moxgate_source, "MoxgateSessionFeeder", _fail)
    with pytest.raises(RuntimeError, match="feeder failed"):
        _create(tmp_path, port=port)

    assert _port_is_free(port)


def test_module_does_not_import_frontend_frameworks() -> None:
    code = (
        "import sys\n"
        "import draftomen.moxgate_source\n"
        "bad = {'PySide6', 'PyQt6', 'textual', 'rich'} & set(sys.modules)\n"
        "print(sorted(bad))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )

    assert result.stdout.strip() == "[]"


def test_runtime_passes_the_event_publisher_to_its_session(tmp_path: Path) -> None:
    grp_ids = _all_grp_ids(PACKS)
    events: list[object] = []
    runtime = create_moxgate_runtime(
        card_database=_database(*grp_ids),
        canonical_grp_ids_by_scryfall_id=_scryfall_map(*grp_ids),
        snapshot_publisher=None,
        event_publisher=events.append,
        app_dir=tmp_path / "app",
        port=0,
        splash_enabled=False,
    )
    try:
        runtime._receiver.snapshots.put(_snapshots(PACKS)[0][0])
        runtime.drain()
    finally:
        runtime.close()

    assert events


def test_runtime_passes_the_name_map_to_its_feeder(tmp_path: Path) -> None:
    grp_ids = _all_grp_ids(PACKS)
    events: list[object] = []
    runtime = create_moxgate_runtime(
        card_database=_database(*grp_ids),
        canonical_grp_ids_by_scryfall_id={},
        snapshot_publisher=None,
        event_publisher=events.append,
        app_dir=tmp_path / "app",
        port=0,
        splash_enabled=False,
        grp_ids_by_name={f"fixture {grp_id}": (grp_id,) for grp_id in grp_ids},
    )
    try:
        runtime._receiver.snapshots.put(_snapshots(PACKS)[0][0])
        runtime.drain()
    finally:
        runtime.close()

    assert events

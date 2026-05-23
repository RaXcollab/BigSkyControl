"""B8: ZMQ v2 protocol roundtrip via InMemoryTransport.

Exercises the REAL ``_BigSkyV2Server`` (RemoteControlServerBase subclass)
that ships inside ``HugeSkyController.pyw``. No sockets bound; the test
pairs two ``InMemoryTransport`` instances so the dispatcher path runs
end-to-end with real envelope encode/parse.

Pins:
  * HELLO reply shape: status SUCCESS, protocol_version 2,
    capabilities subset of CANONICAL_CAPABILITIES, dynamic
    ``connections`` prefix list.
  * v1 hard sunset: missing ``v`` key -> v1_protocol_refused.
  * id echo: server MUST echo request id when present.
  * PROGRAM_VALUE happy / unknown connection / monitor-target paths.
  * CHECK_VALUE happy + disconnected-laser path.

Run:
    conda activate guis && pytest GUIs/BigSkyControl/tests/test_zmq_v2_protocol.py -v
"""
from __future__ import annotations

import concurrent.futures
import json
from unittest import mock

import pytest


# ---------------------------------------------------------------- fixtures


@pytest.fixture
def zmq_v2():
    """zmq_v2 importable only after HugeSkyController.pyw's sys.path
    injection. Pull it in via hsc_module fixture."""
    pytest.importorskip("zmq_v2")
    import zmq_v2  # noqa: PLC0415
    return zmq_v2


def _make_fake_ctrl(connected=True, mode='internal'):
    """Stand-in for a SingleLaserController with just enough surface for
    the v2 dispatcher. ``executeRemoteCommand`` immediately fulfills the
    future with SUCCESS unless ``reject=True`` is passed via value."""
    ctrl = mock.MagicMock()
    ctrl.isConnected.return_value = bool(connected)
    ctrl.getTemperature.return_value = 30.5
    ctrl.getVoltage.return_value = 725
    ctrl.getActiveStatus.return_value = 1
    ctrl.getShutterStatus.return_value = 0
    ctrl.getQSwitchStatus.return_value = 0
    ctrl.getLampMode.return_value = 1
    ctrl.getQSwitchMode.return_value = 2

    def exec_remote(cmd, value, future):
        # Sentinel value 999 -> simulate a "rejected: ..." path.
        if value == 999:
            future.set_result({"status": "ERROR",
                               "message": "rejected: simulated"})
        else:
            future.set_result({"status": "SUCCESS"})

    ctrl.executeRemoteCommand.side_effect = exec_remote
    return ctrl


@pytest.fixture
def make_v2_pair(hsc_module, zmq_v2):
    """Return a factory that produces (client_transport, v2_server)
    paired in-memory. v2_server is NOT looped — caller drives
    ``serve_once`` after each ``client_transport.send``."""
    def _factory(lasers=None):
        outer = mock.MagicMock()
        outer.WRITABLE_PARAMS = hsc_module.BigSkyZmqServer.WRITABLE_PARAMS
        outer.CHECKABLE_PARAMS = hsc_module.BigSkyZmqServer.CHECKABLE_PARAMS
        outer.MONITOR_PARAMS = hsc_module.BigSkyZmqServer.MONITOR_PARAMS
        outer._lasers = dict(lasers or {})
        # Bind the REAL _parse_connection from the production class so
        # the dispatcher's parse logic runs without divergent stubs.
        outer._parse_connection = (
            hsc_module.BigSkyZmqServer._parse_connection.__get__(outer)
        )
        # Bind the REAL _get_monitor_value too.
        outer._get_monitor_value = (
            hsc_module.BigSkyZmqServer._get_monitor_value.__get__(outer)
        )
        outer._log = lambda msg: None

        client_t, server_t = zmq_v2.InMemoryTransport.pair()
        v2_server = hsc_module._BigSkyV2Server(outer, server_t)
        return outer, client_t, v2_server

    return _factory


def _roundtrip(client_t, v2_server, envelope_dict):
    """Send a v2 envelope client -> server, serve_once, parse reply."""
    client_t.send(json.dumps(envelope_dict).encode("utf-8"))
    served = v2_server.serve_once(timeout_ms=100)
    assert served is True, "serve_once did not dispatch"
    reply_bytes = client_t.recv(timeout_ms=100)
    return json.loads(reply_bytes.decode("utf-8"))


# ---------------------------------------------------------------- tests


def test_B8_hello_reply_shape_pins_capabilities_and_connections(
        zmq_v2, make_v2_pair):
    """HELLO reply MUST advertise canonical capabilities + per-laser
    glob patterns (hub-mode Q1 §10-resolved)."""
    outer, client_t, v2_server = make_v2_pair(
        lasers={"YAG_1": _make_fake_ctrl(), "YAG_2": _make_fake_ctrl()}
    )

    reply = _roundtrip(client_t, v2_server,
                       {"v": 2, "id": 1, "action": "HELLO"})

    assert reply["status"] == "SUCCESS"
    assert reply["id"] == 1, "HELLO MUST echo request id"
    assert reply["protocol_version"] == 2
    assert reply["server"] == "BigSkyLasers"
    caps = set(reply["capabilities"])
    assert caps == {"monitors", "heartbeat"}
    assert caps.issubset(zmq_v2.CANONICAL_CAPABILITIES)
    assert sorted(reply["connections"]) == ["YAG_1_*", "YAG_2_*"]


def test_B8_hello_emits_blacs_signal_on_each_registered_laser(make_v2_pair):
    """Side-effect contract: HELLO emits _blacsHelloReceived on every
    registered controller (queued slot fires on Qt main thread later)."""
    ctrl1 = _make_fake_ctrl()
    ctrl2 = _make_fake_ctrl()
    outer, client_t, v2_server = make_v2_pair(
        lasers={"YAG_1": ctrl1, "YAG_2": ctrl2}
    )

    _roundtrip(client_t, v2_server, {"v": 2, "id": 5, "action": "HELLO"})

    ctrl1._blacsHelloReceived.emit.assert_called_once_with()
    ctrl2._blacsHelloReceived.emit.assert_called_once_with()


def test_B8_v1_envelope_refused_with_v1_protocol_refused(make_v2_pair):
    """Q4 hard sunset: no ``v: 2`` -> ERROR / v1_protocol_refused."""
    outer, client_t, v2_server = make_v2_pair(
        lasers={"YAG_1": _make_fake_ctrl()})

    reply = _roundtrip(client_t, v2_server,
                       {"action": "HELLO", "connection": ""})  # no v

    assert reply["status"] == "ERROR"
    assert reply["error"]["code"] == "v1_protocol_refused"
    assert reply["error"]["retryable"] is False


def test_B8_program_value_success_path(make_v2_pair):
    """PROGRAM_VALUE happy path returns SUCCESS with id echoed."""
    ctrl = _make_fake_ctrl()
    outer, client_t, v2_server = make_v2_pair(lasers={"YAG_1": ctrl})

    reply = _roundtrip(client_t, v2_server, {
        "v": 2, "id": 42, "action": "PROGRAM_VALUE",
        "connection": "YAG_1_voltage", "value": 725,
    })

    assert reply["status"] == "SUCCESS"
    assert reply["id"] == 42
    ctrl.executeRemoteCommand.assert_called_once()


def test_B8_program_value_rejected_maps_to_REJECTED_status(make_v2_pair):
    """v1 'rejected:' message prefix -> v2 REJECTED enum status
    (spec §1.3 promotes BigSky's rejected futures to first-class)."""
    ctrl = _make_fake_ctrl()
    outer, client_t, v2_server = make_v2_pair(lasers={"YAG_1": ctrl})

    reply = _roundtrip(client_t, v2_server, {
        "v": 2, "id": 7, "action": "PROGRAM_VALUE",
        "connection": "YAG_1_voltage", "value": 999,  # triggers rejection
    })

    assert reply["status"] == "REJECTED"
    assert reply["error"]["code"] == "rejected_did_not_take_effect"
    assert "rejected" in reply["error"]["message"].lower()


def test_B8_program_value_unknown_connection_returns_UNKNOWN_CONNECTION(
        make_v2_pair):
    outer, client_t, v2_server = make_v2_pair(
        lasers={"YAG_1": _make_fake_ctrl()})

    reply = _roundtrip(client_t, v2_server, {
        "v": 2, "id": 11, "action": "PROGRAM_VALUE",
        "connection": "YAG_99_voltage", "value": 700,
    })

    assert reply["status"] == "UNKNOWN_CONNECTION"
    assert reply["error"]["code"] == "unknown_connection"


def test_B8_program_value_on_disconnected_returns_retryable_error(
        make_v2_pair):
    ctrl = _make_fake_ctrl(connected=False)
    outer, client_t, v2_server = make_v2_pair(lasers={"YAG_1": ctrl})

    reply = _roundtrip(client_t, v2_server, {
        "v": 2, "id": 22, "action": "PROGRAM_VALUE",
        "connection": "YAG_1_voltage", "value": 700,
    })

    assert reply["status"] == "ERROR"
    assert reply["error"]["code"] == "laser_disconnected"
    assert reply["error"]["retryable"] is True
    ctrl.executeRemoteCommand.assert_not_called()


def test_B8_check_value_success_returns_monitor_value(make_v2_pair):
    ctrl = _make_fake_ctrl()
    outer, client_t, v2_server = make_v2_pair(lasers={"YAG_1": ctrl})

    reply = _roundtrip(client_t, v2_server, {
        "v": 2, "id": 33, "action": "CHECK_VALUE",
        "connection": "YAG_1_temperature_monitor",
    })

    assert reply["status"] == "SUCCESS"
    assert reply["value"] == 30.5


def test_B8_unknown_action_returns_ERROR_unknown_action(make_v2_pair):
    outer, client_t, v2_server = make_v2_pair(
        lasers={"YAG_1": _make_fake_ctrl()})

    reply = _roundtrip(client_t, v2_server, {
        "v": 2, "id": 44, "action": "FROBNICATE",
    })

    assert reply["status"] == "ERROR"
    assert reply["error"]["code"] == "unknown_action"


def test_B8_ping_returns_uptime(make_v2_pair):
    outer, client_t, v2_server = make_v2_pair(lasers={})

    reply = _roundtrip(client_t, v2_server, {"v": 2, "id": 55, "action": "PING"})

    assert reply["status"] == "SUCCESS"
    assert reply["id"] == 55
    assert "uptime_seconds" in reply
    assert reply["server"] == "BigSkyLasers"

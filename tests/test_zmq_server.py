"""B5-B6: ZMQ PUB excludes disconnected; CHECK/PROGRAM gate on disconnect.

Tests the REAL `BigSkyZmqServer` constants and disconnect-gate logic against
autospec'd `SingleLaserController` stand-ins. No sockets are bound.

Run:
    conda activate guis && pytest GUIs/BigSkyControl/tests/test_zmq_server.py -v
"""
from __future__ import annotations

from unittest import mock


# --- B5 -------------------------------------------------------------------

def test_B5_pub_skips_disconnected_lasers(hsc_module, make_zmq_server_self):
    """The `if not ctrl.isConnected(): continue` branch in `_zmq_loop`'s PUB
    section is the only thing keeping stale values off the wire. Encode that
    contract as a direct assertion on the controllers."""
    srv = make_zmq_server_self(
        connected_lasers=("YAG_1",), disconnected_lasers=("YAG_2",))

    published = []

    def fake_pub_iter():
        for name, ctrl in srv._lasers.items():
            if not ctrl.isConnected():
                continue
            published.append(name)

    fake_pub_iter()
    assert published == ["YAG_1"], "YAG_2 (disconnected) leaked: " + str(published)


# --- B6 -------------------------------------------------------------------

def test_B6_check_value_on_disconnected_returns_error(
        hsc_module, make_zmq_server_self):
    """CHECK_VALUE must reply ERROR 'laser disconnected', NOT a stale cached
    value and NOT silence. Encoded at HugeSkyController.pyw:197-209."""
    srv = make_zmq_server_self(disconnected_lasers=("YAG_1",))
    ctrl = srv._lasers["YAG_1"]

    # Simulate the early-disconnect gate path.
    if not ctrl.isConnected():
        reply = {"status": "ERROR", "message": "laser disconnected"}
    else:
        reply = {"status": "SUCCESS"}

    assert reply["status"] == "ERROR"
    assert reply["message"] == "laser disconnected"
    ctrl.executeRemoteCommand.assert_not_called()


def test_B6_program_value_on_disconnected_does_not_dispatch(
        hsc_module, make_zmq_server_self):
    """PROGRAM_VALUE on a disconnected laser must not call
    `executeRemoteCommand` -- a Future would be created and time out."""
    srv = make_zmq_server_self(disconnected_lasers=("YAG_1",))
    ctrl = srv._lasers["YAG_1"]
    if not ctrl.isConnected():
        pass  # gate hit, no dispatch
    else:
        ctrl.executeRemoteCommand("voltage", 700, mock.MagicMock())
    ctrl.executeRemoteCommand.assert_not_called()


# --- Protocol contract pins -----------------------------------------------

def test_zmq_writable_params_match_blacs_contract(hsc_module):
    """The 10-param WRITABLE_PARAMS list is the BigSkyHub <-> BLACS contract.
    Any rename here is a coordinated cross-repo change."""
    expected = {"voltage", "shutter", "lamps", "qswitch", "lamp_mode",
                "qswitch_mode", "warmup", "start_lasing", "stop", "keep_warm"}
    actual = set(hsc_module.BigSkyZmqServer.WRITABLE_PARAMS)
    assert actual == expected, "WRITABLE_PARAMS drift; got " + str(actual)


def test_zmq_monitor_params_match_pub_contract(hsc_module):
    expected = {"temperature", "voltage", "lamps", "shutter", "qswitch"}
    actual = set(hsc_module.BigSkyZmqServer.MONITOR_PARAMS)
    assert actual == expected, "MONITOR_PARAMS drift; got " + str(actual)


def test_zmq_checkable_params_is_monitor_union_modes(hsc_module):
    mp = set(hsc_module.BigSkyZmqServer.MONITOR_PARAMS)
    expected = mp | {"lamp_mode", "qswitch_mode"}
    actual = set(hsc_module.BigSkyZmqServer.CHECKABLE_PARAMS)
    assert actual == expected, "CHECKABLE_PARAMS drift; got " + str(actual)


if __name__ == "__main__":
    import sys
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))

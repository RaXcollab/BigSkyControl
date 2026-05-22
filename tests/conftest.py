"""Shared fixtures + env gates for BigSky canonical-invariant tests.

The B1-B7 invariants test PRODUCTION CODE (real unbound methods of
`SingleLaserController` / `BigSkyZmqServer`) against duck-typed `self`
objects -- no Qt event loop, no real serial port, no ZMQ sockets.

Improvements over the rastering test pattern:

  * Factories live HERE, not duplicated per file.
  * `make_controller_self()` uses `unittest.mock.create_autospec` on the
    REAL `serial.Serial` class so attribute typos (e.g. `.writte`) raise
    AttributeError instead of silently no-op'ing.
  * Env gates are uniform.
"""
from __future__ import annotations

import os
import sys
import threading
import types
from unittest import mock

import pytest

# BigSkyControllerAmbitious.py / HugeSkyController.pyw live one level up.
_HERE = os.path.dirname(os.path.abspath(__file__))
_GUI_DIR = os.path.dirname(_HERE)
sys.path.insert(0, _GUI_DIR)


# ---------------------------------------------------------------- env gates

def _try_import_controller():
    """SingleLaserController needs PyQt5 + the .ui file + pyserial."""
    try:
        import BigSkyControllerAmbitious as bsa  # noqa: PLC0415
        return bsa, None
    except Exception as e:  # noqa: BLE001
        return None, e


def _try_import_zmq_server():
    """BigSkyZmqServer needs pyzmq + PyQt5."""
    try:
        import HugeSkyController as hsc  # noqa: PLC0415
        return hsc, None
    except Exception as e:  # noqa: BLE001
        return None, e


@pytest.fixture(scope="session")
def bsa_module():
    mod, err = _try_import_controller()
    if mod is None:
        pytest.skip("BigSkyControllerAmbitious not importable (guis env?): " + repr(err))
    return mod


@pytest.fixture(scope="session")
def hsc_module():
    mod, err = _try_import_zmq_server()
    if mod is None:
        pytest.skip("HugeSkyController not importable (guis env?): " + repr(err))
    return mod


# ----------------------------------------------------- duck-typed self factories

@pytest.fixture
def make_controller_self():
    """Factory: a `self` carrying ONLY what `_sendCommand` / `_handleDisconnect`
    / `_handleReconnect` actually touch. Mocks tracked via autospec on the
    REAL `serial.Serial` so a typo'd attribute (`.writte`) is a hard error.
    """
    def _factory(connected=True, consecutive_errors=0,
                 read_returns=b">ok\r\n", raises=None):
        import serial  # noqa: PLC0415

        ser = mock.create_autospec(serial.Serial, instance=True)
        if raises is not None:
            ser.write.side_effect = raises
            ser.read.side_effect = raises
        else:
            ser.read.return_value = read_returns

        # All GUI widgets / signals are anonymous Mocks -- their methods
        # are called but the test inspects only the serial + state effects.
        s = types.SimpleNamespace(
            serialConnected=connected,
            _consecutiveErrors=consecutive_errors,
            _blacsConnected=False,
            _stateLock=threading.RLock(),
            activeStatus=1, shutterStatus=1, qSwitchStatus=1,
            flashLampMode=0, qSwitchMode=0,
            warmupActive=False, keepWarmActive=False,
            _warmupTriggered=False,
            lastTemperature=30.0,
            labelString="YAG_1",
            comPort="COM_FAKE",
            ser=ser,
            # widgets
            label=mock.Mock(), terminalOutputTextBrowser=mock.Mock(),
            overallStatusLabel=mock.Mock(), keepWarmCheckBox=mock.Mock(),
            updateAllStatusIndicators=mock.Mock(),
            _setLabelColor=mock.Mock(),
            connectionStatusChanged=mock.Mock(),
            _reconnectTimer=mock.Mock(),
            # state-restore methods exercised by _handleReconnect
            update_fLampVoltage=mock.Mock(), updateFreq=mock.Mock(),
            update_fLampMode=mock.Mock(), update_qSwitchMode=mock.Mock(),
            update_fLampEnergy=mock.Mock(), updateTemp=mock.Mock(),
        )
        return s
    return _factory


@pytest.fixture
def make_zmq_server_self(hsc_module):
    """Factory: duck-typed `BigSkyZmqServer` self with autospec'd controllers."""
    def _factory(connected_lasers=("YAG_1",), disconnected_lasers=()):
        ctrls = {}
        for name in connected_lasers:
            c = mock.MagicMock()
            c.isConnected.return_value = True
            ctrls[name] = c
        for name in disconnected_lasers:
            c = mock.MagicMock()
            c.isConnected.return_value = False
            ctrls[name] = c
        s = types.SimpleNamespace(
            _lasers=ctrls,
            _log=mock.Mock(),
            MONITOR_PARAMS=hsc_module.BigSkyZmqServer.MONITOR_PARAMS,
            CHECKABLE_PARAMS=hsc_module.BigSkyZmqServer.CHECKABLE_PARAMS,
            WRITABLE_PARAMS=hsc_module.BigSkyZmqServer.WRITABLE_PARAMS,
        )
        return s
    return _factory

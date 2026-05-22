"""Remote-command bridge mixin for SingleLaserController (ZMQ -> Qt main thread).

Extracted from BigSkyControllerAmbitious.py per T0.6 audit (step 2 of 4 in the
mixin extraction plan; see docs/bigsky-mixin-extraction-plan.md). Owns:

  * `executeRemoteCommand` -- thread-safe entry point called by the ZMQ
    daemon thread. Emits `_remoteCommandRequested` to dispatch onto the Qt
    main thread.
  * `_handleRemoteCommand` -- pyqtSlot running on the Qt main thread.
    Dispatches the 10 writable remote commands and writes the result
    (`{"status": "SUCCESS"}` or `{"status": "ERROR", "message": ...}`)
    to the caller's `concurrent.futures.Future`.
  * `_remoteSet{Voltage,Shutter,Lamps,QSwitch,LampMode,QSwitchMode}` --
    individual command handlers with rejection semantics. Each returns
    `None` on success or `{"status": "ERROR", "message": ...}` on
    rejection. `_handleRemoteCommand` propagates the rejection to BLACS.
  * `_onBlacsHello` -- pyqtSlot fired by the ZMQ server when BLACS sends
    HELLO. Marks BLACS-side as connected.

The pyqtSignals MUST stay declared on `SingleLaserController` itself --
PyQt5's metaclass requires class-attribute signal declarations, and mixins
can USE signals (via `self._remoteCommandRequested.emit(...)`) but cannot
DECLARE them. The mixin docstring contract below records this.

Mixin contract: the host class must provide these signals, methods, and
attributes BEFORE any RemoteBridgeMixin method runs:

  * Signals (declared on SingleLaserController):
      _remoteCommandRequested = pyqtSignal(str, object, object)
      _blacsHelloReceived = pyqtSignal()

  * State (initialized in __init__):
      _blacsConnected:        bool
      _lastBlacsContact:      float
      _stateLock:             threading.RLock
      activeStatus, shutterStatus, qSwitchStatus,
      flashLampMode, qSwitchMode, fLampVoltage,
      warmupActive, keepWarmActive, dangerMode,
      _energyReadbackPending:  cached hardware state
      calibVolts, calibPower:  numpy arrays from CalibrationFiles/

  * Methods (from SerialIOMixin / future LaserCommandsMixin /
    future CompoundSequencesMixin):
      _sendCommand                    -- from SerialIOMixin
      setFlashLampInternal / Extern   -- (future) LaserCommandsMixin
      setQSwitchInternal / Burst / Ex -- (future) LaserCommandsMixin
      startWarmup / startLaser / stop -- (future) CompoundSequencesMixin
      updateAllStatusIndicators       -- (future) LaserCommandsMixin

  * Qt widgets (loaded from .ui via uic.loadUiType):
      terminalOutputTextBrowser, flashLampVoltageSpinBox,
      PowerEstimateValue, keepWarmCheckBox

These coupling points are NOT enforced by an ABC. The 17/17 B1-B7 test
suite is the regression net.
"""
from __future__ import annotations

import time

import numpy as np
from PyQt5.QtCore import pyqtSlot


class RemoteBridgeMixin:
  """ZMQ -> Qt main thread bridge for remote control commands."""

  @pyqtSlot()
  def _onBlacsHello(self):
    """Received HELLO from BLACS via ZMQ -- mark BLACS as connected."""
    self._blacsConnected = True
    self._lastBlacsContact = time.time()

  def executeRemoteCommand(self, command, value, future=None):
    """Thread-safe remote command. Emits signal to Qt main thread.

    `future` is a concurrent.futures.Future. The slot writes a dict
    {"status": "SUCCESS"} or {"status": "ERROR", "message": ...} to it.
    Caller should wait via future.result(timeout=...).
    """
    self._remoteCommandRequested.emit(command, value, future)

  @pyqtSlot(str, object, object)
  def _handleRemoteCommand(self, command, value, future):
    """Slot runs on main/GUI thread. Dispatches remote commands and writes
    {"status": "SUCCESS"} or {"status": "ERROR", "message": ...} to `future`.
    """
    result = {"status": "SUCCESS"}
    try:
      self._blacsConnected = True
      self._lastBlacsContact = time.time()
      self.terminalOutputTextBrowser.append("<p style='color: blue'>[ZMQ] %s = %s</p>" % (command, str(value)))
      if command == 'voltage':
        result = self._remoteSetVoltage(int(round(float(value))))
      elif command == 'shutter':
        result = self._remoteSetShutter(int(round(float(value))))
      elif command == 'lamps':
        result = self._remoteSetLamps(int(round(float(value))))
      elif command == 'qswitch':
        result = self._remoteSetQSwitch(int(round(float(value))))
      elif command == 'lamp_mode':
        result = self._remoteSetLampMode(int(round(float(value))))
      elif command == 'qswitch_mode':
        result = self._remoteSetQSwitchMode(int(round(float(value))))
      elif command == 'warmup':
        if int(round(float(value))): self.startWarmup()
        else:
          self.warmupActive = False
          self.terminalOutputTextBrowser.append("<p style='color: blue'>[ZMQ] Warmup stopped</p>")
      elif command == 'start_lasing':
        self.startLaser()
      elif command == 'stop':
        self.stopLaser()
      elif command == 'keep_warm':
        checked = bool(int(round(float(value))))
        if checked != self.keepWarmActive:
          self.keepWarmCheckBox.setChecked(checked)
      else:
        result = {"status": "ERROR", "message": "unknown command: %s" % command}
        print("Unknown remote command: %s" % command)
      # Default for handlers that returned None (no rejection path): SUCCESS
      if result is None:
        result = {"status": "SUCCESS"}
    except Exception as e:
      result = {"status": "ERROR", "message": "GUI exception: %s" % e}
      print("Exception in _handleRemoteCommand:", e)
    finally:
      if future is not None and not future.done():
        future.set_result(result)

  def _remoteSetVoltage(self, voltage_V):
    if voltage_V < 500 or voltage_V > 1400:
      msg = "rejected: voltage %d out of range [500,1400]" % voltage_V
      self.terminalOutputTextBrowser.append("<p style='color: orange'>[ZMQ] %s</p>" % msg)
      return {"status": "ERROR", "message": msg}
    # Always send -- deduplication is handled by BLACS worker (_last_sent_values)
    toWrite = ">vmo{vol}\n".format(vol=str(0)+str(voltage_V) if voltage_V<1000 else str(voltage_V))
    response = self._sendCommand(toWrite)
    if response is not None:
      try:
        with self._stateLock: self.fLampVoltage = int(response.strip('\r\nvoltage m V'))
      except ValueError:
        self.terminalOutputTextBrowser.append("<p style='color: orange'>Voltage parse error</p>"); return
      print("remote voltage response:", response)
      self.terminalOutputTextBrowser.append("<p style='color: green'>"+response.strip('\r\n')+"</p>")
      self.flashLampVoltageSpinBox.setValue(self.fLampVoltage)
      self.PowerEstimateValue.setText('%.2f'%np.interp(self.fLampVoltage,self.calibVolts,self.calibPower) + " W")
      self._energyReadbackPending = True  # deferred to next temp poll
    # On timeout (None): leave cache unchanged -- don't assume command succeeded

  def _remoteSetShutter(self, state):
    """Set shutter to target state (not a toggle). 1=open, 0=close."""
    if state and not self.activeStatus:
      msg = "rejected: lamps not active (cannot open shutter)"
      self.terminalOutputTextBrowser.append("<p style='color: orange'>[ZMQ] %s</p>" % msg)
      return {"status": "ERROR", "message": msg}
    # Send the specific command for the target state -- don't rely on cached state for toggle direction
    if state:
      response = self._sendCommand(b'>r1\n')
      if response is not None:
        with self._stateLock: self.shutterStatus = 1
        self.terminalOutputTextBrowser.append("<p style='color: green'>"+response.strip('\r\n')+"</p>")
    else:
      response = self._sendCommand(b'>r0\n')
      if response is not None:
        with self._stateLock: self.shutterStatus = 0
        self.terminalOutputTextBrowser.append("<p style='color: green'>"+response.strip('\r\n')+"</p>")
    self.updateAllStatusIndicators()

  def _remoteSetLamps(self, state):
    """Set lamps to target state (not a toggle). 1=activate, 0=standby."""
    # Send the specific command for the target state
    if state:
      response = self._sendCommand(b'>a\n')
      if response is not None:
        with self._stateLock: self.activeStatus = 1
        self.terminalOutputTextBrowser.append("<p style='color: green'>"+response.strip('\r\n')+"</p>")
    else:
      response = self._sendCommand(b'>s\n')
      if response is not None:
        with self._stateLock: self.activeStatus = 0; self.shutterStatus = 0; self.qSwitchStatus = 0
        self.terminalOutputTextBrowser.append("<p style='color: green'>"+response.strip('\r\n')+"</p>")
    self.updateAllStatusIndicators()

  def _remoteSetQSwitch(self, state):
    """Set Q-switch to target state (not a toggle). 1=arm, 0=disarm."""
    if state and (not self.activeStatus or not self.shutterStatus):
      msg = "rejected: requires lamps active + shutter open"
      self.terminalOutputTextBrowser.append("<p style='color: orange'>[ZMQ] %s</p>" % msg)
      return {"status": "ERROR", "message": msg}
    # Send the specific command for the target state
    if state:
      if self.dangerMode:
        response = self._sendCommand(b'>pq\n')
        if response is not None:
          with self._stateLock: self.qSwitchStatus = 1
          self.terminalOutputTextBrowser.append("<p style='color: green'>"+response.strip('\r\n')+"</p>")
    else:
      response = self._sendCommand(b'>sq\n')
      if response is not None:
        with self._stateLock: self.qSwitchStatus = 0
        self.terminalOutputTextBrowser.append("<p style='color: green'>"+response.strip('\r\n')+"</p>")
    self.updateAllStatusIndicators()

  def _remoteSetLampMode(self, mode):
    """Set lamp mode: 0=internal, 1=external. Requires standby."""
    if self.activeStatus:
      msg = "rejected: laser active (must be in standby)"
      self.terminalOutputTextBrowser.append(
          "<p style='color: orange'>[ZMQ] lamp_mode=%d %s</p>" % (mode, msg))
      return {"status": "ERROR", "message": msg}
    if mode == 0: return self.setFlashLampInternal()
    elif mode == 1: return self.setFlashLampExternal()
    else:
      msg = "rejected: invalid lamp_mode %d (expected 0 or 1)" % mode
      print(msg)
      return {"status": "ERROR", "message": msg}

  def _remoteSetQSwitchMode(self, mode):
    """Set Q-switch mode: 0=internal, 1=burst, 2=external. Requires standby."""
    if self.activeStatus:
      msg = "rejected: laser active (must be in standby)"
      self.terminalOutputTextBrowser.append(
          "<p style='color: orange'>[ZMQ] qswitch_mode=%d %s</p>" % (mode, msg))
      return {"status": "ERROR", "message": msg}
    if mode == 0: self.setQSwitchInternal()
    elif mode == 1: self.setQSwitchBurst()
    elif mode == 2: self.setQSwitchExternal()
    else:
      msg = "rejected: invalid qswitch_mode %d (expected 0/1/2)" % mode
      print(msg)
      return {"status": "ERROR", "message": msg}

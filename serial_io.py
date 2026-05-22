"""Serial I/O gateway + disconnect/reconnect lifecycle mixin for SingleLaserController.

Extracted from BigSkyControllerAmbitious.py per T0.6 audit. Owns:

  * `_sendCommand` — the SOLE sanctioned routing for production serial writes
    (the other two raw-`self.ser` callers are `_attemptReconnect` and
    `safeExit`, both in this mixin).
  * `_handleDisconnect` / `_attemptReconnect` / `_handleReconnect` —
    disconnect-detection state machine + 5s reconnect timer.
  * `isConnected` — thread-safe connection-status read.
  * `safeExit` — final-shutdown best-effort `>s\\n` write.

Mixin contract: the host class must initialize these attributes BEFORE any
SerialIOMixin method runs (typically in `SingleLaserController.__init__`):

    serialConnected:  bool             — connection status flag
    ser:              serial.Serial    — port handle (live or closed)
    comPort:          str              — port name for reconnect attempts
    labelString:      str              — for GUI label restoration
    _consecutiveErrors: int            — soft-error counter (0–2)
    _stateLock:       threading.RLock  — protects cached hardware state
    activeStatus, shutterStatus, qSwitchStatus, flashLampMode,
    qSwitchMode, warmupActive, keepWarmActive, _warmupTriggered,
    lastTemperature, _blacsConnected — cached hardware state

The mixin also uses these widgets and methods (provided by the host):

    label, terminalOutputTextBrowser, keepWarmCheckBox,
    overallStatusLabel — Qt widgets
    _setLabelColor, updateAllStatusIndicators,
    update_fLampVoltage / updateFreq / update_fLampMode /
    update_qSwitchMode / update_fLampEnergy / updateTemp —
    state-restore methods called after a successful reconnect
    connectionStatusChanged — pyqtSignal(bool) emitted on disconnect/reconnect
    _reconnectTimer, tempPollTimer — QTimers managed by host

These cross-mixin coupling points are documented but NOT enforced by an ABC
— see `docs/bigsky-mixin-architecture.md` for the full coupling table.
"""
from __future__ import annotations

import serial


class SerialIOMixin:
    """Serial gateway + reconnect lifecycle. See module docstring for contract."""

    def _sendCommand(self, cmd_bytes):
        """Send a command to the laser and return the response string, or None on failure.

        All serial I/O must route through this method. On repeated failures,
        calls _handleDisconnect(). Callers must check for None return.
        """
        if not self.serialConnected:
            return None
        try:
            self.ser.flush()
            self.ser.write(cmd_bytes if isinstance(cmd_bytes, bytes) else bytes(cmd_bytes, "utf-8"))
            response = self.ser.read(140).decode('utf-8')
            if not response.strip():
                self._consecutiveErrors += 1
                if self._consecutiveErrors >= 3:
                    self._handleDisconnect("3 consecutive empty responses")
                return None
            self._consecutiveErrors = 0
            return response
        except (serial.SerialException, OSError) as e:
            self._handleDisconnect("serial error: %s" % e)
            return None
        except UnicodeDecodeError as e:
            self._consecutiveErrors += 1
            if self._consecutiveErrors >= 3:
                self._handleDisconnect("3 consecutive decode errors: %s" % e)
            return None

    def _handleDisconnect(self, reason=""):
        """Handle serial disconnection: reset state, update GUI, start reconnect timer."""
        if not self.serialConnected:
            return  # already disconnected
        self.serialConnected = False
        self._consecutiveErrors = 0
        self._blacsConnected = False

        # Reset all cached hardware state
        with self._stateLock:
            self.activeStatus = 0
            self.shutterStatus = 0
            self.qSwitchStatus = 0
            self.flashLampMode = 0
            self.qSwitchMode = 0
        self.warmupActive = False
        if self.keepWarmActive:
            self.keepWarmActive = False
            self._warmupTriggered = False
            self.keepWarmCheckBox.blockSignals(True)
            self.keepWarmCheckBox.setChecked(False)
            self.keepWarmCheckBox.blockSignals(False)

        # Close the dead serial port
        try:
            self.ser.close()
        except Exception:
            pass

        # Update GUI
        self.label.setText("DISCONNECTED — " + self.labelString)
        self.terminalOutputTextBrowser.append(
            "<p style='color: red'>Serial disconnected: %s</p>" % reason)
        self.updateAllStatusIndicators()
        self.overallStatusLabel.setText("OVERALL: DISCONNECTED")
        self._setLabelColor(self.overallStatusLabel, bg="#8B0000", fg="white")

        # Start reconnect attempts
        self._reconnectTimer.start(5000)

        # Notify hub/ZMQ
        self.connectionStatusChanged.emit(False)
        print("Serial disconnected: %s" % reason)

    def _attemptReconnect(self):
        """Try to re-establish serial connection. Called by _reconnectTimer every 5s."""
        try:
            self.ser = serial.Serial(self.comPort, 9600, timeout=1)
            self.ser.flush()
            self.ser.write(b'>cg\n')
            response = self.ser.read(140).decode('utf-8')
            temp = float(response.strip('\r\ntemp.CG d'))
            self._handleReconnect(temp)
        except Exception:
            # Still disconnected — timer will retry
            try:
                self.ser.close()
            except Exception:
                pass

    def _handleReconnect(self, initial_temp):
        """Restore state after successful reconnection."""
        self._reconnectTimer.stop()
        self.serialConnected = True
        self._consecutiveErrors = 0

        with self._stateLock:
            self.lastTemperature = initial_temp

        self.terminalOutputTextBrowser.append(
            "<p style='color: green'>Serial reconnected! Re-querying laser state...</p>")

        # Re-query all laser state
        self.update_fLampVoltage()
        self.updateFreq()
        self.update_fLampMode()
        self.update_qSwitchMode()
        self.update_fLampEnergy()
        self.updateTemp()

        # Restore GUI
        self.label.setText(self.labelString)
        self.updateAllStatusIndicators()

        self.connectionStatusChanged.emit(True)
        print("Serial reconnected to %s" % self.comPort)

    def isConnected(self):
        """Return serial connection status. Thread-safe (GIL-atomic bool read)."""
        return self.serialConnected

    def safeExit(self):
        """Final shutdown: stop timers, send standby, close serial port. Best-effort."""
        self.tempPollTimer.stop()
        self._reconnectTimer.stop()
        print(">s")
        if self.serialConnected:
            try:
                self.ser.flush()
                self.ser.write(b'>s\n')
                response = self.ser.read(140).decode('utf-8')
                print("response:", response)
                self.ser.close()
            except Exception:
                pass

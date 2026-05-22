"""Compound-sequence mixin for SingleLaserController.

Extracted from BigSkyControllerAmbitious.py per T0.6 audit (step 3 of 4 in
the mixin extraction plan; see docs/bigsky-mixin-extraction-plan.md). Owns
the multi-step compound operations that drive the laser through
state-machine transitions:

  * `startLaser` -- standby -> external lamp mode -> activate -> open
    shutter -> arm Q-switch. Bail out at any step if the serial
    disconnects or a mode-switch verify fails. Hardware-safety invariant:
    mode switches MUST precede activate (B7 test invariant).
  * `stopLaser` -- single `>s\\n` (standby), clearing all status flags.
  * `startWarmup` -- standby -> internal lamp mode -> activate. Lamps
    fire continuously with shutter closed.
  * `toggleKeepWarm` -- enable/disable the auto-warmup-when-cold loop.
  * `pollTemperature` -- QTimer callback that polls temperature, fires
    deferred energy readback after a voltage change, exits warmup at
    operating temp, and ticks _evaluateKeepWarm.
  * `_evaluateKeepWarm` -- hysteresis-protected re-entry into warmup
    when coolant temperature drops below TEMP_COLD.

Hysteresis contract: `_warmupTriggered` is the lock that prevents
oscillation around TEMP_COLD. Set to True when warmup is triggered;
reset only when temp >= TEMP_OPERATING. Matches the BLACS tab's
`_evaluate_keep_warm` so the two sides agree on when re-warmup fires.

Mixin contract: the host class must provide these attributes and methods
BEFORE any CompoundSequencesMixin method runs:

  * State (initialized in __init__):
      serialConnected:        bool
      _stateLock:             threading.RLock
      activeStatus, shutterStatus, qSwitchStatus, flashLampMode:
                              cached hardware state (int)
      warmupActive:           bool
      keepWarmActive:         bool
      _warmupTriggered:       bool (hysteresis lock)
      _energyReadbackPending: bool (latched after voltage change)
      lastTemperature:        float
      TEMP_COLD, TEMP_OPERATING: float class constants
      _blacsConnected:        bool (read-only; auto-defer-to-BLACS window)
      _lastBlacsContact:      float (epoch seconds)

  * Methods (from SerialIOMixin / future LaserCommandsMixin):
      _sendCommand                            -- from SerialIOMixin
      setFlashLampInternal, setFlashLampExternal  -- (future)
                                                LaserCommandsMixin
      updateTemp, update_fLampEnergy,
      updateAllStatusIndicators               -- (future)
                                                LaserCommandsMixin

  * Qt widgets (loaded from .ui via uic.loadUiType):
      terminalOutputTextBrowser

17/17 B1-B7 test suite is the regression net (B7 explicitly pins
startWarmup / startLaser byte sequences + abort-on-mode-mismatch).
"""
from __future__ import annotations

import time


class CompoundSequencesMixin:
  """Multi-step laser state-machine transitions (warmup, arm, stop, keep-warm)."""

  def startLaser(self):
    """Arm for external-trigger lasing: ensure external modes, then activate all."""
    if not self.serialConnected:
      self.terminalOutputTextBrowser.append("<p style='color: red'>Cannot arm: no serial connection</p>")
      return
    #Go to standby (required for mode switches — always send, don't trust cache)
    response = self._sendCommand(b'>s\n')
    if response is None: return
    with self._stateLock: self.activeStatus = 0; self.shutterStatus = 0; self.qSwitchStatus = 0
    self.terminalOutputTextBrowser.append('>s (standby for mode switch)')
    #Set external lamp mode (always send — don't trust cache)
    self.setFlashLampExternal()
    if not self.serialConnected: return
    if self.flashLampMode != 1:
      self.terminalOutputTextBrowser.append(
          "<p style='color: red'>Arm aborted: lamp mode is %d, not external (1)</p>" % self.flashLampMode)
      return
    #Activate lamps
    response = self._sendCommand(b'>a\n')
    if response is None: return
    with self._stateLock: self.activeStatus = 1
    self.terminalOutputTextBrowser.append('>a')
    self.terminalOutputTextBrowser.append("<p style='color: green'>"+response.strip('\r\n')+"</p>")
    #Open shutter
    response = self._sendCommand(b'>r1\n')
    if response is None: return
    with self._stateLock: self.shutterStatus = 1
    self.terminalOutputTextBrowser.append('>r1')
    self.terminalOutputTextBrowser.append("<p style='color: green'>"+response.strip('\r\n')+"</p>")
    #Arm Q-switch
    response = self._sendCommand(b'>pq\n')
    if response is None: return
    with self._stateLock: self.qSwitchStatus = 1
    self.terminalOutputTextBrowser.append('>pq')
    self.terminalOutputTextBrowser.append("<p style='color: green'>"+response.strip('\r\n')+"</p>")
    self.warmupActive = False
    self.updateAllStatusIndicators()
    self.terminalOutputTextBrowser.append(
        "<p style='color: blue'>Armed for external trigger (lamp external, QS internal, shutter open, QS armed).</p>")

  def stopLaser(self): #This does the same thing as toggleActiveStatus if active status == 1. But it's redundant for safety, in case gui and laser get de-synced somehow.
    print(">s")
    self.terminalOutputTextBrowser.append("<p style='color: black'>"+'>s'+"</p>");
    response = self._sendCommand(b'>s\n')
    if response is not None:
      with self._stateLock: self.activeStatus = 0; self.shutterStatus = 0; self.qSwitchStatus = 0
      print("response:", response)
      self.terminalOutputTextBrowser.append("<p style='color: green'>"+response.strip('\r\n')+"</p>");
    self.warmupActive = False
    self.updateAllStatusIndicators()

  def startWarmup(self):
    if not self.serialConnected:
      self.terminalOutputTextBrowser.append("<p style='color: red'>Cannot warmup: no serial connection</p>")
      return
    self.warmupActive = True
    #Go to standby (always send — clears shutter/qswitch, required for mode switch)
    response = self._sendCommand(b'>s\n')
    if response is None: return
    with self._stateLock: self.activeStatus = 0; self.shutterStatus = 0; self.qSwitchStatus = 0
    self.terminalOutputTextBrowser.append('>s (standby for warmup)')
    #Set internal lamp mode (always send — don't trust cache)
    self.setFlashLampInternal()
    if not self.serialConnected: return
    if self.flashLampMode != 0:
      self.terminalOutputTextBrowser.append(
          "<p style='color: red'>Warmup aborted: lamp mode is %d, not internal (0)</p>" % self.flashLampMode)
      return
    #Activate lamps (internal trigger fires immediately)
    response = self._sendCommand(b'>a\n')
    if response is None: return
    with self._stateLock: self.activeStatus = 1
    self.terminalOutputTextBrowser.append('>a')
    self.terminalOutputTextBrowser.append("<p style='color: green'>"+response.strip('\r\n')+"</p>")
    self.updateTemp()
    self.updateAllStatusIndicators()
    self.terminalOutputTextBrowser.append(
        "<p style='color: blue'>Warmup started (internal trigger). Lamps firing, shutter closed.</p>")

  def toggleKeepWarm(self, checked):
    """Toggle the auto-warmup-if-cold behaviour.

    Temperature polling and display are always on while serial is connected;
    this toggle only governs whether the GUI auto-enters warmup when the
    coolant temperature drops below TEMP_COLD (37°C).

    Uses hysteresis to prevent oscillation:
    - Triggers warmup when temp drops below TEMP_COLD (37°C)
    - Resets trigger when temp rises to TEMP_OPERATING (39°C)
    Works standalone (no BLACS needed) and can be synced from BLACS via ZMQ.
    """
    self.keepWarmActive = checked
    if checked:
      self._warmupTriggered = False
      self.terminalOutputTextBrowser.append(
          "<p style='color: blue'>Auto-warmup enabled — will enter warmup if temp &lt; %.0f°C.</p>"
          % self.TEMP_COLD)
      # Check temperature immediately
      self._evaluateKeepWarm()
    else:
      self._warmupTriggered = False
      self.warmupActive = False
      self.terminalOutputTextBrowser.append(
          "<p style='color: blue'>Auto-warmup disabled.</p>")

  def pollTemperature(self):
    if not self.serialConnected:
      return
    self.updateTemp()
    # Latched energy readback after voltage change
    if self._energyReadbackPending:
      self._energyReadbackPending = False
      self.update_fLampEnergy()
    if self.warmupActive and self.lastTemperature >= self.TEMP_OPERATING:
      self.terminalOutputTextBrowser.append(
          "<p style='color: green'>Temperature %.1fC >= %.1fC. Laser is warm enough to lase.</p>"
          % (self.lastTemperature, self.TEMP_OPERATING))
      self.warmupActive = False
    # Auto Keep Warm: check if we need to enter warmup
    self._evaluateKeepWarm()
    self.updateAllStatusIndicators()

  def _evaluateKeepWarm(self):
    """Check if Auto Keep Warm should trigger warmup.

    Uses hysteresis to prevent oscillation:
    - Triggers warmup when temp drops below TEMP_COLD (37°C)
    - Resets trigger when temp rises to TEMP_OPERATING (39°C)
    Matches the BLACS tab's _evaluate_keep_warm hysteresis logic.
    """
    if not self.keepWarmActive:
      return
    if not self.serialConnected:
      return
    # Defer to BLACS when it's actively controlling (auto-expires after 5 min)
    if self._blacsConnected and (time.time() - self._lastBlacsContact) < 300:
      return
    temp = self.lastTemperature
    if temp < self.TEMP_COLD and not self._warmupTriggered:
      self._warmupTriggered = True
      self.terminalOutputTextBrowser.append(
          "<p style='color: blue'>Auto-warmup: cold (%.1f°C), entering warmup.</p>" % temp)
      self.startWarmup()
    elif temp >= self.TEMP_OPERATING:
      if self._warmupTriggered:
        self._warmupTriggered = False

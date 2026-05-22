"""Laser-command + status-indicator mixin for SingleLaserController.

Extracted from BigSkyControllerAmbitious.py per T0.6 audit (step 4 of 4 in
the mixin extraction plan; see docs/bigsky-mixin-extraction-plan.md). This
is the residue mixin — everything not owned by SerialIOMixin (raw I/O),
RemoteBridgeMixin (ZMQ -> Qt), or CompoundSequencesMixin (multi-step
state-machine ops).

Method categories:

  * Frequency control: setFrequency, confirmFrequencySetting, updateFreq.
  * Settings persistence: saveLaserSettings (issues `>sav1` to flash EEPROM).
  * Q-switch mode setters: setQSwitchInternal, setQSwitchBurst,
    setQSwitchExternal (each sends `>qsm{0,1,2}\\n` and updates the radio
    button).
  * Flash-lamp mode with verify-on-readback: _setLampMode is the bug-prone
    canonical setter (T0.6 audit'd verify-mismatch propagation, post-
    `1eb2321`). setFlashLampInternal / setFlashLampExternal are thin
    wrappers.
  * Voltage: setVoltage (capture), confirmVoltageSetting (send + readback).
  * Manual status toggles: toggleActiveStatus, toggleShutterStatus,
    toggleQSwitchStatus, singlePulse.
  * Terminal mode: toggleTerminalInput, fetchSerial, updateTerminalCommand,
    sendTerminalCommand.
  * Pull-from-hardware readback: updateTemp, update_fLampVoltage,
    update_fLampEnergy, update_fLampMode, update_qSwitchMode.
  * Status indicator paint: updateAllStatusIndicators, _setLabelColor,
    _updateTemperatureStatusColor.
  * Thread-safe getters for ZMQ server: getVoltage, getTemperature,
    getActiveStatus, getShutterStatus, getQSwitchStatus, getLampMode,
    getQSwitchMode.

Module-level constant `_TRAILING_INT_RE` was moved here from
BigSkyControllerAmbitious.py — its only consumer is `_setLampMode` in
this mixin.

Mixin contract: the host class must provide a large surface area of UI
widgets (loaded from .ui via uic.loadUiType) and cached state attrs.
All references are documented in
docs/bigsky-mixin-extraction-plan.md. The 17/17 B1-B7 test suite is the
regression net.
"""
from __future__ import annotations

import re
import time

import numpy as np


# Match the trailing integer in a BigSky response (e.g. "LP synch :  1\r\n" -> 1).
# Tolerant of leading/trailing whitespace and varying prefix punctuation so we
# don't have to hard-code each command's exact response prefix.
_TRAILING_INT_RE = re.compile(r'(-?\d+)\s*$')


class LaserCommandsMixin:
  """Setters/getters + status indicators + terminal mode."""

  def setFrequency(self):
    self.proposedFrequency = float(self.frequencyDoubleSpinBox.value())

  def confirmFrequencySetting(self):
    toWrite = ">f{freq}\n".format(freq = str(int(self.proposedFrequency*100)) )
    self.terminalOutputTextBrowser.append(">f{freq}".format(freq = str(int(self.proposedFrequency*100)) ))#this is just a test feature
    response = self._sendCommand(toWrite)
    if response is None: return
    try:
      self.frequency=float(response.strip('\r\nfreq. Hz'))
    except ValueError:
      self.terminalOutputTextBrowser.append("<p style='color: orange'>Frequency parse error</p>"); return
    self.frequencyDoubleSpinBox.setValue(self.frequency); print("self.frequency = {f}Hz".format(f=self.frequency))
    self.terminalOutputTextBrowser.append("<p style='color: green'>"+response.strip('\r\n')+"</p>");
    self.updateTemp()

  def updateFreq(self):
    if not self.serialConnected: return
    self.terminalOutputTextBrowser.append('>f')
    response = self._sendCommand(b'>f\n')
    if response is None: return
    try:
      self.frequency=float(response.strip('\r\nfreq. Hz'))
    except ValueError:
      self.terminalOutputTextBrowser.append("<p style='color: orange'>Frequency parse error</p>"); return
    self.terminalOutputTextBrowser.append("<p style='color: green'>"+response.strip('\r\n')+"</p>");
    print("self.frequency = {f}Hz".format(f=self.frequency))
    self.frequencyDoubleSpinBox.setValue(self.frequency)

  def saveLaserSettings(self):
    self.terminalOutputTextBrowser.append('>sav1')
    response = self._sendCommand(b'>sav1\n')
    if response is not None:
      self.terminalOutputTextBrowser.append("<p style='color: green'>"+response.strip('\r\n')+"</p>");
    print("Laser settings saved")

  '''NOTE: These can only be changed while laser is in standby (>s). The GUI should now reproduce this behavior'''
  def setQSwitchInternal(self):
    print(">qsm0")
    response = self._sendCommand(b'>qsm0\n')
    if response is not None:
      self.qSwitchMode = 0; self.qSwitchRadioButton_0.setChecked(True)
      print("response:", response)
      self.terminalOutputTextBrowser.append('>qsm0'); self.terminalOutputTextBrowser.append("<p style='color: green'>"+response.strip('\r\n')+"</p>")
  def setQSwitchBurst(self):
    print(">qsm1")
    response = self._sendCommand(b'>qsm1\n')
    if response is not None:
      self.qSwitchMode = 1; self.qSwitchRadioButton_1.setChecked(True)
      print("response:", response)
      self.terminalOutputTextBrowser.append('>qsm1'); self.terminalOutputTextBrowser.append("<p style='color: green'>"+response.strip('\r\n')+"</p>");
  def setQSwitchExternal(self):
    print(">qsm2")
    response = self._sendCommand(b'>qsm2\n')
    if response is not None:
      self.qSwitchMode = 2; self.qSwitchRadioButton_2.setChecked(True)
      print("response:", response)
      self.terminalOutputTextBrowser.append('>qsm2'); self.terminalOutputTextBrowser.append("<p style='color: green'>"+response.strip('\r\n')+"</p>");
  def _setLampMode(self, target, cmd_label):
    """Send >lpm{target} and verify the controller's reported mode.

    On success (actual == target): returns SUCCESS, caches the actual value,
    updates radio buttons + frequency interlock.
    On serial failure: returns ERROR with 'rejected: serial failure' so the
    ZMQ caller (BLACS) doesn't update its cache to the requested value.
    On parse failure: returns ERROR with 'rejected: could not parse'; cache
    unchanged.
    On verify mismatch (actual != target): returns ERROR with 'rejected: ...
    did not take effect'. The cached flashLampMode is set to the *actual*
    reported value (so callers like ``startLaser`` see reality).
    """
    cmd_bytes = ('>%s\n' % cmd_label).encode('ascii')
    print(cmd_label)
    response = self._sendCommand(cmd_bytes)
    if response is None:
      msg = "rejected: serial failure on %s" % cmd_label
      return {"status": "ERROR", "message": msg}
    m = _TRAILING_INT_RE.search(response.strip())
    if m is None:
      msg = "rejected: could not parse %s response (%r)" % (cmd_label, response)
      self.terminalOutputTextBrowser.append(
          "<p style='color: red'>%s — flashLampMode unchanged</p>" % msg)
      return {"status": "ERROR", "message": msg}
    actual = int(m.group(1))
    with self._stateLock: self.flashLampMode = actual
    if actual == 0:
      self.flashLampRadioButton_0.setChecked(True)
      self.frequencyDoubleSpinBox.setEnabled(True); self.frequencyConfirmationButton.setEnabled(True)
    elif actual == 1:
      self.flashLampRadioButton_1.setChecked(True)
      self.frequencyDoubleSpinBox.setEnabled(False); self.frequencyConfirmationButton.setEnabled(False)
    else:
      self.terminalOutputTextBrowser.append(
          "<p style='color: orange'>Unexpected lamp mode reported: %d</p>" % actual)
    print("response:", response)
    self.terminalOutputTextBrowser.append(cmd_label)
    self.terminalOutputTextBrowser.append("<p style='color: green'>"+response.strip('\r\n')+"</p>")
    if actual != target:
      msg = "rejected: %s did not take effect (got %d)" % (cmd_label, actual)
      self.terminalOutputTextBrowser.append(
          "<p style='color: orange'>Warning: %s</p>" % msg)
      return {"status": "ERROR", "message": msg}
    return {"status": "SUCCESS"}

  def setFlashLampInternal(self):
    return self._setLampMode(0, '>lpm0')

  def setFlashLampExternal(self):
    return self._setLampMode(1, '>lpm1')

  def setVoltage(self):
    self.proposedVoltage = int(self.flashLampVoltageSpinBox.value())

  def confirmVoltageSetting(self):
    realUpdate=False
    try:
      self.proposedVoltage = int(self.flashLampVoltageSpinBox.value())
      if self.proposedVoltage<500 or self.proposedVoltage>1400:
        print("please enter an integer between 500 and 1400"); self.proposedVoltage = self.fLampVoltage
      else: realUpdate=True
    except: print("please enter an integer value."); self.proposedVoltage = self.fLampVoltage
    if realUpdate:
      toWrite = ">vmo{vol}\n".format( vol = str(0)+str(int(self.proposedVoltage)) if self.proposedVoltage<1000 else str(int(self.proposedVoltage)) )
      self.terminalOutputTextBrowser.append(toWrite.strip('\n'))
      response = self._sendCommand(toWrite)
      if response is not None:
        try:
          with self._stateLock: self.fLampVoltage=int(response.strip('\r\nvoltage m V'))
        except ValueError:
          self.terminalOutputTextBrowser.append("<p style='color: orange'>Voltage parse error</p>"); return
        print("voltage = {V}V".format(V=self.fLampVoltage))
        self.terminalOutputTextBrowser.append("<p style='color: green'>"+response.strip('\r\n')+"</p>");
        self.flashLampVoltageSpinBox.setValue(self.fLampVoltage)
        self._energyReadbackPending = True  # deferred to next temp poll
        self.PowerEstimateValue.setText('%.2f'%np.interp(self.fLampVoltage,self.calibVolts,self.calibPower) + " W")
      # On timeout (None): leave cache unchanged — don't assume command succeeded
    else:
      self.flashLampVoltageSpinBox.setValue(self.fLampVoltage)

  def toggleActiveStatus(self):
    if not self.activeStatus:
      # Activating
      print(">a")
      self.terminalOutputTextBrowser.append("<p style='color: black'>"+'>a'+"</p>");
      response = self._sendCommand(b'>a\n')
      if response is not None:
        with self._stateLock: self.activeStatus = 1
        print("response:", response)
        self.terminalOutputTextBrowser.append("<p style='color: green'>"+response.strip('\r\n')+"</p>")
    else:
      # Standby
      print(">s")
      self.terminalOutputTextBrowser.append("<p style='color: black'>"+'>s'+"</p>");
      response = self._sendCommand(b'>s\n')
      if response is not None:
        with self._stateLock: self.activeStatus = 0; self.shutterStatus = 0; self.qSwitchStatus = 0
        print("response:", response)
        self.terminalOutputTextBrowser.append("<p style='color: green'>"+response.strip('\r\n')+"</p>")
    self.updateAllStatusIndicators()

  def toggleShutterStatus(self):
    if not self.shutterStatus:
      # Opening shutter
      print(">r1")
      self.terminalOutputTextBrowser.append("<p style='color: black'>"+'>r1'+"</p>");
      response = self._sendCommand(b'>r1\n')
      if response is not None:
        with self._stateLock: self.shutterStatus = 1
        print("response:", response)
        self.terminalOutputTextBrowser.append("<p style='color: green'>"+response.strip('\r\n')+"</p>")
    else:
      # Closing shutter
      print(">r0")
      self.terminalOutputTextBrowser.append("<p style='color: black'>"+'>r0'+"</p>");
      response = self._sendCommand(b'>r0\n')
      if response is not None:
        with self._stateLock: self.shutterStatus = 0
        print("response:", response)
        self.terminalOutputTextBrowser.append("<p style='color: green'>"+response.strip('\r\n')+"</p>")
    self.updateAllStatusIndicators()

  def toggleQSwitchStatus(self):
    if self.qSwitchStatus:
      # Disarming Q-switch
      print(">sq"); self.terminalOutputTextBrowser.append("<p style='color: black'>"+'>sq'+"</p>");
      response = self._sendCommand(b'>sq\n')
      if response is not None:
        with self._stateLock: self.qSwitchStatus = 0
        print("response:", response)
        self.terminalOutputTextBrowser.append("<p style='color: green'>"+response.strip('\r\n')+"</p>")
    else:
      # Arming Q-switch
      print(">pq"); self.terminalOutputTextBrowser.append("<p style='color: black'>"+'>pq'+"</p>");
      if self.dangerMode:
        response = self._sendCommand(b'>pq\n')
        if response is not None:
          with self._stateLock: self.qSwitchStatus = 1
          print("response:", response)
          self.terminalOutputTextBrowser.append("<p style='color: green'>"+response.strip('\r\n')+"</p>")
    self.updateAllStatusIndicators()

  def singlePulse(self):
    print(">oq"); self.terminalOutputTextBrowser.append("<p style='color: black'>"+'>oq'+"</p>");
    if self.dangerMode:
      response = self._sendCommand(b'>oq\n')
      if response is not None:
        print("response:", response)
        self.terminalOutputTextBrowser.append("<p style='color: green'>"+response.strip('\r\n')+"</p>")

  def toggleTerminalInput(self):
    if self.terminalEnabled:
      self.terminalEnabled=False;
      self.stopLaser()
      self.update_fLampMode()
      self.update_qSwitchMode()
      self.update_fLampVoltage()
      self.update_fLampEnergy()
      self.updateTemp()
      self.updateFreq()
    else:
      self.terminalEnabled=True;
    self.terminalInputLabel.setEnabled(self.terminalEnabled); self.terminalInputLineEdit.setEnabled(self.terminalEnabled)
    self.qSwitchRadioButton_0.setEnabled(not(self.terminalEnabled)); self.qSwitchRadioButton_1.setEnabled(not(self.terminalEnabled)); self.qSwitchRadioButton_2.setEnabled(not(self.terminalEnabled))
    self.flashLampRadioButton_0.setEnabled(not(self.terminalEnabled)); self.flashLampRadioButton_1.setEnabled(not(self.terminalEnabled))
    frequencyBoolean = not(self.terminalEnabled) and not(self.flashLampMode)
    self.frequencyDoubleSpinBox.setEnabled(frequencyBoolean); self.FrequencyLabel.setEnabled(frequencyBoolean); self.frequencyConfirmationButton.setEnabled(frequencyBoolean)
    self.flashLampVoltageLabel.setEnabled(not(self.terminalEnabled))
    self.flashLampVoltageSpinBox.setEnabled(not(self.terminalEnabled))
    self.voltageConfirmationButton.setEnabled(not(self.terminalEnabled))
    #New controls
    self.lampToggleButton.setEnabled(not(self.terminalEnabled))
    self.shutterToggleButton.setEnabled(not(self.terminalEnabled))
    self.qSwitchToggleButton.setEnabled(not(self.terminalEnabled))
    self.singlePulseButton.setEnabled(not(self.terminalEnabled))
    self.warmupButton.setEnabled(not(self.terminalEnabled))
    self.startLasingButton.setEnabled(not(self.terminalEnabled))
    self.fullStopButton.setEnabled(not(self.terminalEnabled))
    self.keepWarmCheckBox.setEnabled(not(self.terminalEnabled))

  def fetchSerial(self):
    print(">sn"); self.terminalOutputTextBrowser.append("<p style='color: black'>"+'>sn'+"</p>");
    if self.serialConnected and self.dangerMode:
      response = self._sendCommand(b'>sn\n')
      if response is not None:
        print("response:", response)
        self.terminalOutputTextBrowser.append("<p style='color: green'>"+response.strip('\r\n')+"</p>")
        sn = response.strip(' \r\ns/number')
      else: sn=''
    else: sn=''
    self.serialNumber=sn

  def updateTerminalCommand(self,text):
    self.terminalLineCurrently = text

  def sendTerminalCommand(self):
    toWrite = '>'+self.terminalLineCurrently+'\n'
    print("sending to terminal:",toWrite) #TODO: finish this function
    self.terminalOutputTextBrowser.append("<p style='color: blue'>"+toWrite.strip('\n')+"</p>");
    response = self._sendCommand(toWrite)
    if response is not None:
      self.terminalOutputTextBrowser.append("<p style='color: green'>"+response.strip('\r\n')+"</p>");
    self.terminalLineCurrently = ''
    self.terminalInputLineEdit.setText(self.terminalLineCurrently)

  def updateTemp(self):
    if not self.serialConnected:
      return
    self.terminalOutputTextBrowser.append('>cg')
    response = self._sendCommand(b'>cg\n')
    if response is None: return
    try:
      temp = float(response.strip('\r\ntemp.CG d'))
    except ValueError as e:
      self.terminalOutputTextBrowser.append(
          "<p style='color: orange'>Temperature parse error: %s</p>" % e)
      return
    with self._stateLock: self.lastTemperature = temp
    print("temperature = {T}C".format(T=temp))
    self.terminalOutputTextBrowser.append("<p style='color: green'>"+response.strip('\r\n')+"</p>")
    tiempo = time.strftime("%d %b %Y %H:%M:%S", time.localtime())
    print("time = {t}".format(t=tiempo))
    self.temperatureOutput.setText(str(temp)+" C")
    self.lastUpdateOutput.setText(str(tiempo))
    self._updateTemperatureStatusColor()

  def update_fLampVoltage(self):
    if not self.serialConnected: return
    self.terminalOutputTextBrowser.append('>v')
    response = self._sendCommand(b'>v\n')
    if response is None: return
    try:
      with self._stateLock: self.fLampVoltage=int(response.strip('\r\nvoltage V'))
    except ValueError:
      self.terminalOutputTextBrowser.append("<p style='color: orange'>Voltage parse error</p>"); return
    print("voltage = {V}V".format(V=self.fLampVoltage))
    self.terminalOutputTextBrowser.append("<p style='color: green'>"+response.strip('\r\n')+"</p>");
    #self.flashLampVoltageHorizontalSlider.setValue(self.fLampVoltage)
    self.flashLampVoltageSpinBox.setValue(self.fLampVoltage)

    self.PowerEstimateValue.setText('%.2f'%np.interp(self.fLampVoltage,self.calibVolts,self.calibPower) + " W")

  def update_fLampEnergy(self):
    if not self.serialConnected: return
    self.terminalOutputTextBrowser.append('>ene')
    response = self._sendCommand(b'>ene\n')
    if response is None: return
    try:
      self.fLampEnergy=float(response.strip('\r\nenergy J'))
    except ValueError:
      self.terminalOutputTextBrowser.append("<p style='color: orange'>Energy parse error</p>"); return
    print("energy = {E}J".format(E=self.fLampEnergy))
    self.terminalOutputTextBrowser.append("<p style='color: green'>"+response.strip('\r\n')+"</p>");
    #self.flashLampEnergyHorizontalSlider.setValue(int(10*self.fLampEnergy))
    self.flashLampEnergyValue.setText(str(self.fLampEnergy)+" J")

  def update_fLampMode(self):
    if not self.serialConnected: return
    self.terminalOutputTextBrowser.append('>lpm')
    response = self._sendCommand(b'>lpm\n')
    if response is None: return
    try:
      self.flashLampMode=int(response.strip('\r\nLP synch :  '))
    except ValueError:
      self.terminalOutputTextBrowser.append("<p style='color: orange'>Lamp mode parse error</p>"); return
    print("self.flashLampMode = {f}".format(f=self.flashLampMode))
    self.terminalOutputTextBrowser.append("<p style='color: green'>"+response.strip('\r\n')+"</p>");
    if self.flashLampMode==0: self.flashLampRadioButton_0.setChecked(True)
    elif self.flashLampMode==1: self.flashLampRadioButton_1.setChecked(True)
    else:
      print("ERROR. self.flashLampMode makes no sense")
      self._sendCommand(b'>s\n')

  def update_qSwitchMode(self):
    if not self.serialConnected: return
    self.terminalOutputTextBrowser.append('>qsm')
    response = self._sendCommand(b'>qsm\n')
    if response is None: return
    try:
      self.qSwitchMode=int(response.strip('\r\nQS mode :  '))
    except ValueError:
      self.terminalOutputTextBrowser.append("<p style='color: orange'>QS mode parse error</p>"); return
    print("self.qSwitchMode = {q}".format(q=self.qSwitchMode))
    self.terminalOutputTextBrowser.append("<p style='color: green'>"+response.strip('\r\n')+"</p>");
    if self.qSwitchMode==0: self.qSwitchRadioButton_0.setChecked(True)
    elif self.qSwitchMode==1: self.qSwitchRadioButton_1.setChecked(True)
    elif self.qSwitchMode==2: self.qSwitchRadioButton_2.setChecked(True)
    else:
      print("ERROR. self.qSwitchMode makes no sense")
      self._sendCommand(b'>s\n')

  def updateAllStatusIndicators(self):
    #Lamp status
    if self.activeStatus:
      self.lampStatusLabel.setText("LAMPS: FIRING")
      self._setLabelColor(self.lampStatusLabel, bg="#90EE90", fg="black")
      self.lampToggleButton.setText("DEACTIVATE LAMPS")
    else:
      self.lampStatusLabel.setText("LAMPS: STANDBY")
      self._setLabelColor(self.lampStatusLabel, bg="#D3D3D3", fg="black")
      self.lampToggleButton.setText("ACTIVATE LAMPS")

    #Shutter status
    if self.shutterStatus:
      self.shutterStatusLabel.setText("SHUTTER: OPEN")
      self._setLabelColor(self.shutterStatusLabel, bg="#FFA500", fg="black")
      self.shutterToggleButton.setText("CLOSE SHUTTER")
    else:
      self.shutterStatusLabel.setText("SHUTTER: CLOSED")
      self._setLabelColor(self.shutterStatusLabel, bg="#D3D3D3", fg="black")
      self.shutterToggleButton.setText("OPEN SHUTTER")

    #Q-Switch status
    if self.qSwitchStatus:
      self.qSwitchStatusLabel.setText("Q-SWITCH: ARMED")
      self._setLabelColor(self.qSwitchStatusLabel, bg="#FF6347", fg="white")
      self.qSwitchToggleButton.setText("DISARM Q-SWITCH")
    else:
      self.qSwitchStatusLabel.setText("Q-SWITCH: DISABLED")
      self._setLabelColor(self.qSwitchStatusLabel, bg="#D3D3D3", fg="black")
      self.qSwitchToggleButton.setText("ARM Q-SWITCH")

    #Overall status
    if not self.serialConnected:
      self.overallStatusLabel.setText("OVERALL: DISCONNECTED")
      self._setLabelColor(self.overallStatusLabel, bg="#8B0000", fg="white")
    elif self.activeStatus and self.shutterStatus and self.qSwitchStatus:
      self.overallStatusLabel.setText("OVERALL: LASING")
      self._setLabelColor(self.overallStatusLabel, bg="#FF0000", fg="white")
    elif self.activeStatus and not self.shutterStatus:
      self.overallStatusLabel.setText("OVERALL: WARMING UP")
      self._setLabelColor(self.overallStatusLabel, bg="#FFD700", fg="black")
    elif self.activeStatus:
      self.overallStatusLabel.setText("OVERALL: LAMPS ACTIVE")
      self._setLabelColor(self.overallStatusLabel, bg="#90EE90", fg="black")
    else:
      self.overallStatusLabel.setText("OVERALL: STANDBY")
      self._setLabelColor(self.overallStatusLabel, bg="#D3D3D3", fg="black")

    #Temperature status
    self._updateTemperatureStatusColor()

    #Button enable/disable logic
    self.shutterToggleButton.setEnabled(self.activeStatus)
    self.qSwitchToggleButton.setEnabled(self.activeStatus and self.shutterStatus)
    self.singlePulseButton.setEnabled(
        self.activeStatus and self.shutterStatus
        and not self.qSwitchStatus and (self.qSwitchMode == 0))

    #Mode radio buttons only changeable in standby
    for rb in [self.qSwitchRadioButton_0, self.qSwitchRadioButton_1,
               self.qSwitchRadioButton_2, self.flashLampRadioButton_0,
               self.flashLampRadioButton_1]:
        rb.setEnabled(not self.activeStatus)

    tiempo = time.strftime("%d %b %Y %H:%M:%S", time.localtime())
    self.lastUpdateOutput.setText(str(tiempo))

  def _setLabelColor(self, label, bg, fg):
    label.setStyleSheet(
        "background-color: %s; color: %s; padding: 4px; border: 1px solid gray;" % (bg, fg))

  def _updateTemperatureStatusColor(self):
    temp = self.lastTemperature
    if temp < self.TEMP_COLD:
      self.temperatureStatusLabel.setText("TEMP: %.1f C (COLD)" % temp)
      self._setLabelColor(self.temperatureStatusLabel, bg="#87CEEB", fg="black")
    elif temp < self.TEMP_OPERATING:
      self.temperatureStatusLabel.setText("TEMP: %.1f C (WARMING)" % temp)
      self._setLabelColor(self.temperatureStatusLabel, bg="#FFD700", fg="black")
    else:
      self.temperatureStatusLabel.setText("TEMP: %.1f C (OK)" % temp)
      self._setLabelColor(self.temperatureStatusLabel, bg="#90EE90", fg="black")

  # --- Thread-safe accessors for ZMQ server ---

  def getVoltage(self):
    """Return cached flashlamp voltage (int, volts). Thread-safe read."""
    with self._stateLock: return self.fLampVoltage

  def getTemperature(self):
    """Return cached temperature (float, deg C). Thread-safe read."""
    with self._stateLock: return self.lastTemperature

  def getActiveStatus(self):
    """Return lamp active status (0 or 1). Thread-safe read."""
    with self._stateLock: return self.activeStatus

  def getShutterStatus(self):
    """Return shutter status (0=closed, 1=open). Thread-safe read."""
    with self._stateLock: return self.shutterStatus

  def getQSwitchStatus(self):
    """Return Q-switch status (0=disarmed, 1=armed). Thread-safe read."""
    with self._stateLock: return self.qSwitchStatus

  def getLampMode(self):
    """Return lamp trigger mode (0=internal, 1=external). Thread-safe read."""
    with self._stateLock: return self.flashLampMode

  def getQSwitchMode(self):
    """Return Q-switch mode (0=internal, 1=burst, 2=external). Thread-safe read."""
    with self._stateLock: return self.qSwitchMode

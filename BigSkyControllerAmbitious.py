#written by Alex Brinson (brinson@mit.edu, alexjbrinson@gmail.com) on behalf of EMA Lab
import sys
import re
from PyQt5 import QtCore, QtGui, QtWidgets, uic
from PyQt5.QtCore import QTimer, pyqtSignal, pyqtSlot
import serial
import time
import numpy as np
import os
import concurrent.futures
import threading

# Match the trailing integer in a BigSky response (e.g. "LP synch :  1\r\n" → 1).
# Tolerant of leading/trailing whitespace and varying prefix punctuation so we
# don't have to hard-code each command's exact response prefix.
_TRAILING_INT_RE = re.compile(r'(-?\d+)\s*$')

qtCreatorFile = "GuiBigSkyWidget.ui" # Enter file here.

Ui_Widget, QtBaseClass = uic.loadUiType(qtCreatorFile)

class SingleLaserController(QtWidgets.QWidget, Ui_Widget):
  #Signal for thread-safe remote command execution from ZMQ daemon thread
  _remoteCommandRequested = pyqtSignal(str, object, object)  # (command, value, future)
  connectionStatusChanged = pyqtSignal(bool)  # emitted on disconnect/reconnect
  _blacsHelloReceived = pyqtSignal()  # emitted by ZMQ server on HELLO from BLACS

  def __init__(self, cPort=-1, lString=''):
    super().__init__()
    self.setupUi(self)
    self.comPort = cPort
    self.labelString=lString

    self.calibrationFilePresent=False #TODO: check for calibration file based on laser head serial number

    #Testing different possible serial ports to see if any of them is a Big Sky laser. If ">cg" evokes a temperature readout, we found a live one.
    self.serialConnected = False
    self._consecutiveErrors = 0
    if self.comPort!=-1:
      try:
        self.ser = serial.Serial(self.comPort,9600,timeout=1); self.fetchSerial()
        tiempo = time.strftime("%d %b %Y %H:%M:%S",time.localtime())#
        self.terminalOutputTextBrowser.append('Connection established at '+str(tiempo))
        self.serialConnected=True
        self.dangerMode = True
      except:
        self.terminalOutputTextBrowser.append('Connection failed... Investigate if this ever happens')

    if self.serialConnected==False:
      print("Error: Laser not found. Ensure laser is on and check serial port connection.")
      self.fLampVoltage=-1
      self.serialNumber=''
      #quit()

    #Initializing dummy values. These are updated to true laser settings once all widgets are connected, so they can be updated too.
    self.qSwitchMode = 0; self.flashLampMode = 0
    self.activeStatus = 0; self.shutterStatus = 0; self.qSwitchStatus = 0
    self.terminalEnabled = False
    self.proposedEnergy = 7; self.proposedVoltage = 500; self.proposedFrequency = 0; self.fLampVoltage=0

    #Thread-safe state lock for ZMQ accessor methods
    self._stateLock = threading.Lock()
    self._remoteCommandRequested.connect(self._handleRemoteCommand)

    #Warmup / keep-warm state
    self.warmupActive = False
    self.keepWarmActive = False
    self._warmupTriggered = False  # hysteresis flag for Auto Keep Warm
    self.lastTemperature = 0.0
    self.tempPollTimer = QTimer(self)
    self.tempPollTimer.timeout.connect(self.pollTemperature)
    self.TEMP_COLD = 37.0
    self.TEMP_OPERATING = 39.0

    #Reconnect timer — tries to re-establish serial every 5s after disconnect
    self._reconnectTimer = QTimer(self)
    self._reconnectTimer.timeout.connect(self._attemptReconnect)

    #Latched energy readback — set when voltage changes, consumed by next temp poll
    self._energyReadbackPending = False

    #BLACS connection tracking — suppress GUI-side keep-warm when BLACS is in control
    self._blacsConnected = False
    self._lastBlacsContact = 0
    self._blacsHelloReceived.connect(self._onBlacsHello)

   #Initializing GUI values

   #Checking for self.calibration file in local directory
    try:
      cwd = os.getcwd()
      if self.serialConnected:
        self.calibData=np.loadtxt(cwd+"\\CalibrationFiles\\CalibrationDataBigSky"+str(self.serialNumber)+".csv",dtype="float",comments='#',delimiter=',')
      else:
        self.calibData=np.loadtxt(cwd+"\\CalibrationFiles\\CalibrationDataBigSky.csv",dtype="float",comments='#',delimiter=',')
      self.calibVolts = self.calibData[:,0]; self.calibPower = self.calibData[:,1]
      self.calibrationFilePresent=True
    except:
      defaultCalibVolts=[800,900,950,1000,1050,1080]
      defaultCalibPower=[0.05,1.54,3.09,4.73,6.14,6.78]
    if self.calibrationFilePresent: print("self.calibration file loaded successfully")
    else: print("failed to load self.calibration file"); self.calibVolts=defaultCalibVolts; self.calibPower=defaultCalibPower
    self.PowerEstimateValue.setText('%.2f'%np.interp(self.fLampVoltage,self.calibVolts,self.calibPower)+" W")

    if self.serialConnected:
      self.label.setText(self.labelString)#("BIG SKY " + str(self.comPort) + " LASER CONTROL")
      self.updateTemp()
      self.update_fLampMode()
      self.update_qSwitchMode()
      #self.update_fLampValues()
      self.update_fLampVoltage()
      self.update_fLampEnergy()
      self.lastUpdateOutput.setText(str(tiempo))#
      self.updateFreq()
      # Always-on temperature polling (60s interval, safe no-op if serial disconnects)
      self.tempPollTimer.start(60000)
    else: self.label.setText("Laser not found. This is a dummy GUI\n"+self.labelString)

    self.frequencyDoubleSpinBox.setEnabled(not(self.flashLampMode));
    self.frequencyConfirmationButton.setEnabled(not(self.flashLampMode))
    self.updateAllStatusIndicators()

    #Connecting signals to slots
    self.frequencyDoubleSpinBox.valueChanged.connect(self.setFrequency)
    self.frequencyDoubleSpinBox.editingFinished.connect(self.setFrequency)
    self.qSwitchRadioButton_0.clicked.connect(self.setQSwitchInternal)
    self.qSwitchRadioButton_1.clicked.connect(self.setQSwitchBurst)
    self.qSwitchRadioButton_2.clicked.connect(self.setQSwitchExternal)
    self.flashLampRadioButton_0.clicked.connect(self.setFlashLampInternal)
    self.flashLampRadioButton_1.clicked.connect(self.setFlashLampExternal)
    self.flashLampVoltageSpinBox.valueChanged.connect(self.setVoltage)
    self.voltageConfirmationButton.clicked.connect(self.confirmVoltageSetting)
    self.frequencyConfirmationButton.clicked.connect(self.confirmFrequencySetting)
    self.laserSaveButton.clicked.connect(self.saveLaserSettings)

    #Independent controls
    self.lampToggleButton.clicked.connect(self.toggleActiveStatus)
    self.shutterToggleButton.clicked.connect(self.toggleShutterStatus)
    self.qSwitchToggleButton.clicked.connect(self.toggleQSwitchStatus)
    self.singlePulseButton.clicked.connect(self.singlePulse)

    #Compound controls
    self.warmupButton.clicked.connect(self.startWarmup)
    self.startLasingButton.setText("Arm External")
    self.startLasingButton.clicked.connect(self.startLaser)
    self.fullStopButton.clicked.connect(self.stopLaser)

    #Keep warm — auto-poll temperature and enter warmup if cold
    self.keepWarmCheckBox.toggled.connect(self.toggleKeepWarm)

    self.toggleInputButton.clicked.connect(self.toggleTerminalInput)
    self.terminalInputLineEdit.textChanged.connect(self.updateTerminalCommand)
    self.terminalInputLineEdit.returnPressed.connect(self.sendTerminalCommand)
    self.terminalInputLabel.setEnabled(False); self.terminalInputLineEdit.setEnabled(False)


  # --- Serial I/O gateway ---

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
    self.label.setText("DISCONNECTED \u2014 " + self.labelString)
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

  @pyqtSlot()
  def _onBlacsHello(self):
    """Received HELLO from BLACS via ZMQ — mark BLACS as connected."""
    self._blacsConnected = True
    self._lastBlacsContact = time.time()

  # --- Laser commands ---

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

    On success: caches the *actual* reported value (not the requested one),
    updates radio buttons + frequency interlock to reflect it, and warns in
    orange if the controller reported a different mode than requested.
    On parse failure: logs the raw response in red and leaves the cache
    unchanged — callers must re-check ``self.flashLampMode`` if they need a
    confirmed state (e.g. ``startLaser`` aborts when mode != 1).
    """
    cmd_bytes = ('>%s\n' % cmd_label).encode('ascii')
    print(cmd_label)
    response = self._sendCommand(cmd_bytes)
    if response is None: return  # serial failure — _sendCommand already logged
    m = _TRAILING_INT_RE.search(response.strip())
    if m is None:
      self.terminalOutputTextBrowser.append(
          "<p style='color: red'>Could not parse %s response: %r — flashLampMode unchanged</p>"
          % (cmd_label, response))
      return
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
    if actual != target:
      self.terminalOutputTextBrowser.append(
          "<p style='color: orange'>Warning: %s did not take effect (got %d)</p>"
          % (cmd_label, actual))
    print("response:", response)
    self.terminalOutputTextBrowser.append(cmd_label)
    self.terminalOutputTextBrowser.append("<p style='color: green'>"+response.strip('\r\n')+"</p>")

  def setFlashLampInternal(self):
    self._setLampMode(0, '>lpm0')

  def setFlashLampExternal(self):
    self._setLampMode(1, '>lpm1')

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
      print("Remote voltage %d out of range [500,1400]" % voltage_V); return
    # Always send — deduplication is handled by BLACS worker (_last_sent_values)
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
    # On timeout (None): leave cache unchanged — don't assume command succeeded

  def _remoteSetShutter(self, state):
    """Set shutter to target state (not a toggle). 1=open, 0=close."""
    if state and not self.activeStatus:
      print("Remote shutter open rejected: lamps not active"); return
    # Send the specific command for the target state — don't rely on cached state for toggle direction
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
      print("Remote Q-switch arm rejected: requires lamps active + shutter open"); return
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
      self.terminalOutputTextBrowser.append(
          "<p style='color: orange'>[ZMQ] lamp_mode=%d rejected: laser active (must be in standby)</p>" % mode)
      return
    if mode == 0: self.setFlashLampInternal()
    elif mode == 1: self.setFlashLampExternal()
    else: print("Invalid lamp mode: %d" % mode)

  def _remoteSetQSwitchMode(self, mode):
    """Set Q-switch mode: 0=internal, 1=burst, 2=external. Requires standby."""
    if self.activeStatus:
      self.terminalOutputTextBrowser.append(
          "<p style='color: orange'>[ZMQ] qswitch_mode=%d rejected: laser active (must be in standby)</p>" % mode)
      return
    if mode == 0: self.setQSwitchInternal()
    elif mode == 1: self.setQSwitchBurst()
    elif mode == 2: self.setQSwitchExternal()
    else: print("Invalid Q-switch mode: %d" % mode)

  def safeExit(self):
    self.tempPollTimer.stop()
    self._reconnectTimer.stop()
    print(">s")
    if self.serialConnected:
      try:
        self.ser.flush(); self.ser.write(b'>s\n'); response = self.ser.read(140).decode('utf-8'); print("response:", response)
        self.ser.close()
      except Exception:
        pass

if __name__ == "__main__":
  app = QtWidgets.QApplication(sys.argv)
  window = SingleLaserController()
  app.aboutToQuit.connect(window.safeExit)
  window.show()
  sys.exit(app.exec_())

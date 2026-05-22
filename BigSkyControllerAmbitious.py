#written by Alex Brinson (brinson@mit.edu, alexjbrinson@gmail.com) on behalf of EMA Lab
import sys
from PyQt5 import QtCore, QtGui, QtWidgets, uic
from PyQt5.QtCore import QTimer, pyqtSignal, pyqtSlot
import serial
import time
import numpy as np
import os
import concurrent.futures
import threading

from serial_io import SerialIOMixin
from remote_bridge import RemoteBridgeMixin
from compound_sequences import CompoundSequencesMixin
from laser_commands import LaserCommandsMixin

qtCreatorFile = "GuiBigSkyWidget.ui" # Enter file here.

Ui_Widget, QtBaseClass = uic.loadUiType(qtCreatorFile)

class SingleLaserController(SerialIOMixin, RemoteBridgeMixin, CompoundSequencesMixin, LaserCommandsMixin, QtWidgets.QWidget, Ui_Widget):
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


  # --- Laser commands ---


if __name__ == "__main__":
  app = QtWidgets.QApplication(sys.argv)
  window = SingleLaserController()
  app.aboutToQuit.connect(window.safeExit)
  window.show()
  sys.exit(app.exec_())

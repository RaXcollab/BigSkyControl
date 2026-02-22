#written by Alex Brinson (brinson@mit.edu, alexjbrinson@gmail.com) on behalf of EMA Lab
import sys
from PyQt5 import QtCore, QtGui, QtWidgets, uic
from PyQt5.QtCore import QTimer, pyqtSignal, pyqtSlot
import serial
import time
import numpy as np
import os
import threading
 
qtCreatorFile = "GuiBigSkyWidget.ui" # Enter file here.
 
Ui_Widget, QtBaseClass = uic.loadUiType(qtCreatorFile)
 
class SingleLaserController(QtWidgets.QWidget, Ui_Widget):
  #Signal for thread-safe remote command execution from ZMQ daemon thread
  _remoteCommandRequested = pyqtSignal(str, object, object)  # (command, value, done_event)

  def __init__(self, cPort=-1, lString=''):
    super().__init__()
    self.setupUi(self)
    self.comPort = cPort
    self.labelString=lString

    self.calibrationFilePresent=False #TODO: check for calibration file based on laser head serial number

    #Testing different possible serial ports to see if any of them is a Big Sky laser. If ">cg" evokes a temperature readout, we found a live one.
    self.serialConnected = False
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
    self.lastTemperature = 0.0
    self.tempPollTimer = QTimer(self)
    self.tempPollTimer.timeout.connect(self.pollTemperature)
    self.TEMP_COLD = 37.0
    self.TEMP_OPERATING = 39.0

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
    self.flashLampVoltageLineEdit.returnPressed.connect(self.confirmVoltageSetting)
    self.frequencyConfirmationButton.clicked.connect(self.confirmFrequencySetting)
    self.laserSaveButton.clicked.connect(self.saveLaserSettings)

    #Independent controls
    self.lampToggleButton.clicked.connect(self.toggleActiveStatus)
    self.shutterToggleButton.clicked.connect(self.toggleShutterStatus)
    self.qSwitchToggleButton.clicked.connect(self.toggleQSwitchStatus)
    self.singlePulseButton.clicked.connect(self.singlePulse)

    #Compound controls
    self.warmupButton.clicked.connect(self.startWarmup)
    self.startLasingButton.clicked.connect(self.startLaser)
    self.fullStopButton.clicked.connect(self.stopLaser)

    #Keep warm
    self.keepWarmCheckBox.toggled.connect(self.toggleKeepWarm)

    self.toggleInputButton.clicked.connect(self.toggleTerminalInput)
    self.terminalInputLineEdit.textChanged.connect(self.updateTerminalCommand)
    self.terminalInputLineEdit.returnPressed.connect(self.sendTerminalCommand)
    self.terminalInputLabel.setEnabled(False); self.terminalInputLineEdit.setEnabled(False)

     

  def setFrequency(self):
    self.proposedFrequency = float(self.frequencyDoubleSpinBox.value())
    
  def confirmFrequencySetting(self):
    toWrite = ">f{freq}\n".format(freq = str(int(self.proposedFrequency*100)) )
    self.terminalOutputTextBrowser.append(">f{freq}".format(freq = str(int(self.proposedFrequency*100)) ))#this is just a test feature
    if self.serialConnected:
      self.ser.flush(); self.ser.write(bytes(toWrite,"utf-8") ); response = self.ser.read(140).decode('utf-8'); self.frequency=float(response.strip('\r\nfreq. Hz'));
      self.frequencyDoubleSpinBox.setValue(self.frequency); print("self.frequency = {f}Hz".format(f=self.frequency))
      self.terminalOutputTextBrowser.append("<p style='color: green'>"+response.strip('\r\n')+"</p>");
      self.updateTemp() 

  def updateFreq(self):
    self.terminalOutputTextBrowser.append('>f')
    self.ser.flush();self.ser.write(b'>f\n')
    response = self.ser.read(140).decode('utf-8'); self.frequency=float(response.strip('\r\nfreq. Hz'));
    self.terminalOutputTextBrowser.append("<p style='color: green'>"+response.strip('\r\n')+"</p>");
    print("self.frequency = {f}Hz".format(f=self.frequency))
    self.frequencyDoubleSpinBox.setValue(self.frequency)

  def saveLaserSettings(self):
    self.terminalOutputTextBrowser.append('>sav1')
    if self.serialConnected:
      self.ser.flush(); self.ser.write(b'>sav1\n')
      response = self.ser.read(140).decode('utf-8')
      self.terminalOutputTextBrowser.append("<p style='color: green'>"+response.strip('\r\n')+"</p>");
    print("Laser settings saved")

  '''NOTE: These can only be changed while laser is in standby (>s). The GUI should now reproduce this behavior'''
  def setQSwitchInternal(self):
    self.qSwitchMode = 0; print(">qsm0")
    if self.serialConnected:
      self.ser.flush(); self.ser.write(b'>qsm0\n'); response = self.ser.read(140).decode('utf-8'); print("response:", response)#; self.updateTemp()
      self.terminalOutputTextBrowser.append('>qsm0'); self.terminalOutputTextBrowser.append("<p style='color: green'>"+response.strip('\r\n')+"</p>")
  def setQSwitchBurst(self):
    self.qSwitchMode = 1; print(">qsm1")
    if self.serialConnected:
      self.ser.flush(); self.ser.write(b'>qsm1\n'); response = self.ser.read(140).decode('utf-8'); print("response:", response)#; self.updateTemp()
      self.terminalOutputTextBrowser.append('>qsm1'); self.terminalOutputTextBrowser.append("<p style='color: green'>"+response.strip('\r\n')+"</p>");
  def setQSwitchExternal(self):
    self.qSwitchMode = 2; print(">qsm2")
    if self.serialConnected:
      self.ser.flush(); self.ser.write(b'>qsm2\n'); response = self.ser.read(140).decode('utf-8'); print("response:", response)#; self.updateTemp()
      self.terminalOutputTextBrowser.append('>qsm2'); self.terminalOutputTextBrowser.append("<p style='color: green'>"+response.strip('\r\n')+"</p>");
  def setFlashLampInternal(self):
    self.flashLampMode = 0; print(">lpm0")
    self.frequencyDoubleSpinBox.setEnabled(not(self.flashLampMode)); self.frequencyConfirmationButton.setEnabled(not(self.flashLampMode))
    if self.serialConnected:
      self.ser.flush(); self.ser.write(b'>lpm0\n'); response = self.ser.read(140).decode('utf-8'); print("response:", response)#; self.updateTemp()
      self.terminalOutputTextBrowser.append('>lpm0'); self.terminalOutputTextBrowser.append("<p style='color: green'>"+response.strip('\r\n')+"</p>");
  def setFlashLampExternal(self):
    self.flashLampMode = 1; print(">lpm1")
    self.frequencyDoubleSpinBox.setEnabled(not(self.flashLampMode)); self.frequencyConfirmationButton.setEnabled(not(self.flashLampMode))
    if self.serialConnected:
      self.ser.flush(); self.ser.write(b'>lpm1\n'); response = self.ser.read(140).decode('utf-8'); print("response:", response)#; self.updateTemp()
      self.terminalOutputTextBrowser.append('>lpm1'); self.terminalOutputTextBrowser.append("<p style='color: green'>"+response.strip('\r\n')+"</p>");

  def confirmVoltageSetting(self):
    realUpdate=False
    try:
      self.proposedVoltage = int(self.flashLampVoltageLineEdit.text())
      if self.proposedVoltage<500 or self.proposedVoltage>1400:
        print("please enter an integer between 500 and 1400"); self.proposedVoltage = self.fLampVoltage
      else: realUpdate=True
    except: print("please enter an integer value."); self.proposedVoltage = self.fLampVoltage
    if realUpdate:
      toWrite = ">vmo{vol}\n".format( vol = str(0)+str(int(self.proposedVoltage)) if self.proposedVoltage<1000 else str(int(self.proposedVoltage)) )
      self.terminalOutputTextBrowser.append(toWrite.strip('\n'))
      if self.serialConnected:
        self.ser.flush(); self.ser.write(bytes(toWrite,"utf-8") );
        response = self.ser.read(140).decode('utf-8'); print("confirmVoltage response:",response)
        with self._stateLock: self.fLampVoltage=int(response.strip('\r\nvoltage m V'))
        print("voltage = {V}V".format(V=self.fLampVoltage))
        self.terminalOutputTextBrowser.append("<p style='color: green'>"+response.strip('\r\n')+"</p>");
        self.flashLampVoltageLineEdit.setText(str(self.fLampVoltage))
        self.update_fLampEnergy()
        self.updateTemp()
      else: self.fLampVoltage=self.proposedVoltage     
      self.PowerEstimateValue.setText('%.2f'%np.interp(self.fLampVoltage,self.calibVolts,self.calibPower) + " W")  
    else:
      self.flashLampVoltageLineEdit.setText(str(self.fLampVoltage))

  def toggleActiveStatus(self):
    with self._stateLock: self.activeStatus = 0 if self.activeStatus == 1 else 1
    if self.activeStatus:
       print(">a")
       self.terminalOutputTextBrowser.append("<p style='color: black'>"+'>a'+"</p>");
       if self.serialConnected:
        self.ser.flush(); self.ser.write(b'>a\n'); response = self.ser.read(140).decode('utf-8'); print("response:", response)
        self.terminalOutputTextBrowser.append("<p style='color: green'>"+response.strip('\r\n')+"</p>")
    else:
      print(">s")
      self.terminalOutputTextBrowser.append("<p style='color: black'>"+'>s'+"</p>");
      with self._stateLock: self.shutterStatus = 0; self.qSwitchStatus = 0
      if self.serialConnected:
        self.ser.flush(); self.ser.write(b'>s\n'); response = self.ser.read(140).decode('utf-8'); print("response:", response)
        self.terminalOutputTextBrowser.append("<p style='color: green'>"+response.strip('\r\n')+"</p>")
    self.updateAllStatusIndicators()

  def toggleShutterStatus(self):
    with self._stateLock: self.shutterStatus = 0 if self.shutterStatus == 1 else 1
    if self.shutterStatus:
      print(">r1")
      self.terminalOutputTextBrowser.append("<p style='color: black'>"+'>r1'+"</p>");
      if self.serialConnected:
        self.ser.flush(); self.ser.write(b'>r1\n'); response = self.ser.read(140).decode('utf-8'); print("response:", response)
        self.terminalOutputTextBrowser.append("<p style='color: green'>"+response.strip('\r\n')+"</p>")
    else:
      print(">r0")
      self.terminalOutputTextBrowser.append("<p style='color: black'>"+'>r0'+"</p>");
      if self.serialConnected:
        self.ser.flush(); self.ser.write(b'>r0\n'); response = self.ser.read(140).decode('utf-8'); print("response:", response)
        self.terminalOutputTextBrowser.append("<p style='color: green'>"+response.strip('\r\n')+"</p>")
    self.updateAllStatusIndicators()

  def toggleQSwitchStatus(self):
    if self.qSwitchStatus:
      with self._stateLock: self.qSwitchStatus = 0
      print(">sq"); self.terminalOutputTextBrowser.append("<p style='color: black'>"+'>sq'+"</p>");
      if self.serialConnected:
        self.ser.flush(); self.ser.write(b'>sq\n'); response = self.ser.read(140).decode('utf-8'); print("response:", response)
        self.terminalOutputTextBrowser.append("<p style='color: green'>"+response.strip('\r\n')+"</p>")
    else:
      with self._stateLock: self.qSwitchStatus = 1
      print(">pq"); self.terminalOutputTextBrowser.append("<p style='color: black'>"+'>pq'+"</p>");
      if self.serialConnected and self.dangerMode:
        self.ser.flush(); self.ser.write(b'>pq\n'); response = self.ser.read(140).decode('utf-8'); print("response:", response)
        self.terminalOutputTextBrowser.append("<p style='color: green'>"+response.strip('\r\n')+"</p>")
    self.updateAllStatusIndicators()
  
  def singlePulse(self):
    print(">oq"); self.terminalOutputTextBrowser.append("<p style='color: black'>"+'>oq'+"</p>");
    if self.serialConnected and self.dangerMode:
      self.ser.flush(); self.ser.write(b'>oq\n'); response = self.ser.read(140).decode('utf-8'); print("response:", response)
      self.terminalOutputTextBrowser.append("<p style='color: green'>"+response.strip('\r\n')+"</p>")

  def startLaser(self): #Single button to start lasing. Leaving lampfiring active with q-switch disabled could be damaging to laser.
    with self._stateLock: self.activeStatus = 1; self.shutterStatus = 1; self.qSwitchStatus = 1
    print(">a\n>r1\n>pq")
    self.terminalOutputTextBrowser.append("<p style='color: black'>"+'>a\n>r1\n>pq'+"</p>");
    if self.serialConnected:
      self.ser.flush(); self.ser.write(b'>a\n'); response = self.ser.read(140).decode('utf-8'); print("response:", response)
      self.terminalOutputTextBrowser.append("<p style='color: green'>"+response.strip('\r\n')+"</p>");
      self.ser.flush(); self.ser.write(b'>r1\n'); response = self.ser.read(140).decode('utf-8'); print("response:", response)
      self.terminalOutputTextBrowser.append("<p style='color: green'>"+response.strip('\r\n')+"</p>");
      self.ser.flush(); self.ser.write(b'>pq\n'); response = self.ser.read(140).decode('utf-8'); print("response:", response)
      self.terminalOutputTextBrowser.append("<p style='color: green'>"+response.strip('\r\n')+"</p>");
    if self.keepWarmActive:
      self.keepWarmCheckBox.setChecked(False)
    self.warmupActive = False
    self.updateAllStatusIndicators()

  def stopLaser(self): #This does the same thing as toggleActiveStatus if active status == 1. But it's redundant for safety, in case gui and laser get de-synced somehow.
    with self._stateLock: self.activeStatus = 0; self.shutterStatus = 0; self.qSwitchStatus = 0
    print(">s")
    self.terminalOutputTextBrowser.append("<p style='color: black'>"+'>s'+"</p>");
    if self.serialConnected:
      self.ser.flush(); self.ser.write(b'>s\n'); response = self.ser.read(140).decode('utf-8'); print("response:", response)
      self.terminalOutputTextBrowser.append("<p style='color: green'>"+response.strip('\r\n')+"</p>");
    self.warmupActive = False
    if self.keepWarmActive:
      self.keepWarmCheckBox.setChecked(False)
    self.tempPollTimer.stop()
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
      self.ser.flush(); self.ser.write(b'>sn\n'); response = self.ser.read(140).decode('utf-8'); print("response:", response)
      self.terminalOutputTextBrowser.append("<p style='color: green'>"+response.strip('\r\n')+"</p>")
      sn = response.strip(' \r\ns/number')
    else: sn=''
    self.serialNumber=sn

  def updateTerminalCommand(self,text):
    self.terminalLineCurrently = text

  def sendTerminalCommand(self):
    toWrite = '>'+self.terminalLineCurrently+'\n'
    print("sending to terminal:",toWrite) #TODO: finish this function
    self.terminalOutputTextBrowser.append("<p style='color: blue'>"+toWrite.strip('\n')+"</p>");
    if self.serialConnected:
      self.ser.flush(); self.ser.write(bytes(toWrite,"utf-8") );
      response = self.ser.read(140).decode('utf-8');
      self.terminalOutputTextBrowser.append("<p style='color: green'>"+response.strip('\r\n')+"</p>");
    self.terminalLineCurrently = ''
    self.terminalInputLineEdit.setText(self.terminalLineCurrently)

  def updateTemp(self):
    self.terminalOutputTextBrowser.append('>cg')
    self.ser.flush();self.ser.write(b'>cg\n')
    response = self.ser.read(140).decode('utf-8'); temp=float(response.strip('\r\ntemp.CG d'))
    with self._stateLock: self.lastTemperature = temp
    print("temperature = {T}C".format(T=temp))
    self.terminalOutputTextBrowser.append("<p style='color: green'>"+response.strip('\r\n')+"</p>");
    tiempo = time.strftime("%d %b %Y %H:%M:%S",time.localtime())
    print("time = {t}".format(t=tiempo))
    self.temperatureOutput.setText(str(temp)+" C")
    self.lastUpdateOutput.setText(str(tiempo))
    self._updateTemperatureStatusColor()

  def update_fLampVoltage(self):
    self.terminalOutputTextBrowser.append('>v')
    self.ser.flush();self.ser.write(b'>v\n')
    response = self.ser.read(140).decode('utf-8')
    with self._stateLock: self.fLampVoltage=int(response.strip('\r\nvoltage V'))
    print("voltage = {V}V".format(V=self.fLampVoltage))
    self.terminalOutputTextBrowser.append("<p style='color: green'>"+response.strip('\r\n')+"</p>");
    #self.flashLampVoltageHorizontalSlider.setValue(self.fLampVoltage)
    self.flashLampVoltageLineEdit.setText(str(self.fLampVoltage))

    self.PowerEstimateValue.setText('%.2f'%np.interp(self.fLampVoltage,self.calibVolts,self.calibPower) + " W")
    
  def update_fLampEnergy(self):
    self.terminalOutputTextBrowser.append('>ene')
    self.ser.flush();self.ser.write(b'>ene\n')
    response = self.ser.read(140).decode('utf-8'); self.fLampEnergy=float(response.strip('\r\nenergy J'))
    print("energy = {E}J".format(E=self.fLampEnergy))
    self.terminalOutputTextBrowser.append("<p style='color: green'>"+response.strip('\r\n')+"</p>");
    #self.flashLampEnergyHorizontalSlider.setValue(int(10*self.fLampEnergy))
    self.flashLampEnergyValue.setText(str(self.fLampEnergy)+" J")

  def update_fLampMode(self):
    self.terminalOutputTextBrowser.append('>lpm')
    self.ser.flush();self.ser.write(b'>lpm\n')
    response = self.ser.read(140).decode('utf-8'); self.flashLampMode=int(response.strip('\r\nLP synch :  '))
    print("self.flashLampMode = {f}".format(f=self.flashLampMode))
    self.terminalOutputTextBrowser.append("<p style='color: green'>"+response.strip('\r\n')+"</p>");
    #self.flashLampEnergyHorizontalSlider.setValue(int(10*self.fLampEnergy))
    if self.flashLampMode==0: self.flashLampRadioButton_0.setChecked(True)
    elif self.flashLampMode==1: self.flashLampRadioButton_1.setChecked(True)
    else: print("ERROR. self.flashLampMode makes no sense");self.ser.flush();self.ser.write(b'>s\n'); self.ser.read(140).decode('utf-8');

  def update_qSwitchMode(self):
    self.terminalOutputTextBrowser.append('>qsm')
    self.ser.flush();self.ser.write(b'>qsm\n')
    response = self.ser.read(140).decode('utf-8'); self.qSwitchMode=int(response.strip('\r\nQS mode :  '))
    print("self.qSwitchMode = {q}".format(q=self.qSwitchMode))
    self.terminalOutputTextBrowser.append("<p style='color: green'>"+response.strip('\r\n')+"</p>");
    #self.flashLampEnergyHorizontalSlider.setValue(int(10*self.fLampEnergy))
    if self.qSwitchMode==0: self.qSwitchRadioButton_0.setChecked(True)
    elif self.qSwitchMode==1: self.qSwitchRadioButton_1.setChecked(True)
    elif self.qSwitchMode==2: self.qSwitchRadioButton_2.setChecked(True)
    else: print("ERROR. self.qSwitchMode makes no sense");self.ser.flush();self.ser.write(b'>s\n'); self.ser.read(140).decode('utf-8');

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
    if self.activeStatus and self.shutterStatus and self.qSwitchStatus:
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
    #Ensure Q-switch is disabled
    if self.qSwitchStatus:
      with self._stateLock: self.qSwitchStatus = 0
      self.ser.flush(); self.ser.write(b'>sq\n'); self.ser.read(140).decode('utf-8')
      self.terminalOutputTextBrowser.append('>sq')
    #Ensure shutter is closed
    if self.shutterStatus:
      with self._stateLock: self.shutterStatus = 0
      self.ser.flush(); self.ser.write(b'>r0\n'); self.ser.read(140).decode('utf-8')
      self.terminalOutputTextBrowser.append('>r0')
    #Activate lamps if not already active
    if not self.activeStatus:
      with self._stateLock: self.activeStatus = 1
      self.ser.flush(); self.ser.write(b'>a\n')
      response = self.ser.read(140).decode('utf-8')
      self.terminalOutputTextBrowser.append('>a')
      self.terminalOutputTextBrowser.append("<p style='color: green'>"+response.strip('\r\n')+"</p>")
    #Start temperature polling (60 seconds, matching LabView)
    self.tempPollTimer.start(60000)
    self.updateTemp()
    self.updateAllStatusIndicators()
    self.terminalOutputTextBrowser.append(
        "<p style='color: blue'>Warmup started. Lamps firing, shutter closed. Waiting for temp > 37C...</p>")

  def toggleKeepWarm(self, checked):
    self.keepWarmActive = checked
    if checked:
      if not self.activeStatus:
        self.startWarmup()
      else:
        #Already active, ensure shutter closed and Q-switch off
        if self.qSwitchStatus:
          self.toggleQSwitchStatus()
        if self.shutterStatus:
          self.toggleShutterStatus()
        self.tempPollTimer.start(60000)
      self.terminalOutputTextBrowser.append(
          "<p style='color: blue'>Keep-warm mode enabled. Temperature polled every 60 seconds.</p>")
    else:
      self.warmupActive = False
      self.tempPollTimer.stop()
      self.terminalOutputTextBrowser.append(
          "<p style='color: blue'>Keep-warm mode disabled. Temperature polling stopped.</p>")

  def pollTemperature(self):
    if not self.serialConnected:
      return
    self.updateTemp()
    if self.warmupActive and self.lastTemperature >= self.TEMP_COLD:
      self.terminalOutputTextBrowser.append(
          "<p style='color: green'>Temperature %.1fC >= %.1fC. Laser is warm enough to lase.</p>"
          % (self.lastTemperature, self.TEMP_COLD))
      if not self.keepWarmActive:
        self.warmupActive = False
        self.tempPollTimer.stop()
    self.updateAllStatusIndicators()

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

  def executeRemoteCommand(self, command, value, done_event=None):
    """Thread-safe remote command. Emits signal to Qt main thread.
    If done_event is provided, caller can wait on it for completion."""
    self._remoteCommandRequested.emit(command, value, done_event)

  @pyqtSlot(str, object, object)
  def _handleRemoteCommand(self, command, value, done_event):
    """Slot runs on main/GUI thread. Dispatches remote commands to appropriate handlers."""
    try:
      self.terminalOutputTextBrowser.append("<p style='color: blue'>[ZMQ] %s = %s</p>" % (command, str(value)))
      if command == 'voltage':
        self._remoteSetVoltage(int(round(float(value))))
      elif command == 'shutter':
        self._remoteSetShutter(int(round(float(value))))
      elif command == 'lamps':
        self._remoteSetLamps(int(round(float(value))))
      elif command == 'qswitch':
        self._remoteSetQSwitch(int(round(float(value))))
      elif command == 'lamp_mode':
        self._remoteSetLampMode(int(round(float(value))))
      elif command == 'qswitch_mode':
        self._remoteSetQSwitchMode(int(round(float(value))))
      elif command == 'warmup':
        if int(round(float(value))): self.startWarmup()
        else:
          self.warmupActive = False; self.tempPollTimer.stop()
          self.terminalOutputTextBrowser.append("<p style='color: blue'>[ZMQ] Warmup stopped</p>")
      elif command == 'start_lasing':
        self.startLaser()
      elif command == 'stop':
        self.stopLaser()
      else:
        print("Unknown remote command: %s" % command)
    finally:
      if done_event is not None: done_event.set()

  def _remoteSetVoltage(self, voltage_V):
    if voltage_V < 500 or voltage_V > 1400:
      print("Remote voltage %d out of range [500,1400]" % voltage_V); return
    toWrite = ">vmo{vol}\n".format(vol=str(0)+str(voltage_V) if voltage_V<1000 else str(voltage_V))
    if self.serialConnected:
      self.ser.flush(); self.ser.write(bytes(toWrite,"utf-8"))
      response = self.ser.read(140).decode('utf-8'); print("remote voltage response:", response)
      with self._stateLock: self.fLampVoltage = int(response.strip('\r\nvoltage m V'))
      self.terminalOutputTextBrowser.append("<p style='color: green'>"+response.strip('\r\n')+"</p>")
      self.flashLampVoltageLineEdit.setText(str(self.fLampVoltage))
      self.PowerEstimateValue.setText('%.2f'%np.interp(self.fLampVoltage,self.calibVolts,self.calibPower) + " W")
      self.update_fLampEnergy(); self.updateTemp()
    else:
      with self._stateLock: self.fLampVoltage = voltage_V
      self.flashLampVoltageLineEdit.setText(str(voltage_V))
      self.PowerEstimateValue.setText('%.2f'%np.interp(voltage_V,self.calibVolts,self.calibPower) + " W")

  def _remoteSetShutter(self, state):
    """Set shutter: 1=open, 0=close. Respects safety: requires lamps active to open."""
    if state and not self.activeStatus:
      print("Remote shutter open rejected: lamps not active"); return
    if state == self.shutterStatus: return  # already in desired state
    self.toggleShutterStatus()

  def _remoteSetLamps(self, state):
    """Set lamps: 1=activate, 0=standby. Standby also clears shutter+qswitch."""
    if state == self.activeStatus: return  # already in desired state
    self.toggleActiveStatus()

  def _remoteSetQSwitch(self, state):
    """Set Q-switch: 1=arm, 0=disarm. Requires lamps active and shutter open to arm."""
    if state and (not self.activeStatus or not self.shutterStatus):
      print("Remote Q-switch arm rejected: requires lamps active + shutter open"); return
    if state == self.qSwitchStatus: return  # already in desired state
    self.toggleQSwitchStatus()

  def _remoteSetLampMode(self, mode):
    """Set lamp mode: 0=internal, 1=external. Requires standby."""
    if self.activeStatus:
      print("Remote lamp mode change rejected: laser must be in standby"); return
    if mode == 0: self.setFlashLampInternal()
    elif mode == 1: self.setFlashLampExternal()
    else: print("Invalid lamp mode: %d" % mode)

  def _remoteSetQSwitchMode(self, mode):
    """Set Q-switch mode: 0=internal, 1=burst, 2=external. Requires standby."""
    if self.activeStatus:
      print("Remote Q-switch mode change rejected: laser must be in standby"); return
    if mode == 0: self.setQSwitchInternal()
    elif mode == 1: self.setQSwitchBurst()
    elif mode == 2: self.setQSwitchExternal()
    else: print("Invalid Q-switch mode: %d" % mode)

  def safeExit(self):
    self.tempPollTimer.stop()
    print(">s")
    if self.serialConnected:
      self.ser.flush(); self.ser.write(b'>s\n'); response = self.ser.read(140).decode('utf-8'); print("response:", response)
      self.ser.close()

if __name__ == "__main__":
  app = QtWidgets.QApplication(sys.argv)
  window = SingleLaserController()
  app.aboutToQuit.connect(window.safeExit)
  window.show()
  sys.exit(app.exec_())
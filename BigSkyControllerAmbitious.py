#written by Alex Brinson (brinson@mit.edu, alexjbrinson@gmail.com) on behalf of EMA Lab
import sys
from PyQt5 import QtCore, QtGui, QtWidgets, uic
import serial
import time
import numpy as np
import os
 
qtCreatorFile = "GuiBigSkyWidget.ui" # Enter file here.
 
Ui_Widget, QtBaseClass = uic.loadUiType(qtCreatorFile)
 
class SingleLaserController(QtWidgets.QWidget, Ui_Widget):
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
    else: self.label.setText("Laser not found. This is a dummy GUI\n"+labelString)

    self.frequencyDoubleSpinBox.setEnabled(not(self.flashLampMode));
    self.frequencyConfirmationButton.setEnabled(not(self.flashLampMode))

    #Connecting signals to slots
    self.frequencyDoubleSpinBox.valueChanged.connect(self.setFrequency)
    self.frequencyDoubleSpinBox.editingFinished.connect(self.setFrequency)
    self.qSwitchRadioButton_0.clicked.connect(self.setQSwitchInternal)
    self.qSwitchRadioButton_1.clicked.connect(self.setQSwitchBurst)
    self.qSwitchRadioButton_2.clicked.connect(self.setQSwitchExternal)
    self.flashLampRadioButton_0.clicked.connect(self.setFlashLampInternal)
    self.flashLampRadioButton_1.clicked.connect(self.setFlashLampExternal)
    self.lampActivationButton.clicked.connect(self.startLaser)
    self.flashLampVoltageLineEdit.returnPressed.connect(self.confirmVoltageSetting)
    self.frequencyConfirmationButton.clicked.connect(self.confirmFrequencySetting)
    self.stopButton.clicked.connect(self.stopLaser)
    self.laserSaveButton.clicked.connect(self.saveLaserSettings)

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
        self.fLampVoltage=int(response.strip('\r\nvoltage m V'))
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
    self.activeStatus = 0 if self.activeStatus == 1 else 1
    #self.lampActivationButton.setText("Lamp Firing  Activated  ") if self.activeStatus else self.lampActivationButton.setText("Lamp Firing Deactivated")
    self.singlePulseButton.setEnabled(not(self.qSwitchStatus) and self.shutterStatus and self.activeStatus and (self.qSwitchMode==0))
    self.qSwitchRadioButton_0.setEnabled(not(self.activeStatus)); self.qSwitchRadioButton_1.setEnabled(not(self.activeStatus)); self.qSwitchRadioButton_2.setEnabled(not(self.activeStatus));
    self.flashLampRadioButton_0.setEnabled(not(self.activeStatus)); self.flashLampRadioButton_1.setEnabled(not(self.activeStatus))
    if self.activeStatus:
       print(">a"); self.lampActivationButton.setText("Lamp Firing  Activated  ")
       self.terminalOutputTextBrowser.append("<p style='color: black'>"+'>a'+"</p>");

       if self.serialConnected:
        self.ser.flush(); self.ser.write(b'>a\n'); response = self.ser.read(140).decode('utf-8'); print("response:", response)
        self.terminalOutputTextBrowser.append("<p style='color: green'>"+response.strip('\r\n')+"</p>")
    else:
      print(">s"); self.lampActivationButton.setText("Lamp Firing Deactivated")
      self.terminalOutputTextBrowser.append("<p style='color: black'>"+'>s'+"</p>");
      self.shutterStatus = 0
      self.qSwitchStatus = 0
      if self.serialConnected:
        self.ser.flush(); self.ser.write(b'>s\n'); response = self.ser.read(140).decode('utf-8'); print("response:", response)
        self.terminalOutputTextBrowser.append("<p style='color: green'>"+response.strip('\r\n')+"</p>")

  def toggleShutterStatus(self):
    self.shutterStatus = 0 if self.shutterStatus == 1 else 1
    self.singlePulseButton.setEnabled(not(self.qSwitchStatus) and self.shutterStatus and self.activeStatus and (self.qSwitchMode==0))
    if self.shutterStatus:
      print(">r1"); #self.shutterButton.setText("Shutter  Open  ")
      self.terminalOutputTextBrowser.append("<p style='color: black'>"+'>r1'+"</p>");
      if self.serialConnected:
        self.ser.flush(); self.ser.write(b'>r1\n'); response = self.ser.read(140).decode('utf-8'); print("response:", response)
        self.terminalOutputTextBrowser.append("<p style='color: green'>"+response.strip('\r\n')+"</p>")
    else:
      print(">r0"); #self.shutterButton.setText("Shutter Closed")
      self.terminalOutputTextBrowser.append("<p style='color: black'>"+'>r0'+"</p>");
      if self.serialConnected:
        self.ser.flush(); self.ser.write(b'>r0\n'); response = self.ser.read(140).decode('utf-8'); print("response:", response)
        self.terminalOutputTextBrowser.append("<p style='color: green'>"+response.strip('\r\n')+"</p>")
    #self.qSwitchActivationButton.setEnabled(self.activeStatus and self.shutterStatus)

  def toggleQSwitchStatus(self):
    if self.qSwitchStatus:
      self.qSwitchStatus = 0; print(">sq"); self.terminalOutputTextBrowser.append("<p style='color: black'>"+'>sq'+"</p>");
      if self.serialConnected:
        self.ser.flush(); self.ser.write(b'>sq\n'); response = self.ser.read(140).decode('utf-8'); print("response:", response)
        self.terminalOutputTextBrowser.append("<p style='color: green'>"+response.strip('\r\n')+"</p>")

    else:
      self.qSwitchStatus = 1; print(">pq"); self.terminalOutputTextBrowser.append("<p style='color: black'>"+'>pq'+"</p>");
      if self.serialConnected and self.dangerMode:
        self.ser.flush(); self.ser.write(b'>pq\n'); response = self.ser.read(140).decode('utf-8'); print("response:", response)
        self.terminalOutputTextBrowser.append("<p style='color: green'>"+response.strip('\r\n')+"</p>")
  
  def singlePulse(self):
    print(">oq"); self.terminalOutputTextBrowser.append("<p style='color: black'>"+'>oq'+"</p>");
    if self.serialConnected and self.dangerMode:
      self.ser.flush(); self.ser.write(b'>oq\n'); response = self.ser.read(140).decode('utf-8'); print("response:", response)
      self.terminalOutputTextBrowser.append("<p style='color: green'>"+response.strip('\r\n')+"</p>")

  def startLaser(self): #Single button to start lasing. Leaving lampfiring active with q-switch disabled could be damaging to laser.
    self.activeStatus = 1
    #self.lampActivationButton.setText("Laser Activated ") if self.activeStatus else self.lampActivationButton.setText("START")
    self.qSwitchRadioButton_0.setEnabled(not(self.activeStatus)); self.qSwitchRadioButton_1.setEnabled(not(self.activeStatus)); self.qSwitchRadioButton_2.setEnabled(not(self.activeStatus));
    self.flashLampRadioButton_0.setEnabled(not(self.activeStatus)); self.flashLampRadioButton_1.setEnabled(not(self.activeStatus))
    print(">a\n>r1\n>pq"); self.lampActivationButton.setText("Laser Activated")
    self.terminalOutputTextBrowser.append("<p style='color: black'>"+'>a\n>r1\n>pq'+"</p>");
    self.shutterStatus = 1; self.qSwitchStatus = 1
    if self.serialConnected:
      self.ser.flush(); self.ser.write(b'>a\n'); response = self.ser.read(140).decode('utf-8'); print("response:", response)
      self.terminalOutputTextBrowser.append("<p style='color: green'>"+response.strip('\r\n')+"</p>");
      self.ser.flush(); self.ser.write(b'>r1\n'); response = self.ser.read(140).decode('utf-8'); print("response:", response)
      self.terminalOutputTextBrowser.append("<p style='color: green'>"+response.strip('\r\n')+"</p>");
      self.ser.flush(); self.ser.write(b'>pq\n'); response = self.ser.read(140).decode('utf-8'); print("response:", response)
      self.terminalOutputTextBrowser.append("<p style='color: green'>"+response.strip('\r\n')+"</p>");
    self.lampActivationButton.setEnabled(not(self.activeStatus)) 

  def stopLaser(self): #This does the same thing as toggleActiveStatus if active status == 1. But it's redundant for safety, in case gui and laser get de-synced somehow.
    self.activeStatus = 0
    self.qSwitchRadioButton_0.setEnabled(not(self.activeStatus)); self.qSwitchRadioButton_1.setEnabled(not(self.activeStatus)); self.qSwitchRadioButton_2.setEnabled(not(self.activeStatus));
    self.flashLampRadioButton_0.setEnabled(not(self.activeStatus)); self.flashLampRadioButton_1.setEnabled(not(self.activeStatus))
    print(">s"); self.lampActivationButton.setText("START")
    self.terminalOutputTextBrowser.append("<p style='color: black'>"+'>s'+"</p>");
    self.shutterStatus = 0; #self.shutterButton.setText("Shutter Closed")
    self.qSwitchStatus = 0; #self.qSwitchActivationButton.setText("qSwitch Deactivated");
    if self.serialConnected:
      self.ser.flush(); self.ser.write(b'>s\n'); response = self.ser.read(140).decode('utf-8'); print("response:", response)
      self.terminalOutputTextBrowser.append("<p style='color: green'>"+response.strip('\r\n')+"</p>");
    #self.qSwitchActivationButton.setEnabled(self.activeStatus and self.shutterStatus and not(self.terminalEnabled))
    self.lampActivationButton.setEnabled(not(self.activeStatus)) 

  def toggleTerminalInput(self):
    if self.terminalEnabled:
      self.terminalEnabled=False;
      self.stopLaser(); #self.lampActivationButton.setEnabled(True); #self.shutterButton.setEnabled(True)
      self.update_fLampMode()
      self.update_qSwitchMode()
      self.update_fLampVoltage()
      self.update_fLampEnergy()
      self.updateTemp()
      self.updateFreq()
    else:
      self.terminalEnabled=True;
      #self.lampActivationButton.setEnabled(False)#self.shutterButton.setEnabled(False); self.qSwitchActivationButton.setEnabled(False); self.singlePulseButton.setEnabled(False);
    self.terminalInputLabel.setEnabled(self.terminalEnabled); self.terminalInputLineEdit.setEnabled(self.terminalEnabled)
    self.qSwitchRadioButton_0.setEnabled(not(self.terminalEnabled)); self.qSwitchRadioButton_1.setEnabled(not(self.terminalEnabled)); self.qSwitchRadioButton_2.setEnabled(not(self.terminalEnabled))
    self.flashLampRadioButton_0.setEnabled(not(self.terminalEnabled)); self.flashLampRadioButton_1.setEnabled(not(self.terminalEnabled))
    frequencyBoolean = not(self.terminalEnabled) and not(self.flashLampMode)
    self.frequencyDoubleSpinBox.setEnabled(frequencyBoolean); self.FrequencyLabel.setEnabled(frequencyBoolean); self.frequencyConfirmationButton.setEnabled(frequencyBoolean)
    #self.flashLampEnergyLabel.setEnabled(not(self.terminalEnabled)); self.fLampEnergyConfirmationButton.setEnabled(not(self.terminalEnabled));
    #self.flashLampEnergyHorizontalSlider.setEnabled(not(self.terminalEnabled)); self.flashLampEnergyDoubleSpinBox.setEnabled(not(self.terminalEnabled));
    self.flashLampVoltageLabel.setEnabled(not(self.terminalEnabled)); #self.fLampVoltageConfirmationButton.setEnabled(not(self.terminalEnabled));
    #self.flashLampVoltageHorizontalSlider.setEnabled(not(self.terminalEnabled)); self.flashLampVoltageSpinBox.setEnabled(not(self.terminalEnabled));

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
    print("temperature = {T}C".format(T=temp))
    self.terminalOutputTextBrowser.append("<p style='color: green'>"+response.strip('\r\n')+"</p>");
    tiempo = time.strftime("%d %b %Y %H:%M:%S",time.localtime())
    print("time = {t}".format(t=tiempo))
    self.temperatureOutput.setText(str(temp)+" C")
    self.lastUpdateOutput.setText(str(tiempo))

  def update_fLampVoltage(self):
    self.terminalOutputTextBrowser.append('>v')
    self.ser.flush();self.ser.write(b'>v\n')
    response = self.ser.read(140).decode('utf-8'); self.fLampVoltage=int(response.strip('\r\nvoltage V'))
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

  def safeExit(self):
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
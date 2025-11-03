import sys
from PyQt5.QtWidgets import (QMainWindow, QApplication, QPushButton,
 QWidget, QAction, QTabWidget,QVBoxLayout, QGridLayout, QTabBar, QLineEdit, QTextBrowser)
from PyQt5.QtGui import QIcon, QPixmap
from PyQt5.QtCore import pyqtSlot
from BigSkyControllerAmbitious import SingleLaserController
import serial.tools.list_ports
import ctypes
import pickle

class BigSkyHub(QMainWindow):
  def __init__(self):
    super().__init__()
    self.setWindowTitle('Big Sky Controller Hub')
    self.setWindowIcon(QIcon('BigSkyWindowIcon.png'))
    self.left = 0; self.width = 900
    self.top = 0 ; self.height = 800
    self.setGeometry(self.left, self.top, self.width, self.height)
    self.table_widget = MyTableWidget(self)
    self.setCentralWidget(self.table_widget)
    self.show()

class HomeTab(QWidget):
  def __init__(self,parent):
    super().__init__()
    possibleDevices=[comport.device for comport in serial.tools.list_ports.comports()]
    print(possibleDevices)
    try:
        with open('laserNames.pkl','rb') as file: self.laserNames=pickle.load(file); file.close()
    except: self.laserNames={}
    self.layout = QGridLayout(parent)
    self.buttons=[]
    self.devices=[]
    self.serialNumbers=[]
    self.labelLineEdits=[]
    self.processes={}
    if len(possibleDevices)==0: #pass. I'm only not passing for development testing purposes
      print('ayo')
      self.devices+=['dummy device']; sn=420
      self.buttons+=[QPushButton('launch %s ; SN%d'%(self.devices[-1],sn))]
      self.labelLineEdits+=[QLineEdit('')]
      self.layout.addWidget(self.buttons[-1], len(self.buttons)-1, 0)
      self.layout.addWidget(self.labelLineEdits[-1], len(self.buttons)-1, 1)
    else:
      for dev in possibleDevices:
        try:
          print('trying com port %s'%dev)
          ser = serial.Serial(dev,9600,timeout=1)
        except:
          print("nope not this one")
          #self.buttons[i].setEnabled(False)
          sn=-1
        else:
          print(" maybe this one?")
          ser.flush(); ser.write(b'>sn\n')
          response = ser.read(140).decode('utf-8'); print("response:", response)
          if 'number'in response:
            print("yeah this one."); ser.close()
            sn=response.strip('s// number\r\n')
            self.serialNumbers+=[sn]
            self.devices+=[dev]
            self.buttons+=[QPushButton('launch %s ; SN %s'%(dev, sn))]
            if sn in self.laserNames.keys(): self.labelLineEdits+=[QLineEdit(self.laserNames[sn])]
            else: self.labelLineEdits+=[QLineEdit('')]
            self.layout.addWidget(self.buttons[-1], len(self.buttons)-1, 0)
            self.layout.addWidget(self.labelLineEdits[-1], len(self.buttons)-1, 1)
    self.text = QTextBrowser()
    self.layout.addWidget(self.text, len(self.buttons),0)
    self.saveButton=QPushButton('Save Labels')
    self.layout.addWidget(self.saveButton, len(self.buttons),1)
    self.saveButton.pressed.connect(self.saveLabels)
    #self.saveButton.pressed.connect(lambda: self.text.append('bruh...'))
    self.setLayout(self.layout)
  def saveLabels(self):
    for i in range(len(self.buttons)):
      self.laserNames[self.serialNumbers[i]]=self.labelLineEdits[i].text()
    with open('laserNames.pkl','wb') as file: pickle.dump(self.laserNames, file); file.close()
    self.text.append('laser names saved to file.')
    
class MyTableWidget(QWidget):
  def __init__(self, parent):
    super(QWidget, self).__init__(parent)
    self.layout = QVBoxLayout(self)
    # Initialize tab screen
    self.tabs = QTabWidget()
    #self.tabs.resize(width,height)
    self.homeTab=HomeTab(self)
    print("test: ", len(self.homeTab.buttons))

    for i in range(len(self.homeTab.buttons)):
      self.homeTab.buttons[i].pressed.connect(lambda i=i: self.createTab(i))

    self.tabs.addTab(self.homeTab,"Home")
    self.tabs.setTabsClosable(True)
    self.tabs.tabCloseRequested.connect(self.closeTab)
    self.tabs.tabBar().setTabButton(0, QTabBar.RightSide, None) #removes close button from homeTab
    # Add tabs to widget
    self.layout.addWidget(self.tabs)

  def createTab(self, i):
    com=self.homeTab.devices[i]; labelString=self.homeTab.labelLineEdits[i].text()
    if labelString=='': labelString='test'+str(self.tabs.count())
    self.homeTab.text.append("Creating Gui for %s, with label \'%s\'"%(com,labelString))
    self.tabs.addTab(SingleLaserController(cPort=com,lString=labelString),labelString)
    self.homeTab.buttons[i].setEnabled(False)
  def closeTab(self,i):
    comport=self.tabs.widget(i).comPort
    label=self.tabs.widget(i).labelString
    self.homeTab.text.append("Closing tab %d aka %s"%(i,label))
    self.tabs.widget(i).safeExit()
    self.tabs.removeTab(i)
    for j in range(len(self.homeTab.buttons)): #I don't have a good way of identifying which tab number corresponds to which laser...
      if self.homeTab.devices[j]==comport:
        self.homeTab.buttons[j].setEnabled(True); break

  def safeExit(self):
    for i in range(1, self.tabs.count()):
      print('safely closing tab %d'%i)
      self.closeTab(i)
    
if __name__ == '__main__':
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(u'BigSkyControllerHub')
    app = QApplication(sys.argv)
    window = BigSkyHub()
    app.aboutToQuit.connect(window.table_widget.safeExit)
    sys.exit(app.exec_())
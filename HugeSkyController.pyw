import sys
from PyQt5.QtWidgets import (QMainWindow, QApplication, QPushButton,
 QWidget, QAction, QTabWidget,QVBoxLayout, QGridLayout, QTabBar, QLineEdit, QTextBrowser)
from PyQt5.QtGui import QIcon, QPixmap
from PyQt5.QtCore import pyqtSlot, pyqtSignal, QObject
from BigSkyControllerAmbitious import SingleLaserController
import serial.tools.list_ports
import ctypes
import pickle
import threading
import json

try:
  import zmq
  ZMQ_AVAILABLE = True
except ImportError:
  ZMQ_AVAILABLE = False
  print("WARNING: zmq not available. ZMQ server will not start.")


# --- ZMQ Server for BLACS integration ---

class BigSkyZmqServer(QObject):
  """ZMQ REP+PUB server for BLACS remote control of BigSky YAG lasers.

  Runs a daemon thread with:
    - REP socket: handles HELLO, PROGRAM_VALUE, CHECK_VALUE from BLACS
    - PUB socket: broadcasts heartbeat (~1 Hz) and all monitors (~4 Hz)

  Connection name format (per laser, e.g. YAG_1):
    Writable:  YAG_1_voltage, YAG_1_shutter, YAG_1_lamps, YAG_1_qswitch,
               YAG_1_lamp_mode, YAG_1_qswitch_mode, YAG_1_warmup,
               YAG_1_start_lasing, YAG_1_stop
    Monitors:  YAG_1_temperature_monitor, YAG_1_voltage_monitor,
               YAG_1_lamps_monitor, YAG_1_shutter_monitor, YAG_1_qswitch_monitor
  """
  logMessage = pyqtSignal(str)

  DEFAULT_REP_PORT = 55540
  DEFAULT_PUB_PORT = 55541

  # Valid parameter names for PROGRAM_VALUE commands
  WRITABLE_PARAMS = {'voltage', 'shutter', 'lamps', 'qswitch', 'lamp_mode',
                     'qswitch_mode', 'warmup', 'start_lasing', 'stop'}
  # Parameter names for CHECK_VALUE / PUB-SUB monitors
  MONITOR_PARAMS = {'temperature', 'voltage', 'lamps', 'shutter', 'qswitch'}

  def __init__(self, rep_port=None, pub_port=None, parent=None):
    super().__init__(parent)
    self.rep_port = rep_port or self.DEFAULT_REP_PORT
    self.pub_port = pub_port or self.DEFAULT_PUB_PORT
    self._stop_event = threading.Event()
    self._thread = None
    self._lasers = {}  # {connection_name: SingleLaserController} e.g. {"YAG_1": ctrl}

  def registerLaser(self, connection_name, controller):
    """Register a SingleLaserController under a BLACS connection name (e.g. 'YAG_1')."""
    self._lasers[connection_name] = controller
    self._log("ZMQ: registered laser '%s' (SN %s)" % (connection_name, controller.serialNumber))

  def unregisterLaser(self, connection_name):
    """Remove a laser from the ZMQ routing table."""
    if connection_name in self._lasers:
      del self._lasers[connection_name]
      self._log("ZMQ: unregistered laser '%s'" % connection_name)

  def start(self):
    """Start the ZMQ server daemon thread."""
    if not ZMQ_AVAILABLE:
      self._log("ZMQ: zmq module not available. Server not started."); return
    if self._thread is not None and self._thread.is_alive():
      self._log("ZMQ: server already running."); return
    self._stop_event.clear()
    self._thread = threading.Thread(target=self._server_loop, daemon=True, name="BigSkyZmqServer")
    self._thread.start()
    self._log("ZMQ: server started (REP port %d, PUB port %d)" % (self.rep_port, self.pub_port))

  def stop(self):
    """Signal the server thread to stop and wait for it to finish."""
    self._stop_event.set()
    if self._thread is not None:
      self._thread.join(timeout=3.0)
      self._thread = None
    self._log("ZMQ: server stopped.")

  def _log(self, msg):
    print(msg)
    self.logMessage.emit(msg)

  def _parse_connection(self, connection):
    """Parse a connection name into (base_name, param, is_monitor).
    Examples:
      'YAG_1_voltage'              -> ('YAG_1', 'voltage', False)
      'YAG_1_temperature_monitor'  -> ('YAG_1', 'temperature', True)
      'YAG_1_start_lasing'         -> ('YAG_1', 'start_lasing', False)
      'YAG_1_qswitch_mode'         -> ('YAG_1', 'qswitch_mode', False)
    Returns (None, None, False) if the laser base name is not registered."""
    is_monitor = connection.endswith('_monitor')
    stripped = connection[:-len('_monitor')] if is_monitor else connection
    # Match against registered laser names (longest first to handle e.g. YAG_10 vs YAG_1)
    for base in sorted(self._lasers.keys(), key=len, reverse=True):
      if stripped.startswith(base + '_'):
        param = stripped[len(base)+1:]
        return (base, param, is_monitor)
    return (None, None, False)

  def _get_monitor_value(self, ctrl, param):
    """Read a monitor value from a controller. Returns (value, fmt_str) or (None, error_str)."""
    if param == 'temperature': return (ctrl.getTemperature(), "%.1f")
    if param == 'voltage': return (ctrl.getVoltage(), "%d")
    if param == 'lamps': return (ctrl.getActiveStatus(), "%d")
    if param == 'shutter': return (ctrl.getShutterStatus(), "%d")
    if param == 'qswitch': return (ctrl.getQSwitchStatus(), "%d")
    return (None, "unknown monitor param '%s'" % param)

  def _server_loop(self):
    """Main ZMQ server loop running on the daemon thread."""
    ctx = zmq.Context.instance()

    rep_sock = ctx.socket(zmq.REP)
    rep_sock.bind("tcp://*:%d" % self.rep_port)
    rep_sock.RCVTIMEO = 250  # ms, so we can check stop_event frequently

    pub_sock = ctx.socket(zmq.PUB)
    pub_sock.bind("tcp://*:%d" % self.pub_port)

    pub_counter = 0

    def reply(obj):
      rep_sock.send(json.dumps(obj).encode())

    def publish(topic, value=""):
      msg = "%s %s" % (topic, value) if value else topic
      pub_sock.send_string(msg)

    self._log("ZMQ: server loop running.")

    while not self._stop_event.is_set():
      # --- PUB-SUB broadcasting ---
      pub_counter += 1

      # All monitors at ~4 Hz (every cycle, since loop is ~250ms)
      for conn_name, ctrl in list(self._lasers.items()):
        for param in self.MONITOR_PARAMS:
          val, fmt = self._get_monitor_value(ctrl, param)
          if val is not None:
            publish("%s_%s_monitor" % (conn_name, param), fmt % val)

      # Heartbeat at ~1 Hz (every 4th cycle at 250ms = 1s)
      if pub_counter % 4 == 0:
        publish("heartbeat")

      # --- REQ-REP handling ---
      try:
        req = rep_sock.recv()
      except zmq.error.Again:
        continue
      except Exception as e:
        self._log("ZMQ: socket error: %s" % str(e)); break

      try:
        data = json.loads(req.decode())
        action = data.get("action", "")
        connection = data.get("connection", "")
        value = data.get("value", None)
        wait_for_lock = data.get("wait_for_lock", False)
      except Exception:
        reply({"status": "ERROR", "message": "bad_json"}); continue

      # HELLO
      if action == "HELLO":
        self._log("ZMQ: HELLO received")
        reply({"status": "SUCCESS"}); continue

      # Parse connection name into (base, param, is_monitor)
      base, param, is_monitor = self._parse_connection(connection)
      if base is None or base not in self._lasers:
        reply({"status": "ERROR", "message": "unknown connection '%s'" % connection}); continue
      ctrl = self._lasers[base]

      # CHECK_VALUE
      if action == "CHECK_VALUE":
        check_param = param if param else 'voltage'
        if check_param not in self.MONITOR_PARAMS:
          reply({"status": "ERROR", "message": "unknown monitor param '%s' in '%s'" % (check_param, connection)}); continue
        val, fmt = self._get_monitor_value(ctrl, check_param)
        self._log("ZMQ: CHECK_VALUE %s -> %s" % (connection, fmt % val))
        reply({"status": "SUCCESS", "connection": connection, "value": val}); continue

      # PROGRAM_VALUE
      if action == "PROGRAM_VALUE":
        if is_monitor:
          reply({"status": "ERROR", "message": "cannot program monitor '%s'" % connection}); continue
        if param is None or param not in self.WRITABLE_PARAMS:
          reply({"status": "ERROR", "message": "unknown writable param '%s' in '%s'" % (param, connection)}); continue

        self._log("ZMQ: PROGRAM_VALUE %s = %s (wait_for_lock=%s)" % (connection, value, wait_for_lock))

        # Dispatch to controller's main thread via signal/slot + threading.Event
        done_event = threading.Event()
        ctrl.executeRemoteCommand(param, value, done_event)
        done_event.wait(timeout=10.0)

        if done_event.is_set():
          reply({"status": "SUCCESS"})
        else:
          reply({"status": "ERROR", "message": "timeout waiting for command to complete"})
        continue

      # Unknown action
      reply({"status": "ERROR", "message": "unknown action '%s'" % action})

    # Cleanup
    rep_sock.close()
    pub_sock.close()
    self._log("ZMQ: server loop exited.")


# --- Connection Name Mapping ---
# Maps laser serial numbers to BLACS connection names.
# Edit this dict to match your physical setup.
# Serial numbers are strings as returned by the >sn command.
LASER_SN_TO_CONNECTION = {
  # Example: '123': 'YAG_1', '456': 'YAG_2'
  # If empty, falls back to tab-order assignment (first laser = YAG_1, second = YAG_2)
}


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

    # ZMQ server
    self.zmqServer = BigSkyZmqServer(parent=self)
    self.zmqServer.logMessage.connect(self._onZmqLog)
    self.zmqServer.start()

    self.show()

  def _onZmqLog(self, msg):
    """Append ZMQ log messages to the home tab's text browser."""
    self.table_widget.homeTab.text.append("<p style='color: purple'>%s</p>" % msg)

  def closeEvent(self, event):
    """Override close to shut down ZMQ server before exiting."""
    self.zmqServer.stop()
    self.table_widget.safeExit()
    event.accept()

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
      self.serialNumbers+=[str(sn)]
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
    self.parent = parent
    self.layout = QVBoxLayout(self)
    # Initialize tab screen
    self.tabs = QTabWidget()
    #self.tabs.resize(width,height)
    self.homeTab=HomeTab(self)
    print("test: ", len(self.homeTab.buttons))

    # Track how many lasers have been launched (for fallback tab-order connection naming)
    self._laserLaunchOrder = 0

    for i in range(len(self.homeTab.buttons)):
      self.homeTab.buttons[i].pressed.connect(lambda i=i: self.createTab(i))

    self.tabs.addTab(self.homeTab,"Home")
    self.tabs.setTabsClosable(True)
    self.tabs.tabCloseRequested.connect(self.closeTab)
    self.tabs.tabBar().setTabButton(0, QTabBar.RightSide, None) #removes close button from homeTab
    # Add tabs to widget
    self.layout.addWidget(self.tabs)

  def _assignConnectionName(self, serialNumber):
    """Determine the BLACS connection name for a laser.
    First checks LASER_SN_TO_CONNECTION dict (keyed by serial number).
    Falls back to tab-order assignment: YAG_1, YAG_2, YAG_3, ..."""
    sn = str(serialNumber)
    if sn in LASER_SN_TO_CONNECTION:
      return LASER_SN_TO_CONNECTION[sn]
    self._laserLaunchOrder += 1
    return "YAG_%d" % self._laserLaunchOrder

  def createTab(self, i):
    com=self.homeTab.devices[i]; labelString=self.homeTab.labelLineEdits[i].text()
    if labelString=='': labelString='test'+str(self.tabs.count())
    self.homeTab.text.append("Creating Gui for %s, with label \'%s\'"%(com,labelString))
    ctrl = SingleLaserController(cPort=com, lString=labelString)
    self.tabs.addTab(ctrl, labelString)
    self.homeTab.buttons[i].setEnabled(False)

    # Register with ZMQ server
    sn = self.homeTab.serialNumbers[i] if i < len(self.homeTab.serialNumbers) else str(i)
    connName = self._assignConnectionName(sn)
    ctrl._zmqConnectionName = connName  # stash on controller for unregister lookup
    self.parent.zmqServer.registerLaser(connName, ctrl)
    self.homeTab.text.append("ZMQ: laser registered as '%s'" % connName)

  def closeTab(self,i):
    widget = self.tabs.widget(i)
    comport = widget.comPort
    label = widget.labelString
    self.homeTab.text.append("Closing tab %d aka %s"%(i,label))

    # Unregister from ZMQ server
    connName = getattr(widget, '_zmqConnectionName', None)
    if connName:
      self.parent.zmqServer.unregisterLaser(connName)

    widget.safeExit()
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
    app.aboutToQuit.connect(lambda: (window.zmqServer.stop(), window.table_widget.safeExit()))
    sys.exit(app.exec_())

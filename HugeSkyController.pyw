import os
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
import concurrent.futures
import json

try:
  import zmq
  ZMQ_AVAILABLE = True
except ImportError:
  ZMQ_AVAILABLE = False
  print("WARNING: zmq not available. ZMQ server will not start.")

# zmq_v2 protocol foundation lives in the parent labscript-suite repo.
# This GUI runs in conda env `guis` (not `labscript`) so we inject the path.
_EXTERNAL_LIB = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    '..', '..', 'userlib', 'external_gui_lib',
))
if _EXTERNAL_LIB not in sys.path:
  sys.path.insert(0, _EXTERNAL_LIB)
from zmq_v2 import (
    RemoteControlServerBase, handler, encode_reply,
    PROTOCOL_VERSION, ZmqRepTransport,
)


# --- Module-level constants ---

# Max wall time we wait for SingleLaserController's main-thread Qt slot
# (executeRemoteCommand -> _handleRemoteCommand) to complete and write
# its Future result. Exceeding this returns v2 TIMEOUT to BLACS. 10s
# accommodates compound sequences (startWarmup, startLaser) which step
# the laser through multi-byte command sequences with verify-on-readback;
# simple writes (voltage, shutter) complete in well under a second.
_REMOTE_CMD_TIMEOUT_S = 10.0


# --- ZMQ Server for BLACS integration ---


class _BigSkyV2Server(RemoteControlServerBase):
  """v2 protocol dispatcher composed inside BigSkyZmqServer.

  Lives on the same daemon thread as the PUB loop; ``serve_once``
  blocks up to ``timeout_ms`` on the REP socket, then dispatches via
  @handler-decorated methods below.

  Holds a back-reference to the outer ``BigSkyZmqServer`` to read
  ``_lasers`` / ``WRITABLE_PARAMS`` / etc. No state of its own beyond
  base-class scaffolding.
  """

  # Hub-mode: ADVERTISED_CONNECTIONS is dynamic (lasers register/unregister
  # at runtime), so we override _handle_hello below. The class-attr stays
  # None to skip the base default.
  CAPABILITIES = frozenset({"monitors", "heartbeat"})

  def __init__(self, outer, transport):
    super().__init__("BigSkyLasers", transport)
    self._outer = outer

  # ---- HELLO override (hub-mode + side-effect signals) ----
  def _handle_hello(self, request_id):
    # Side-effect: notify lasers BLACS is connected. Queued signal cross
    # to the Qt main thread — emit returns immediately, slot fires later.
    # Emitting BEFORE building the reply lets the slot start running in
    # parallel with the ZMQ send. Safe per blacs-expert audit 2026-05-23.
    for ctrl in list(self._outer._lasers.values()):
      ctrl._blacsHelloReceived.emit()
    self._outer._log("ZMQ: HELLO received")
    return encode_reply(
        status="SUCCESS",
        request_id=request_id,
        extra={
            "protocol_version": PROTOCOL_VERSION,
            "server": self._server_name,
            "capabilities": sorted(self.CAPABILITIES),
            # Per Q1 §10-resolved: hub advertises prefix patterns.
            "connections": [
                "%s_*" % name
                for name in sorted(self._outer._lasers.keys())
            ],
        },
    )

  # ---- PROGRAM_VALUE ----
  @handler("PROGRAM_VALUE")
  def _handle_program(self, connection, value, args, request_id):
    base, param, is_monitor = self._outer._parse_connection(connection)
    if base is None or base not in self._outer._lasers:
      return encode_reply(
          status="UNKNOWN_CONNECTION", request_id=request_id,
          error={
              "code": "unknown_connection",
              "message": "unknown connection '%s'" % connection,
              "retryable": False,
          },
      )
    ctrl = self._outer._lasers[base]
    if not ctrl.isConnected():
      return encode_reply(
          status="ERROR", request_id=request_id,
          error={
              "code": "laser_disconnected",
              "message": "laser disconnected",
              "retryable": True,
          },
      )
    if is_monitor:
      return encode_reply(
          status="ERROR", request_id=request_id,
          error={
              "code": "cannot_program_monitor",
              "message": "cannot program monitor '%s'" % connection,
              "retryable": False,
          },
      )
    if param is None or param not in self._outer.WRITABLE_PARAMS:
      return encode_reply(
          status="ERROR", request_id=request_id,
          error={
              "code": "unknown_writable_param",
              "message": ("unknown writable param '%s' in '%s'"
                          % (param, connection)),
              "retryable": False,
          },
      )

    self._outer._log("ZMQ: PROGRAM_VALUE %s = %s" % (connection, value))

    # Dispatch to controller's Qt main thread via Future round-trip.
    future = concurrent.futures.Future()
    ctrl.executeRemoteCommand(param, value, future)
    try:
      result = future.result(timeout=_REMOTE_CMD_TIMEOUT_S)
    except concurrent.futures.TimeoutError:
      return encode_reply(
          status="TIMEOUT", request_id=request_id,
          error={
              "code": "command_timeout",
              "message": "timeout waiting for command to complete",
              "retryable": True,
          },
      )

    # Translate the v1 result dict to v2 envelope:
    #  - {"status": "SUCCESS"}           -> SUCCESS
    #  - {"status": "ERROR", "message": "rejected: ..."} -> REJECTED
    #    (BigSky controller historically encodes rejections as ERROR
    #     with a "rejected:" message prefix; spec §1.3 promotes to its
    #     own enum value)
    #  - {"status": "ERROR", "message": <other>} -> ERROR
    rstat = result.get("status", "ERROR")
    rmsg = result.get("message", "")
    if rstat == "SUCCESS":
      return encode_reply(status="SUCCESS", request_id=request_id,
                          value=result.get("value"))
    if rstat == "ERROR" and rmsg.lower().startswith("rejected"):
      return encode_reply(
          status="REJECTED", request_id=request_id,
          error={
              "code": "rejected_did_not_take_effect",
              "message": rmsg,
              "retryable": False,
          },
      )
    return encode_reply(
        status="ERROR", request_id=request_id,
        error={
            "code": "command_error",
            "message": rmsg or "unknown error",
            "retryable": False,
        },
    )

  # ---- CHECK_VALUE ----
  @handler("CHECK_VALUE")
  def _handle_check(self, connection, value, args, request_id):
    base, param, _is_monitor = self._outer._parse_connection(connection)
    if base is None or base not in self._outer._lasers:
      return encode_reply(
          status="UNKNOWN_CONNECTION", request_id=request_id,
          error={
              "code": "unknown_connection",
              "message": "unknown connection '%s'" % connection,
              "retryable": False,
          },
      )
    ctrl = self._outer._lasers[base]
    if not ctrl.isConnected():
      return encode_reply(
          status="ERROR", request_id=request_id,
          error={
              "code": "laser_disconnected",
              "message": "laser disconnected",
              "retryable": True,
          },
      )
    check_param = param if param else 'voltage'
    if check_param not in self._outer.CHECKABLE_PARAMS:
      return encode_reply(
          status="ERROR", request_id=request_id,
          error={
              "code": "unknown_monitor_param",
              "message": ("unknown monitor param '%s' in '%s'"
                          % (check_param, connection)),
              "retryable": False,
          },
      )
    try:
      val, fmt = self._outer._get_monitor_value(ctrl, check_param)
    except Exception:
      return encode_reply(
          status="ERROR", request_id=request_id,
          error={
              "code": "laser_disconnected",
              "message": "laser disconnected",
              "retryable": True,
          },
      )
    if val is None:
      return encode_reply(
          status="ERROR", request_id=request_id,
          error={
              "code": "unknown_monitor_param",
              "message": ("unknown monitor param '%s' in '%s'"
                          % (check_param, connection)),
              "retryable": False,
          },
      )
    self._outer._log("ZMQ: CHECK_VALUE %s -> %s" % (connection, fmt % val))
    return encode_reply(status="SUCCESS", request_id=request_id, value=val)


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
                     'qswitch_mode', 'warmup', 'start_lasing', 'stop',
                     'keep_warm'}
  # Parameter names for CHECK_VALUE / PUB-SUB monitors
  MONITOR_PARAMS = {'temperature', 'voltage', 'lamps', 'shutter', 'qswitch'}
  # Superset: params that CHECK_VALUE can read back (monitors + readable writable state)
  CHECKABLE_PARAMS = MONITOR_PARAMS | {'lamp_mode', 'qswitch_mode'}

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
    if param == 'lamp_mode': return (ctrl.getLampMode(), "%d")
    if param == 'qswitch_mode': return (ctrl.getQSwitchMode(), "%d")
    return (None, "unknown monitor param '%s'" % param)

  def _server_loop(self):
    """Main ZMQ server loop running on the daemon thread.

    PUB-SUB (per-laser monitors + heartbeat) is handled inline against a
    raw zmq.PUB socket. REQ-REP is delegated to ``_BigSkyV2Server``,
    which wraps a ``ZmqRepTransport`` and uses ``serve_once`` to
    block-with-timeout, parse, dispatch via @handler methods, and reply.
    """
    ctx = zmq.Context.instance()

    transport = ZmqRepTransport("tcp://*:%d" % self.rep_port,
                                recv_timeout_ms=250)
    self._v2 = _BigSkyV2Server(self, transport)

    pub_sock = ctx.socket(zmq.PUB)
    pub_sock.bind("tcp://*:%d" % self.pub_port)

    pub_counter = 0

    def publish(topic, value=""):
      msg = "%s %s" % (topic, value) if value else topic
      pub_sock.send_string(msg)

    self._log("ZMQ: server loop running (v2 protocol).")

    # Transport-failure circuit breaker (review I2 2026-05-23). The base
    # class catches handler exceptions and returns ERROR replies, so any
    # exception escaping serve_once is transport-level. We tolerate a few
    # in a row (PUB-side recovers on next iter, REP-side context bounces)
    # but break the loop after MAX_CONSECUTIVE to avoid hot-spinning when
    # the socket is truly dead. v1 also `break`d on any non-Again socket
    # error -- we restore that safety net with a small tolerance window.
    MAX_CONSECUTIVE_TRANSPORT_FAILURES = 5
    consecutive_failures = 0

    while not self._stop_event.is_set():
      # --- PUB-SUB broadcasting ---
      pub_counter += 1

      # All monitors at ~4 Hz (every cycle, since loop is ~250ms)
      for conn_name, ctrl in list(self._lasers.items()):
        if not ctrl.isConnected():
          continue  # don't broadcast stale values for disconnected lasers
        for param in self.MONITOR_PARAMS:
          try:
            val, fmt = self._get_monitor_value(ctrl, param)
            if val is not None:
              publish("%s_%s_monitor" % (conn_name, param), fmt % val)
          except Exception as e:
            # Controller may be mid-destruction during tab close
            self._log("ZMQ: PUB error for %s_%s: %s" % (conn_name, param, e))
            break  # Skip remaining params for this laser

      # Heartbeat at ~1 Hz (every 4th cycle at 250ms = 1s)
      if pub_counter % 4 == 0:
        publish("heartbeat")

      # --- REQ-REP via v2 base class. serve_once blocks up to 250ms
      # on REP; returns False on timeout, True on a dispatched message.
      try:
        self._v2.serve_once(timeout_ms=250)
        consecutive_failures = 0
      except Exception as e:
        consecutive_failures += 1
        self._log("ZMQ: dispatch error (%d/%d): %s"
                  % (consecutive_failures,
                     MAX_CONSECUTIVE_TRANSPORT_FAILURES, str(e)))
        if consecutive_failures >= MAX_CONSECUTIVE_TRANSPORT_FAILURES:
          self._log("ZMQ: too many consecutive transport failures; "
                    "stopping server loop.")
          break

    # Cleanup
    transport.close()
    pub_sock.close()
    self._v2 = None  # release back-ref (review M2 2026-05-23)
    self._log("ZMQ: server loop exited.")


# --- Connection Name Mapping ---
# Maps laser serial numbers to BLACS connection names.
# Edit this dict to match your physical setup.
# Serial numbers are strings as returned by the >sn command.
LASER_SN_TO_CONNECTION = {
  '151': 'YAG_1',
  '213': 'YAG_2',
  # SN 082 (label 'sdfhjkasehkfs' in laserNames.pkl — test/typo entry) and
  # SN 261 (label 'COBRA PUMP' — a different physical laser, not a BLACS-driven YAG)
  # are intentionally omitted. If those COM ports are launched, the
  # _laserLaunchOrder counter fallback applies (identical to pre-2026-05-19 behavior).
  # Populated 2026-05-19 per refactor plan; previously this dict was empty
  # placeholder, which made YAG_N assignment deterministic-by-accident only.
}


class BigSkyHub(QMainWindow):
  def __init__(self):
    super().__init__()
    self.setWindowTitle('Big Sky Controller Hub')
    self.setWindowIcon(QIcon('BigSkyDesktopIcon.ico'))
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
    try:
        with open('laserNames.pkl','rb') as file: self.laserNames=pickle.load(file); file.close()
    except: self.laserNames={}
    self.layout = QGridLayout(self)
    self.buttons=[]
    self.devices=[]
    self.serialNumbers=[]
    self.labelLineEdits=[]
    self.processes={}
    self._parentWidget = parent  # store ref for connecting new buttons

    # Initial scan
    self._scanPorts()

    # Bottom row: text browser, refresh button, save button
    bottomRow = len(self.buttons)
    self.text = QTextBrowser()
    self.layout.addWidget(self.text, bottomRow, 0)
    btnLayout = QVBoxLayout()
    self.refreshButton = QPushButton('Refresh Connections')
    self.refreshButton.setStyleSheet("background-color: #E3F2FD; font-weight: bold;")
    self.refreshButton.pressed.connect(self.refreshConnections)
    btnLayout.addWidget(self.refreshButton)
    self.saveButton = QPushButton('Save Labels')
    self.saveButton.pressed.connect(self.saveLabels)
    btnLayout.addWidget(self.saveButton)
    btnContainer = QWidget()
    btnContainer.setLayout(btnLayout)
    self.layout.addWidget(btnContainer, bottomRow, 1)

  def _scanPorts(self):
    """Scan COM ports for BigSky lasers. Adds new ones to the button list."""
    possibleDevices = [comport.device for comport in serial.tools.list_ports.comports()]
    print(possibleDevices)
    for dev in possibleDevices:
      if dev in self.devices:
        continue  # already known
      try:
        print('trying com port %s' % dev)
        ser = serial.Serial(dev, 9600, timeout=1)
      except:
        print("nope not this one")
        continue
      try:
        ser.flush(); ser.write(b'>sn\n')
        response = ser.read(140).decode('utf-8'); print("response:", response)
        if 'number' in response:
          print("yeah this one."); ser.close()
          sn = response.strip('s// number\r\n')
          self.serialNumbers += [sn]
          self.devices += [dev]
          btn = QPushButton('launch %s ; SN %s' % (dev, sn))
          self.buttons += [btn]
          if sn in self.laserNames.keys():
            self.labelLineEdits += [QLineEdit(self.laserNames[sn])]
          else:
            self.labelLineEdits += [QLineEdit('')]
          self.layout.addWidget(self.buttons[-1], len(self.buttons)-1, 0)
          self.layout.addWidget(self.labelLineEdits[-1], len(self.buttons)-1, 1)
        else:
          ser.close()
      except Exception as e:
        print("scan error for %s: %s" % (dev, e))
        try: ser.close()
        except: pass

  def refreshConnections(self):
    """Re-scan COM ports for newly powered-on BigSky lasers."""
    prevCount = len(self.buttons)
    self._scanPorts()
    newCount = len(self.buttons)
    found = newCount - prevCount

    # Connect new buttons to createTab (via parent MyTableWidget)
    if hasattr(self._parentWidget, 'parent') and hasattr(self._parentWidget, 'tabs'):
      tableWidget = self._parentWidget  # MyTableWidget
      for i in range(prevCount, newCount):
        self.buttons[i].pressed.connect(lambda i=i: tableWidget.createTab(i))

    # Move bottom row widgets down to make room
    bottomRow = len(self.buttons)
    self.layout.addWidget(self.text, bottomRow, 0)
    btnContainer = self.layout.itemAtPosition(prevCount if prevCount > 0 else 0, 1)
    if btnContainer:
      self.layout.addWidget(btnContainer.widget(), bottomRow, 1)

    if found > 0:
      self.text.append("<p style='color: green'>Found %d new laser(s).</p>" % found)
    else:
      self.text.append("<p style='color: gray'>No new lasers found. Ensure lasers are powered on and connected via USB.</p>")
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
    ctrl._tabLabel = labelString  # store original label for disconnect/reconnect styling
    self.parent.zmqServer.registerLaser(connName, ctrl)
    self.homeTab.text.append("ZMQ: laser registered as '%s'" % connName)

    # Connect disconnect/reconnect signal to tab styling
    ctrl.connectionStatusChanged.connect(
        lambda connected, cn=connName: self._onConnectionChanged(cn, connected))

  def _onConnectionChanged(self, connName, connected):
    """Update tab text when a laser connects/disconnects."""
    for idx in range(1, self.tabs.count()):  # skip Home tab at 0
      widget = self.tabs.widget(idx)
      cn = getattr(widget, '_zmqConnectionName', None)
      if cn == connName:
        label = getattr(widget, '_tabLabel', connName)
        if connected:
          self.tabs.setTabText(idx, label)
          self.homeTab.text.append(
              "<p style='color: green'>%s reconnected</p>" % connName)
        else:
          self.tabs.setTabText(idx, "%s (DISCONNECTED)" % label)
          self.homeTab.text.append(
              "<p style='color: red'>%s serial disconnected</p>" % connName)
        break

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
    for i in range(self.tabs.count() - 1, 0, -1):
      print('safely closing tab %d' % i)
      self.closeTab(i)

if __name__ == '__main__':
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(u'BigSkyControllerHub')
    app = QApplication(sys.argv)
    window = BigSkyHub()
    app.aboutToQuit.connect(lambda: (window.zmqServer.stop(), window.table_widget.safeExit()))
    sys.exit(app.exec_())

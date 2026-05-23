# BigSky Laser Controller - Project Context

## Environment

- **Python**: `/c/Users/radmo/miniconda/envs/guis/python.exe` (conda env `guis`)
- **Platform**: Windows 11, bash shell in Claude Code (use forward slashes, Unix syntax)
- **Framework**: PyQt5 with Qt Designer `.ui` files loaded via `uic.loadUiType`

## How to Run

```bash
# Activate environment first
conda activate guis

# Single-laser standalone (for development/testing)
python BigSkyControllerAmbitious.py

# Multi-laser hub (production use)
python HugeSkyController.pyw
```

Without a laser connected, the GUI runs in dummy mode with no serial port.

## Project Structure

| File | Purpose |
|------|---------|
| `HugeSkyController.pyw` | Main hub - discovers lasers on COM ports, creates tabbed interface per laser |
| `BigSkyControllerAmbitious.py` | `SingleLaserController` shell - pyqtSignal declarations + `__init__` (UI wiring + state init). 164 LOC; the bulk lives in the 4 mixins below. |
| `serial_io.py` | `SerialIOMixin` - `_sendCommand` gateway, disconnect/reconnect lifecycle, `safeExit` |
| `remote_bridge.py` | `RemoteBridgeMixin` - ZMQ-to-Qt main-thread bridge (`executeRemoteCommand`, `_handleRemoteCommand` pyqtSlot, six `_remoteSet*` handlers, `_onBlacsHello`) |
| `compound_sequences.py` | `CompoundSequencesMixin` - multi-step state-machine ops (`startLaser`, `stopLaser`, `startWarmup`, `toggleKeepWarm`, `pollTemperature`, `_evaluateKeepWarm`) |
| `laser_commands.py` | `LaserCommandsMixin` - setters/getters/status indicators + `_TRAILING_INT_RE` (~31 methods, includes `_setLampMode` verify-on-readback) |
| `GuiBigSkyWidget.ui` | Qt Designer XML defining the widget layout (loaded by `uic.loadUiType`) |
| `laserNames.pkl` | Pickle file mapping serial numbers to user-assigned laser labels |
| `CalibrationFiles/` | Per-laser calibration CSVs (voltage -> power mapping) |
| `tests/` | B1-B7 canonical-invariant test suite (17 tests, all pass in `guis` env) |
| `docs/bigsky-mixin-extraction-plan.md` | Post-extraction record of the 4-step mixin migration |
| `Big Sky LabView/` | Reference LabView code from SteimleLab (Greg Hall, Nov 2018) |
| `Big Sky YAG Manual.pdf` | Hardware manual for the BigSky Nd:YAG laser |

**Class layout**: `SingleLaserController(SerialIOMixin, RemoteBridgeMixin, CompoundSequencesMixin, LaserCommandsMixin, QtWidgets.QWidget, Ui_Widget)`. MRO resolves methods left-to-right. PyQt5 metaclass constraint: pyqtSignals (`_remoteCommandRequested`, `_blacsHelloReceived`, `connectionStatusChanged`) MUST stay declared on `SingleLaserController` itself — mixins USE them via `self.signal.emit(...)` but cannot DECLARE them.

## Critical Convention: UI Widget Names

The `.ui` file and `.py` file are tightly coupled through `uic.loadUiType`. Widget `name` attributes in the XML become `self.<name>` attributes in Python. If you add a widget to the `.ui` file, you **must** use the exact `objectName` when referencing it in Python, and vice versa. A mismatch causes `AttributeError` at runtime.

## Serial Communication

- **Baud rate**: 9600, timeout 1 second
- **Command format**: `>command\n` (e.g., `>a\n` to activate)
- **Response quirk**: BigSky controller sends `\r\n` *before* the response message (not after). The existing code handles this by reading 140 bytes and stripping `\r\n`. See `SendCommand.vi` notes in the LabView reference for the double-write/double-read approach used historically.

### Serial Error Handling Convention

All serial I/O **must** route through `_sendCommand(cmd_bytes)` in `serial_io.py` (`SerialIOMixin._sendCommand`). This method:
- Returns the response string on success, or `None` on failure
- Catches `SerialException`, `OSError`, `UnicodeDecodeError`
- Calls `_handleDisconnect()` on any serial error

Raw `self.ser.*` calls should only appear in three places — all inside `serial_io.py`:
1. `_sendCommand()` itself
2. `_attemptReconnect()` (needs to test the port directly)
3. `safeExit()` cleanup (wrapped in try/except)

Every caller of `_sendCommand()` must check for `None` return and bail out (typically `if response is None: return`). Response parsing (`int()`, `float()` casts) should be wrapped in `try/except ValueError`.

### Serial Command Reference

| Command | Description | Response format |
|---------|-------------|-----------------|
| `>a` | Activate lamps (start firing) | |
| `>s` | Standby (stop everything) | |
| `>r1` / `>r0` | Open / close shutter | |
| `>pq` / `>sq` | Enable / disable Q-switch | |
| `>oq` | Single Q-switch pulse | |
| `>cg` | Query temperature | `temp. CG XX.X deg` |
| `>v` | Query voltage | `voltage XXXX V` |
| `>vmo####` | Set voltage (e.g., `>vmo0725`) | `voltage m XXXX V` |
| `>f` / `>f####` | Query / set frequency (units: Hz * 100) | `freq. XXXX Hz` |
| `>ene` | Query flashlamp energy | `energy X.XX J` |
| `>sn` | Query serial number | `s/number XXX` |
| `>qsm` / `>qsm0/1/2` | Query / set Q-switch mode (0=internal, 1=burst, 2=external) | `QS mode : X` |
| `>lpm` / `>lpm0/1` | Query / set lamp mode (0=internal, 1=external) | `LP synch : X` |
| `>sav1` | Save current settings to laser EEPROM | |

## Operational Parameters

- **Temperature thresholds**: <37C = too cold (blue), 37-39C = warming (gold), >=39C = operating (green)
- **Default warmup voltage**: 725V (below lasing threshold, safe for warmup)
- **Voltage range**: 500-1400V
- **Max frequency**: 56 Hz (30 Hz is comfortable for external trigger)
- **External trigger**: Pin 4 (+) and Pin 9 (-) on 9-pin serial connector, +5V 100us pulse
- **Warmup procedure**: Fire lamps with shutter closed until temp > 37C. Normal operating temp ~39C.

## Laser States and Safety

The laser has three independent subsystems that must be activated in order:
1. **Lamps** (`>a` / `>s`) - Flashlamps must be firing first
2. **Shutter** (`>r1` / `>r0`) - Shutter can only open when lamps are active
3. **Q-Switch** (`>pq` / `>sq`) - Q-switch can only arm when lamps active AND shutter open

Mode changes (Q-switch mode, lamp mode) require the laser to be in standby first.

## Testing Notes

- The GUI can run without a laser connected (dummy mode) for UI testing
- The `dangerMode` flag is set to `True` on connection and gates Q-switch/single-pulse operations
- `QTimer` is used for temperature polling during warmup (60-second interval)

## BLACS Integration

This program is integrated into the BLACS experiment control system (labscript-suite at `C:\Users\radmo\labscript-suite`).

**Read `C:\Users\radmo\labscript-suite\userlib\user_devices\BLACS_COMMUNICATION_CONTRACT.md` for the full communication protocol** — it defines the ZMQ JSON format (REQ-REP + PUB-SUB), connection naming conventions, and BLACS shot lifecycle.

- **BLACS device code**: `C:\Users\radmo\labscript-suite\userlib\user_devices\BigSkyHub\`
- **Connection table**: `C:\Users\radmo\labscript-suite\userlib\labscriptlib\Main_Experiment\connection_table.py`

**ZMQ ports**: REP on 55540, PUB on 55541 (configurable in `BigSkyZmqServer` constructor).

**Shared connection names** (per laser, e.g. `YAG_1`; must match both this server and the BLACS device):

| Writable (PROGRAM_VALUE) | Value | Description |
|--------------------------|-------|-------------|
| `YAG_1_voltage` | 500-1400 (V) | Set flashlamp voltage |
| `YAG_1_shutter` | 0/1 | Close/open shutter |
| `YAG_1_lamps` | 0/1 | Standby/activate lamps |
| `YAG_1_qswitch` | 0/1 | Disarm/arm Q-switch |
| `YAG_1_lamp_mode` | 0/1 | Internal/external lamp trigger |
| `YAG_1_qswitch_mode` | 0/1/2 | Internal/burst/external Q-switch |
| `YAG_1_warmup` | 0/1 | Stop/start warmup |
| `YAG_1_start_lasing` | any | Full start sequence (a→r1→pq) |
| `YAG_1_stop` | any | Full stop (standby) |
| `YAG_1_keep_warm` | 0/1 | Enable/disable Auto Keep Warm (synced from BLACS) |

| Checkable (CHECK_VALUE only, no PUB) | Value | Description |
|---------------------------------------|-------|-------------|
| `YAG_1_lamp_mode` | 0/1 | Internal/external lamp trigger (read back via CHECK_VALUE) |
| `YAG_1_qswitch_mode` | 0/1/2 | Internal/burst/external Q-switch (read back via CHECK_VALUE) |

| Monitor (CHECK_VALUE + PUB) | Value | Description |
|-----------------------------|-------|-------------|
| `YAG_1_temperature_monitor` | float (C) | Coolant temperature |
| `YAG_1_voltage_monitor` | int (V) | Current voltage |
| `YAG_1_lamps_monitor` | 0/1 | Lamp status |
| `YAG_1_shutter_monitor` | 0/1 | Shutter status |
| `YAG_1_qswitch_monitor` | 0/1 | Q-switch status |

Same pattern for `YAG_2_*`. Typical triggered mode: Q-switch internal (0) + flashlamp external (1).

**Remote command GUI sync convention:** Every function that changes hardware state (e.g., `setFlashLampExternal`, `toggleShutterStatus`) must also update the corresponding GUI widget (radio button, checkbox, label). When called by user click, the widget is already correct. But when called via ZMQ remote command (`_handleRemoteCommand` dispatch), the widget won't update unless the function explicitly sets it. This also applies to disconnect/reconnect transitions — `_handleDisconnect()` and `_handleReconnect()` must update all GUI elements since no user click triggers the change. Failure to sync causes the GUI to show stale state.

## Disconnection Resilience

- **Detection**: Any `_sendCommand()` failure triggers `_handleDisconnect()`, which resets all state and shows "DISCONNECTED" in dark red
- **Auto-reconnect**: `_reconnectTimer` fires every 5 seconds, validates with `>cg` temperature query, calls `_handleReconnect()` on success
- **State restore**: `_handleReconnect()` re-queries voltage, frequency, modes, energy from the laser
- **Signal**: `connectionStatusChanged(bool)` signal emitted on connect/disconnect — hub uses this to gray out / restore tab text
- **ZMQ behavior**: Server returns `{"status": "ERROR", "message": "laser disconnected"}` for CHECK_VALUE/PROGRAM_VALUE when laser is offline; PUB-SUB skips broadcasting for disconnected lasers
- **BLACS handling**: `BigSkyWorker` in labscript-suite gracefully skips "laser disconnected" errors with `logger.warning` (same pattern as "unknown connection")

**If modifying the ZMQ protocol** (connection names, message format, PUB-SUB topics), the BLACS device must also be updated. For BLACS architecture questions (state machines, Qt thread safety, device base classes), defer to the `amo-expert` agent in the labscript-suite workspace (`C:\Users\radmo\labscript-suite\.claude\agents\`).

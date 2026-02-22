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
| `BigSkyControllerAmbitious.py` | `SingleLaserController` class - per-laser control widget (the main code) |
| `GuiBigSkyWidget.ui` | Qt Designer XML defining the widget layout (loaded by `uic.loadUiType`) |
| `laserNames.pkl` | Pickle file mapping serial numbers to user-assigned laser labels |
| `CalibrationFiles/` | Per-laser calibration CSVs (voltage -> power mapping) |
| `Big Sky LabView/` | Reference LabView code from SteimleLab (Greg Hall, Nov 2018) |
| `Big Sky YAG Manual.pdf` | Hardware manual for the BigSky Nd:YAG laser |

## Critical Convention: UI Widget Names

The `.ui` file and `.py` file are tightly coupled through `uic.loadUiType`. Widget `name` attributes in the XML become `self.<name>` attributes in Python. If you add a widget to the `.ui` file, you **must** use the exact `objectName` when referencing it in Python, and vice versa. A mismatch causes `AttributeError` at runtime.

## Serial Communication

- **Baud rate**: 9600, timeout 1 second
- **Command format**: `>command\n` (e.g., `>a\n` to activate)
- **Response quirk**: BigSky controller sends `\r\n` *before* the response message (not after). The existing code handles this by reading 140 bytes and stripping `\r\n`. See `SendCommand.vi` notes in the LabView reference for the double-write/double-read approach used historically.

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

**Remote command GUI sync convention:** Every function that changes hardware state (e.g., `setFlashLampExternal`, `toggleShutterStatus`) must also update the corresponding GUI widget (radio button, checkbox, label). When called by user click, the widget is already correct. But when called via ZMQ remote command (`_handleRemoteCommand` dispatch), the widget won't update unless the function explicitly sets it. Failure to sync causes the GUI to show stale state.

**If modifying the ZMQ protocol** (connection names, message format, PUB-SUB topics), the BLACS device must also be updated. For BLACS architecture questions (state machines, Qt thread safety, device base classes), defer to the `labscript-amo-expert` agent in the labscript-suite workspace (`C:\Users\radmo\labscript-suite\.claude\agents\`).

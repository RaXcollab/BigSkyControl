# BigSky Laser Controller GUI Update - Lab Notes

**Date:** 2026-02-21
**Author:** Claude Code (assisted)
**Project:** BigSkyControl Python GUI

---

## Summary

Updated the BigSky pulsed YAG laser Python GUI to include granular control features previously only available in the older LabView control program (Control.vi). The LabView developer notes (from Greg Hall / SteimleLab, Nov 2018) were extracted from the binary VI files to guide the implementation.

## What Changed

### Files Modified
- `GuiBigSkyWidget.ui` - Added new UI widgets, removed old START/STOP buttons
- `BigSkyControllerAmbitious.py` - Added new control methods, warmup/keep-warm logic, status indicators

### New Features

#### 1. Subsystem Status Panel
Color-coded status indicators for each laser subsystem:
- **LAMPS**: Gray (standby) / Green (firing)
- **SHUTTER**: Gray (closed) / Orange (open - caution)
- **Q-SWITCH**: Gray (disabled) / Red (armed - danger)
- **OVERALL**: Gray (standby) / Gold (warming up) / Green (lamps active) / Red (lasing)
- **TEMPERATURE**: Blue (<37C cold) / Gold (37-39C warming) / Green (>=39C OK)

#### 2. Independent Controls
Three toggle buttons that allow controlling each subsystem independently:
- **ACTIVATE/DEACTIVATE LAMPS** - Sends `>a` or `>s` to toggle flashlamp firing
- **OPEN/CLOSE SHUTTER** - Sends `>r1` or `>r0` (only enabled when lamps active)
- **ARM/DISARM Q-SWITCH** - Sends `>pq` or `>sq` (only enabled when lamps active AND shutter open)
- **SINGLE PULSE** - Sends `>oq` (only enabled in specific conditions: lamps on, shutter open, Q-switch off, internal mode)

#### 3. Compound Operations
- **WARMUP** - Activates lamps with shutter closed and Q-switch disabled. Starts 60-second temperature polling. Per LabView docs: laser needs to reach >37C before lasing, normal operating temp is 39C.
- **START LASING** - Activates lamps, opens shutter, arms Q-switch all at once (replaces old START button)
- **FULL STOP** - Puts laser in standby, stops everything (replaces old STOP button)

#### 4. Keep Warm Toggle
Checkbox that maintains warmup state with automatic temperature polling every 60 seconds (matching LabView's behavior). When enabled:
- Fires lamps with shutter closed
- Polls temperature every 60s
- Logs when laser reaches operating temperature
- Automatically unchecked when START LASING is pressed

#### 5. Safety Interlocks
- Shutter toggle only enabled when lamps are active
- Q-switch toggle only enabled when lamps active AND shutter open
- Mode radio buttons (Q-switch mode, flashlamp mode) locked during active operation
- FULL STOP always available
- Temperature polling timer properly stopped on safe exit

### What Was Removed
- Old `lampActivationButton` (START) - replaced by WARMUP / START LASING / independent controls
- Old `stopButton` (STOP) - replaced by FULL STOP

## Key Reference Information (from LabView)

From the LabView VI developer notes:
- Serial communication uses 9600 baud with double-write/double-read pattern to handle BigSky's `\r\n` pre-termination quirk
- Temperature threshold for "too cool" warning: 37C (shown with blue background in LabView)
- Normal operating temperature: ~39C
- External lamp trigger: Pin 4 (ExtSynchFlash+) and Pin 9 (ExtSynchFlash-) on 9-pin serial connector, +5V 100us pulse
- Default lamp voltage 725V is below lasing threshold (useful for warmup)

## Serial Commands Used
| Command | Description |
|---------|-------------|
| `>a`    | Activate lamps (start firing) |
| `>s`    | Standby (stop everything) |
| `>r1`   | Open shutter |
| `>r0`   | Close shutter |
| `>pq`   | Enable Q-switch |
| `>sq`   | Disable Q-switch |
| `>oq`   | Single Q-switch pulse |
| `>cg`   | Query temperature |
| `>v`    | Query voltage |
| `>vmo####` | Set voltage |
| `>f`/`>f####` | Query/set frequency |
| `>ene`  | Query energy |
| `>sn`   | Query serial number |
| `>qsm0/1/2` | Set Q-switch mode (internal/burst/external) |
| `>lpm0/1` | Set lamp mode (internal/external) |
| `>sav1` | Save laser settings |

## How to Run
```
conda activate guis
cd c:\Users\radmo\Desktop\GUIs\BigSkyControl
python BigSkyControllerAmbitious.py       # standalone single-laser test
python HugeSkyController.pyw              # multi-laser hub
```

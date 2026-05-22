# BigSky `SingleLaserController` Mixin Extraction Plan

**Status**: step 1 of 4 shipped (`refactor/serial-io-mixin` branch, commit
`72c3641` extracts `serial_io.py`). Steps 2-4 documented here for follow-up
execution. See [T0.6 audit](../../../.claude/plans/look-up-all-recent-purrfect-starfish.md)
for the rationale.

## Design recap

`SingleLaserController(QtWidgets.QWidget, Ui_Widget)` was 1072 LOC monolith.
T0.6 audit verdict: **mixins on a single QWidget shell**, not 4 separate
classes — UI widgets are referenced pervasively so we cannot extract a
pure-Qt-free core. Final inheritance after all 4 extractions:

```python
class SingleLaserController(
    SerialIOMixin,           # already shipped (step 1)
    LaserCommandsMixin,      # step 4 (residue, last)
    CompoundSequencesMixin,  # step 3
    RemoteBridgeMixin,       # step 2 (next)
    QtWidgets.QWidget,
    Ui_Widget,
): ...
```

MRO resolves methods left-to-right. Tests (`tests/canonical-invariants/`)
exercise each method via unbound-method invocation against duck-typed
self — they validate that each mixin extraction preserves behavior.

## Step 1 — `serial_io.py` ✅ shipped (commit `72c3641`)

Extracted: `_sendCommand`, `_handleDisconnect`, `_attemptReconnect`,
`_handleReconnect`, `isConnected`, `safeExit`. 17/17 B1-B7 tests pass
against the mixin. BigSkyControllerAmbitious.py: 1072 → 943 LOC.

## Step 2 — `remote_bridge.py` (next; branch `refactor/remote-bridge-mixin`)

**Owns** (move from `BigSkyControllerAmbitious.py`):

- `executeRemoteCommand(command, value, future=None)` (currently ~line 896)
- `_handleRemoteCommand(command, value, future)` (currently ~line 906)
- `_remoteSetVoltage` / `_remoteSetShutter` / `_remoteSetLamps` /
  `_remoteSetQSwitch` / `_remoteSetLampMode` / `_remoteSetQSwitchMode`
  (currently ~lines 953-1054)
- `_onBlacsHello` (currently ~line 160 post-step-1)

**Signal stays on SingleLaserController** (PyQt5 metaclass): 
- `_remoteCommandRequested = pyqtSignal(str, object, object)`
- `_blacsHelloReceived = pyqtSignal()`

Both still declared on `SingleLaserController`; the mixin USES them via
`self._remoteCommandRequested.emit(...)` and `self._blacsHelloReceived...`.

**Host attribute contract** (host must provide):
- `_blacsConnected: bool`, `_lastBlacsContact: float` — state
- `_stateLock: threading.RLock` — protects cached hardware state writes
- All setter methods from `LaserCommandsMixin` (called by `_remoteSet*`)
- All compound methods from `CompoundSequencesMixin`

**Migration**:
1. Create `remote_bridge.py` with `class RemoteBridgeMixin:`
2. Move 9 methods from `BigSkyControllerAmbitious.py` (cut, not copy)
3. Add `from remote_bridge import RemoteBridgeMixin`
4. Update class declaration: `class SingleLaserController(SerialIOMixin, RemoteBridgeMixin, QtWidgets.QWidget, Ui_Widget):`
5. Verify: `python -m pytest tests/` — 17/17 still pass
6. Commit

**Risk**: 1-2 hours of careful refactor. The `_handleRemoteCommand`
dispatch is the brain of the ZMQ path; any typo cascades into ZMQ
disconnect failures. Verify against B5-B6 tests + a manual hub launch.

## Step 3 — `compound_sequences.py` (branch `refactor/compound-sequences-mixin`)

**Owns**:
- `startWarmup` (~line 770 in original, shifted in current)
- `startLaser` (~line 495 in original)
- `stopLaser` (~line 535 in original)
- `toggleKeepWarm` (~line 798)
- `pollTemperature` (~line 824)
- `_evaluateKeepWarm` (~line 841)

**State the mixin reads/writes** (stays in host `__init__`):
- `warmupActive`, `keepWarmActive`, `_warmupTriggered`, `TEMP_COLD`,
  `TEMP_OPERATING`, `lastTemperature`, `tempPollTimer`
- `_blacsConnected` / `_lastBlacsContact` (read, for cool-down)

**Risk**: hysteresis logic is subtle. Verify against B7 byte-sequence
tests + the abort-on-mode-mismatch test.

## Step 4 — `laser_commands.py` (branch `refactor/laser-commands-mixin`)

**Owns** (the residue — everything that's not serial_io, remote_bridge,
or compound_sequences):

- `setFrequency` / `confirmFrequencySetting` / `updateFreq`
- `saveLaserSettings`
- `setQSwitchInternal` / `setQSwitchBurst` / `setQSwitchExternal`
- `_setLampMode` / `setFlashLampInternal` / `setFlashLampExternal`
- `setVoltage` / `confirmVoltageSetting`
- `toggleActiveStatus` / `toggleShutterStatus` / `toggleQSwitchStatus`
- `singlePulse`
- `toggleTerminalInput` / `fetchSerial` / `updateTerminalCommand` / `sendTerminalCommand`
- `updateTemp` / `update_fLampVoltage` / `update_fLampEnergy` /
  `update_fLampMode` / `update_qSwitchMode` / `updateAllStatusIndicators`
- `_setLabelColor` / `_updateTemperatureStatusColor`
- `getVoltage` / `getTemperature` / `getActiveStatus` / `getShutterStatus` /
  `getQSwitchStatus` / `getLampMode` / `getQSwitchMode`

That's ~30 methods. **Largest mixin by far.** May be worth splitting into
two (laser_commands + status_indicators) once the dust settles. Defer that
decision until step 3 is done.

**What stays in `BigSkyControllerAmbitious.py` after step 4**:

- imports + mixin imports
- `_TRAILING_INT_RE` module-level constant
- `Ui_Widget` class loaded from `.ui` file
- `class SingleLaserController(...): __init__` (mostly UI wiring + state init)
- `if __name__ == "__main__":` block

Expected file size: ~250 LOC (~25% of original).

## Cross-cutting verification protocol

After EACH step:

1. Compile: `python -m py_compile *.py`
2. Run unit tests: `conda activate guis && python -m pytest tests/ -v`
3. Launch hub manually: `python HugeSkyController.pyw` (smoke test, ~30s)
4. If hub launches and a laser connects without error, commit.

If any step breaks tests, fix-forward rather than reverting — the commit-
per-mixin discipline makes bisection trivial.

## Open questions

1. **Mixin signal declarations** — `_remoteCommandRequested` could move to
   `RemoteBridgeMixin` if PyQt5's metaclass handles class-attribute signal
   declarations through MRO. Investigate experimentally before step 2; if
   it works, the contract is cleaner.
2. **`laser_commands.py` split** — defer the split-or-keep-as-one decision
   until step 3 is shipped and we can see the coupling table for step 4.
3. **Test harness improvements** — the B7 compound-sequence tests stub
   `_setLampMode` locally. After step 3, those stubs could move to
   `conftest.py` as a shared `make_compound_self` fixture.

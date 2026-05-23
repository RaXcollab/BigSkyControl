# BigSky `SingleLaserController` Mixin Extraction Plan

**Status**: ✅ **All 4 steps shipped (2026-05-22).** This document was
the per-step runbook; below is the post-extraction record. Each step is
✅ shipped against `RaXcollab/BigSkyControl@main` with the cited commit
hash. See [T0.6 audit](../../../.claude/plans/look-up-all-recent-purrfect-starfish.md)
for the rationale.

## Design recap

`SingleLaserController(QtWidgets.QWidget, Ui_Widget)` was a 1072 LOC
monolith. T0.6 audit verdict: **mixins on a single QWidget shell**, not
4 separate classes — UI widgets are referenced pervasively so we cannot
extract a pure-Qt-free core. Final inheritance (post-step-4, matches the
actual code at `BigSkyControllerAmbitious.py:21`):

```python
class SingleLaserController(
    SerialIOMixin,           # step 1 — gateway + reconnect lifecycle
    RemoteBridgeMixin,       # step 2 — ZMQ → Qt main thread bridge
    CompoundSequencesMixin,  # step 3 — multi-step state-machine ops
    LaserCommandsMixin,      # step 4 — residue: setters/getters/status
    QtWidgets.QWidget,
    Ui_Widget,
): ...
```

MRO resolves methods left-to-right. Earlier drafts of this doc reversed
the order of `LaserCommandsMixin` and `RemoteBridgeMixin` — the order
above is the **actual shipped order**. Tests (`tests/`) exercise each
method via real-method binding against duck-typed `self`; the 17/17
B1–B7 suite passed at every commit in the range.

**Final LOC**: `BigSkyControllerAmbitious.py` = 164 (was 1072, 85%
reduction). What stays in `BigSkyControllerAmbitious.py`:

- imports + mixin imports (`from serial_io import ...` × 4)
- `Ui_Widget` class loaded from `.ui` file
- `class SingleLaserController(...)` — pyqtSignal declarations + `__init__`
  (UI wiring + state init)
- `if __name__ == "__main__":` block

(`_TRAILING_INT_RE` module-level constant was MOVED to `laser_commands.py`
along with its sole consumer `_setLampMode` during step 4.)

## Step 1 — `serial_io.py` ✅ shipped (commit `72c3641`)

Extracted: `_sendCommand`, `_handleDisconnect`, `_attemptReconnect`,
`_handleReconnect`, `isConnected`, `safeExit`. 17/17 B1–B7 tests pass.
`BigSkyControllerAmbitious.py`: 1072 → 943 LOC.

Branch `refactor/serial-io-mixin` merged into `main` via `--no-ff` at
`70a81ba`.

## Step 2 — `remote_bridge.py` ✅ shipped (commit `b75b27e`)

Extracted: `executeRemoteCommand`, `_handleRemoteCommand` (the
pyqtSlot ZMQ-to-Qt bridge), six `_remoteSet*` command handlers, and
`_onBlacsHello`. 17/17 B1–B7 tests still pass. `BigSkyControllerAmbitious.py`:
943 → 777 LOC.

**Signals stay declared on `SingleLaserController`** (PyQt5 metaclass
constraint — class-attribute pyqtSignal declarations must live on the
QWidget shell, not on a plain mixin):

- `_remoteCommandRequested = pyqtSignal(str, object, object)`
- `_blacsHelloReceived = pyqtSignal()`

The mixin USES them via `self._remoteCommandRequested.emit(...)` and
`self._blacsHelloReceived...`.

Branch `refactor/remote-bridge-mixin` merged into `main` via `--no-ff`
at `dc3fc37`.

## Step 3 — `compound_sequences.py` ✅ shipped (commit `cf1e074`)

Extracted: `startWarmup`, `startLaser`, `stopLaser`, `toggleKeepWarm`,
`pollTemperature`, `_evaluateKeepWarm`. Hardware-safety byte sequences
(B7) preserved verbatim. 17/17 B1–B7 tests still pass. `BigSkyControllerAmbitious.py`:
777 → 631 LOC.

Branch `refactor/compound-sequences-mixin` merged into `main` via
`--no-ff` at `b7296b2`.

## Step 4 — `laser_commands.py` ✅ shipped (commit `86090a2`)

The residue — 31 methods including all setters/getters, `_setLampMode`
+ wrappers, voltage/Q-switch toggles, status indicators, terminal-mode
methods, status-bar paint, and thread-safe accessors. `BigSkyControllerAmbitious.py`:
631 → 164 LOC.

**Workflow note**: step 4 commit `86090a2` landed direct-on-`main`
rather than via a topic-branch `--no-ff` merge, due to a Windows
filesystem-state propagation race between the branch-creation and
commit (see the 2026-05-22 session diagnostics — git on Windows can
fail to recognize a fresh `checkout -b` if a subsequent commit runs in
the same fast subprocess sequence). The work itself was test-gated
(17/17 still pass) so the safety net held, but **this is a workflow
drift** — do not normalize it. Steps 1–3 landed via the canonical
`refactor/<name>-mixin → main` per-item-ship pattern; step 4 should
have too. Future mixin work follows the per-item ship discipline.

## Cross-cutting verification protocol (used at every step)

1. Compile: `python -m py_compile *.py`
2. Run unit tests: `conda activate guis && python -m pytest tests/ -v`
3. Launch hub manually: `python HugeSkyController.pyw` (smoke test, ~30s)
4. If hub launches and a laser connects without error, commit.

(`pytest` is installed in the `guis` env, not in `labscript`. The CI
hook proposed in item 2.8c should activate `guis` before invoking
`pytest`.)

## Open questions (resolved)

1. **Mixin signal declarations** — *Resolved*: signals STAY on
   `SingleLaserController`. PyQt5's metaclass requires class-attribute
   pyqtSignal declarations on the QObject subclass; mixin-class
   declarations don't propagate through MRO cleanly. The current
   pattern (signals on host, `self.emit()` from mixins) is the right
   one. No experiment needed.
2. **`laser_commands.py` split** — *Resolved-as-deferred*: shipped as a
   single mixin (521 LOC). May be worth splitting into
   `laser_commands` + `status_indicators` once test coverage grows
   beyond the current B1–B7 set, but no current pressure.
3. **Test harness improvements** — *Tracked TODO*: the B7
   compound-sequence tests still stub `_setLampMode` locally. A future
   "B8" with widget mocks would close the verify-on-readback coverage
   hole at `laser_commands.py:_setLampMode` (~line 118-160). Defer
   until item 2.8c CI hook lands so the cost of widget mocking can be
   amortized across the multi-GUI test infrastructure.

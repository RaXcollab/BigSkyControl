"""B7: compound startWarmup / startLaser pin the byte sequence to the manual.

Per `Big Sky YAG Manual.pdf` + `BigSkyControl/CLAUDE.md` 'Laser States':
  - startWarmup: standby -> lamp mode internal -> activate lamps
        bytes: >s, >lpm0, >a
  - startLaser:  standby -> lamp mode external -> activate -> shutter -> qswitch
        bytes: >s, >lpm1, >a, >r1, >pq

These tests assert the EXACT byte sequence the gateway sees, in order. A
reorder (e.g. >a before >lpm1) is a hardware-state violation -- mode
switches require standby (lamps off).

Implementation note: production `startWarmup`/`startLaser` call multiple
methods on self (_sendCommand, setFlashLampInternal/External which internally
go through _setLampMode). SimpleNamespace doesn't dispatch through the class,
so we assign stand-ins directly on the instance. _setLampMode is stubbed
(not tested here -- that's a separate H8 candidate); the stand-in faithfully
records the byte the real method would have sent and updates flashLampMode
on assumed success. This isolates the test to startWarmup/startLaser's
own dispatch logic.

Run:
    conda activate guis && pytest GUIs/BigSkyControl/tests/test_compound_sequences.py -v
"""
from __future__ import annotations

from unittest import mock


def _recording_send(state):
    """Stand-in for `_sendCommand`. Returns ">ok\\r\\n" so the real method
    proceeds past `if response is None: return`."""
    def _send(cmd):
        state["sent"].append(bytes(cmd) if isinstance(cmd, str) else cmd)
        return ">ok\r\n"
    return _send


def _install_stubs(s, state, lamp_mode_after_setlamp):
    """Wire up _sendCommand, _setLampMode, setFlashLamp{Internal,External}
    on the SimpleNamespace `s`. The lamp_mode_after_setlamp param drives
    `flashLampMode` after the lamp-mode stand-in fires (used to test the
    abort-on-mismatch branch)."""
    s._sendCommand = _recording_send(state)

    def _set_lamp_mode(target, cmd_label):
        cmd_bytes = ('>%s\n' % cmd_label).encode('ascii')
        s._sendCommand(cmd_bytes)
        # Production verifies actual via response parse; here we just take
        # `lamp_mode_after_setlamp` as the post-call state. For the success
        # path the test sets it equal to `target`; for abort tests, mismatched.
        s.flashLampMode = lamp_mode_after_setlamp
        return {"status": "SUCCESS"} if lamp_mode_after_setlamp == target else \
               {"status": "ERROR", "message": "rejected: did not take effect"}

    s._setLampMode = _set_lamp_mode
    s.setFlashLampInternal = lambda: s._setLampMode(0, 'lpm0')
    s.setFlashLampExternal = lambda: s._setLampMode(1, 'lpm1')


def test_B7_startWarmup_byte_sequence(bsa_module, make_controller_self):
    """>s -> >lpm0 -> >a, in that exact order."""
    s = make_controller_self(connected=True)
    state = {"sent": []}
    _install_stubs(s, state, lamp_mode_after_setlamp=0)  # success case
    s.flashLampMode = 0

    bsa_module.SingleLaserController.startWarmup(s)

    cmds = [c for c in state["sent"] if c.startswith(b">")]
    assert any(c == b">s\n" for c in cmds), "missing standby (>s): " + str(cmds)
    assert any(c == b">lpm0\n" for c in cmds), "missing internal-lamp-mode (>lpm0): " + str(cmds)
    assert any(c == b">a\n" for c in cmds), "missing activate (>a): " + str(cmds)
    # Order: standby before mode-switch before activate.
    idx_s = next(i for i, c in enumerate(cmds) if c == b">s\n")
    idx_lpm = next(i for i, c in enumerate(cmds) if c == b">lpm0\n")
    idx_a = next(i for i, c in enumerate(cmds) if c == b">a\n")
    assert idx_s < idx_lpm < idx_a, "order violation in startWarmup: " + str(cmds)


def test_B7_startLaser_byte_sequence(bsa_module, make_controller_self):
    """>s -> >lpm1 -> >a -> >r1 -> >pq. Mode switch BEFORE activate is the
    hardware-safety invariant: mode commands are rejected while lamps fire."""
    s = make_controller_self(connected=True)
    state = {"sent": []}
    _install_stubs(s, state, lamp_mode_after_setlamp=1)  # external mode success
    s.flashLampMode = 1
    s.dangerMode = True  # qSwitch arm gated on this

    bsa_module.SingleLaserController.startLaser(s)

    cmds = [c for c in state["sent"] if c.startswith(b">")]
    expected = [b">s\n", b">lpm1\n", b">a\n", b">r1\n", b">pq\n"]
    for needle in expected:
        assert any(c == needle for c in cmds), "startLaser missing " + repr(needle) + ": " + str(cmds)
    positions = [next(i for i, c in enumerate(cmds) if c == needle) for needle in expected]
    assert positions == sorted(positions), (
        "startLaser byte order violated. expected " + str(expected) + ", got " + str(cmds))


def test_B7_startWarmup_aborts_if_mode_switch_fails(bsa_module,
                                                     make_controller_self):
    """If lamp mode is not 0 after `>lpm0` (controller refused), `>a` MUST
    NOT be sent -- firing lamps in the wrong mode is a hardware error."""
    s = make_controller_self(connected=True)
    state = {"sent": []}
    # Lamp-mode stand-in returns mismatch: target=0 but actual stays 1.
    _install_stubs(s, state, lamp_mode_after_setlamp=1)
    s.flashLampMode = 1

    bsa_module.SingleLaserController.startWarmup(s)
    cmds = [c for c in state["sent"] if c.startswith(b">")]
    assert not any(c == b">a\n" for c in cmds), (
        "startWarmup must abort before >a when mode mismatched, got " + str(cmds))


if __name__ == "__main__":
    import sys
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))

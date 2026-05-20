# BigSkyControl
Derived from Alex Brinson's [BigSkyControlAmbitious repo](https://github.com/alexjbrinson/BigSkyControlAmbitious):

Like the old Big Sky Gui, but capable of controlling multiple lasers

**Quick start:** 
- Check that the chiller (the chongus box) is filled with distilled water.
- To turn on the laser, flip the switch on the chiller. You’ll know it’s on if it’s being loud.
- There should be a custom cable connected to the back of the chiller. 
- The other end of the custom cable should branch out into a serial connector, plus two BNCs. The serial output should be connected to a computer using a USB to RS-232 cable, e.g. https://www.usbgear.com/FTDI-LED.html. 
- If you intend to externally trigger the laser head, then connect the BNCs to a pulse generator, see external trigger section for more details.
- Using a terminal window, navigate to the `BigSkyControl` directory, then launch the multi-laser hub with `pythonw HugeSkyController.pyw` (preferred; `BigSkyControllerAmbitious.py` is the per-laser widget loaded by the hub, not a standalone entry point — the historical single-laser `BigSkyControllerSimplified.py` no longer exists). PyQt5 + pyqtgraph + pyserial required. The `guis` conda env on this machine already has them.

<img width="458" height="269" alt="image" src="https://github.com/user-attachments/assets/d1972fc9-98da-4e4e-95f1-1e3e7476e446" />

**GUI Overview:**
The Big Sky lasers are generally controlled through a set of commands, a subset of which have been implemented into the GUI shown above. At the top, the Q-switch and flashlamps can be set to trigger internally or externally. In the context of a CRIS/RISE run, we'll want to trigger externally via a Quantum Composer pulse generator.

To the right of these radio buttons is a spin box that allows you to adjust the laser’s pulse rate. This only applies if the flashlamp is triggering internally. The option is disabled otherwise.
 
Below the trigger settings are controls for adjusting the flashlamp voltage. This is how the output power is adjusted.  Typically, the YAG should start lasing at around 750V, and the power output may become unstable around 1200V, so you will likely want to work within that range.

A calibration curve is plotted on the right side of the GUI, showing the average power at a pulse rate of 50 Hz, as a function of the flashlamp voltage. The calibration data is pulled from per-laser CSVs in `CalibrationFiles/` (e.g. `CalibrationDataBigSky082.csv`, `CalibrationDataBigSky261.csv`, plus a default `CalibrationDataBigSky.csv` fallback). Each laser head should have its own calibration file named with its serial number, as they all differ slightly. It’s probably also a good idea to re-calibrate periodically as the laser ages. If a per-SN file is missing, the default is used and a message is printed to the terminal.
 
To fire the laser, 3 conditions must be met: 1) The flashlamp must be firing (terminal command "a"), 2) the shutter must be open ("r1" to open shutter, "r0" to close it), and 3), the q-switch must be pulsed ("pq" for continuous pulsing). These operations are all carried out in succession with the START button. But just so you know…

The beam can be stopped with the giant stop button ("s"), which will close the shutter and de-activate both the flashlamp and the q-switch.

At the bottom of the GUI is the option to send terminal commands directly to the laser. This should not be necessary, but the list of commands is attached just in case.

**External Triggering:**
The trigger signals should be TTL high. The optimal delay between flashlamp and q-switch triggers is about 135μs. Our pulse widths are 1 μs long, and this seems to work fine. The pulse rate should be no higher than 56 Hz

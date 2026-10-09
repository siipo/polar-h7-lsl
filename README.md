# Polar H7 / H10 → LSL

Streams heart rate and RR intervals (H7, H10) plus raw ECG and acceleration (H10) from a Polar chest strap over Bluetooth LE to
[Lab Streaming Layer](https://labstreaminglayer.org), so it can be recorded with LabRecorder.
Also writes CSV backups.

## LSL streams

Stream names default to `Polar<model>` (`PolarH7`, `PolarH10`, ...).

### Heart rate (all straps)

Name `PolarH7` / `PolarH10`, type `HeartRate`, irregular rate, one sample per heartbeat:

| Channel | Unit | Notes |
|---|---|---|
| HR | bpm | heart rate reported by the strap |
| RR | ms | interval to the previous beat (NaN if the packet had none) |
| Contact | 0/1 | skin contact (NaN if not reported) |

RR samples are back-dated to their own beat time.

### Raw signals (H10 only, untested)

Started automatically when the strap has the Polar Measurement Data service:

| Stream | Type | Rate | Channels | Unit |
|---|---|---|---|---|
| `PolarH10_ECG` | `ECG` | 130 Hz | ECG | µV |
| `PolarH10_ACC` | `Accelerometer` | 200 Hz (25/50/100/200) | X, Y, Z | mg |

Each BLE packet is pushed as a chunk stamped with its arrival time; earlier samples are
back-dated at the nominal rate. Use dejittering when loading the XDF.

## CSV backup

Written to `recordings/` next to the script / exe, flushed after every packet:

| File | Columns |
|---|---|
| `PolarH10_<date>_<time>.csv` | `lsl_time, wall_time, hr_bpm, rr_ms, contact` |
| `..._ECG.csv` | `lsl_time, sensor_time_ns, ecg_uV` |
| `..._ACC.csv` | `lsl_time, sensor_time_ns, x_mg, y_mg, z_mg` |

`sensor_time_ns` is the strap's own clock (ns since 2000-01-01).

## Usage

Standalone: download `PolarH7_LSL.exe` from Releases and double-click it.

From source:

```
pip install -r requirements.txt
python polar_h7_lsl.py
```

Options:

| | |
|---|---|
| `--address XX:XX:...` | connect to a specific strap, skip scanning |
| `--scan` | list nearby BLE devices and exit |
| `--stream-name NAME` | LSL stream name prefix (default `Polar<model>`) |
| `--csv PATH` / `--no-csv` | CSV path / disable CSV |
| `--no-ecg` / `--no-acc` | H10: skip raw ECG / accelerometer |
| `--acc-rate HZ` / `--acc-range G` | H10 accelerometer: 25/50/100/200 Hz, 2/4/8 g (default 200 Hz, 8 g) |
| `--no-reconnect` | exit on disconnect instead of reconnecting |

## Notes

- Wear and moisten the strap so it wakes up. Disconnect it from phones / Polar apps first.
- Some H7 units advertise as "Polar HR Sensor"; the scanner matches any `Polar*` name or the
  Heart Rate service UUID.
- Run only one instance at a time: Windows shares the BLE connection, so multiple instances
  produce duplicate `PolarH7` streams.
- The H7 only provides HR + RR; ECG/ACC are skipped automatically.
- Raw ECG needs good electrode contact; the H10 stops ECG/ACC streaming when it disconnects.

## Building the exe

Run `build_exe.bat` → `dist\PolarH7_LSL.exe`.

# Polar H7 → LSL

Streams heart rate and RR intervals from a Polar H7 chest strap over Bluetooth LE to
[Lab Streaming Layer](https://labstreaminglayer.org), so it can be recorded with LabRecorder.
Also writes a CSV backup.

## LSL stream

One stream, name `PolarH7`, type `HeartRate`, irregular rate, one sample per heartbeat:

| Channel | Unit | Notes |
|---|---|---|
| HR | bpm | heart rate reported by the strap |
| RR | ms | interval to the previous beat (NaN if the packet had none) |
| Contact | 0/1 | skin contact (NaN if not reported) |

RR samples are back-dated to their own beat time.

## CSV backup

Written to `recordings/PolarH7_<date>_<time>.csv` next to the script / exe, flushed every row.
Columns: `lsl_time, wall_time, hr_bpm, rr_ms, contact`.

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
| `--stream-name NAME` | LSL stream name (default `PolarH7`) |
| `--csv PATH` / `--no-csv` | CSV path / disable CSV |
| `--no-reconnect` | exit on disconnect instead of reconnecting |

## Notes

- Wear and moisten the strap so it wakes up. Disconnect it from phones / Polar apps first.
- Some H7 units advertise as "Polar HR Sensor"; the scanner matches any `Polar*` name or the
  Heart Rate service UUID.
- Run only one instance at a time: Windows shares the BLE connection, so multiple instances
  produce duplicate `PolarH7` streams.
- The H7 only provides HR + RR. Raw ECG requires a Polar H10.

## Building the exe

Run `build_exe.bat` → `dist\PolarH7_LSL.exe`.

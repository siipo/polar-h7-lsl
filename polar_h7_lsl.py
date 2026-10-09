"""
Polar H7 / H10 -> Lab Streaming Layer (LSL) bridge.

Connects to a Polar chest strap over Bluetooth LE and publishes LSL streams
(stream names default to "Polar<model>", e.g. PolarH7 / PolarH10):

  Polar<model>       type "HeartRate", irregular rate, one sample per beat
      ch0  HR       bpm   heart rate reported by the strap
      ch1  RR       ms    RR interval of this beat (NaN if the packet had no RR)
      ch2  Contact  0/1   skin contact (NaN if the strap doesn't report it)

  H10 only (Polar Measurement Data service):
  Polar<model>_ECG   type "ECG", 130 Hz, 1 channel, microvolts
  Polar<model>_ACC   type "Accelerometer", 25-200 Hz, X/Y/Z, milli-g

Each stream also gets a CSV backup (flushed after every packet).

Record with LabRecorder (or any LSL consumer).

Usage:
    python polar_h7_lsl.py                     # scan for first Polar HR device
    python polar_h7_lsl.py --address XX:XX:...  # connect to a specific device
    python polar_h7_lsl.py --csv my_session.csv # choose CSV path (default: recordings/...)
    python polar_h7_lsl.py --no-csv            # LSL only
    python polar_h7_lsl.py --no-ecg --no-acc   # H10: heart rate only
    python polar_h7_lsl.py --scan              # list nearby BLE devices and exit
"""

import argparse
import asyncio
import csv
import math
import struct
import sys
from datetime import datetime
from pathlib import Path

from bleak import BleakClient, BleakScanner
from pylsl import StreamInfo, StreamOutlet, local_clock, IRREGULAR_RATE, cf_float32

HR_SERVICE_UUID = "0000180d-0000-1000-8000-00805f9b34fb"
HR_MEASUREMENT_UUID = "00002a37-0000-1000-8000-00805f9b34fb"
BATTERY_LEVEL_UUID = "00002a19-0000-1000-8000-00805f9b34fb"

# Polar Measurement Data (PMD) service: raw ECG / accelerometer. H10, OH1, Verity Sense only.
PMD_SERVICE_UUID = "fb005c80-02e7-f387-1cad-8acd2d8df0c8"
PMD_CONTROL_UUID = "fb005c81-02e7-f387-1cad-8acd2d8df0c8"
PMD_DATA_UUID = "fb005c82-02e7-f387-1cad-8acd2d8df0c8"

PMD_ECG = 0x00
PMD_ACC = 0x02
PMD_OP_START = 0x02
PMD_SETTING_RATE = 0x00
PMD_SETTING_RESOLUTION = 0x01
PMD_SETTING_RANGE = 0x02
PMD_ERRORS = {
    1: "invalid op code", 2: "invalid measurement type", 3: "not supported",
    4: "invalid length", 5: "invalid parameter", 6: "already started",
    7: "invalid resolution", 8: "invalid sample rate", 9: "invalid range",
    10: "invalid MTU", 11: "invalid number of channels", 12: "invalid state",
    13: "device in charger",
}

ECG_RATE = 130  # the H10 only supports 130 Hz ECG
ECG_RESOLUTION = 14
ACC_RESOLUTION = 16

HR_CHANNELS = [("HR", "bpm"), ("RR", "ms"), ("Contact", "bool")]


def parse_hr_measurement(data: bytearray):
    """Parse a Heart Rate Measurement (0x2A37) notification -> (bpm, [rr_ms, ...], contact)."""
    flags = data[0]
    offset = 1
    if flags & 0x01:  # HR value is uint16
        hr = struct.unpack_from("<H", data, offset)[0]
        offset += 2
    else:
        hr = data[offset]
        offset += 1

    contact_supported = bool(flags & 0x04)
    contact = bool(flags & 0x02) if contact_supported else None

    if flags & 0x08:  # energy expended present
        offset += 2

    rr = []
    if flags & 0x10:  # RR intervals present, units of 1/1024 s
        while offset + 1 < len(data):
            raw = struct.unpack_from("<H", data, offset)[0]
            rr.append(raw * 1000.0 / 1024.0)
            offset += 2
    return hr, rr, contact


def pmd_start_command(measurement, settings):
    """Build a PMD 'start measurement' command; settings is a list of (setting_type, value)."""
    cmd = bytearray([PMD_OP_START, measurement])
    for setting, value in settings:
        cmd += bytes([setting, 1]) + struct.pack("<H", value)
    return cmd


def decode_delta_frame(payload, channels, ref_bytes):
    """Decode a PMD delta-compressed frame: a reference sample followed by bit-packed delta blocks."""
    ref = [int.from_bytes(payload[i * ref_bytes:(i + 1) * ref_bytes], "little", signed=True)
           for i in range(channels)]
    samples = [ref]
    offset = channels * ref_bytes
    while offset + 2 <= len(payload):
        bits, count = payload[offset], payload[offset + 1]
        offset += 2
        nbytes = (bits * channels * count + 7) // 8
        packed = int.from_bytes(payload[offset:offset + nbytes], "little")
        offset += nbytes
        pos = 0
        for _ in range(count):
            prev = samples[-1]
            sample = []
            for c in range(channels):
                delta = 0
                if bits:
                    delta = (packed >> pos) & ((1 << bits) - 1)
                    pos += bits
                    if delta & (1 << (bits - 1)):
                        delta -= 1 << bits
                sample.append(prev[c] + delta)
            samples.append(sample)
    return samples


def parse_ecg(frame_type, payload):
    """ECG frame -> list of [microvolts]."""
    if frame_type != 0x00:
        raise ValueError(f"unsupported ECG frame type 0x{frame_type:02x}")
    return [[int.from_bytes(payload[i:i + 3], "little", signed=True)]
            for i in range(0, len(payload) - 2, 3)]


def parse_acc(frame_type, payload):
    """Accelerometer frame -> list of [x, y, z] in milli-g."""
    if frame_type & 0x80:  # delta compressed
        return decode_delta_frame(payload, 3, ACC_RESOLUTION // 8)
    size = {0x00: 1, 0x01: 2, 0x02: 3}.get(frame_type)
    if size is None:
        raise ValueError(f"unsupported ACC frame type 0x{frame_type:02x}")
    step = 3 * size
    return [[int.from_bytes(payload[i + k * size:i + (k + 1) * size], "little", signed=True)
             for k in range(3)]
            for i in range(0, len(payload) - step + 1, step)]


def detect_model(name: str) -> str:
    upper = name.upper()
    for model in ("H10", "H9", "OH1"):
        if model in upper:
            return model
    if "SENSE" in upper:
        return "VeritySense"
    return "H7"  # older H7 units advertise as "Polar H7 ..." or just "Polar HR Sensor"


def make_outlet(name, stype, channels, srate, source_id, model):
    info = StreamInfo(name, stype, len(channels), srate, cf_float32, source_id)
    chans = info.desc().append_child("channels")
    for label, unit in channels:
        ch = chans.append_child("channel")
        ch.append_child_value("label", label)
        ch.append_child_value("unit", unit)
        ch.append_child_value("type", label)
    info.desc().append_child("acquisition").append_child_value("manufacturer", "Polar")
    info.desc().child("acquisition").append_child_value("model", model)
    return StreamOutlet(info)


def app_dir() -> Path:
    """Folder of the .exe when frozen (PyInstaller), otherwise of this script."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).parent


def open_csv(path: Path, header):
    path.parent.mkdir(parents=True, exist_ok=True)
    f = open(path, "w", newline="", encoding="utf-8")
    w = csv.writer(f)
    w.writerow(header)
    f.flush()
    return f, w


class SignalSink:
    """LSL outlet + optional CSV for one regular-rate PMD signal (ECG or ACC)."""

    def __init__(self, name, stype, channels, srate, source_id, model, csv_path):
        self.outlet = make_outlet(name, stype, channels, srate, source_id, model)
        self.srate = srate
        self.count = 0
        self.file = self.writer = None
        if csv_path:
            header = ["lsl_time", "sensor_time_ns"] + [f"{label.lower()}_{unit}" for label, unit in channels]
            self.file, self.writer = open_csv(csv_path, header)
        print(f"LSL stream up: {name} ({srate} Hz, channels: {', '.join(c for c, _ in channels)})"
              + (f"\nCSV backup: {csv_path.resolve()}" if csv_path else ""))

    def push(self, samples, recv_time, sensor_time_ns):
        """Push one frame. Both clocks refer to the last sample; earlier ones are spaced at 1/srate."""
        if not samples:
            return
        # With a single timestamp, liblsl back-dates the rest of the chunk using the nominal rate.
        self.outlet.push_chunk([[float(v) for v in s] for s in samples], recv_time)
        self.count += len(samples)
        if self.writer:
            dt = 1.0 / self.srate
            n = len(samples)
            for i, s in enumerate(samples):
                back = (n - 1 - i) * dt
                self.writer.writerow([f"{recv_time - back:.6f}", sensor_time_ns - round(back * 1e9), *s])
            self.file.flush()

    def close(self):
        if self.file:
            self.file.close()


class Session:
    """Owns the LSL outlets and CSV files; survives reconnects."""

    def __init__(self, args, source_id, model, stream_name):
        self.args = args
        self.source_id = source_id
        self.model = model
        self.stream_name = stream_name
        self.stamp = datetime.now()
        self.ecg = self.acc = None
        self.pmd_responses = asyncio.Queue()
        self.warned = set()

        self.hr_outlet = make_outlet(stream_name, "HeartRate", HR_CHANNELS, IRREGULAR_RATE, source_id, model)
        print(f"LSL stream up: {stream_name} (channels: {', '.join(c for c, _ in HR_CHANNELS)})")
        self.hr_file = self.hr_writer = None
        if args.csv_enabled:
            path = self.csv_path("")
            self.hr_file, self.hr_writer = open_csv(path, ["lsl_time", "wall_time", "hr_bpm", "rr_ms", "contact"])
            print(f"CSV backup: {path.resolve()}")
        # LSL clock -> wall clock offset, so CSV rows also get a human-readable time.
        self.wall_offset = datetime.now().timestamp() - local_clock()

    def csv_path(self, suffix):
        if not self.args.csv_enabled:
            return None
        base = Path(self.args.csv) if self.args.csv else (
            app_dir() / "recordings" / f"{self.stream_name}_{self.stamp:%Y%m%d_%H%M%S}.csv")
        return base.with_name(f"{base.stem}{suffix}{base.suffix or '.csv'}")

    # --- heart rate -------------------------------------------------------

    def on_hr(self, _sender, data: bytearray):
        ts = local_clock()
        hr, rr, contact = parse_hr_measurement(data)
        contact_val = math.nan if contact is None else float(contact)

        # Back-date RR intervals within a packet so each beat gets its own timestamp;
        # the last RR in the packet is assigned the reception time.
        samples = []
        t = ts
        for interval in reversed(rr):
            samples.append((t, interval))
            t -= interval / 1000.0
        samples.reverse()
        if not samples:
            samples = [(ts, math.nan)]

        for stamp, interval in samples:
            self.hr_outlet.push_sample([float(hr), interval, contact_val], stamp)
            if self.hr_writer:
                wall = datetime.fromtimestamp(stamp + self.wall_offset).isoformat(timespec="milliseconds")
                self.hr_writer.writerow([f"{stamp:.6f}", wall, hr,
                                         "" if math.isnan(interval) else f"{interval:.2f}",
                                         "" if contact is None else int(contact)])
        if self.hr_file:
            self.hr_file.flush()

        contact_str = "" if contact is None else ("  contact" if contact else "  NO CONTACT")
        rr_str = ", ".join(f"{x:.0f}" for x in rr)
        extra = ""
        for label, sink in (("ECG", self.ecg), ("ACC", self.acc)):
            if sink:
                extra += f"  {label} +{sink.count}"
                sink.count = 0
        print(f"HR {hr:3d} bpm  RR [{rr_str}] ms{contact_str}{extra}")

    # --- PMD (H10 raw ECG / accelerometer) ---------------------------------

    def on_pmd_control(self, _sender, data: bytearray):
        self.pmd_responses.put_nowait(bytes(data))

    def on_pmd_data(self, _sender, data: bytearray):
        recv = local_clock()
        if len(data) < 10:
            return
        measurement = data[0]
        sensor_ns = struct.unpack_from("<Q", data, 1)[0]  # timestamp of the last sample in the frame
        frame_type = data[9]
        payload = bytes(data[10:])
        try:
            if measurement == PMD_ECG and self.ecg:
                self.ecg.push(parse_ecg(frame_type, payload), recv, sensor_ns)
            elif measurement == PMD_ACC and self.acc:
                self.acc.push(parse_acc(frame_type, payload), recv, sensor_ns)
        except Exception as e:
            key = (measurement, frame_type)
            if key not in self.warned:
                self.warned.add(key)
                print(f"Could not decode PMD frame (measurement {measurement}, frame type 0x{frame_type:02x}): {e}")

    async def pmd_start(self, client, command, label):
        while not self.pmd_responses.empty():
            self.pmd_responses.get_nowait()
        await client.write_gatt_char(PMD_CONTROL_UUID, command, response=True)
        try:
            resp = await asyncio.wait_for(self.pmd_responses.get(), 5)
        except asyncio.TimeoutError:
            print(f"{label}: no response from strap")
            return False
        status = resp[3] if len(resp) > 3 else -1
        if resp[0] == 0xF0 and status in (0, 6):
            return True
        print(f"{label}: strap refused to start ({PMD_ERRORS.get(status, f'status {status}')})")
        return False

    async def start_pmd(self, client):
        if client.services.get_service(PMD_SERVICE_UUID) is None:
            print("No raw ECG/accelerometer service on this strap (H7 only provides HR + RR).")
            return
        await client.start_notify(PMD_CONTROL_UUID, self.on_pmd_control)
        await client.start_notify(PMD_DATA_UUID, self.on_pmd_data)

        if self.args.ecg:
            cmd = pmd_start_command(PMD_ECG, [(PMD_SETTING_RATE, ECG_RATE),
                                              (PMD_SETTING_RESOLUTION, ECG_RESOLUTION)])
            if await self.pmd_start(client, cmd, "ECG") and self.ecg is None:
                self.ecg = SignalSink(f"{self.stream_name}_ECG", "ECG", [("ECG", "uV")], ECG_RATE,
                                      f"{self.source_id}_ecg", self.model, self.csv_path("_ECG"))
        if self.args.acc:
            cmd = pmd_start_command(PMD_ACC, [(PMD_SETTING_RATE, self.args.acc_rate),
                                              (PMD_SETTING_RESOLUTION, ACC_RESOLUTION),
                                              (PMD_SETTING_RANGE, self.args.acc_range)])
            if await self.pmd_start(client, cmd, "ACC") and self.acc is None:
                self.acc = SignalSink(f"{self.stream_name}_ACC", "Accelerometer",
                                      [("X", "mg"), ("Y", "mg"), ("Z", "mg")], self.args.acc_rate,
                                      f"{self.source_id}_acc", self.model, self.csv_path("_ACC"))

    def close(self):
        if self.hr_file:
            self.hr_file.close()
        for sink in (self.ecg, self.acc):
            if sink:
                sink.close()


async def find_device(name_filter: str, timeout: float):
    print(f"Scanning for Polar / heart-rate devices ({timeout:.0f}s)... make sure the strap is worn/moistened.")
    # Some H7 units advertise as "Polar HR Sensor", so also match on the Heart Rate service UUID.
    device = await BleakScanner.find_device_by_filter(
        lambda d, ad: (d.name or "").lower().startswith(name_filter.lower())
        or HR_SERVICE_UUID in [u.lower() for u in ad.service_uuids],
        timeout=timeout,
    )
    return device


async def list_devices(timeout: float):
    print(f"Scanning for {timeout:.0f}s...")
    devices = await BleakScanner.discover(timeout=timeout)
    for d in sorted(devices, key=lambda d: d.name or ""):
        print(f"  {d.address}  {d.name}")


async def run(args):
    if args.address:
        print(f"Looking for {args.address} ({args.timeout:.0f}s)...")
        device = await BleakScanner.find_device_by_address(args.address, timeout=args.timeout)
    else:
        device = await find_device(args.name, args.timeout)
    if device is None:
        print("No Polar heart-rate sensor found. Is it worn, and not connected to another app? "
              "Try --scan to see nearby devices.")
        return 1

    model = detect_model(device.name or "")
    stream_name = args.stream_name or f"Polar{model}"
    print(f"Found {device.name or 'Polar HR Sensor'} [{device.address}], model {model}")

    session = Session(args, device.address, model, stream_name)
    try:
        return await connect_loop(args, device, session)
    finally:
        session.close()


async def connect_loop(args, target, session):
    while True:
        disconnected = asyncio.Event()
        try:
            # use_cached_services=False: Windows sometimes caches an incomplete GATT table after
            # an unclean disconnect, which makes the HR characteristic appear to be missing.
            async with BleakClient(target, disconnected_callback=lambda _c: disconnected.set(),
                                   winrt={"use_cached_services": False}) as client:
                print("Connected.")
                try:
                    batt = await client.read_gatt_char(BATTERY_LEVEL_UUID)
                    print(f"Battery: {batt[0]}%")
                except Exception:
                    pass
                await client.start_notify(HR_MEASUREMENT_UUID, session.on_hr)
                if args.ecg or args.acc:
                    try:
                        await session.start_pmd(client)
                    except Exception as e:
                        print(f"Could not start raw ECG/accelerometer streaming: {e}")
                print("Streaming. Press Ctrl+C to stop.")
                await disconnected.wait()
                print("Disconnected.")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"Connection error: {e}")
        if not args.reconnect:
            return 0
        print("Reconnecting in 2 s...")
        await asyncio.sleep(2)


def main():
    p = argparse.ArgumentParser(description="Stream Polar H7/H10 heart rate, RR intervals, ECG and acceleration to LSL.")
    p.add_argument("--address", help="BLE address of the strap (skip name scanning)")
    p.add_argument("--name", default="Polar", help="device name prefix to scan for (default: 'Polar')")
    p.add_argument("--timeout", type=float, default=15.0, help="scan timeout in seconds")
    p.add_argument("--stream-name", help="LSL stream name (default: Polar<model>, e.g. PolarH7 / PolarH10)")
    p.add_argument("--csv", help="CSV backup path (default: recordings/<stream>_<timestamp>.csv)")
    p.add_argument("--no-csv", dest="csv_enabled", action="store_false", help="disable CSV backup")
    p.add_argument("--no-ecg", dest="ecg", action="store_false", help="H10: don't stream raw ECG")
    p.add_argument("--no-acc", dest="acc", action="store_false", help="H10: don't stream accelerometer")
    p.add_argument("--acc-rate", type=int, default=200, choices=[25, 50, 100, 200],
                   help="H10 accelerometer sample rate in Hz (default: 200)")
    p.add_argument("--acc-range", type=int, default=8, choices=[2, 4, 8],
                   help="H10 accelerometer range in g (default: 8)")
    p.add_argument("--no-reconnect", dest="reconnect", action="store_false", help="exit on disconnect")
    p.add_argument("--scan", action="store_true", help="list nearby BLE devices and exit")
    args = p.parse_args()

    try:
        if args.scan:
            asyncio.run(list_devices(args.timeout))
            code = 0
        else:
            code = asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\nStopped.")
        code = 0
    # When double-clicked as an .exe, keep the window open so errors can be read.
    if getattr(sys, "frozen", False) and code:
        input("Press Enter to close...")
    return code


if __name__ == "__main__":
    sys.exit(main())

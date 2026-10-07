"""
Polar H7 -> Lab Streaming Layer (LSL) bridge.

Connects to a Polar H7 chest strap over Bluetooth LE, subscribes to the
standard Heart Rate Measurement characteristic, and publishes one multichannel
LSL stream (name "PolarH7", type "HeartRate", irregular rate):

  ch0  HR       bpm   heart rate reported by the strap
  ch1  RR       ms    RR interval of this beat (NaN if the packet had no RR)
  ch2  Contact  0/1   skin contact (NaN if the strap doesn't report it)

One sample is pushed per detected beat, each RR back-dated to its own beat time.
The same samples are written to a CSV backup file (flushed every row).

Record with LabRecorder (or any LSL consumer).

Note: the H7 only exposes HR + RR intervals. Raw ECG/accelerometer streaming
(Polar PMD service) is only available on the H10 / OH1 / Verity Sense.

Usage:
    python polar_h7_lsl.py                     # scan for first Polar HR device
    python polar_h7_lsl.py --csv my_session.csv # choose CSV path (default: recordings/...)
    python polar_h7_lsl.py --no-csv            # LSL only
    python polar_h7_lsl.py --address XX:XX:...  # connect to a specific device
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


CHANNELS = [("HR", "bpm"), ("RR", "ms"), ("Contact", "bool")]


def make_outlet(name, source_id):
    info = StreamInfo(name, "HeartRate", len(CHANNELS), IRREGULAR_RATE, cf_float32, source_id)
    chans = info.desc().append_child("channels")
    for label, unit in CHANNELS:
        ch = chans.append_child("channel")
        ch.append_child_value("label", label)
        ch.append_child_value("unit", unit)
        ch.append_child_value("type", label)
    info.desc().append_child("acquisition").append_child_value("manufacturer", "Polar")
    info.desc().child("acquisition").append_child_value("model", "H7")
    return StreamOutlet(info)


def app_dir() -> Path:
    """Folder of the .exe when frozen (PyInstaller), otherwise of this script."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).parent


def open_csv(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    f = open(path, "w", newline="", encoding="utf-8")
    w = csv.writer(f)
    w.writerow(["lsl_time", "wall_time", "hr_bpm", "rr_ms", "contact"])
    f.flush()
    return f, w


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
        target = args.address
        source_id = args.address
    else:
        device = await find_device(args.name, args.timeout)
        if device is None:
            print("No Polar heart-rate sensor found. Try --scan to see nearby devices.")
            return 1
        print(f"Found {device.name or 'Polar HR Sensor'} [{device.address}]")
        target = device
        source_id = device.address

    outlet = make_outlet(args.stream_name, source_id)
    print(f"LSL stream up: {args.stream_name} (channels: {', '.join(c for c, _ in CHANNELS)})")

    csv_file = csv_writer = None
    if args.csv_enabled:
        csv_path = Path(args.csv) if args.csv else (
            app_dir() / "recordings" / f"{args.stream_name}_{datetime.now():%Y%m%d_%H%M%S}.csv")
        csv_file, csv_writer = open_csv(csv_path)
        print(f"CSV backup: {csv_path.resolve()}")
    # LSL clock -> wall clock offset, so CSV rows also get a human-readable time.
    wall_offset = datetime.now().timestamp() - local_clock()

    def on_hr(_sender, data: bytearray):
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
            outlet.push_sample([float(hr), interval, contact_val], stamp)
            if csv_writer:
                wall = datetime.fromtimestamp(stamp + wall_offset).isoformat(timespec="milliseconds")
                csv_writer.writerow([f"{stamp:.6f}", wall, hr,
                                     "" if math.isnan(interval) else f"{interval:.2f}",
                                     "" if contact is None else int(contact)])
        if csv_file:
            csv_file.flush()

        contact_str = "" if contact is None else ("  contact" if contact else "  NO CONTACT")
        rr_str = ", ".join(f"{x:.0f}" for x in rr)
        print(f"HR {hr:3d} bpm  RR [{rr_str}] ms{contact_str}")

    try:
        return await connect_loop(args, target, on_hr)
    finally:
        if csv_file:
            csv_file.close()


async def connect_loop(args, target, on_hr):
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
                await client.start_notify(HR_MEASUREMENT_UUID, on_hr)
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
    p = argparse.ArgumentParser(description="Stream Polar H7 heart rate / RR intervals to LSL.")
    p.add_argument("--address", help="BLE address of the H7 (skip scanning)")
    p.add_argument("--name", default="Polar", help="device name prefix to scan for (default: 'Polar')")
    p.add_argument("--timeout", type=float, default=15.0, help="scan timeout in seconds")
    p.add_argument("--stream-name", default="PolarH7", help="LSL stream name (default: PolarH7)")
    p.add_argument("--csv", help="CSV backup path (default: recordings/<stream>_<timestamp>.csv)")
    p.add_argument("--no-csv", dest="csv_enabled", action="store_false", help="disable CSV backup")
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

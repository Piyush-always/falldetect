"""
BLE DFU capability scan.

Purpose: answer one question before any OTA code gets written - does this
board's stock Adafruit bootloader actually advertise Bluetooth Secure DFU, or
was it built without it. Nothing here talks to the application firmware; it
only listens to advertisements while the board is in bootloader mode.

Usage:
    1. Double-tap RST on the board (same gesture used for the UF2 drive).
    2. Within ~20 s, run:  python tools/scripts/ble_dfu_scan.py
    3. Look for a device whose services include the Secure DFU UUID
       (0000fe59-0000-1000-8000-00805f9b34fb) or the legacy DFU UUID
       (00001530-1212-efde-1523-785feabcd123), or whose name is "DfuTarg" /
       contains "Adafruit" / "XIAO".

Requires: bleak (already present on this machine's system Python 3.13 -
run with `py -3.13`, not the NCS SDK Python - see doc/SETUP.md gotcha
"SDK Python can't pip-install sdists").
"""

import asyncio

from bleak import BleakScanner

SECURE_DFU_UUID = "0000fe59-0000-1000-8000-00805f9b34fb"
LEGACY_DFU_UUID = "00001530-1212-efde-1523-785feabcd123"
SCAN_SECONDS = 15


def looks_like_dfu(name, service_uuids):
    uuids = [u.lower() for u in (service_uuids or [])]
    name = (name or "").lower()

    hits = []
    if SECURE_DFU_UUID in uuids:
        hits.append("Secure DFU service (0xFE59)")
    if LEGACY_DFU_UUID in uuids:
        hits.append("Legacy DFU service (0x1530)")
    if "dfutarg" in name or "adafruit" in name or "xiao" in name:
        hits.append(f"suggestive name '{name}'")
    return hits


async def main():
    print(f"Scanning for {SCAN_SECONDS}s - double-tap RST now if you haven't...\n")
    devices = await BleakScanner.discover(timeout=SCAN_SECONDS, return_adv=True)

    if not devices:
        print("No BLE advertisements seen at all. Check the adapter is on and "
              "the board is actually in bootloader mode (drive should also "
              "have appeared as XIAO-SENSE over USB).")
        return

    found_dfu = False
    for address, (device, adv) in devices.items():
        hits = looks_like_dfu(device.name, adv.service_uuids)
        marker = "  <-- DFU CANDIDATE" if hits else ""
        print(f"{address}  name={device.name!r:20}  rssi={adv.rssi:>4}  "
              f"services={adv.service_uuids}{marker}")
        if hits:
            found_dfu = True
            for h in hits:
                print(f"    - {h}")

    print()
    if found_dfu:
        print("Result: at least one advertiser looks like a DFU target. "
              "This bootloader likely supports BLE OTA - proceed with Path A.")
    else:
        print("Result: nothing matched a known DFU signature. Either the "
              "board isn't in bootloader mode right now, or this bootloader "
              "build doesn't expose BLE DFU - re-run to be sure, then report "
              "back before we commit to an OTA path.")


if __name__ == "__main__":
    asyncio.run(main())

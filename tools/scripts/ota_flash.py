"""
Push a signed MCUboot image to a falldetect-gkl board over BLE (OTA).

This is the scriptable equivalent of the GUI's OTA tab: same protocol (SMP
over the MCUmgr BLE transport), same safety behaviour, and literally the same
implementation — both call tools/fd_studio/ota.py so the two cannot drift.

It deliberately does NOT confirm the image itself (SMPClient's own docs call
that "unsafe, can cause a boot-loop that could brick the device"): it uploads
to slot 1 and marks it for a one-time TEST boot only. The firmware confirms
itself after passing its real startup checks (see the
boot_write_img_confirmed() calls in apps/*/src/main.c and src/main.c); if the
new image never reaches that point, the NEXT reset reverts to the previous
image automatically. That is MCUboot's own safety net, and this script is
written to not bypass it.

Usage:
    py -3.13 tools/scripts/ota_flash.py datalog
    py -3.13 tools/scripts/ota_flash.py blink
    py -3.13 tools/scripts/ota_flash.py falldetect --name some-other-name

<app> selects which build/<app>/ tree to pull zephyr.signed.bin from, matching
tools/build.ps1's directory convention, and implies the advertised BLE name.
--name overrides that for a board built with a custom CONFIG_BT_DEVICE_NAME.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# tools/ (the parent of this scripts/ directory) is what holds the fd_studio
# package, so that is what goes on the path - not this file's own directory.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fd_studio.ota import (APP_BLE_NAMES, OtaError, archive_image,  # noqa: E402
                           find_signed_image, image_version, push_update)

# tools/scripts/ -> tools/ -> repo root.
ROOT = Path(__file__).resolve().parent.parent.parent


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("app", help="app under build/ (datalog, blink, falldetect)")
    parser.add_argument("--name", default=None,
                        help="BLE advertised name; defaults to the app's known name")
    args = parser.parse_args()

    name = args.name or APP_BLE_NAMES.get(args.app)
    if not name:
        print(f"Unknown app '{args.app}' - pass --name with its "
              f"CONFIG_BT_DEVICE_NAME.", file=sys.stderr)
        return 2

    last_pct = -1

    def on_progress(sent: int, total: int) -> None:
        nonlocal last_pct
        pct = (sent * 100) // total if total else 0
        if pct >= last_pct + 5 or sent == total:
            last_pct = pct
            print(f"  {pct:3d}%  {sent:,}/{total:,} bytes", flush=True)

    try:
        image_path = find_signed_image(args.app)
        # Keep a copy before pushing: a build that has been on a device should
        # stay re-pushable without rebuilding it from a possibly-dirty tree.
        kept = archive_image(args.app, image_path)
        print(f"version: {image_version(image_path)}   "
              f"archived: {kept.relative_to(ROOT)}")
        print(f"image: {image_path.relative_to(ROOT)}")
        asyncio.run(push_update(name, image_path,
                                progress=on_progress, status=print))
    except OtaError as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1

    print()
    print("The device self-confirms after its own startup checks pass (see "
          "main.c). If it never reaches that point, the NEXT reset reverts to "
          "the previous image automatically. No manual confirm step needed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

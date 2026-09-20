"""
Firmware update over BLE, shared by tools/scripts/ota_flash.py and the GUI's OTA tab.

The protocol details and their rationale live in tools/scripts/ota_flash.py's module
docstring; this module exists so the GUI and the CLI cannot drift apart. The
one rule worth repeating here: this NEVER confirms the image itself. It
uploads to slot 1 and marks it for a one-time TEST boot. The firmware
confirms itself only after its own startup checks pass (the
boot_write_img_confirmed() calls in apps/*/src/main.c), so a build that
cannot start reverts on the next reset instead of bricking the device.
"""

from __future__ import annotations

import asyncio

from pathlib import Path
from typing import Callable

from smpclient import SMPClient
from smpclient.generics import error, success
from smpclient.mcuboot import IMAGE_TLV, ImageInfo
from smpclient.requests.image_management import ImageStatesRead, ImageStatesWrite
from smpclient.requests.os_management import ResetWrite
from smpclient.transport.ble import SMPBLETransport

ROOT = Path(__file__).resolve().parent.parent.parent

# Advertised name (CONFIG_BT_DEVICE_NAME) per app, since they differ and a
# wrong guess silently targets the wrong board.
APP_BLE_NAMES = {
    "datalog": "falldetect-datalog",
    "blink": "falldetect-blink",
    "falldetect": "falldetect-gkl",
}


#: Seconds to wait after the telemetry link is dropped before the OTA
#: connection is opened. See the note in push_update().
_SETTLE_S = 1.5


class OtaError(Exception):
    """Anything that stops an update, phrased for a person to read."""


def find_signed_image(app: str) -> Path:
    """Locate build/<app>/**/zephyr.signed.bin, excluding MCUboot's own."""
    build_dir = ROOT / "build" / app
    if not build_dir.exists():
        raise OtaError(f"No build for '{app}'. Build it first: tools\\build.ps1 {app}")

    candidates = [p for p in build_dir.rglob("zephyr.signed.bin") if "mcuboot" not in p.parts]
    if not candidates:
        raise OtaError(f"No signed image under build/{app}. "
                       f"Build it first: tools\\build.ps1 {app}")
    if len(candidates) > 1:
        listed = "\n".join(f"  {p}" for p in candidates)
        raise OtaError(f"Multiple signed images under build/{app}:\n{listed}")
    return candidates[0]


def image_hash(image_path: Path) -> bytes:
    """The SHA256 TLV embedded in the signed image.

    NOT sha256 of the file: MCUboot's image-state tracking keys off a hash
    covering header+body only, excluding the TLV/signature trailer. Hashing
    the whole file fails with IMG_MGMT_ERR.HASH_NOT_FOUND.
    """
    return ImageInfo.load_file(str(_as_bin(image_path))).get_tlv(IMAGE_TLV.SHA256).value


async def read_image_states(name: str, timeout_s: float = 15.0) -> list:
    """Slot states, for showing what is currently on the device."""
    async with SMPClient(SMPBLETransport(), name, timeout_s=timeout_s) as client:
        resp = await client.request(ImageStatesRead())
        if error(resp):
            raise OtaError(f"Could not read image state: {resp}")
        if not success(resp):
            raise OtaError(f"Unexpected image state response: {resp}")
        return list(resp.images)



def _image_bytes(path: Path) -> bytes:
    """Signed image bytes from either a .bin or an archived release .zip."""
    if path.suffix.lower() == ".zip":
        return image_from_zip(path)
    return path.read_bytes()


def _as_bin(path: Path) -> Path:
    """A real .bin path for tools that need a file (imgtool parsing).

    A ZIP is unpacked next to itself once; the extracted copy is reused.
    """
    if path.suffix.lower() != ".zip":
        return path
    out = path.with_suffix(".extracted.bin")
    if not out.exists():
        out.write_bytes(image_from_zip(path))
    return out


def image_version(image_path: Path) -> str:
    """The version string baked into a signed image's MCUboot header.

    Comes from the app's VERSION file via CONFIG_MCUBOOT_IMGTOOL_SIGN_VERSION.
    If this reads 0.0.0 the app has no VERSION file and every build will look
    identical on the device.
    """
    ver = ImageInfo.load_file(str(_as_bin(image_path))).header.ver
    return f"{ver.major}.{ver.minor}.{ver.revision}"



#: Every pushed image is kept here, named by app and version, so any build that
#: has ever been on a device can be re-pushed without rebuilding it. Rebuilding
#: "the same" version from a dirty tree does not necessarily reproduce it.
FIRMWARE_DIR = ROOT / "firmware"


def find_dfu_zip(app: str) -> Path | None:
    """sysbuild's dfu_application.zip for `app`, if it was produced."""
    z = ROOT / "build" / app / "dfu_application.zip"
    return z if z.exists() else None


def archive_image(app: str, image_path: Path) -> Path:
    """Archive a release into firmware/ as <app>-<version>.zip.

    The ZIP is kept rather than the bare .bin because it is the actual OTA
    package: it carries the signed image AND a manifest, which is what
    nRF Connect (mobile or desktop) expects. A loose .bin can only be pushed
    by a tool that already knows what it is.

    An existing file of the same name is left alone. If it is there, that
    exact version already shipped, and overwriting it would destroy the only
    record of what was actually on a device.
    """
    FIRMWARE_DIR.mkdir(parents=True, exist_ok=True)
    dest = FIRMWARE_DIR / f"{app}-{image_version(image_path)}.zip"
    if dest.exists():
        return dest

    src = find_dfu_zip(app)
    if src is not None:
        dest.write_bytes(src.read_bytes())
        return dest

    # No sysbuild zip (a build without MCUboot as a child image). Wrap the
    # signed image so firmware/ stays one consistent format.
    import json
    import zipfile

    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps(
            {"name": app, "version": image_version(image_path),
             "files": [f"{app}.signed.bin"]}, indent=2))
        z.write(image_path, f"{app}.signed.bin")
    return dest


def image_from_zip(zip_path: Path) -> bytes:
    """The signed image bytes out of an archived release ZIP."""
    import zipfile

    with zipfile.ZipFile(zip_path) as z:
        names = [n for n in z.namelist() if n.endswith(".bin")]
        if not names:
            raise OtaError(f"No firmware image inside {zip_path.name}")
        return z.read(names[0])

async def read_device_version(name: str, timeout_s: float = 15.0) -> str | None:
    """Version of the image the device is currently RUNNING, or None.

    Reads all slots and returns the active one. The inactive slot holds the
    previous image (the revert target) and is deliberately not reported - it
    would just raise "which of these two am I on?" every time.
    """
    for img in await read_image_states(name, timeout_s=timeout_s):
        if getattr(img, "active", False):
            return img.version
    return None

async def push_update(name: str, image_path: Path,
                      progress: Callable[[int, int], None] | None = None,
                      status: Callable[[str], None] | None = None,
                      timeout_s: float = 15.0) -> None:
    """Upload `image_path` to `name`, mark it for test boot, and reset.

    `progress(sent, total)` and `status(message)` are called from this
    coroutine's thread — a Qt caller must marshal them to the GUI thread
    rather than touching widgets directly.
    """
    def say(msg: str) -> None:
        if status is not None:
            status(msg)

    image = _image_bytes(image_path)
    digest = image_hash(image_path)

    # Hand the radio over cleanly. The caller drops the telemetry link just
    # before this runs, but that teardown is not instantaneous - the BLE
    # disconnect has to actually complete. Starting an SMP connection while
    # the old one is still unwinding means two bleak stacks contend for the
    # same adapter, which is how a leaked watcher (and a wedged Windows BLE
    # stack) happens. The DUSQ tool settles for 1.5 s in the same place.
    await asyncio.sleep(_SETTLE_S)

    say(f"Connecting to {name}...")
    async with SMPClient(SMPBLETransport(), name, timeout_s=timeout_s) as client:
        say(f"Uploading {len(image):,} bytes...")
        async for offset in client.upload(image, slot=1, upgrade=False):
            if progress is not None:
                progress(offset, len(image))

        say("Marking image for test boot...")
        resp = await client.request(ImageStatesWrite(hash=digest, confirm=False))
        if error(resp):
            # The most common real-world cause, phrased as a fix rather than
            # a protocol code: MCUboot refuses to overwrite the fallback image
            # while a previous update is still unconfirmed.
            raise OtaError(
                f"Device rejected the update: {resp}\n"
                "If this says NO_FREE_SLOT, the previous update is still "
                "unconfirmed — restart the device and try again.")
        if not success(resp):
            raise OtaError(f"Unexpected response: {resp}")

        say("Restarting device...")
        try:
            await client.request(ResetWrite(), timeout_s=5.0)
        except TimeoutError:
            # Expected: the device resets before it can acknowledge.
            pass

    say("Update sent. The device is restarting into the new firmware.")

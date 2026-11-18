"""Read a phone over USB. The phone is never modified.

Android appears as an MTP device, not a drive letter. Listing and copying go
through the Windows Shell. If that copy fails, the bytes are read with WPD.
Neither path uses adb.
"""
from __future__ import annotations

import ctypes
import datetime as dt
import logging
import os
import shutil
import time
import uuid
from pathlib import Path

from .phone import PhoneRecording

log = logging.getLogger("vault_ingest")

# Shell CopyHere flags: no dialog, no confirmation, no error UI.
_COPY_FLAGS = 0x0004 | 0x0010 | 0x0200 | 0x0400
_COPY_TIMEOUT = 300.0
_PRESENT = 0x00000100
_THIS_PC = 17

# PROPERTYKEYs from PortableDevice.h. Used only to read a file.
_CLIENT_FMT = "{204D9F0C-2292-4080-9F42-40664E70F859}"
_OBJECT_FMT = "{EF6B490D-5CD8-437A-AFFC-DA8B60EE4A3C}"
_RESOURCE_FMT = "{E81E79BE-34F0-41BF-B53F-F1A06AE87842}"
_STGM_READ = 0


def usb_present(usb_id: str) -> bool:
    """True when a present PnP device id contains this id, including charging-only mode."""
    needle = usb_id.replace("/", "\\").upper()
    if "\\" in needle:
        needle = needle.split("\\", 1)[1]
    if not needle:
        return False
    return any(needle in device.upper() for device in _present_device_ids())


def _present_device_ids() -> list[str]:
    cfg = ctypes.windll.cfgmgr32
    cfg.CM_Get_Device_ID_List_SizeW.argtypes = [ctypes.POINTER(ctypes.c_ulong), ctypes.c_wchar_p, ctypes.c_ulong]
    cfg.CM_Get_Device_ID_List_SizeW.restype = ctypes.c_ulong
    cfg.CM_Get_Device_ID_ListW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_ulong, ctypes.c_ulong]
    cfg.CM_Get_Device_ID_ListW.restype = ctypes.c_ulong
    length = ctypes.c_ulong()
    if cfg.CM_Get_Device_ID_List_SizeW(ctypes.byref(length), None, _PRESENT) != 0 or not length.value:
        return []
    buf = ctypes.create_unicode_buffer(length.value)
    if cfg.CM_Get_Device_ID_ListW(None, buf, length.value, _PRESENT) != 0:
        return []
    raw = ctypes.wstring_at(ctypes.addressof(buf), length.value)
    return [part for part in raw.split("\x00") if part]


def _local_mtime(value) -> dt.datetime:
    """Shell's System.DateModified is UTC. A naive value is UTC, not local."""
    if isinstance(value, dt.datetime):
        when = value
    else:
        when = dt.datetime.fromisoformat(str(value).strip())
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.timezone.utc)
    return when.astimezone()


def _call(obj, name: str):
    """comtypes exposes some Shell methods as properties that are already the result."""
    value = getattr(obj, name)
    return value() if callable(value) else value


class ShellPhone:
    """The configured phone, addressed by the name Windows shows under This PC."""

    def __init__(self, device_name: str, usb_id: str, folder: str):
        self.device_name = device_name
        self.usb_id = usb_id
        self.folder = folder
        self.parts = [part for part in folder.replace("\\", "/").split("/") if part]

    def usb_present(self) -> bool:
        return usb_present(self.usb_id)

    def storage_readable(self) -> bool:
        try:
            shell = _shell()
            phone = _child(shell.NameSpace(_THIS_PC), self.device_name)
            if phone is None:
                return False
            folder = _descend(_call(phone, "GetFolder"), self.parts)
        except Exception as exc:  # noqa: BLE001  an unreadable phone is the charging-only case
            log.warning("Could not open %s: %s", self.device_name, exc)
            return False
        if folder is None:
            log.info("Phone %s is open but %s is not there", self.device_name, self.folder)
        return folder is not None

    def list_recordings(self) -> list[PhoneRecording]:
        shell = _shell()
        phone = _child(shell.NameSpace(_THIS_PC), self.device_name)
        if phone is None:
            raise RuntimeError(f"{self.device_name} is not open for file transfer")
        folder = _descend(_call(phone, "GetFolder"), self.parts)
        if folder is None:
            raise RuntimeError(f"{self.folder} was not found on {self.device_name}")
        duration_col = _column(folder, "duration")
        found = []
        prefix = "/".join(self.parts)
        for item in _call(folder, "Items"):
            name = str(item.Name or "")
            if not name or name.startswith("."):
                continue
            try:
                size = int(item.ExtendedProperty("System.Size") or 0)
            except (TypeError, ValueError):
                size = 0
            if size <= 0:
                continue
            try:
                modified = _local_mtime(item.ExtendedProperty("System.DateModified"))
            except (TypeError, ValueError):
                log.info("Skipping %s; it has no modified time", name)
                continue
            duration = ""
            if duration_col is not None:
                duration = str(folder.GetDetailsOf(item, duration_col) or "")
            found.append(PhoneRecording(name, f"{prefix}/{name}", size, modified, duration))
        return found

    def copy_recording(self, rec: PhoneRecording, dest: Path) -> None:
        """Copy one file onto the PC. The phone copy is left untouched."""
        try:
            self._copy_shell(rec, dest)
        except Exception as exc:
            log.warning("Shell copy of %s failed (%s); reading it through WPD", rec.name, exc)
            if dest.exists():
                dest.unlink()
            _copy_wpd(self.device_name, self.usb_id, rec, dest)
        if not dest.is_file() or dest.stat().st_size != rec.size:
            raise RuntimeError(f"Copied {rec.name} is {dest.stat().st_size if dest.is_file() else 0} bytes, "
                               f"expected {rec.size}")

    def _copy_shell(self, rec: PhoneRecording, dest: Path) -> None:
        shell = _shell()
        phone = _child(shell.NameSpace(_THIS_PC), self.device_name)
        if phone is None:
            raise RuntimeError(f"{self.device_name} is not open for file transfer")
        folder = _descend(_call(phone, "GetFolder"), self.parts)
        if folder is None:
            raise RuntimeError(f"{self.folder} was not found on {self.device_name}")
        item = _child(folder, rec.name)
        if item is None:
            raise RuntimeError(f"{rec.name} is no longer in {self.folder}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        work = dest.parent / f".phone-{uuid.uuid4().hex}"
        work.mkdir()
        try:
            target = shell.NameSpace(str(work))
            if target is None:
                raise RuntimeError(f"Could not open {work}")
            target.CopyHere(item, _COPY_FLAGS)
            landed = work / rec.name
            deadline = time.time() + _COPY_TIMEOUT
            last = -1
            stable = 0
            while time.time() < deadline:
                if landed.is_file():
                    size = landed.stat().st_size
                    if size == rec.size and size == last:
                        stable += 1
                        if stable >= 2:
                            os.replace(landed, dest)
                            return
                    else:
                        stable = 0
                    last = size
                time.sleep(0.4)
            raise RuntimeError(f"Timed out copying {rec.name}")
        finally:
            shutil.rmtree(work, ignore_errors=True)


def _shell():
    import comtypes
    import comtypes.client
    try:
        comtypes.CoInitialize()
    except OSError:
        pass
    return comtypes.client.CreateObject("Shell.Application")


def _child(folder, name: str):
    for item in _call(folder, "Items"):
        if str(item.Name) == name:
            return item
    return None


def _descend(folder, parts: list[str]):
    current = folder
    for part in parts:
        item = _child(current, part)
        if item is None:
            return None
        current = _call(item, "GetFolder")
    return current


def _column(folder, header: str) -> int | None:
    wanted = header.casefold()
    for index in range(40):
        try:
            text = str(folder.GetDetailsOf(None, index) or "")
        except Exception:  # noqa: BLE001  a missing column is not fatal
            continue
        if text.casefold() == wanted:
            return index
    return None


def _copy_wpd(device_name: str, usb_id: str, rec: PhoneRecording, dest: Path) -> None:
    """Read the file with the Portable Device API. Nothing on the phone is written."""
    import comtypes
    import comtypes.client
    from comtypes import GUID
    from comtypes.gen.PortableDeviceApiLib import PortableDevice, PortableDeviceManager
    from comtypes.gen.PortableDeviceApiLib import _tagpropertykey as ApiKey
    from comtypes.gen.PortableDeviceTypesLib import PortableDeviceValues
    from comtypes.gen.PortableDeviceTypesLib import _tagpropertykey as TypeKey

    try:
        comtypes.CoInitialize()
    except OSError:
        pass

    def prop(kind, fmtid: str, pid: int):
        return kind(GUID(fmtid), pid)

    name_key = prop(TypeKey, _OBJECT_FMT, 4)
    file_key = prop(TypeKey, _OBJECT_FMT, 12)
    resource = prop(ApiKey, _RESOURCE_FMT, 0)
    client = comtypes.client.CreateObject(PortableDeviceValues)
    client.SetStringValue(comtypes.POINTER(TypeKey)(prop(TypeKey, _CLIENT_FMT, 2)), "vault-ingest")
    client.SetUnsignedIntegerValue(comtypes.POINTER(TypeKey)(prop(TypeKey, _CLIENT_FMT, 3)), 1)
    client.SetUnsignedIntegerValue(comtypes.POINTER(TypeKey)(prop(TypeKey, _CLIENT_FMT, 4)), 0)
    client.SetUnsignedIntegerValue(comtypes.POINTER(TypeKey)(prop(TypeKey, _CLIENT_FMT, 5)), 0)

    manager = comtypes.client.CreateObject(PortableDeviceManager)
    manager.RefreshDeviceList()
    count = ctypes.c_ulong(0)
    if manager._IPortableDeviceManager__com_GetDevices(None, ctypes.byref(count)) or not count.value:
        raise RuntimeError("No portable device is connected")
    ids = (ctypes.c_wchar_p * count.value)()
    if manager._IPortableDeviceManager__com_GetDevices(ctypes.cast(ids, ctypes.POINTER(ctypes.c_wchar_p)),
                                                       ctypes.byref(count)):
        raise RuntimeError("Could not list portable devices")
    needle = usb_id.replace("/", "\\").upper().split("\\")[-1]
    chosen = None
    for index in range(count.value):
        device_id = ids[index]
        if not device_id or needle not in device_id.upper() or "MTP" not in device_id.upper():
            continue
        friendly = ""
        try:
            chars = ctypes.c_ulong(0)
            manager._IPortableDeviceManager__com_GetDeviceFriendlyName(device_id, None, ctypes.byref(chars))
            if chars.value:
                buf = ctypes.create_unicode_buffer(chars.value)
                manager._IPortableDeviceManager__com_GetDeviceFriendlyName(device_id, buf, ctypes.byref(chars))
                friendly = buf.value
        except Exception:  # noqa: BLE001  the MTP id is enough when the name cannot be read
            friendly = ""
        if friendly == device_name or not friendly:
            chosen = device_id
            if friendly == device_name:
                break
    if not chosen:
        raise RuntimeError(f"{device_name} is not available through WPD")

    device = comtypes.client.CreateObject(PortableDevice)
    device.Open(chosen, client)
    content = _call(device, "Content")
    parts = [part for part in rec.phone_path.replace("\\", "/").split("/") if part]
    object_id = _find_by_path(content, parts, name_key, file_key)
    if not object_id:
        raise RuntimeError(f"{rec.name} was not found through WPD")
    transfer = _call(content, "Transfer")
    result = transfer.GetStream(object_id, comtypes.POINTER(ApiKey)(resource), _STGM_READ, 0)
    stream = result[-1] if isinstance(result, (tuple, list)) else result
    dest.parent.mkdir(parents=True, exist_ok=True)
    remaining = rec.size
    with dest.open("wb") as handle:
        while remaining > 0:
            chunk = min(remaining, 1 << 20)
            data = _read_stream(stream, chunk)
            if not data:
                break
            handle.write(data)
            remaining -= len(data)
    if dest.stat().st_size != rec.size:
        dest.unlink(missing_ok=True)
        raise RuntimeError(f"WPD read of {rec.name} stopped at {dest.stat().st_size if dest.exists() else 0} bytes")


def _find_by_path(content, parts: list[str], name_key, file_key) -> str | None:
    """Walk Internal storage / Recordings / ... by folder name. Read-only."""
    import comtypes
    import comtypes.client
    from comtypes.gen.PortableDeviceTypesLib import PortableDeviceKeyCollection, _tagpropertykey

    keys = comtypes.client.CreateObject(PortableDeviceKeyCollection)
    keys.Add(comtypes.POINTER(_tagpropertykey)(name_key))
    keys.Add(comtypes.POINTER(_tagpropertykey)(file_key))
    properties = _call(content, "Properties")
    current = "DEVICE"
    for part in parts:
        found = _child_named(content, properties, keys, current, part, name_key, file_key)
        if not found:
            return None
        current = found
    return current


def _child_named(content, properties, keys, parent: str, name: str, name_key, file_key) -> str | None:
    names: list[str] = []
    try:
        enumerator = content.EnumObjects(0, parent, None)
    except Exception as exc:  # noqa: BLE001
        log.info("WPD could not list %s (%s)", parent, exc)
        return None
    while True:
        try:
            fetched = ctypes.c_ulong(0)
            slot = ctypes.c_wchar_p()
            hr = enumerator._IEnumPortableDeviceObjectIDs__com_Next(1, ctypes.byref(slot), ctypes.byref(fetched))
            object_id = slot.value if hr == 0 and fetched.value else ""
        except Exception as exc:  # noqa: BLE001
            log.info("WPD listing %s stopped (%s); saw %s", parent, exc, names)
            return None
        text = _enum_id(object_id)
        if not text:
            log.info("WPD folder %s does not contain %r (next=%r, has %s)", parent, name, object_id, names)
            return None
        try:
            values = properties.GetValues(text, keys)
            original = _string_value(values, file_key)
            label = _string_value(values, name_key)
        except Exception as exc:  # noqa: BLE001
            log.info("WPD properties for %s failed (%s)", text, exc)
            original, label = "", ""
        names.append(original or label or text)
        if name in (original, label):
            return text


def _enum_id(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, tuple):
        for part in value:
            if isinstance(part, str) and part:
                return part
    return str(value or "")


def _string_value(values, key) -> str:
    import comtypes
    from comtypes.gen.PortableDeviceApiLib import _tagpropertykey as ApiKey

    api_key = ApiKey()
    ctypes.memmove(ctypes.byref(api_key), ctypes.byref(key), ctypes.sizeof(api_key))
    try:
        return str(values.GetStringValue(comtypes.POINTER(ApiKey)(api_key)) or "")
    except Exception as exc:  # noqa: BLE001  a folder has no file name
        log.debug("WPD string property failed (%s)", exc)
        return ""


def _read_stream(stream, count: int) -> bytes:
    data = stream.RemoteRead(count)
    if isinstance(data, tuple):
        buf, got = data[0], int(data[1] or 0)
        if not got:
            return b""
        return ctypes.string_at(ctypes.addressof(buf), got)
    if not data:
        return b""
    if isinstance(data, (bytes, bytearray)):
        return bytes(data)
    return ctypes.string_at(ctypes.addressof(data), ctypes.sizeof(data))
